"""What the agent changed, message by message — the changes card, and the way back (BlenderAgent.md §2.5).

THREE KINDS OF CHANGE, THREE WAYS BACK:
- Scene changes: every writing tool is one named step on Blender's undo stack. A message's steps can be
  undone together only while they are the newest — Blender does not let Python read its undo history, so the
  card counts its own steps and watches for changes it did not make (`depsgraph_update_post` while no tool
  runs). After the user's own edits, it says to undo those first, as Unity's card does.
- Files outside the .blend (texts saved to disk, images overwritten, exports): copied before their first
  change in a message, under the add-on's folder, and put back (or removed, for a new file) on Undo.
- Copies of the whole file (decision D6): before a message's first risky call, when the user ticks it on the
  card or Preferences says so — a saved copy of the .blend to restore when undo can't.
"""

import os
import shutil
import time
import uuid

import bpy

from ..core import state

MAX_TURNS = 40
COPY_LIMIT_PER_FILE = 3
COPY_LIMIT_BYTES = 2 * 1024 ** 3
COPY_DEFAULT_MAX = 200 * 1024 ** 2
COPY_KEEP_SECONDS = 7 * 24 * 3600

turns = []  # newest last: {"id", "label", "lines": [...], "steps": int, "foreign": bool, "copy": path|None}
_busy = 0  # >0 while a tool runs: scene updates then are the agent's own
_last_work = 0.0  # when the agent last changed something
GRACE_SECONDS = 1.5  # Blender reports updates when it next evaluates — just after a tool returns, often


def _folder(*parts):
    path = os.path.join(state.path("changes"), *parts)
    os.makedirs(path, exist_ok=True)
    return path


def current():
    return turns[-1] if turns else None


def begin_turn(label):
    """A new message: a new set of lines on its card."""
    turn = {"id": uuid.uuid4().hex[:12], "label": label[:80], "lines": [], "steps": 0, "foreign": False,
            "copy": None, "copy_note": None, "at": time.time()}
    turns.append(turn)
    for old in turns[:-MAX_TURNS]:
        shutil.rmtree(os.path.join(state.path("changes"), old["id"]), ignore_errors=True)
    del turns[:-MAX_TURNS]
    return turn


class running:
    """with changes.running(): scene updates inside are the agent's own."""

    def __enter__(self):
        global _busy
        _busy += 1

    def __exit__(self, *_):
        global _busy, _last_work
        _busy = max(0, _busy - 1)
        _last_work = time.time()


def scene_step(label):
    """One undo step the agent pushed."""
    turn = current()
    if turn is None:
        return
    turn["steps"] += 1
    turn["foreign"] = False  # this step is now the newest
    turn["lines"].append({"kind": "scene", "label": label, "state": "done"})


def before_file(path):
    """Call before writing or deleting a file outside the .blend: keeps a copy the first time in a message."""
    turn = current()
    if turn is None:
        return
    for line in turn["lines"]:
        if line.get("kind") == "file" and line.get("path") == path:
            return
    backup = None
    if os.path.isfile(path):
        backup = os.path.join(_folder(turn["id"], "files"), f"{len(turn['lines'])}-{os.path.basename(path)}")
        shutil.copy2(path, backup)
    turn["lines"].append({"kind": "file", "path": path, "backup": backup, "state": "done",
                          "label": ("Changed " if backup else "Created ") + bpy.path.relpath(path)})


def note(label):
    """A change that cannot be undone (a render saved, a preference), listed without an Undo button."""
    turn = current()
    if turn is not None:
        turn["lines"].append({"kind": "note", "label": label, "state": "done"})


def _on_depsgraph(_scene, _depsgraph):
    if _busy or time.time() - _last_work < GRACE_SECONDS:
        return
    turn = current()
    if turn is not None and turn["steps"]:
        turn["foreign"] = True


def undo_file(line):
    if line.get("state") != "done" or line.get("kind") != "file":
        return None
    path, backup = line["path"], line.get("backup")
    try:
        if backup and os.path.isfile(backup):
            shutil.copy2(backup, path)
        elif os.path.isfile(path):
            os.remove(path)
    except OSError as error:
        return f"Could not put {os.path.basename(path)} back: {error}"
    line["state"] = "undone"
    return None


