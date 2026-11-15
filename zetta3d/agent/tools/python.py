"""run_python — the long tail: Python the model writes, run in the open Blender (BlenderAgent.md §3.5).

Blender's whole API is Python, so no set of structured tools covers everything (a bmesh fix, a driver, a rig
fix-up). This tool runs the model's code on the main thread, with three things around it:

- THE GUARD FIRST. `agent.guard` reads the code and refuses what an assistant has no business doing; the
  refusal is the tool's error, so the model rewrites. The code's builtins back the guard up while it runs:
  no exec/eval/compile/input, and `open`, `__import__`, `getattr`, `setattr`, `delattr` and `vars` that apply
  the same rules to names and paths only known at run time. `open` also maps "//" to the .blend's folder and
  records a file before it is overwritten, so the changes card can put it back.
- WHAT HAPPENED. Everything it printed, the value of a final bare expression (as the Python console shows
  it), and a traceback cut to the code's own frames — the add-on's frames would only confuse the model.
- WHAT CHANGED. The model can't see the viewport, so the result says which objects were added, removed or
  edited (transform, data, modifiers, vertex count, materials) and which data-blocks came or went, from a
  cheap snapshot taken before and after.

It is one undo step either way: the dispatcher pushes it on success, and a run that fails half-way pushes its
own, so what it did before the error can still be undone.
"""

import builtins
import contextlib
import io
import linecache
import os
import sys
import traceback

import bmesh
import bpy
import math
import mathutils
import random

from .. import changes, errors, guard, targets
from . import Result, ToolError, push_undo, tool

MAX_CODE = 20_000
MAX_OUTPUT = 20_000
MAX_VALUE = 5_000
MAX_NAMES = 10

# Data-blocks whose arrival or removal the result reports (objects are reported in more detail).
DATA_KINDS = (("materials", "material"), ("meshes", "mesh"), ("collections", "collection"),
              ("node_groups", "node group"), ("images", "image"), ("worlds", "world"), ("cameras", "camera"),
              ("lights", "light"), ("curves", "curve"), ("armatures", "armature"), ("actions", "action"),
              ("texts", "text"))

_runs = 0


@tool("run_python", undoable=True, risky=True)
def run_python(args, ctx):
    code = args.get("code")
    if not isinstance(code, str) or not code.strip():
        raise ToolError("code is the Python to run.")
    if len(code) > MAX_CODE:
        raise ToolError(f"code is {len(code):,} characters; the limit is {MAX_CODE:,}. Split it into steps.")
    refusal = guard.check(code)
    if refusal:
        raise ToolError(f"Refused by the add-on's safety rules: {refusal}")
    label = _label(args.get("purpose"))

    filename = _register_source(code)
    try:
        body, last = _compile(code, filename)
    except SyntaxError as error:
        text = "".join(traceback.format_exception_only(type(error), error)).rstrip()
        errors.record("run_python", text.splitlines()[-1])
        return Result(text, error=True)

    _sync_edit_mode()
    before = _snapshot()
    namespace = _namespace()
    out = io.StringIO()
    value, failure = None, None
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            exec(body, namespace)  # noqa: S102 — the point of the tool; the guard has read it
            if last is not None:
                value = eval(last, namespace)  # noqa: S307 — the code's own final expression
        except BaseException as error:  # noqa: BLE001 — SystemExit or Ctrl+C must not leave Blender
            failure = error
    _finish_edit_mode()
    after = _snapshot()
    changed = _report(before, after)

    parts = []
    printed = out.getvalue().rstrip()
    if printed:
        parts.append(printed)
    if failure is None and value is not None:
        shown = repr(value)
        if len(shown) > MAX_VALUE:
            shown = shown[:MAX_VALUE] + f"… ({len(shown):,} characters)"
        parts.append(f"Value: {shown}")
    if failure is not None:
        trace = _traceback(failure, filename)
        parts.append(trace)
        errors.record("run_python", trace.splitlines()[-1] if trace else type(failure).__name__)
    text = _cut("\n".join(parts)) if parts else "(no output)"
    if failure is not None:
        if changed:
            # The dispatcher only pushes a step for a success; what ran before the error is still a change.
            push_undo(label)
            changes.scene_step(label)
            text += f"\n{changed} — made before the error; undo removes them."
        else:
            text += "\nNothing in the scene changed."
        return Result(text, error=True)
    text += "\n" + (changed or "Changed: no objects or data-blocks added, removed or edited.")
    return Result(text, label=label)


def _label(purpose):
    """The undo step's name from the purpose: "Zetta Agent: <label>" fits Blender's 63 characters."""
    text = " ".join(str(purpose or "").split()).rstrip(".")
    if not text:
        return "Ran Python"
    if len(text) > 48:
        text = text[:47].rsplit(" ", 1)[0] + "…"
    return text


# ─── Running ─────────────────────────────────────────────────────────────────────────────────────


