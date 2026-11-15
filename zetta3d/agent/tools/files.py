"""The agent's file tools: import_file, export_file, write_text, edit_text, file_op, open_file.

EVERY PATH STAYS NEXT TO THE .BLEND. Paths are "//" paths resolved by `targets.file_path`, which refuses
anything outside the .blend's folder (and reads attached files from the add-on's store). The agent works on
the user's project, not their disk.

THREE KINDS OF CHANGE (agent.changes). Imports, appends and Text data-blocks change the .blend: each is one
step on Blender's undo stack. Files written to disk (exports, "//" texts, a saved copy) are recorded with
`changes.before_file` before they are written, so the changes card can put the old file back. Saving the
open file and opening another can't be taken back, so they are only noted on the card.

The registry's `undoable` flag is per tool, but write_text, edit_text and file_op change the .blend for some
targets and only the disk for others. Those register as not undoable and push their own undo step when the
.blend changed — the same two calls the dispatcher makes — so a disk-only call never adds an empty step.

VERSIONS. Blender 4.2 LTS through 5.x. Importer and exporter operators moved over the years (OBJ, STL and PLY
went from Python add-ons to built-in `wm.*` operators), and `hasattr(bpy.ops.wm, name)` is always true, so an
operator is looked up by its RNA type and the first one that exists is used.
"""

import difflib
import os

import bpy
from mathutils import Vector

from .. import changes, targets
from . import Result, ToolError, push_undo, tool

MAX_LISTED = 25

# extension → format
FORMATS = {".glb": "glb", ".gltf": "gltf", ".fbx": "fbx", ".obj": "obj", ".usd": "usd", ".usda": "usd",
           ".usdc": "usd", ".usdz": "usd", ".stl": "stl", ".ply": "ply", ".abc": "abc", ".svg": "svg"}

# format → operators to try, newest first
IMPORTERS = {
    "glb": ["import_scene.gltf"],
    "gltf": ["import_scene.gltf"],
    "fbx": ["import_scene.fbx"],
    "obj": ["wm.obj_import", "import_scene.obj"],
    "usd": ["wm.usd_import"],
    "stl": ["wm.stl_import", "import_mesh.stl"],  # wm.stl_import is built in from 4.2; the old add-on before
    "ply": ["wm.ply_import", "import_mesh.ply"],
    "abc": ["wm.alembic_import"],
    "svg": ["import_curve.svg"],  # the bundled "Scalable Vector Graphics" add-on, which the user can turn off
}

# format → [(operator, fixed settings)], newest first
EXPORTERS = {
    "glb": [("export_scene.gltf", {"export_format": "GLB"})],
    "fbx": [("export_scene.fbx", {})],
    "obj": [("wm.obj_export", {}), ("export_scene.obj", {})],
    "usd": [("wm.usd_export", {})],
    "stl": [("wm.stl_export", {}), ("export_mesh.stl", {})],
    "ply": [("wm.ply_export", {}), ("export_mesh.ply", {})],
    "abc": [("wm.alembic_export", {"as_background_job": False})],  # the file must be written before we answer
}
EXTENSIONS = {"glb": (".glb",), "fbx": (".fbx",), "obj": (".obj",), "usd": (".usd", ".usdc", ".usda", ".usdz"),
              "stl": (".stl",), "ply": (".ply",), "abc": (".abc",)}

# The exporters' "selected only" switch has a different name in each.
SELECTION_KEYS = ("use_selection", "export_selected_objects", "selected_objects_only", "selected")

# Operator settings the tools set themselves, and the file browser's own.
RESERVED = {"filepath", "directory", "files", "filename", "check_existing", "filter_glob", "rna_type"}


# ─── Operators ───────────────────────────────────────────────────────────────────────────────────


def _operator(names):
    """(operator, "module.name", {setting names}) for the first of names this Blender has, or None."""
    for name in names:
        module, _, op_name = name.partition(".")
        op = getattr(getattr(bpy.ops, module), op_name)
        try:
            rna = op.get_rna_type()
        except (KeyError, AttributeError, RuntimeError):
            continue  # not in this version (or its add-on is off)
        settings = {p.identifier for p in rna.properties
                    if p.identifier not in RESERVED and not p.identifier.startswith("filter_")}
        return op, name, settings
    return None


