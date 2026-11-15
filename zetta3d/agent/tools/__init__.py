"""The agent's client tools: what the Zetta Agent does in Blender, one function per tool.

A tool is a function `fn(args, ctx)` registered with `@tool(name)`. It runs on the main thread and either
returns its result at once — a string, or a `Result` — or returns `LATER` and calls `ctx.finish(result)` when
its work is done (a render, a download, a card the user answers). It raises `ToolError` with one line the
model can act on when it cannot do what was asked; any other exception is reported the same way and logged
for `recent_errors`.

A tool registered with `undoable=True` is one step on Blender's undo stack, named after what it did
("Zetta Agent: Added Bevel to Chair"): the dispatcher pushes the step after the tool succeeds, using the
`label` the result gives (or the tool's name), and tells the changes card.

The names, inputs and effects are the server's (BlenderAgentProtocol.md); the server decides which tools the
model sees, and the effect it streams with each call decides whether the user is asked first.
"""

import base64
import traceback

import bpy

from .. import changes, errors

MAX_RESULT = 100_000
MAX_IMAGES = 4

LATER = object()  # returned by a tool that finishes through ctx.finish

_tools = {}


class ToolError(Exception):
    """A tool could not do what was asked. The message goes to the model as the result."""


class Result:
    """What a tool hands back: text for the model, pictures, and what the changes card shows."""

    def __init__(self, text, images=None, label=None, error=False, changes=None):
        self.text = text or ""
        self.images = list(images or [])  # [(bytes, media type)]
        self.label = label  # the undo step's name, and the line on the changes card
        self.error = error
        self.changes = list(changes or [])  # [{"kind": "file", "path": …}] — see agent.changes

    def payload(self, tool_use_id):
        """The toolResults entry the server takes."""
        text = self.text
        if len(text) > MAX_RESULT:
            text = text[:MAX_RESULT] + f"\n… (cut: {len(self.text):,} characters in all)"
        entry = {"toolUseId": tool_use_id, "content": text or "(no output)", "isError": bool(self.error)}
        if self.images:
            entry["images"] = [
                {"data": base64.b64encode(data).decode("ascii"), "mediaType": media}
                for data, media in self.images[:MAX_IMAGES]
            ]
        return entry


class Spec:
    def __init__(self, name, fn, undoable, risky):
        self.name = name
        self.fn = fn
        self.undoable = undoable
        self.risky = risky


def tool(name, undoable=False, risky=False):
    """Registers fn as the client tool `name`.

    undoable: one undo step when it succeeds. risky: the add-on may keep a copy of the file before it runs
    (BlenderAgentProtocol.md › Copies of the file) — run_python, destructive calls, applying modifiers, imports.
    """

    def register(fn):
        _tools[name] = Spec(name, fn, undoable, risky)
        return fn

    return register


def spec(name):
    return _tools.get(name)


def names():
    return sorted(_tools)


class Context:
    """What a tool can reach besides its arguments. Made by the runtime for each call."""

    def __init__(self, call, finish, card=None, session=None):
        self.call = call  # {"toolUseId", "name", "input", "effect"}
        self._finish = finish
        self._card = card
        self.session = session or {}
        self.finished = False

    @property
    def tool_use_id(self):
        return self.call.get("toolUseId", "")

    def finish(self, result):
        """Ends a LATER tool. Safe to call once; later calls are ignored."""
        if self.finished:
            return
        self.finished = True
        self._finish(_normalised(result))

    def card(self, kind, payload, on_answer):
        """Shows the user a card in the chat and calls on_answer(answer) when they act on it.

        kind "generate" (payload: title, lines, credits, button) answers True or False; kind "ask_user"
        answers the user's answers. Without a chat (headless tests) it is declined.
        """
        if self._card is None:
            on_answer(None)
            return
        self._card(kind, payload, on_answer)


def _normalised(result):
    if isinstance(result, Result):
        return result
    if result is None:
        return Result("Done.")
    return Result(str(result))


def error_result(message):
    return Result(str(message), error=True)


def run(call, ctx):
    """Runs one call; the result reaches ctx's finish (now, or later for a LATER tool)."""
    name = call.get("name", "")
    found = _tools.get(name)
    if found is None:
        ctx.finish(error_result(
            f"This version of the Zetta 3D add-on does not have the tool {name}. Ask the user to update the add-on."
        ))
        return
    args = call.get("input") or {}
    if not isinstance(args, dict):
        ctx.finish(error_result(f"{name} takes an object of arguments."))
        return

    inner_finish = ctx._finish

    def finish(result):
        result = _normalised(result)
        if found.undoable and not result.error:
            label = result.label or name.replace("_", " ")
            push_undo(label)
            changes.scene_step(label)
        inner_finish(result)

    ctx._finish = finish
    try:
        with changes.running():
            out = found.fn(args, ctx)
    except ToolError as error:
        ctx.finish(error_result(str(error)))
        return
    except Exception as error:  # noqa: BLE001 — a tool's bug is the model's error line, never a lost turn
        traceback.print_exc()
        errors.record(f"tool {name}", f"{type(error).__name__}: {error}")
        ctx.finish(error_result(f"{name} failed: {type(error).__name__}: {error}"))
        return
    if out is LATER:
        return
    ctx.finish(out)


def push_undo(label):
    """One named step on Blender's undo stack."""
    try:
        bpy.ops.ed.undo_push(message=f"Zetta Agent: {label}"[:63])
    except RuntimeError:
        pass  # no undo stack in background mode


MODULES = ("read", "view", "scene", "nodes", "files", "python", "zetta")


def load_all():
    """Imports every tool module so each registers itself. One that fails to import is reported, not fatal:
    the rest of the agent still works, and a call to a missing tool says to update the add-on."""
    import importlib

    for name in MODULES:
        try:
            importlib.import_module(f"{__name__}.{name}")
        except Exception as error:  # noqa: BLE001
            errors.record("Zetta Agent", f"tools {name} could not load: {type(error).__name__}: {error}")
            print(f"Zetta 3D: agent tools {name} could not load: {error}")


def _loaded():
    import sys

    return [sys.modules[f"{__name__}.{name}"] for name in MODULES if f"{__name__}.{name}" in sys.modules]


def register():
    """Starts what a tool module keeps running (the Zetta tools follow the agent's jobs and runs)."""
    for module in _loaded():
        if hasattr(module, "register"):
            module.register()


def unregister():
    for module in reversed(_loaded()):
        if hasattr(module, "unregister"):
            module.unregister()