def _register_source(code):
    """A name for this run's code, with its lines in linecache so tracebacks show them — also for functions
    it leaves behind (a handler, an operator) that fail later."""
    global _runs
    _runs += 1
    filename = f"<run_python #{_runs}>"
    linecache.cache[filename] = (len(code), None, code.splitlines(True), filename)
    for old in [name for name in linecache.cache if name.startswith("<run_python #")]:
        if int(old[len("<run_python #"):-1]) <= _runs - 20:
            linecache.cache.pop(old, None)
    return filename


def _compile(code, filename):
    """(the code to exec, the final bare expression to eval or None)."""
    import ast

    tree = ast.parse(code, filename=filename)
    last = None
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        last = compile(ast.Expression(tree.body.pop().value), filename, "eval")
    return compile(tree, filename, "exec"), last


def _namespace():
    """A fresh namespace with what the protocol promises preloaded, and the guarded builtins. __name__ is
    "__main__" so a script ending in `if __name__ == "__main__": register()` runs as it would in the Text
    Editor."""
    return {
        "__name__": "__main__",
        "__builtins__": _builtins(),
        "bpy": bpy, "bmesh": bmesh, "mathutils": mathutils, "math": math, "random": random,
        "Vector": mathutils.Vector, "Matrix": mathutils.Matrix, "Euler": mathutils.Euler,
        "Quaternion": mathutils.Quaternion, "Color": mathutils.Color,
        "C": bpy.context, "D": bpy.data,
    }


def _builtins():
    safe = dict(vars(builtins))
    for name in ("exec", "eval", "compile", "breakpoint", "input", "help", "exit", "quit"):
        safe.pop(name, None)
    safe.update(__import__=_import, open=_open, getattr=_getattr, setattr=_setattr, delattr=_delattr, vars=_vars)
    return safe


def _refused(text):
    return guard.Refused(f"Refused by the add-on's safety rules: {text}")


def _import(name, globals=None, locals=None, fromlist=(), level=0):  # noqa: A002 — __import__'s own signature
    if level:
        raise _refused(guard.message("dynamic", "a relative import"))
    rule = guard.module_rule(name)
    if rule:
        raise _refused(guard.message(rule, f"import {name}"))
    for each in fromlist or ():
        dotted = f"{name}.{each}"
        rule = guard.name_rule(dotted) or guard.module_rule(dotted)
        if rule:
            raise _refused(guard.message(rule, dotted))
    return builtins.__import__(name, globals, locals, fromlist, level)


def _getattr(obj, name, *default):
    if isinstance(name, str):
        reason = guard.attribute_refusal(obj, name)
        if reason:
            raise _refused(reason)
    return getattr(obj, name, *default)


def _prefs(obj):
    return type(obj).__name__.startswith("Preferences") or isinstance(obj, bpy.types.AddonPreferences)


def _setattr(obj, name, value):
    if isinstance(name, str):
        reason = guard.attribute_refusal(obj, name)
        if reason:
            raise _refused(reason)
    if _prefs(obj):
        raise _refused(guard.message("blender", "setattr() on the preferences"))
    return setattr(obj, name, value)


def _delattr(obj, name):
    if _prefs(obj) or (isinstance(name, str) and guard.attribute_refusal(obj, name)):
        raise _refused(guard.message("dynamic", "delattr()"))
    return delattr(obj, name)


def _vars(*args):
    import types

    if args and isinstance(args[0], types.ModuleType):
        raise _refused(guard.message("dynamic", "vars() of a module"))
    if not args:
        return sys._getframe(1).f_locals  # the caller's locals, as vars() would give — not this wrapper's
    return vars(*args)


def _inside(path, folder):
    try:
        return os.path.commonpath([path, folder]) == folder
    except ValueError:  # another drive
        return False


def _open(file, mode="r", *args, **kwargs):
    """open() for the code: "//" is the .blend's folder; writes stay inside it and are recorded first; the
    add-on's own folder can't be read."""
    if isinstance(file, int):
        raise _refused(guard.message("write", "a file descriptor"))
    path = os.fsdecode(os.fspath(file))
    if path.startswith("//"):
        if not bpy.data.filepath:
            raise _refused(guard.message("write", f"{path} before the file is saved"))
        path = bpy.path.abspath(path)
    path = os.path.abspath(path)
    real = os.path.realpath(path)  # the checks follow links; the record keeps the path as the .blend sees it
    if any(_inside(real, folder) for folder in guard.private_folders()):
        raise _refused(guard.message("addon", path))
    if any(flag in str(mode) for flag in "wax+"):
        folder = targets.blend_folder()
        if not folder or not _inside(real, os.path.realpath(folder)):
            raise _refused(guard.message("write", path))
        changes.before_file(path)
    return builtins.open(path, mode, *args, **kwargs)


def _traceback(error, filename):
    """The traceback with only the code's own frames, as the model wrote it."""
    if isinstance(error, SyntaxError) and error.filename == filename:
        return "".join(traceback.format_exception_only(type(error), error)).rstrip()
    frames = [frame for frame in traceback.extract_tb(error.__traceback__)
              if frame.filename.startswith("<run_python #")]
    lines = []
    if frames:
        lines.append("Traceback (most recent call last):\n")
        lines.extend(traceback.format_list(frames))
    if isinstance(error, guard.Refused):
        lines.append(str(error))  # its text is the whole line; the class's module path would only be noise
    else:
        lines.extend(traceback.format_exception_only(type(error), error))
    return "".join(lines).rstrip()