def _options(options, name, settings, fixed):
    """The model's options, checked against the operator's own settings so a typo is a clear error."""
    if options is None:
        return {}
    if not isinstance(options, dict):
        raise ToolError("options is an object of the operator's settings, like {\"global_scale\": 0.01}.")
    out = {}
    for key, value in options.items():
        if key in RESERVED or key in fixed:
            raise ToolError(f"{key} is set by the tool; leave it out of options.")
        if key not in settings:
            close = difflib.get_close_matches(key, sorted(settings), n=3, cutoff=0.5)
            hint = f" Did you mean: {', '.join(close)}?" if close else ""
            raise ToolError(f"{name} has no setting \"{key}\".{hint} Its settings: {', '.join(sorted(settings))}.")
        out[key] = value
    return out


def _call(op, name, what, **kwargs):
    try:
        result = op(**kwargs)
    except TypeError as error:
        raise ToolError(f"{name} did not take those options: {error}") from None
    except RuntimeError as error:
        detail = str(error).replace("Error: ", "").strip().splitlines()
        raise ToolError(f"Blender could not {what}: {detail[0] if detail else 'it failed'}") from None
    if "FINISHED" not in result:
        raise ToolError(f"Blender could not {what} (the operator was cancelled).")


def _override():
    """A window and 3D View for operators, when Blender has one (none in background mode)."""
    wm = bpy.context.window_manager
    for window in wm.windows if wm else ():
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                region = next((r for r in area.regions if r.type == "WINDOW"), None)
                return {"window": window, "area": area, "region": region}
    return {}


def _object_mode():
    """Importers need Object Mode. Returns True when it switched."""
    if bpy.context.mode == "OBJECT" or bpy.context.object is None:
        return False
    bpy.ops.object.mode_set(mode="OBJECT")
    return True


def _size(path):
    try:
        size = os.path.getsize(path)
    except OSError:
        return "?"
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:,.0f} {unit}" if unit == "bytes" else f"{size:,.1f} {unit}"
        size /= 1024


def _listed(names):
    shown = ", ".join(names[:MAX_LISTED])
    return shown + (f" and {len(names) - MAX_LISTED} more" if len(names) > MAX_LISTED else "")


# ─── import_file ─────────────────────────────────────────────────────────────────────────────────


def _layer_collection(layer, collection):
    if layer.collection == collection:
        return layer
    for child in layer.children:
        found = _layer_collection(child, collection)
        if found is not None:
            return found
    return None


def _parents(collection, scene):
    return [p for p in [scene.collection, *bpy.data.collections]
            if p.children.get(collection.name) == collection]


def _extent(objects):
    """"1.20 × 0.80 × 2.00 m" across the objects' bounding boxes — scale mistakes show up here first."""
    bpy.context.view_layer.update()
    low, high = [float("inf")] * 3, [float("-inf")] * 3
    for obj in objects:
        if obj.type not in ("MESH", "CURVE", "SURFACE", "FONT", "META", "CURVES", "POINTCLOUD", "VOLUME"):
            continue
        for corner in obj.bound_box:
            world = obj.matrix_world @ Vector(corner)
            for axis in range(3):
                low[axis] = min(low[axis], world[axis])
                high[axis] = max(high[axis], world[axis])
    if low[0] == float("inf"):
        return None
    return " × ".join(f"{high[i] - low[i]:.2f}" for i in range(3)) + " m"