def undo_turn(turn):
    """Undoes a whole message: its files, then its scene steps if they are still the newest."""
    problems = []
    for line in reversed(turn["lines"]):
        if line.get("kind") == "file":
            problem = undo_file(line)
            if problem:
                problems.append(problem)
    scene = [line for line in turn["lines"] if line.get("kind") == "scene" and line["state"] == "done"]
    if scene:
        if turn is not current() or turn.get("foreign"):
            problems.append("The scene has changed since — undo your own changes first (Edit › Undo), or "
                            "restore the copy of the file.")
        else:
            with running():
                for _ in range(len(scene)):
                    try:
                        bpy.ops.ed.undo()
                    except RuntimeError as error:
                        problems.append(str(error))
                        break
                    else:
                        scene[-1]["state"] = "undone"
                        scene.pop()
            turn["steps"] = 0
    return problems


def keep(turn, line=None):
    for each in turn["lines"] if line is None else [line]:
        if each["state"] == "done":
            each["state"] = "kept"


# ─── Copies of the file ──────────────────────────────────────────────────────────────────────────


def blend_size():
    path = bpy.data.filepath
    try:
        return os.path.getsize(path) if path else 0
    except OSError:
        return 0


def copy_offer():
    """What the card's "Keep a copy of the file first" says: (ticked by default, note) — or (False, why)."""
    if not bpy.data.filepath:
        return False, "The file has never been saved, so there is nothing to copy yet."
    size = blend_size()
    if size > COPY_DEFAULT_MAX:
        return False, f"This file is {size / 1024 ** 2:,.0f} MB; a copy takes a moment."
    return True, None


def keep_copy(reason):
    """Saves a copy of the .blend for this message (once). Returns a line for the tool result, or None."""
    turn = current()
    if turn is None or turn.get("copy"):
        return None
    if not bpy.data.filepath:
        turn["copy_note"] = "No copy kept: the file has never been saved."
        return None
    name = os.path.splitext(os.path.basename(bpy.data.filepath))[0]
    folder = _folder("copies", name)
    target = os.path.join(folder, f"{time.strftime('%Y%m%d-%H%M%S')}-{turn['id']}.blend")
    try:
        bpy.ops.wm.save_as_mainfile(filepath=target, copy=True, compress=True)
    except RuntimeError as error:
        turn["copy_note"] = f"No copy kept: {error}"
        return None
    turn["copy"] = target
    turn["lines"].insert(0, {"kind": "copy", "path": target, "state": "done",
                             "label": f"Copy of the file kept before {reason}"})
    _prune_copies(folder)
    return "(A copy of the file was kept before this — the user can restore it from the changes card.)"


def _prune_copies(folder):
    cutoff = time.time() - COPY_KEEP_SECONDS
    root = os.path.join(state.path("changes"), "copies")
    every = []
    for dirpath, _dirs, files in os.walk(root):
        for file in files:
            full = os.path.join(dirpath, file)
            try:
                stat = os.stat(full)
            except OSError:
                continue
            if stat.st_mtime < cutoff:
                os.remove(full)
            else:
                every.append((stat.st_mtime, stat.st_size, full))
    mine = sorted((e for e in every if os.path.dirname(e[2]) == folder), reverse=True)
    for _, _, full in mine[COPY_LIMIT_PER_FILE:]:
        os.remove(full)
    total = 0
    for mtime, size, full in sorted(every, reverse=True):
        if not os.path.exists(full):
            continue
        total += size
        if total > COPY_LIMIT_BYTES:
            os.remove(full)


def restore_copy(turn):
    """Opens the copy in place of the current file (Blender asks nothing: the card confirmed it)."""
    path = turn.get("copy")
    if not path or not os.path.isfile(path):
        return "That copy is gone (copies are kept for a week)."
    original = bpy.data.filepath
    bpy.ops.wm.open_mainfile(filepath=path)
    if original:
        bpy.ops.wm.save_as_mainfile(filepath=original)
    return None


def register():
    if _on_depsgraph not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph)


def unregister():
    if _on_depsgraph in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph)