def _cut(text):
    if len(text) <= MAX_OUTPUT:
        return text
    head, tail = MAX_OUTPUT * 3 // 5, MAX_OUTPUT * 2 // 5
    return f"{text[:head]}\n… ({len(text) - head - tail:,} characters cut) …\n{text[-tail:]}"


# ─── Edit Mode ───────────────────────────────────────────────────────────────────────────────────


def _edit_objects():
    try:
        return [obj for obj in bpy.context.objects_in_mode if obj.type == "MESH"]
    except AttributeError:
        return []


def _sync_edit_mode():
    """In Edit Mode the mesh data lags the edit mesh; bring it up to date so the code reads what the user sees."""
    if bpy.context.mode == "EDIT_MESH":
        for obj in _edit_objects():
            obj.update_from_editmode()


def _finish_edit_mode():
    """Code that edited bmesh.from_edit_mesh() leaves the viewport stale until update_edit_mesh; and the
    snapshot reads the mesh data, so sync it back."""
    if bpy.context.mode != "EDIT_MESH":
        return
    for obj in _edit_objects():
        try:
            bmesh.update_edit_mesh(obj.data)
            obj.update_from_editmode()
        except (RuntimeError, ReferenceError, ValueError):
            pass


# ─── What changed ────────────────────────────────────────────────────────────────────────────────


def _snapshot():
    """Cheap facts per object (by pointer, so a rename is an edit), and the names of other data-blocks."""
    objects = {}
    for obj in bpy.data.objects:
        data = obj.data
        objects[obj.as_pointer()] = {
            "name": obj.name,
            "type": obj.type,
            "transform": tuple(round(v, 5) for row in obj.matrix_basis for v in row),
            "parent": obj.parent.name if obj.parent else None,
            "data": data.name if data is not None else None,
            "modifiers": tuple((m.name, m.type) for m in obj.modifiers),
            "vertices": len(data.vertices) if obj.type == "MESH" and data is not None else None,
            "materials": tuple(slot.material.name if slot.material else "" for slot in obj.material_slots),
        }
    data = {}
    for attr, _ in DATA_KINDS:
        collection = getattr(bpy.data, attr, None)
        if collection is not None:
            data[attr] = {block.as_pointer(): block.name for block in collection}
    return {"objects": objects, "data": data}


def _names(names):
    shown = ", ".join(names[:MAX_NAMES])
    return shown + (f" and {len(names) - MAX_NAMES} more" if len(names) > MAX_NAMES else "")


def _plural(count, word):
    return f"{count} {word}" + ("" if count == 1 else ("es" if word.endswith("sh") else "s"))


def _report(before, after):
    """"Changed: 2 objects added (Rock, Rock.001), 1 edited (Terrain: modifiers)" — or "" for no change."""
    old, new = before["objects"], after["objects"]
    added, removed, edited = [], [], []
    for key, facts in new.items():
        was = old.get(key)
        if was is None or was["type"] != facts["type"]:
            added.append(facts["name"])
            if was is not None:
                removed.append(was["name"])
            continue
        what = []
        if was["name"] != facts["name"]:
            what.append(f"renamed from {was['name']}")
        if was["transform"] != facts["transform"]:
            what.append("transform")
        if was["parent"] != facts["parent"]:
            what.append("parent")
        if was["data"] != facts["data"]:
            what.append("data")
        if was["modifiers"] != facts["modifiers"]:
            what.append("modifiers")
        if was["vertices"] != facts["vertices"]:
            what.append(f"vertices {was['vertices']}→{facts['vertices']}")
        if was["materials"] != facts["materials"]:
            what.append("materials")
        if what:
            edited.append(f"{facts['name']}: {', '.join(what)}")
    removed.extend(facts["name"] for key, facts in old.items() if key not in new)

    pieces = []
    for names, verb in ((added, "added"), (removed, "removed"), (edited, "edited")):
        if names:
            # "2 objects added (…), 1 edited (…)": the noun once, on the first.
            count = _plural(len(names), "object") if not pieces else str(len(names))
            pieces.append(f"{count} {verb} ({_names(names)})")
    others = []
    for attr, word in DATA_KINDS:
        if attr == "meshes":
            continue  # a new mesh object's mesh is no news
        was, now = before["data"].get(attr, {}), after["data"].get(attr, {})
        came = [name for key, name in now.items() if key not in was]
        went = [name for key, name in was.items() if key not in now]
        if came:
            others.append(f"{_plural(len(came), word)} added ({_names(came)})")
        if went:
            others.append(f"{_plural(len(went), word)} removed ({_names(went)})")
    if not pieces and not others:
        return ""
    return "Changed: " + "; ".join(filter(None, [", ".join(pieces), "; ".join(others)]))