@tool("import_file", undoable=True, risky=True)
def import_file(args, ctx):
    path = args.get("path")
    full = targets.file_path(path, must_exist=True)
    if os.path.isdir(full):
        raise ToolError(f"{path} is a folder; give the file inside it.")
    ext = os.path.splitext(full)[1].lower()
    fmt = args.get("format") or FORMATS.get(ext)
    if fmt not in IMPORTERS:
        raise ToolError(f"Can't tell what kind of file {path} is. Pass format: one of {', '.join(IMPORTERS)}.")
    found = _operator(IMPORTERS[fmt])
    if found is None:
        if fmt == "svg":
            raise ToolError("The SVG importer is off. Ask the user to enable the \"Scalable Vector Graphics (SVG)\" "
                            "add-on in Edit › Preferences › Add-ons.")
        raise ToolError(f"This Blender ({bpy.app.version_string}) has no {fmt} importer.")
    op, op_name, settings = found
    kwargs = _options(args.get("options"), op_name, settings, ())

    scene = bpy.context.scene
    view_layer = bpy.context.view_layer
    wanted = (args.get("collection") or "").strip()
    if wanted.startswith("collection:"):
        wanted = wanted[len("collection:"):]
    target = bpy.data.collections.get(wanted) if wanted else None
    created = target is None
    if created:
        target = bpy.data.collections.new(wanted or os.path.splitext(os.path.basename(full))[0] or "Import")
    if not _layer_collection(view_layer.layer_collection, target):
        scene.collection.children.link(target)

    before_objects = set(bpy.data.objects)
    before_collections = set(bpy.data.collections)
    previous_layer = view_layer.active_layer_collection
    switched = False
    try:
        with bpy.context.temp_override(**_override()):
            switched = _object_mode()
            # Importers put what they make in the active collection: make it ours.
            layer = _layer_collection(view_layer.layer_collection, target)
            if layer is not None:
                view_layer.active_layer_collection = layer
            _call(op, op_name, f"import {path}", filepath=full, **kwargs)
    except ToolError:
        if created and not target.objects and not target.children:
            bpy.data.collections.remove(target)
        raise
    finally:
        try:
            view_layer.active_layer_collection = previous_layer
        except (ReferenceError, TypeError):
            pass

    new_objects = [o for o in bpy.data.objects if o not in before_objects]
    new_collections = [c for c in bpy.data.collections if c not in before_collections and c != target]
    if not new_objects:
        if created and not target.objects and not target.children:
            bpy.data.collections.remove(target)
        raise ToolError(f"{path} had nothing Blender could import.")

    # Collections the importer made (glTF's "scene as collection") hang under ours, wherever it put them.
    ours = [target, *new_collections]
    for collection in new_collections:
        parents = _parents(collection, scene)
        if not any(p in ours for p in parents):
            target.children.link(collection)
            for parent in parents:
                parent.children.unlink(collection)
    for obj in new_objects:
        if not any(c in ours for c in obj.users_collection):
            for owner in list(obj.users_collection):
                owner.objects.unlink(obj)
            target.objects.link(obj)

    listed = [f"{o.name} ({o.type})" for o in new_objects]
    text = (f"Imported {path} into the collection \"{target.name}\": "
            f"{len(new_objects)} object{'s' if len(new_objects) != 1 else ''} — {_listed(listed)}.")
    extent = _extent(new_objects)
    if extent:
        text += f" Overall size: {extent}."
    if switched:
        text += " (Switched to Object Mode first.)"
    return Result(text, label=f"Imported {os.path.basename(full)}")


# ─── export_file ─────────────────────────────────────────────────────────────────────────────────


@tool("export_file")
def export_file(args, ctx):
    fmt = args.get("format")
    if fmt not in EXPORTERS:
        raise ToolError(f"format is one of {', '.join(EXPORTERS)}.")
    path = args.get("path")
    if isinstance(path, str) and path.strip() and not os.path.splitext(path.strip())[1]:
        path = path.strip() + EXTENSIONS[fmt][0]
    full = targets.file_path(path, write=True)
    ext = os.path.splitext(full)[1].lower()
    if ext not in EXTENSIONS[fmt]:
        raise ToolError(f"{path} ends in {ext} but format is {fmt}: use a {EXTENSIONS[fmt][0]} path.")
    if os.path.isdir(full):
        raise ToolError(f"{path} is a folder; give a file name.")
    candidates = EXPORTERS[fmt]
    found = _operator([name for name, _ in candidates])
    if found is None:
        raise ToolError(f"This Blender ({bpy.app.version_string}) has no {fmt} exporter.")
    op, op_name, settings = found
    fixed = {k: v for name, f in candidates if name == op_name for k, v in f.items() if k in settings}
    selection_key = next((key for key in SELECTION_KEYS if key in settings), None)
    kwargs = _options(args.get("options"), op_name, settings, set(fixed) | {selection_key})

    view_layer = bpy.context.view_layer
    in_layer = set(view_layer.objects)
    given = args.get("targets")
    if given:
        chosen = []
        for obj in targets.objects(given):
            for each in [obj, *obj.children_recursive]:
                if each not in chosen:
                    chosen.append(each)
        outside = [o.name for o in chosen if o not in in_layer]
        if outside:
            raise ToolError(f"{_listed(outside)} {'is' if len(outside) == 1 else 'are'} not in the view layer (an "
                            "excluded collection), so Blender can't export it. Include the collection first.")
        scope = "the given objects"
    else:
        chosen = [o for o in view_layer.objects if o.select_get()]
        scope = "the selection"
        if not chosen:
            chosen = list(view_layer.objects)
            scope = "everything in the view layer"
    if not chosen:
        raise ToolError("There is nothing to export: the scene has no objects.")

    existed = os.path.exists(full)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    changes.before_file(full)
    previous = [o for o in view_layer.objects if o.select_get()]
    active = view_layer.objects.active
    try:
        with bpy.context.temp_override(**_override()):
            if bpy.context.mode == "EDIT_MESH":
                for obj in bpy.context.objects_in_mode:
                    obj.update_from_editmode()  # export what the user sees, not the mesh as Edit Mode began
            for obj in view_layer.objects:
                obj.select_set(False)
            for obj in chosen:
                obj.select_set(True)
            if selection_key:
                kwargs[selection_key] = True
            _call(op, op_name, f"export {path}", filepath=full, **fixed, **kwargs)
    finally:
        for obj in view_layer.objects:
            try:
                obj.select_set(obj in previous)
            except RuntimeError:
                pass
        view_layer.objects.active = active
    if not os.path.exists(full):
        raise ToolError(f"The {fmt} exporter finished but wrote no file at {path}.")

    names = [o.name for o in chosen]
    return Result(f"{'Replaced' if existed else 'Wrote'} {targets.relative(full)} ({_size(full)}): "
                  f"{len(names)} object{'s' if len(names) != 1 else ''} from {scope} — {_listed(names)}.",
                  label=f"Exported {os.path.basename(full)}")


# ─── write_text / edit_text ──────────────────────────────────────────────────────────────────────


def _text_target(target, write):
    """("text", name) for "text:Name", or ("file", absolute path) for a "//" path."""
    if not isinstance(target, str) or not target.strip():
        raise ToolError("target is \"text:Name\" or a \"//\" path.")
    target = target.strip()
    if target.startswith("text:"):
        name = target[len("text:"):]
        if not name:
            raise ToolError("Name the text: \"text:notes.py\".")
        return "text", name
    if not target.startswith("//") and not target.startswith("attached:"):
        raise ToolError(f"target is \"text:Name\" or a path relative to the .blend, starting \"//\" — not {target}.")
    return "file", targets.file_path(target, write=write)


def _line_count(content):
    return content.count("\n") + (1 if content and not content.endswith("\n") else 0)


def _scene_step(label):
    """The undo step the dispatcher would push for an undoable tool (see the module's docstring)."""
    push_undo(label)
    changes.scene_step(label)


def _write_file(full, content):
    os.makedirs(os.path.dirname(full), exist_ok=True)
    changes.before_file(full)
    with open(full, "w", encoding="utf-8", newline="") as handle:  # newline="": the content's own line endings
        handle.write(content)


def _texts_showing(full):
    """Text data-blocks that show this file, so the result can say they will offer to reload."""
    found = []
    for text in bpy.data.texts:
        if text.filepath and not text.is_in_memory:
            try:
                if os.path.realpath(bpy.path.abspath(text.filepath)) == os.path.realpath(full):
                    found.append(text.name)
            except (ValueError, OSError):
                continue
    return found


@tool("write_text")
def write_text(args, ctx):
    content = args.get("content")
    if not isinstance(content, str):
        raise ToolError("content is the text to write.")
    overwrite = bool(args.get("overwrite", False))
    kind, where = _text_target(args.get("target"), write=True)
    lines = _line_count(content)
    if kind == "text":
        text = bpy.data.texts.get(where)
        if text is not None and not overwrite:
            raise ToolError(f"text:{where} already exists ({len(text.lines)} lines). Pass overwrite: true to replace "
                            "it whole, or change it with edit_text.")
        created = text is None
        if created:
            text = bpy.data.texts.new(where)
        text.from_string(content)
        label = f"{'Wrote' if created else 'Replaced'} text {text.name}"
        _scene_step(label)
        renamed = f" (Blender named it text:{text.name})" if text.name != where else ""
        return Result(f"{'Created' if created else 'Replaced'} text:{text.name} ({lines} lines){renamed}. It is "
                      "stored in the .blend and shows in the Text Editor.", label=label)

    if os.path.isdir(where):
        raise ToolError(f"{args.get('target')} is a folder.")
    existed = os.path.exists(where)
    if existed and not overwrite:
        raise ToolError(f"{args.get('target')} already exists. Pass overwrite: true to replace it whole, or change "
                        "it with edit_text.")
    _write_file(where, content)
    text = (f"{'Replaced' if existed else 'Created'} {targets.relative(where)} ({lines} lines, "
            f"{_size(where)}).")
    showing = _texts_showing(where)
    if showing:
        text += f" text:{showing[0]} shows this file and will offer to reload it."
    return Result(text, label=f"Wrote {os.path.basename(where)}")


def _changed_lines(old, new):
    """"lines 12–14, 40" in the new text, for the result."""
    ranges = []
    matcher = difflib.SequenceMatcher(None, old.splitlines(), new.splitlines(), autojunk=False)
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if j2 <= j1:
            ranges.append(f"{j1 + 1} (removed lines)")
        elif j2 - j1 == 1:
            ranges.append(str(j1 + 1))
        else:
            ranges.append(f"{j1 + 1}–{j2}")
    if not ranges:
        return "no lines changed"
    shown = ", ".join(ranges[:10]) + (f" and {len(ranges) - 10} more" if len(ranges) > 10 else "")
    return ("line " if len(ranges) == 1 and "–" not in ranges[0] else "lines ") + shown


@tool("edit_text")
def edit_text(args, ctx):
    edits = args.get("edits")
    if not isinstance(edits, list) or not edits:
        raise ToolError("edits is a list of {find, replace}.")
    target = args.get("target")
    kind, where = _text_target(target, write=True)
    if kind == "text":
        text = bpy.data.texts.get(where)
        if text is None:
            close = difflib.get_close_matches(where, bpy.data.texts.keys(), n=3, cutoff=0.5)
            hint = f" Did you mean: {', '.join('text:' + c for c in close)}?" if close else ""
            raise ToolError(f"There is no text:{where}.{hint}")
        original = text.as_string()
    else:
        if not os.path.isfile(where):
            raise ToolError(f"There is no file {target}.")
        try:
            with open(where, "r", encoding="utf-8", newline="") as handle:
                original = handle.read()
        except UnicodeDecodeError:
            raise ToolError(f"{target} is not a UTF-8 text file.") from None

    # All or nothing: work on a copy, and write only when every edit applied.
    content, replaced = original, 0
    for index, edit in enumerate(edits, 1):
        if not isinstance(edit, dict) or not isinstance(edit.get("find"), str) or not isinstance(
                edit.get("replace"), str):
            raise ToolError(f"Edit {index} needs find and replace strings.")
        find, replace = edit["find"], edit["replace"]
        if not find:
            raise ToolError(f"Edit {index} has an empty find.")
        count = content.count(find)
        snippet = find.strip().splitlines()[0][:60] if find.strip() else repr(find)
        if count == 0:
            raise ToolError(f"Edit {index}: \"{snippet}\" is not in {target} (after the edits before it). Nothing "
                            "was changed — read_text first.")
        if count > 1 and not edit.get("all"):
            raise ToolError(f"Edit {index}: \"{snippet}\" appears {count} times in {target}. Nothing was changed — "
                            "add context to make it unique, or pass all: true.")
        content = content.replace(find, replace) if edit.get("all") else content.replace(find, replace, 1)
        replaced += count if edit.get("all") else 1

    where_lines = _changed_lines(original, content)
    summary = (f"{len(edits)} edit{'s' if len(edits) != 1 else ''}, {replaced} replacement"
               f"{'s' if replaced != 1 else ''}; changed {where_lines}; now {_line_count(content)} lines.")
    if kind == "text":
        text.from_string(content)
        label = f"Edited text {text.name}"
        _scene_step(label)
        return Result(f"Edited text:{text.name}: {summary}", label=label)
    _write_file(where, content)
    return Result(f"Edited {targets.relative(where)}: {summary}", label=f"Edited {os.path.basename(where)}")


# ─── file_op ─────────────────────────────────────────────────────────────────────────────────────


def _blend_path(path, must_exist):
    full = targets.file_path(path, must_exist=must_exist, write=not must_exist)
    if not must_exist and not os.path.splitext(full)[1]:
        full += ".blend"
    if os.path.splitext(full)[1].lower() != ".blend":
        raise ToolError(f"{path} is not a .blend file.")
    return full


def _same_file(a, b):
    return bool(a and b) and os.path.realpath(a) == os.path.realpath(b)


@tool("file_op", risky=True)
def file_op(args, ctx):
    action = args.get("action")
    if action == "save":
        return _save(args)
    if action == "save_copy":
        return _save_copy(args)
    if action in ("append", "link"):
        return _append(args, link=action == "link")
    raise ToolError("action is save, save_copy, append or link.")


def _save(args):
    if not bpy.data.filepath:
        raise ToolError("The file has never been saved, so it has no place on disk yet. Ask the user to save it "
                        "(File › Save As) where they want it.")
    path = args.get("path")
    if path and not _same_file(_blend_path(path, must_exist=False), bpy.data.filepath):
        raise ToolError("save saves the open file where it is. For another file use save_copy.")
    name = os.path.basename(bpy.data.filepath)
    try:
        bpy.ops.wm.save_mainfile()
    except RuntimeError as error:
        raise ToolError(f"Blender could not save {name}: {str(error).strip()}") from None
    changes.note(f"Saved {name}")
    return Result(f"Saved {name} ({_size(bpy.data.filepath)}).", label=f"Saved {name}")


def _save_copy(args):
    if not args.get("path"):
        raise ToolError("save_copy needs path: where to save the copy, like \"//versions/chair-v2.blend\".")
    full = _blend_path(args["path"], must_exist=False)
    if _same_file(full, bpy.data.filepath):
        raise ToolError("That is the open file itself — use save.")
    existed = os.path.exists(full)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    changes.before_file(full)
    try:
        bpy.ops.wm.save_as_mainfile(filepath=full, copy=True)
    except RuntimeError as error:
        raise ToolError(f"Blender could not save the copy: {str(error).strip()}") from None
    return Result(f"{'Replaced' if existed else 'Saved'} a copy at {targets.relative(full)} ({_size(full)}); the "
                  "open file is unchanged.", label=f"Saved a copy {os.path.basename(full)}")


def _wanted(items):
    """{"objects": ["Chair"], "materials": ["Oak"]} from ["object:Chair", "material:Oak"]."""
    if not isinstance(items, list) or not items:
        raise ToolError("items lists what to bring in: [\"object:Chair\", \"material:Oak\", \"collection:Props\"].")
    wanted = {}
    for item in items:
        kind, name = targets.split(item)
        attr = targets.KINDS[kind]
        if name not in wanted.setdefault(attr, []):
            wanted[attr].append(name)
    return wanted


def _append(args, link):
    verb = "Linked" if link else "Appended"
    if not args.get("path"):
        raise ToolError(f"{'link' if link else 'append'} needs path: the other .blend, like \"//library/props.blend\".")
    full = _blend_path(args["path"], must_exist=True)
    if _same_file(full, bpy.data.filepath):
        raise ToolError("That is the open file itself.")
    wanted = _wanted(args.get("items"))

    # First only read what the other file has, so one wrong name fails the call before anything is loaded.
    missing = []
    with bpy.data.libraries.load(full) as (source, _):
        for attr, names in wanted.items():
            available = list(getattr(source, attr, None) or [])
            for name in names:
                if name not in available:
                    close = difflib.get_close_matches(name, available, n=3, cutoff=0.5)
                    kind = next(k for k, a in targets.KINDS.items() if a == attr)
                    hint = f" (did you mean {', '.join(close)}?)" if close else ""
                    missing.append(f"{kind}:{name}{hint}")
    if missing:
        raise ToolError(f"{args['path']} has no {'; '.join(missing)}. Nothing was {verb.lower()}.")

    with bpy.data.libraries.load(full, link=link, relative=True) as (_, loaded):
        for attr, names in wanted.items():
            setattr(loaded, attr, list(names))

    scene = bpy.context.scene
    home = bpy.context.view_layer.active_layer_collection.collection
    lines, brought = [], []
    collections = [c for c in (getattr(loaded, "collections", None) or []) if c is not None]
    for collection in collections:
        if not _parents(collection, scene):
            scene.collection.children.link(collection)
    in_collections = {o for c in collections for o in c.all_objects}
    for attr, names in wanted.items():
        kind = next(k for k, a in targets.KINDS.items() if a == attr)
        for name, block in zip(names, getattr(loaded, attr)):
            if block is None:
                lines.append(f"{kind} {name} (could not be loaded)")
                continue
            brought.append(block)
            if isinstance(block, bpy.types.Object) and block not in in_collections and not block.users_scene:
                home.objects.link(block)
            lines.append(f"{kind} {name}" + (f" (as {block.name})" if block.name != name else ""))
    if not brought:
        raise ToolError(f"Blender loaded nothing from {args['path']}.")

    label = f"{verb} {len(brought)} item{'s' if len(brought) != 1 else ''} from {os.path.basename(full)}"
    _scene_step(label)
    text = f"{verb} from {targets.relative(full)}: {', '.join(lines)}."
    if collections or any(isinstance(b, bpy.types.Object) for b in brought):
        text += " Objects and collections were put in the scene."
    if link:
        text += (" Linked data stays in the other file and can't be edited here; append it to get an editable "
                 "copy.")
    return Result(text, label=label)


# ─── open_file ───────────────────────────────────────────────────────────────────────────────────


@tool("open_file", risky=True)
def open_file(args, ctx):
    action = args.get("action")
    current = os.path.basename(bpy.data.filepath) if bpy.data.filepath else "the unsaved file"
    lost = f" The unsaved changes to {current} were discarded." if bpy.data.is_dirty else ""
    ending = " This conversation belonged to that file, so the turn ends here."
    if action == "open":
        if not args.get("path"):
            raise ToolError("open needs path: the .blend to open, like \"//shots/shot-010.blend\".")
        full = _blend_path(args["path"], must_exist=True)
        changes.note(f"Opened {os.path.basename(full)}")
        try:
            bpy.ops.wm.open_mainfile(filepath=full)
        except RuntimeError as error:
            raise ToolError(f"Blender could not open {args['path']}: {str(error).strip()}") from None
        return Result(f"Opened {os.path.basename(full)}.{lost}{ending}", label=f"Opened {os.path.basename(full)}")
    if action == "new":
        changes.note("Started a new file")
        try:
            bpy.ops.wm.read_homefile(app_template="")  # File › New › General: the user's startup file
        except RuntimeError as error:
            raise ToolError(f"Blender could not start a new file: {str(error).strip()}") from None
        return Result(f"Started a new file from the startup file.{lost}{ending}", label="New file")
    if action == "revert":
        if not bpy.data.filepath:
            raise ToolError("The file has never been saved, so there is nothing to revert to.")
        changes.note(f"Reverted {current}")
        try:
            bpy.ops.wm.revert_mainfile()
        except RuntimeError as error:
            raise ToolError(f"Blender could not revert {current}: {str(error).strip()}") from None
        return Result(f"Reverted {current} to its saved state.{lost}{ending}", label=f"Reverted {current}")
    raise ToolError("action is open, new or revert.")
