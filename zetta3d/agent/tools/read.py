"""The agent's reading tools: what is in the open file, and what this Blender's own API offers.

Nothing here changes the file. Every result is written for the model to act on next: targets it can pass to
another tool ("Chair", "material:Oak", "Chair/modifiers/Bevel"), and properties as the data paths
set_properties takes (`modifiers["Bevel"].width = 0.02`), so the model never has to guess a name it saw only
in its training data — Blender renames things between releases.

`outline_text()` is also the snapshot's `outline` field, sent with every message, so it is kept compact: one
object per line, indented by depth.
"""

import glob
import math
import os
import re
import time

import bpy

from .. import errors, targets, values
from . import ToolError, tool

MAX_INSPECT_LINES = 700
OUTLINE_CHARS = 20_000

# Properties every struct has, or that only repeat what another line says: left out of inspect so the
# lines the model reads are the ones it can use.
_NOISE = {
    "rna_type", "name_full", "original", "users", "use_fake_user", "use_extra_user", "is_embedded_data",
    "is_evaluated", "is_missing", "is_runtime_data", "is_editmode", "tag", "is_library_indirect", "library",
    "library_weak_reference", "override_library", "preview", "asset_data", "session_uid", "id_type",
    "matrix_world", "matrix_local", "matrix_basis", "matrix_parent_inverse", "bound_box", "id_data",
    "is_override_data", "is_linked_packed", "is_override_library",
}
# Structs inspect does not open: huge, or settings for the user's tools rather than the target.
_NO_DIVE = {"tool_settings", "sequence_editor", "keying_sets", "keying_sets_all", "timeline_markers",
            "statistics", "grease_pencil_settings", "safe_areas", "depsgraph", "cursor", "texture_slots",
            "shape_keys", "pose", "animation_data", "node_tree", "field", "collision", "rigid_body",
            "rigid_body_constraint", "image_user", "active_material", "motion_path", "image_paint",
            "lightgroup", "cycles_curves", "lineart", "display_settings", "sequencer_colorspace_settings",
            "hydra", "bake", "views", "stereo_3d", "image_settings", "ffmpeg", "simplify_gpencil"}

TEXT_EXTENSIONS = {".py", ".txt", ".md", ".json", ".glsl", ".osl", ".csv", ".xml", ".yaml", ".yml", ".toml",
                   ".ini", ".cfg", ".log", ".html", ".css", ".js", ".mtl", ".usda", ".gltf"}


# ─── Small helpers ───────────────────────────────────────────────────────────────────────────────


def _clamp(value, low, high, default):
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolError(f"Expected a number between {low} and {high}, not {value!r}.")
    return max(low, min(high, int(value)))


def _quote(name):
    return f'"{bpy.utils.escape_identifier(name)}"'


def _show(value):
    if value is None:
        return "null"
    return values.show(value)


def _scene():
    return bpy.context.scene


def _view_layer():
    return bpy.context.view_layer or _scene().view_layers[0]


def _is_default(prop, value):
    """True when a property still has its RNA default — inspect skips those for big structs."""
    try:
        if prop.type == "POINTER":
            return value is None
        length = getattr(prop, "array_length", 0) or 0
        if length:
            defaults = list(prop.default_array)
            return all(abs(float(a) - float(b)) < 1e-6 for a, b in zip(list(value), defaults))
        if prop.type == "ENUM":
            if getattr(prop, "is_enum_flag", False):
                return set(value) == set(prop.default_flag)
            return value == prop.default
        if prop.type == "FLOAT":
            return abs(value - prop.default) < 1e-6
        return value == prop.default
    except (TypeError, ValueError, AttributeError):
        return False


def _dump(struct, prefix, out, depth=1, changed_only=False, skip=(), always=()):
    """`prefix.prop = value` for each writable property of struct, opening plain (non-ID) structs to depth.

    changed_only leaves out what still has its default, except the `always` properties — the ones a user
    asks about even when untouched (a light's power, a camera's lens)."""
    if struct is None or not hasattr(struct, "bl_rna"):
        return
    join = (prefix + ".") if prefix else ""
    for prop in struct.bl_rna.properties:
        key = prop.identifier
        if key in _NOISE or key in skip or key.startswith("bl_") or (prefix and key == "name"):
            continue  # a nested "name" renames data or an add-on's settings — never what the model wants
        if len(out) > MAX_INSPECT_LINES:
            return
        try:
            value = getattr(struct, key)
        except (AttributeError, RuntimeError, TypeError):
            continue
        if prop.type == "COLLECTION":
            continue
        if prop.type == "POINTER":
            if isinstance(value, bpy.types.ID) or (value is None and _points_to_id(prop)):
                if prop.is_readonly or (changed_only and value is None and key not in always):
                    continue
                out.append(f"{join}{key} = {_show(value)}")
            elif value is not None and depth > 0 and key not in _NO_DIVE:
                _dump(value, f"{join}{key}", out, depth - 1, changed_only, always=always)
            continue
        if prop.is_readonly:
            continue
        if changed_only and key not in always and _is_default(prop, value):
            continue
        line = f"{join}{key} = {_show(value)}"
        if prop.type == "FLOAT" and getattr(prop, "unit", "") == "ROTATION":
            line += f"  # degrees {_degrees(value)}"
        out.append(line)


def _points_to_id(prop):
    fixed = getattr(prop, "fixed_type", None)
    try:
        return fixed is not None and issubclass(getattr(bpy.types, fixed.identifier), bpy.types.ID)
    except (AttributeError, TypeError):
        return False


def _degrees(value):
    try:
        return "[" + ", ".join(f"{math.degrees(v):.4g}" for v in value) + "]"
    except TypeError:
        return f"{math.degrees(value):.4g}"


def _layer_collections():
    found = {}

    def walk(layer):
        found[layer.collection.name] = layer
        for child in layer.children:
            walk(child)

    walk(_view_layer().layer_collection)
    return found


# ─── blender_info ────────────────────────────────────────────────────────────────────────────────


@tool("blender_info")
def blender_info(args, ctx):
    scene = _scene()
    render = scene.render
    lines = [f"Blender {bpy.app.version_string}"]
    path = bpy.data.filepath
    if path:
        lines.append(f"File: {path} ({'unsaved changes' if bpy.data.is_dirty else 'saved'})")
    else:
        lines.append("File: never saved (\"//\" paths need it saved first)")
    unit = scene.unit_settings
    lines.append(f"Units: {unit.system}, scale {unit.scale_length:g}, length {unit.length_unit}")
    engine = render.engine
    device = ""
    if engine == "CYCLES" and hasattr(scene, "cycles"):
        device = f", device {scene.cycles.device}"
        try:
            prefs = bpy.context.preferences.addons["cycles"].preferences
            device += f" ({prefs.compute_device_type})"
        except (KeyError, AttributeError):
            pass
    lines.append(f"Render: {engine}{device}, {render.resolution_x}×{render.resolution_y} at "
                 f"{render.resolution_percentage}%")
    view = scene.view_settings
    lines.append(f"Colour management: view {view.view_transform}, look {view.look}, exposure {view.exposure:g}, "
                 f"display {scene.display_settings.display_device}")
    fps = render.fps / (render.fps_base or 1)
    lines.append(f"Frames: {scene.frame_start}–{scene.frame_end}, current {scene.frame_current}, {fps:g} fps")
    for each in bpy.data.scenes:
        layers = ", ".join(layer.name for layer in each.view_layers)
        mark = " (open)" if each == scene else ""
        lines.append(f"Scene scene:{each.name}{mark}: view layers {layers}; camera "
                     f"{each.camera.name if each.camera else 'none'}; world "
                     f"{('world:' + each.world.name) if each.world else 'none'}")
    try:
        mode = bpy.context.mode
    except AttributeError:
        mode = "OBJECT"
    workspace = getattr(bpy.context, "workspace", None)
    lines.append(f"Mode: {mode}" + (f", workspace {workspace.name}" if workspace else ""))
    counts = []
    for label, attr in (("objects", "objects"), ("meshes", "meshes"), ("materials", "materials"),
                        ("images", "images"), ("node groups", "node_groups"), ("actions", "actions"),
                        ("collections", "collections"), ("cameras", "cameras"), ("lights", "lights"),
                        ("texts", "texts"), ("worlds", "worlds")):
        data = getattr(bpy.data, attr, None)
        if data is not None:
            counts.append(f"{len(data)} {label}")
    lines.append("Holds: " + ", ".join(counts))
    addons = sorted(bpy.context.preferences.addons.keys())
    shown = ", ".join(addons[:80]) + (f" … {len(addons)} in all" if len(addons) > 80 else "")
    lines.append(f"Add-ons enabled: {shown}")
    if not bpy.app.background:
        try:
            import gpu

            lines.append(f"GPU: {gpu.platform.backend_type_get()}, {gpu.platform.renderer_get()}")
        except Exception:  # noqa: BLE001 — the GPU module is not always ready; the line is optional
            pass
    else:
        lines.append("Blender runs in the background: no window, so no 3D View.")
    return "\n".join(lines)


# ─── scene_outline / outline_text ────────────────────────────────────────────────────────────────


def _object_summary(obj, scene):
    parts = [obj.type]
    data = obj.data
    if obj.type == "LIGHT" and data is not None:
        parts[0] = f"LIGHT {data.type} {data.energy:g}W"
    elif obj.type == "CAMERA" and data is not None:
        parts[0] = f"CAMERA {data.lens:g}mm" if data.type == "PERSP" else f"CAMERA {data.type}"
    if data is not None and getattr(data, "users", 1) > 1 and obj.type not in {"EMPTY"}:
        kind = targets.name_of(data)
        parts[0] += f" {kind} ×{data.users}"
    if len(obj.modifiers):
        names = [m.name for m in obj.modifiers]
        parts.append(", ".join(names[:6]) + (f" +{len(names) - 6}" if len(names) > 6 else ""))
    materials = [slot.material.name for slot in obj.material_slots if slot.material]
    if materials:
        parts.append(", ".join(materials[:4]) + (f" +{len(materials) - 4}" if len(materials) > 4 else ""))
    flags = []
    try:
        hidden = obj.hide_get()
    except RuntimeError:  # not in the view layer
        hidden = False
    if hidden or obj.hide_viewport:
        flags.append("(hidden)")
    if obj.hide_render:
        flags.append("(render off)")
    if obj.instance_type == "COLLECTION" and obj.instance_collection:
        flags.append(f"(instance of {obj.instance_collection.name})")
    if scene.camera == obj:
        flags.append("(scene camera)")
    if obj.library:
        flags.append(f"(linked from {os.path.basename(obj.library.filepath)})")
    return f"{obj.name} [{' · '.join(parts)}]" + (" " + " ".join(flags) if flags else "")


def _collection_line(collection, layers):
    flags = []
    layer = layers.get(collection.name)
    if layer is not None:
        if layer.exclude:
            flags.append("(excluded)")
        elif layer.hide_viewport:
            flags.append("(hidden)")
    if collection.hide_viewport:
        flags.append("(disabled)")
    if collection.hide_render:
        flags.append("(render off)")
    return f"Collection {collection.name}" + (" " + " ".join(flags) if flags else "")


def _outline_lines(root, depth):
    scene = _scene()
    layers = _layer_collections()
    lines = []

    def count_inside(collection):
        return len(collection.all_objects)

    def add_object(obj, level, members):
        line = _object_summary(obj, scene)
        if obj.parent and obj.parent.name not in members:
            line += f" (child of {obj.parent.name})"
        kids = [child for child in obj.children if child.name in members]
        if level >= depth and kids:
            line += f" (+{len(kids)} children)"
        lines.append("  " * level + line)
        if level < depth:
            for child in sorted(kids, key=lambda o: o.name):
                add_object(child, level + 1, members)

    def add_collection(collection, level, title=None):
        if title is None:
            line = _collection_line(collection, layers)
            if level >= depth and (collection.children or collection.objects):
                line += f" (… {count_inside(collection)} objects inside)"
            lines.append("  " * level + line)
            if level >= depth:
                return
            inner = level + 1
        else:
            lines.append(title)
            inner = level + 1
        for child in collection.children:
            add_collection(child, inner)
        members = set(collection.objects.keys())
        for obj in sorted(collection.objects, key=lambda o: o.name):
            if obj.parent is None or obj.parent.name not in members:
                add_object(obj, inner, members)

    if root is None:
        add_collection(scene.collection, 0, title=f"Scene {scene.name}")
    elif isinstance(root, bpy.types.Collection):
        add_collection(root, 0)
    elif isinstance(root, bpy.types.Scene):
        add_collection(root.collection, 0, title=f"Scene {root.name}")
    elif isinstance(root, bpy.types.Object):
        everything = {root.name} | {child.name for child in root.children_recursive}
        add_object(root, 0, everything)
    else:
        raise ToolError(f"{targets.name_of(root)} has no outline. Use an object or \"collection:Name\".")
    return lines


def outline_text(root=None, depth=3, limit=200, offset=0, max_chars=OUTLINE_CHARS):
    """The scene as compact indented lines (the snapshot's `outline`, and scene_outline's result).

    root: None (the open scene), a target string, or a data-block.
    """
    if isinstance(root, str):
        root = targets.datablock(root)
    lines = _outline_lines(root, max(1, depth))
    head, body = lines[:1], lines[1:]
    page = body[offset:offset + limit]
    out = head + page
    rest = len(body) - offset - len(page)
    text = "\n".join(out)
    if len(text) > max_chars:
        cut = text[:max_chars].rsplit("\n", 1)[0]
        shown = cut.count("\n")
        return cut + f"\n… cut at {max_chars:,} characters: call scene_outline with offset={offset + shown}"
    if rest > 0:
        text += f"\n… {rest} more lines: call scene_outline with offset={offset + len(page)}"
    return text


@tool("scene_outline")
def scene_outline(args, ctx):
    depth = _clamp(args.get("depth"), 1, 8, 3)
    limit = _clamp(args.get("limit"), 1, 500, 200)
    offset = _clamp(args.get("offset"), 0, 10 ** 7, 0)
    root = args.get("root")
    # A whole result is allowed to be bigger than the snapshot's field.
    return outline_text(root or None, depth=depth, limit=limit, offset=offset, max_chars=90_000)


# ─── inspect ─────────────────────────────────────────────────────────────────────────────────────

SECTIONS = ("transform", "data", "modifiers", "constraints", "materials", "custom", "animation", "physics")
_TRANSFORM = ("location", "rotation_mode", "scale", "dimensions", "delta_location", "delta_rotation_euler",
              "delta_scale", "parent", "parent_type", "hide_viewport", "hide_render", "hide_select",
              "display_type", "show_in_front", "show_wire", "show_name", "instance_type", "instance_collection",
              "empty_display_type", "empty_display_size", "color")


def _transform_lines(obj, out):
    for key in _TRANSFORM:
        prop = obj.bl_rna.properties.get(key)
        if prop is None:
            continue
        value = getattr(obj, key)
        if key.startswith("delta_") and _is_default(prop, value):
            continue
        if key in {"empty_display_type", "empty_display_size"} and obj.type != "EMPTY":
            continue
        if key == "instance_collection" and obj.instance_type != "COLLECTION":
            continue
        if key in {"show_in_front", "show_wire", "show_name", "color", "hide_select", "instance_type"} and \
                _is_default(prop, value):
            continue
        line = f"{key} = {_show(value)}"
        if key == "delta_rotation_euler":
            line += f"  # degrees {_degrees(value)}"
        out.append(line)
        if key == "rotation_mode":
            mode = obj.rotation_mode
            if mode == "QUATERNION":
                out.append(f"rotation_quaternion = {_show(obj.rotation_quaternion)}")
            elif mode == "AXIS_ANGLE":
                out.append(f"rotation_axis_angle = {_show(obj.rotation_axis_angle)}")
            else:
                out.append(f"rotation_euler = {_show(obj.rotation_euler)}  # degrees "
                           f"{_degrees(obj.rotation_euler)}")
    if obj.parent and obj.parent_type == "BONE":
        out.append(f"parent_bone = {_show(obj.parent_bone)}")
    try:
        if obj.hide_get():
            out.append("# hidden in this view layer (the eye icon) — modify_object or select can't see it")
    except RuntimeError:
        out.append("# not in the open view layer")
    collections = ", ".join(f"collection:{c.name}" for c in obj.users_collection) or "none"
    out.append(f"# in {collections}")
    if obj.children:
        out.append("# children: " + ", ".join(child.name for child in obj.children[:40]))


def _mesh_stats(obj, out):
    import bmesh
    import numpy as np  # bundled with every Blender

    mesh = obj.data
    if obj.mode == "EDIT":
        bm = bmesh.from_edit_mesh(mesh)
        owned = False
    else:
        bm = None
        owned = True
    verts = len(bm.verts) if bm else len(mesh.vertices)
    edges = len(bm.edges) if bm else len(mesh.edges)
    faces = len(bm.faces) if bm else len(mesh.polygons)
    if bm is not None:
        sizes = np.fromiter((len(f.verts) for f in bm.faces), dtype=np.int64, count=faces)
    else:
        sizes = np.empty(faces, dtype=np.int64)
        mesh.polygons.foreach_get("loop_total", sizes)
    tris = int((sizes - 2).sum()) if faces else 0
    quads = int((sizes == 4).sum())
    ngons = int((sizes > 4).sum())
    out.append(f"# mesh mesh:{mesh.name}: {verts:,} verts, {edges:,} edges, {faces:,} faces, {tris:,} tris "
               f"({quads:,} quads, {ngons:,} n-gons)" + (" — Edit Mode" if obj.mode == "EDIT" else ""))
    # Non-manifold edges need each edge's face count: bmesh has it; skipped on huge meshes, where it would
    # stall Blender for seconds.
    if verts <= 2_000_000:
        if bm is None:
            bm = bmesh.new()
            bm.from_mesh(mesh)
        boundary = sum(1 for e in bm.edges if e.is_boundary)
        non_manifold = sum(1 for e in bm.edges if not e.is_manifold)
        wire = sum(1 for e in bm.edges if e.is_wire)
        loose = sum(1 for v in bm.verts if not v.link_edges)
        out.append(f"# non-manifold edges: {non_manifold:,} ({boundary:,} open borders, {wire:,} wire)"
                   + (f"; {loose:,} loose verts" if loose else ""))
        if owned:
            bm.free()
    if faces:
        smooth = sum(1 for p in mesh.polygons if p.use_smooth) if faces <= 2_000_000 else None
        if smooth is not None:
            shade = "smooth" if smooth == faces else "flat" if smooth == 0 else f"mixed ({smooth:,} smooth)"
            out.append(f"# shading: {shade}")
    uvs = [uv.name + (" (active)" if uv.active else "") for uv in mesh.uv_layers]
    out.append(f"# UV maps: {', '.join(uvs) if uvs else 'none'}")
    colors = [c.name for c in getattr(mesh, "color_attributes", [])]
    if colors:
        out.append(f"# colour attributes: {', '.join(colors)}")
    builtin = {"position", "sharp_face", "sharp_edge", "material_index", ".edge_verts", ".corner_vert",
               ".corner_edge", ".select_vert", ".select_edge", ".select_poly", "UVMap"}
    attributes = [a.name for a in mesh.attributes if not a.name.startswith(".") and a.name not in builtin
                  and a.name not in colors and a.name not in mesh.uv_layers]
    if attributes:
        out.append(f"# other attributes: {', '.join(attributes[:30])}")
    if obj.modifiers:
        try:
            evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
            data = evaluated.data
            if data is not None and hasattr(data, "polygons"):
                tris_after = sum(p.loop_total - 2 for p in data.polygons) if len(data.polygons) < 3_000_000 else None
                out.append(f"# after modifiers: {len(data.vertices):,} verts, {len(data.polygons):,} faces"
                           + (f", {tris_after:,} tris" if tris_after is not None else ""))
        except (RuntimeError, AttributeError):
            pass


_LIGHT_KEYS = {"type", "energy", "color", "use_shadow", "shadow_soft_size", "spot_size", "spot_blend", "shape",
               "size", "size_y", "angle", "use_temperature", "temperature"}
_CAMERA_KEYS = {"type", "lens", "lens_unit", "sensor_width", "sensor_fit", "clip_start", "clip_end", "shift_x",
                "shift_y", "ortho_scale", "use_dof", "focus_object", "focus_distance", "aperture_fstop"}


def _data_lines(obj, out):
    data = obj.data
    if data is None:
        if obj.type == "EMPTY":
            out.append("# an empty: no data")
        return
    out.append(f"# data {targets.name_of(data)}" + (f", shared by {data.users} users" if data.users > 1 else ""))
    if obj.type == "MESH":
        _mesh_stats(obj, out)
        _dump(data, "data", out, depth=0, changed_only=True, skip={"texspace_location", "texspace_size"})
        if obj.vertex_groups:
            names = [f"{g.name}" + (" (locked)" if g.lock_weight else "") for g in obj.vertex_groups]
            out.append(f"# vertex groups ({len(names)}): {', '.join(names[:60])}")
            out.append(f"vertex_groups.active_index = {obj.vertex_groups.active_index}")
    elif obj.type == "LIGHT":
        _dump(data, "data", out, depth=0, changed_only=True, always=_LIGHT_KEYS)
    elif obj.type == "CAMERA":
        _dump(data, "data", out, depth=1, changed_only=True, always=_CAMERA_KEYS, skip={"stereo"})
    elif obj.type == "ARMATURE":
        _dump(data, "data", out, depth=0)
        _bones(obj, out)
    else:
        _dump(data, "data", out, depth=1, changed_only=True)
    keys = getattr(data, "shape_keys", None)
    if keys is not None:
        out.append(f"# shape keys ({len(keys.key_blocks)}), reference {keys.reference_key.name}")
        for block in keys.key_blocks[:80]:
            path = f"data.shape_keys.key_blocks[{_quote(block.name)}]"
            if block == keys.reference_key:
                continue
            out.append(f"{path}.value = {block.value:.4g}  # {block.slider_min:g}–{block.slider_max:g}, relative "
                       f"to {block.relative_key.name}" + (", muted" if block.mute else ""))


def _bones(obj, out):
    bones = obj.data.bones
    out.append(f"# bones ({len(bones)}):")
    for bone in list(bones)[:200]:
        parent = bone.parent.name if bone.parent else "none"
        head = values.show(bone.head_local)
        tail = values.show(bone.tail_local)
        out.append(f"#   {bone.name}: parent {parent}, head {head}, tail {tail}"
                   + ("" if bone.use_deform else ", no deform"))
    if len(bones) > 200:
        out.append(f"#   … {len(bones) - 200} more")
    if obj.pose:
        for pose_bone in obj.pose.bones:
            prefix = f"pose.bones[{_quote(pose_bone.name)}]"
            for key in ("location", "rotation_quaternion", "rotation_euler", "scale"):
                if key == "rotation_quaternion" and pose_bone.rotation_mode != "QUATERNION":
                    continue
                if key == "rotation_euler" and pose_bone.rotation_mode in {"QUATERNION", "AXIS_ANGLE"}:
                    continue
                prop = pose_bone.bl_rna.properties[key]
                value = getattr(pose_bone, key)
                if not _is_default(prop, value):
                    out.append(f"{prefix}.{key} = {_show(value)}")
            for constraint in pose_bone.constraints:
                out.append(f"# {prefix}.constraints[{_quote(constraint.name)}] — {constraint.type}")


def _modifier_lines(obj, out):
    if not obj.modifiers:
        out.append("# no modifiers")
        return
    for index, modifier in enumerate(obj.modifiers):
        prefix = f"modifiers[{_quote(modifier.name)}]"
        out.append(f"# {prefix} — {modifier.type}, index {index}")
        _dump(modifier, prefix, out, depth=1, skip={"name", "type", "is_active", "is_override_data",
                                                    "persistent_uid", "show_expanded", "use_pin_to_last"})
        if modifier.type == "NODES" and modifier.node_group:
            _node_modifier_inputs(modifier, prefix, out)


def _node_modifier_inputs(modifier, prefix, out):
    """A Geometry Nodes modifier's inputs, keyed by each socket's identifier.

    Blender 5.x shows them as RNA (`properties.inputs.Socket_2.value`); before that they were ID properties on
    the modifier (`["Socket_2"]`). Both print as paths set_properties takes.
    """
    try:
        items = modifier.node_group.interface.items_tree
    except AttributeError:
        return
    inputs = getattr(getattr(modifier, "properties", None), "inputs", None)
    for item in items:
        if getattr(item, "item_type", "") != "SOCKET" or item.in_out != "INPUT":
            continue
        if item.socket_type == "NodeSocketGeometry":
            continue
        key = item.identifier
        if inputs is not None:
            socket = getattr(inputs, key, None)
            if socket is None or not hasattr(socket, "value"):
                continue
            path = f"{prefix}.properties.inputs.{key}"
            out.append(f"{path}.value = {_show(socket.value)}  # {item.name} ({item.socket_type})")
            if getattr(socket, "type", "VALUE") != "VALUE":
                out.append(f"{path}.type = {_show(socket.type)}")
                out.append(f"{path}.attribute_name = {_show(socket.attribute_name)}")
            continue
        try:
            if key not in modifier.keys():
                continue
            value = modifier[key]
        except TypeError:
            continue
        if hasattr(value, "to_list"):
            value = value.to_list()
        out.append(f"{prefix}[{_quote(key)}] = {_show(value)}  # {item.name} ({item.socket_type})")


def _constraint_lines(obj, out):
    if not obj.constraints:
        out.append("# no constraints")
        return
    for constraint in obj.constraints:
        prefix = f"constraints[{_quote(constraint.name)}]"
        out.append(f"# {prefix} — {constraint.type}" + ("" if constraint.is_valid else " (INVALID: check its "
                                                                                         "target)"))
        _dump(constraint, prefix, out, depth=0, skip={"name", "type", "show_expanded", "is_override_data",
                                                      "active"})


def _material_lines(obj, out):
    if not obj.material_slots:
        out.append("# no material slots")
        return
    per_slot = {}
    if obj.type == "MESH" and obj.mode != "EDIT" and len(obj.data.polygons) <= 5_000_000:
        for poly in obj.data.polygons:
            per_slot[poly.material_index] = per_slot.get(poly.material_index, 0) + 1
    for index, slot in enumerate(obj.material_slots):
        faces = f", {per_slot.get(index, 0):,} faces" if per_slot or obj.type == "MESH" else ""
        out.append(f"material_slots[{index}].material = {_show(slot.material)}  # link {slot.link}{faces}")
        out.append(f"material_slots[{index}].link = {_show(slot.link)}")
    out.append(f"active_material_index = {obj.active_material_index}")
    out.append("# a material's nodes: inspect_nodes with owner \"material:<name>\"")


def _custom_lines(block, prefix, out):
    try:
        keys = list(block.keys())
    except TypeError:
        return
    rna = block.bl_rna.properties
    for key in keys:
        if key == "_RNA_UI" or key in rna:
            continue  # add-on settings stored as ID properties: not the user's own
        value = block[key]
        if hasattr(value, "to_dict"):
            value = value.to_dict()
        elif hasattr(value, "to_list"):
            value = value.to_list()
        text = repr(value) if isinstance(value, dict) else _show(value)
        out.append(f"{prefix}[{_quote(key)}] = {text[:300]}")


def _fcurves(action, slot):
    """An action's F-curves for one slot.

    Blender 4.4 made actions layered (slots, layers, strips, channel bags); 5.0 removed `Action.fcurves`.
    Older Blenders have only `action.fcurves`.
    """
    legacy = getattr(action, "fcurves", None)
    if legacy is not None and not getattr(action, "is_action_layered", False):
        return list(legacy)
    try:
        from bpy_extras import anim_utils

        bag = anim_utils.action_get_channelbag_for_slot(action, slot) if slot is not None else None
        if bag is not None:
            return list(bag.fcurves)
    except (ImportError, AttributeError):
        pass
    found = []
    for layer in getattr(action, "layers", []):
        for strip in layer.strips:
            for bag in getattr(strip, "channelbags", []):
                if slot is None or getattr(bag, "slot", None) == slot:
                    found.extend(bag.fcurves)
    if not found and legacy is not None:
        found = list(legacy)
    return found


def _animation_lines(block, prefix, out):
    data = getattr(block, "animation_data", None)
    if data is None:
        return False
    label = prefix or "itself"
    action = data.action
    slot = getattr(data, "action_slot", None)
    if action is not None:
        slot_text = f", slot {slot.name_display}" if slot is not None and hasattr(slot, "name_display") else ""
        out.append(f"# animation on {label}: action:{action.name}{slot_text}, frames "
                   f"{action.frame_range[0]:g}–{action.frame_range[1]:g}")
        for curve in _fcurves(action, slot)[:120]:
            points = curve.keyframe_points
            if len(points):
                first, last = points[0].co.x, points[-1].co.x
                interp = points[0].interpolation
                out.append(f"#   {curve.data_path}[{curve.array_index}]: {len(points)} keys, frames "
                           f"{first:g}–{last:g}, {interp}" + (" (muted)" if curve.mute else ""))
            else:
                out.append(f"#   {curve.data_path}[{curve.array_index}]: no keys")
    for driver_curve in data.drivers[:60]:
        driver = driver_curve.driver
        state = "" if driver.is_valid else " — INVALID (see recent_errors)"
        what = driver.expression if driver.type == "SCRIPTED" else driver.type
        out.append(f"# driver on {label}: {driver_curve.data_path}[{driver_curve.array_index}] = {what}{state}")
    for track in data.nla_tracks:
        strips = ", ".join(f"{s.name} ({s.action.name if s.action else 'no action'})" for s in track.strips)
        out.append(f"# NLA track {track.name}" + (" (muted)" if track.mute else "") + f": {strips}")
    return True


def _physics_lines(obj, out):
    found = False
    if obj.rigid_body:
        found = True
        out.append("# rigid body")
        _dump(obj.rigid_body, "rigid_body", out, depth=0)
    if obj.rigid_body_constraint:
        found = True
        _dump(obj.rigid_body_constraint, "rigid_body_constraint", out, depth=0)
    if obj.field and obj.field.type != "NONE":
        found = True
        _dump(obj.field, "field", out, depth=0, changed_only=True)
    for modifier in obj.modifiers:
        if modifier.type in {"CLOTH", "SOFT_BODY", "COLLISION", "FLUID", "DYNAMIC_PAINT", "PARTICLE_SYSTEM"}:
            found = True
            out.append(f"# physics: modifiers[{_quote(modifier.name)}] — {modifier.type}")
            if modifier.type == "CLOTH":
                _dump(modifier.settings, f"modifiers[{_quote(modifier.name)}].settings", out, depth=0,
                      changed_only=True)
            if modifier.type == "COLLISION" and obj.collision:
                _dump(obj.collision, "collision", out, depth=0, changed_only=True)
    for system in obj.particle_systems:
        found = True
        settings = system.settings
        out.append(f"# particle system {system.name}: particle:{settings.name}, {settings.type}, "
                   f"count {settings.count}")
    if not found:
        out.append("# no physics")


def _inspect_object(obj, sections, out):
    out.append(f"{obj.name} — {obj.type} object. Set any line with set_properties on target \"{obj.name}\".")
    if "transform" in sections:
        out.append("# — transform —")
        _transform_lines(obj, out)
    if "data" in sections:
        out.append("# — data —")
        _data_lines(obj, out)
    if "modifiers" in sections and obj.type not in {"EMPTY", "CAMERA", "LIGHT", "ARMATURE"}:
        out.append("# — modifiers —")
        _modifier_lines(obj, out)
    if "constraints" in sections:
        out.append("# — constraints —")
        _constraint_lines(obj, out)
    if "materials" in sections and obj.type in {"MESH", "CURVE", "SURFACE", "FONT", "META", "GPENCIL",
                                                 "GREASEPENCIL", "CURVES", "POINTCLOUD", "VOLUME"}:
        out.append("# — materials —")
        _material_lines(obj, out)
    if "custom" in sections:
        before = len(out)
        out.append("# — custom properties —")
        _custom_lines(obj, "", out)
        if obj.data is not None:
            _custom_lines(obj.data, "data", out)
        if len(out) == before + 1:
            out.pop()
    if "animation" in sections:
        out.append("# — animation —")
        any_found = _animation_lines(obj, "", out)
        if obj.data is not None:
            any_found = _animation_lines(obj.data, "data", out) or any_found
            keys = getattr(obj.data, "shape_keys", None)
            if keys is not None:
                any_found = _animation_lines(keys, "data.shape_keys", out) or any_found
        if not any_found:
            out.append("# not animated")
    if "physics" in sections:
        out.append("# — physics —")
        _physics_lines(obj, out)


def _part_prefix(target, owner, item):
    """The data path from the owning object to a part ("Chair/modifiers/Bevel" → modifiers["Bevel"])."""
    collection = target.split("/", 2)[1]
    name = _quote(item.name)
    return {
        "bones": f"data.bones[{name}]",
        "pose_bones": f"pose.bones[{name}]",
        "shape_keys": f"data.shape_keys.key_blocks[{name}]",
    }.get(collection, f"{collection}[{name}]")


def _scene_lines(scene, out):
    """A scene's own settings, its render settings, and only the active engine's settings — the other
    engines' hundreds of lines would bury the ones that matter."""
    _dump(scene, "", out, depth=0)
    _dump(scene.render, "render", out, depth=0)
    for key in ("view_settings", "display_settings", "unit_settings"):
        _dump(getattr(scene, key), key, out, depth=0)
    engine = scene.render.engine
    if engine == "CYCLES" and hasattr(scene, "cycles"):
        _dump(scene.cycles, "cycles", out, depth=0)
    elif engine.startswith("BLENDER_EEVEE") and hasattr(scene, "eevee"):
        _dump(scene.eevee, "eevee", out, depth=0)
    elif engine == "BLENDER_WORKBENCH":
        _dump(scene.display.shading, "display.shading", out, depth=0)
    out.append("# other engines' settings: inspect does not list them; api_lookup \"CyclesRenderSettings\" or "
               "\"SceneEEVEE\" names them")


def _inspect_datablock(block, sections, out):
    target = targets.name_of(block)
    out.append(f"{target} — {block.bl_rna.identifier}. Set any line with set_properties on target \"{target}\".")
    if getattr(block, "users", None) is not None:
        out.append(f"# users: {block.users}" + (" (fake user)" if getattr(block, "use_fake_user", False) else ""))
    if "data" in sections:
        if isinstance(block, bpy.types.Scene):
            _scene_lines(block, out)
        else:
            _dump(block, "", out, depth=1, changed_only=isinstance(block, bpy.types.Mesh))
    if isinstance(block, (bpy.types.Material, bpy.types.World)) or (isinstance(block, bpy.types.NodeTree)):
        out.append(f"# its nodes: inspect_nodes with owner \"{target}\"")
    if isinstance(block, bpy.types.Material):
        users = [o.name for o in bpy.data.objects if any(s.material == block for s in o.material_slots)]
        out.append(f"# used by: {', '.join(users[:40]) if users else 'no objects'}")
    if isinstance(block, bpy.types.Collection):
        out.append(f"# objects: {', '.join(o.name for o in block.objects[:80]) or 'none'}")
        out.append(f"# child collections: {', '.join(c.name for c in block.children) or 'none'}")
    if isinstance(block, bpy.types.Image):
        out.append(f"# size {block.size[0]}×{block.size[1]}, source {block.source}, "
                   f"{'packed' if block.packed_file else 'file ' + (block.filepath or 'none')}")
    if isinstance(block, bpy.types.Action):
        out.append(f"# frames {block.frame_range[0]:g}–{block.frame_range[1]:g}")
        for slot in getattr(block, "slots", [None]) or [None]:
            curves = _fcurves(block, slot)
            if slot is not None:
                out.append(f"# slot {getattr(slot, 'name_display', slot.identifier)}")
            for curve in curves[:120]:
                out.append(f"#   {curve.data_path}[{curve.array_index}]: {len(curve.keyframe_points)} keys")
    if "custom" in sections:
        _custom_lines(block, "", out)
    if "animation" in sections:
        _animation_lines(block, "", out)


@tool("inspect")
def inspect(args, ctx):
    target = args.get("target")
    sections = args.get("sections") or list(SECTIONS)
    if not isinstance(sections, list) or any(s not in SECTIONS for s in sections):
        raise ToolError(f"sections takes a list of: {', '.join(SECTIONS)}.")
    owner, item = targets.part(target)
    out = []
    if item is not None:
        prefix = _part_prefix(target, owner, item)
        out.append(f"{target} — {item.bl_rna.identifier}. Set any line with set_properties on target "
                   f"\"{owner.name}\".")
        _dump(item, prefix, out, depth=1)
        if isinstance(item, bpy.types.Bone) and owner.pose:
            pose_bone = owner.pose.bones.get(item.name)
            if pose_bone:
                _dump(pose_bone, f"pose.bones[{_quote(item.name)}]", out, depth=0)
        if getattr(item, "type", "") == "NODES" and getattr(item, "node_group", None):
            _node_modifier_inputs(item, prefix, out)
    elif isinstance(owner, bpy.types.Object):
        _inspect_object(owner, set(sections), out)
    else:
        _inspect_datablock(owner, set(sections), out)
    if len(out) > MAX_INSPECT_LINES:
        out = out[:MAX_INSPECT_LINES] + [f"… cut at {MAX_INSPECT_LINES} lines: pass sections to see less at once"]
    return "\n".join(out)


# ─── inspect_nodes ───────────────────────────────────────────────────────────────────────────────

_NODE_BASE = set(bpy.types.Node.bl_rna.properties.keys()) | {"location_absolute", "warning_propagation"}
_defaults_cache = {}  # (tree type, node type) → {socket identifier: default}


def _node_owner(owner):
    """The tree an owner names — without making or switching on anything, as reading must not."""
    if not isinstance(owner, str) or not owner.strip():
        raise ToolError("owner names a node tree: \"material:Oak\", \"world\", \"compositor\", "
                        "\"node_group:Name\" or \"Object/modifiers/GeometryNodes\".")
    owner = owner.strip()
    scene = _scene()
    if owner == "world":
        if scene.world is None or scene.world.node_tree is None:
            raise ToolError("The scene's world has no nodes.")
        return scene.world.node_tree
    if owner == "compositor":
        tree = getattr(scene, "compositing_node_group", None)  # Blender 5.0+
        if tree is None:
            tree = getattr(scene, "node_tree", None)  # before 5.0
        if tree is None:
            raise ToolError("The compositor has no node tree yet.")
        return tree
    kind, _ = targets.split(owner)
    if kind in {"material", "world"}:
        block = targets.datablock(owner)
        if block.node_tree is None:
            raise ToolError(f"{owner} has no nodes. edit_nodes with create: \"shader\" makes them.")
        return block.node_tree
    return targets.node_tree(owner)


def _socket_defaults(tree, node):
    """What each input of a fresh node of this type holds, so only the inputs the user changed are listed."""
    key = (tree.bl_idname, node.bl_idname)
    if key in _defaults_cache:
        return _defaults_cache[key]
    found = {}
    if node.bl_idname.endswith("Group") or node.bl_idname in {"NodeGroupInput", "NodeGroupOutput", "NodeFrame",
                                                              "NodeReroute"}:
        _defaults_cache[key] = found
        return found
    scratch = None
    try:
        # A throwaway tree of the same kind, removed at once: Blender gives no other way to read a node
        # type's socket defaults.
        scratch = bpy.data.node_groups.new(".zetta-defaults", tree.bl_idname)
        fresh = scratch.nodes.new(node.bl_idname)
        for socket in fresh.inputs:
            if hasattr(socket, "default_value"):
                value = socket.default_value
                found[socket.identifier] = list(value) if hasattr(value, "__len__") and not isinstance(
                    value, str) else value
    except (RuntimeError, TypeError, ValueError):
        found = None
    finally:
        if scratch is not None:
            bpy.data.node_groups.remove(scratch)
    _defaults_cache[key] = found
    return found


def _same(a, b):
    try:
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            return abs(a - b) < 1e-6
        if hasattr(a, "__len__") and hasattr(b, "__len__") and not isinstance(a, str):
            return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
        return a == b
    except TypeError:
        return False


def _socket_name(node, socket, outputs):
    sockets = node.outputs if outputs else node.inputs
    same = [s for s in sockets if s.name == socket.name and getattr(s, "enabled", True)]
    if len(same) > 1:
        index = list(sockets).index(socket)
        return f"{node.name}.{index}"
    return f"{node.name}.{socket.name}"


@tool("inspect_nodes")
def inspect_nodes(args, ctx):
    owner = args.get("owner")
    tree = _node_owner(owner)
    nodes = list(tree.nodes)
    lines = [f"{owner} — {tree.bl_idname} \"{tree.name}\": {len(nodes)} nodes, {len(tree.links)} links."
             " Sockets are \"Node.Socket\" (or \"Node.<index>\" where names repeat) for edit_nodes."]
    if isinstance(tree, bpy.types.NodeTree) and getattr(tree, "users", 0) and tree.users > 1 and \
            tree in bpy.data.node_groups.values():
        lines.append(f"# node_group:{tree.name} is used in {tree.users} places: edits change all of them")
    frames = [n for n in nodes if n.bl_idname == "NodeFrame"]
    for node in nodes[:300]:
        if node.bl_idname == "NodeFrame":
            continue
        head = f"{node.name} [{node.bl_idname}] at ({node.location.x:.0f}, {node.location.y:.0f})"
        if node.label:
            head += f' label "{node.label}"'
        if node.parent:
            head += f" in frame {node.parent.name}"
        if node.mute:
            head += " (muted)"
        if tree.nodes.active == node:
            head += " (active)"
        lines.append(head)
        for prop in node.bl_rna.properties:
            key = prop.identifier
            if key in _NODE_BASE or key.startswith("bl_") or prop.type == "COLLECTION" or key in _NOISE:
                continue
            try:
                value = getattr(node, key)
            except AttributeError:
                continue
            if prop.type == "POINTER":
                if isinstance(value, bpy.types.ID) or (value is None and _points_to_id(prop)):
                    lines.append(f"  {key} = {_show(value)}")
                continue
            if prop.is_readonly:
                continue
            lines.append(f"  {key} = {_show(value)}")
        defaults = _socket_defaults(tree, node)
        group_defaults = {}
        if getattr(node, "node_tree", None) is not None:
            try:
                for item in node.node_tree.interface.items_tree:
                    if getattr(item, "item_type", "") == "SOCKET" and item.in_out == "INPUT" and \
                            hasattr(item, "default_value"):
                        group_defaults[item.identifier] = item.default_value
            except AttributeError:
                pass
        for index, socket in enumerate(node.inputs):
            if not getattr(socket, "enabled", True) or socket.is_linked or not hasattr(socket, "default_value"):
                continue
            value = socket.default_value
            if defaults is None and not group_defaults:
                pass
            else:
                base = group_defaults.get(socket.identifier, (defaults or {}).get(socket.identifier))
                if base is not None and _same(value, base):
                    continue
            lines.append(f"  in {_socket_name(node, socket, False)} = {_show(value)}")
    if len(nodes) > 300:
        lines.append(f"… {len(nodes) - 300} more nodes")
    if tree.links:
        lines.append("Links:")
        for link in list(tree.links)[:600]:
            muted = " (muted)" if getattr(link, "is_muted", False) else ""
            invalid = " (INVALID)" if not link.is_valid else ""
            lines.append(f"  {_socket_name(link.from_node, link.from_socket, True)} → "
                         f"{_socket_name(link.to_node, link.to_socket, False)}{muted}{invalid}")
    if frames:
        lines.append("Frames:")
        for frame in frames:
            inside = [n.name for n in nodes if n.parent == frame]
            lines.append(f"  {frame.name}" + (f' "{frame.label}"' if frame.label else "") +
                         f": {', '.join(inside) or 'empty'}")
    interface = getattr(tree, "interface", None)
    if interface is not None and tree in bpy.data.node_groups.values():
        items = [item for item in interface.items_tree]
        if items:
            lines.append("Interface:")
            for item in items:
                if getattr(item, "item_type", "") == "PANEL":
                    lines.append(f"  panel {item.name}")
                    continue
                text = f"  {item.in_out.lower()} {item.name} ({item.socket_type}, identifier {item.identifier})"
                if hasattr(item, "default_value"):
                    text += f" default {_show(item.default_value)}"
                if hasattr(item, "min_value"):
                    text += f", {_show(item.min_value)} to {_show(item.max_value)}"
                lines.append(text)
    return "\n".join(lines)


# ─── find ────────────────────────────────────────────────────────────────────────────────────────

_KIND_DATA = {"materials": ("materials", "material"), "images": ("images", "image"),
              "node_groups": ("node_groups", "node_group"), "actions": ("actions", "action"),
              "texts": ("texts", "text"), "collections": ("collections", "collection"),
              "worlds": ("worlds", "world"), "meshes": ("meshes", "mesh"), "cameras": ("cameras", "camera"),
              "lights": ("lights", "light")}


def _strip_kind(value, kind):
    if isinstance(value, str) and value.startswith(kind + ":"):
        return value[len(kind) + 1:]
    return value


@tool("find")
def find(args, ctx):
    kind = args.get("kind") or "objects"
    limit = _clamp(args.get("limit"), 1, 200, 50)
    name = (args.get("name") or "").lower()
    if kind != "objects":
        if kind not in _KIND_DATA:
            raise ToolError(f"kind is one of: objects, {', '.join(_KIND_DATA)}.")
        attr, prefix = _KIND_DATA[kind]
        found = [block for block in getattr(bpy.data, attr) if name in block.name.lower()]
        lines = []
        for block in found[:limit]:
            extra = f" ({block.users} users)" if hasattr(block, "users") else ""
            if kind == "node_groups":
                extra = f" ({block.bl_idname}, {block.users} users)"
            elif kind == "images":
                extra = f" ({block.size[0]}×{block.size[1]}, {block.source.lower()})"
            lines.append(f"{prefix}:{block.name}{extra}")
        return _found(lines, len(found), limit, kind)
    pool = list(_scene().objects)
    collection = args.get("collection")
    if collection:
        block = bpy.data.collections.get(_strip_kind(collection, "collection"))
        if block is None:
            raise targets._missing("collection", _strip_kind(collection, "collection"), bpy.data.collections.keys())
        members = set(block.all_objects)
        pool = [o for o in pool if o in members]
    material = args.get("material")
    if material:
        wanted = bpy.data.materials.get(_strip_kind(material, "material"))
        if wanted is None:
            raise targets._missing("material", _strip_kind(material, "material"), bpy.data.materials.keys())
        pool = [o for o in pool if any(slot.material == wanted for slot in o.material_slots)]
    kind_filter = (args.get("type") or "").upper()
    if kind_filter:
        if kind_filter == "GPENCIL":  # Blender 4.3 renamed the Grease Pencil object type
            pool = [o for o in pool if o.type in {"GPENCIL", "GREASEPENCIL"}]
        else:
            pool = [o for o in pool if o.type == kind_filter]
    modifier = (args.get("modifier") or "").upper()
    if modifier:
        pool = [o for o in pool if any(m.type == modifier for m in o.modifiers)]
    if name:
        pool = [o for o in pool if name in o.name.lower()]
    pool.sort(key=lambda o: o.name)
    lines = []
    for obj in pool[:limit]:
        where = ", ".join(c.name for c in obj.users_collection)
        lines.append(f"{obj.name} ({obj.type}, collection {where or 'none'})")
    return _found(lines, len(pool), limit, "objects")


def _found(lines, total, limit, what):
    if not lines:
        return f"No {what.replace('_', ' ')} match."
    head = f"{total} {what.replace('_', ' ')} match" + (f" (first {limit}):" if total > limit else ":")
    return "\n".join([head] + lines)


# ─── get_selection ───────────────────────────────────────────────────────────────────────────────


def _windows():
    # Background Blender still loads the startup file's windows and screens, but nothing draws them.
    if bpy.app.background:
        return []
    manager = bpy.context.window_manager
    return list(manager.windows) if manager else []


@tool("get_selection")
def get_selection(args, ctx):
    import bmesh

    layer = _view_layer()
    try:
        mode = bpy.context.mode
    except AttributeError:
        mode = "OBJECT"
    active = layer.objects.active
    selected = [o for o in layer.objects if o.select_get(view_layer=layer)]
    lines = [f"Mode: {mode}", f"Active: {active.name + ' (' + active.type + ')' if active else 'none'}"]
    names = ", ".join(f"{o.name} ({o.type})" for o in selected[:100])
    lines.append(f"Selected ({len(selected)}): {names or 'nothing'}" + (" …" if len(selected) > 100 else ""))
    for obj in [o for o in layer.objects if o.mode == "EDIT" and o.type == "MESH"]:
        bm = bmesh.from_edit_mesh(obj.data)
        verts = sum(1 for v in bm.verts if v.select)
        edges = sum(1 for e in bm.edges if e.select)
        faces = sum(1 for f in bm.faces if f.select)
        lines.append(f"Edit Mode {obj.name}: {verts} verts, {edges} edges, {faces} faces selected "
                     f"(of {len(bm.verts)}, {len(bm.edges)}, {len(bm.faces)})")
    if active is not None and active.mode == "POSE" and active.pose:
        bones = [b.name for b in active.pose.bones if b.bone.select] if hasattr(bpy.types.Bone, "select") else []
        lines.append(f"Pose Mode bones selected: {', '.join(bones[:60]) or 'none'}")
    if active is not None and active.material_slots:
        slot = active.material_slots[active.active_material_index] if active.active_material_index < len(
            active.material_slots) else None
        name = f"material:{slot.material.name}" if slot and slot.material else "empty"
        lines.append(f"Active material slot: {active.active_material_index} ({name})")
    lines.append(f"3D cursor: {values.show(_scene().cursor.location)}")
    windows = _windows()
    if not windows:
        lines.append("No 3D View is open (Blender runs in the background).")
        return "\n".join(lines)
    for window in windows:
        for area in window.screen.areas:
            if area.type != "NODE_EDITOR":
                continue
            space = area.spaces.active
            tree = getattr(space, "edit_tree", None)
            if tree is None:
                continue
            node = tree.nodes.active
            owner = getattr(space, "id", None)
            where = targets.name_of(owner) if owner is not None else tree.name
            picked = [n.name for n in tree.nodes if n.select]
            lines.append(f"Node editor ({space.tree_type}) on {where}: active node "
                         f"{node.name if node else 'none'}; selected {', '.join(picked[:30]) or 'none'}")
    if not any(a.type == "VIEW_3D" for w in windows for a in w.screen.areas):
        lines.append("No 3D View is open.")
    return "\n".join(lines)


# ─── read_text / search_text ─────────────────────────────────────────────────────────────────────

MAX_FILE_BYTES = 8 * 1024 * 1024


def _read_file(full, shown):
    try:
        size = os.path.getsize(full)
    except OSError as error:
        raise ToolError(f"Cannot read {shown}: {error.strerror}.") from None
    if size > MAX_FILE_BYTES:
        raise ToolError(f"{shown} is {size / 1024 ** 2:.0f} MB — too big to read as text.")
    with open(full, "rb") as handle:
        raw = handle.read()
    if b"\x00" in raw[:8192]:
        raise ToolError(f"{shown} is a binary file, not text.")
    return raw.decode("utf-8", errors="replace")


def _text_source(target):
    if not isinstance(target, str) or not target.strip():
        raise ToolError("target is \"text:Name\" or a \"//\" path.")
    target = target.strip()
    if target.startswith("text:"):
        name = target[5:]
        text = bpy.data.texts.get(name)
        if text is None:
            raise targets._missing("text", name, bpy.data.texts.keys())
        return target, text.as_string()
    full = targets.file_path(target, must_exist=True)
    if os.path.isdir(full):
        raise ToolError(f"{target} is a folder. Use search_text with in: \"files\" to look inside it.")
    return target, _read_file(full, target)


@tool("read_text")
def read_text(args, ctx):
    shown, content = _text_source(args.get("target"))
    lines = content.splitlines()
    total = len(lines)
    start = _clamp(args.get("startLine"), 1, 10 ** 9, 1)
    end = _clamp(args.get("endLine"), 1, 10 ** 9, start + 1999)
    if end < start:
        raise ToolError("endLine comes before startLine.")
    end = min(end, start + 1999, total)
    if total == 0:
        return f"{shown} is empty."
    if start > total:
        raise ToolError(f"{shown} has {total} lines; startLine {start} is past the end.")
    width = len(str(end))
    out = [f"{shown} — {total} lines, showing {start}–{end}:"]
    for number in range(start, end + 1):
        line = lines[number - 1]
        if len(line) > 2000:
            line = line[:2000] + " …"
        out.append(f"{number:>{width}}  {line}")
    if end < total:
        out.append(f"… {total - end} more lines: read again with startLine={end + 1}")
    return "\n".join(out)


def _files(pattern):
    """The text files a "//" glob names, all inside the .blend's folder (hidden folders skipped)."""
    folder = targets.blend_folder()
    if folder is None:
        raise ToolError("search_text in files needs the file saved first — ask the user to save it.")
    pattern = pattern or "//**/*"
    if not pattern.startswith("//"):
        raise ToolError(f"glob is relative to the .blend, starting \"//\" — not {pattern}.")
    # The part before the first wildcard must itself stay inside the folder ("//../" is refused there).
    fixed = re.split(r"[*?\[]", pattern, maxsplit=1)[0]
    targets.file_path(fixed.rsplit("/", 1)[0] + "/" if "/" in fixed[2:] else "//")
    root = os.path.normpath(folder)
    any_type = not pattern.endswith(("*", "/"))  # "//**/*.py" names its type; "//**/*" means text files
    found = []
    for path in glob.iglob(os.path.join(root, pattern[2:]), recursive=True):
        path = os.path.normpath(path)
        if os.path.commonpath([path, root]) != root or not os.path.isfile(path):
            continue
        if any(part.startswith(".") for part in os.path.relpath(path, root).split(os.sep)):
            continue
        if not any_type and os.path.splitext(path)[1].lower() not in TEXT_EXTENSIONS:
            continue
        found.append(path)
        if len(found) >= 2000:
            break
    return root, sorted(found)


@tool("search_text")
def search_text(args, ctx):
    pattern = args.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        raise ToolError("pattern is a regular expression.")
    try:
        regex = re.compile(pattern)
    except re.error as error:
        raise ToolError(f"pattern is not a valid regular expression: {error}.") from None
    where = args.get("in") or "texts"
    limit = _clamp(args.get("limit"), 1, 200, 50)
    hits = []
    searched = 0
    if where == "texts":
        if args.get("glob"):
            raise ToolError("glob is for in: \"files\"; Text data-blocks are searched by pattern alone.")
        for text in bpy.data.texts:
            searched += 1
            for number, line in enumerate(text.as_string().splitlines(), 1):
                if regex.search(line):
                    hits.append(f"text:{text.name}:{number}: {line.strip()[:300]}")
                    if len(hits) > limit:
                        break
            if len(hits) > limit:
                break
        what = "Text data-blocks"
    elif where == "files":
        root, files = _files(args.get("glob"))
        for path in files:
            try:
                if os.path.getsize(path) > MAX_FILE_BYTES:
                    continue
                with open(path, "rb") as handle:
                    raw = handle.read()
            except OSError:
                continue
            if b"\x00" in raw[:8192]:
                continue
            searched += 1
            shown = "//" + os.path.relpath(path, root).replace(os.sep, "/")
            for number, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), 1):
                if regex.search(line):
                    hits.append(f"{shown}:{number}: {line.strip()[:300]}")
                    if len(hits) > limit:
                        break
            if len(hits) > limit:
                break
        what = "files"
    else:
        raise ToolError("in is \"texts\" or \"files\".")
    if not hits:
        return f"No match for {pattern!r} in {searched} {what}."
    more = len(hits) > limit
    hits = hits[:limit]
    return "\n".join(hits + (["… more matches: narrow the pattern or raise limit (max 200)"] if more else []))


# ─── recent_errors ───────────────────────────────────────────────────────────────────────────────


@tool("recent_errors")
def recent_errors(args, ctx):
    limit = _clamp(args.get("limit"), 1, 100, 20)
    found = errors.recent(limit)
    if not found:
        return "No errors or warnings since Blender started."
    now = time.time()
    lines = []
    for entry in found:
        ago = now - entry["at"]
        when = f"{ago:.0f}s ago" if ago < 120 else f"{ago / 60:.0f} min ago" if ago < 7200 else \
            f"{ago / 3600:.0f} h ago"
        lines.append(f"[{entry['source']}, {when}] {entry['message']}")
    return f"{len(found)} newest first:\n" + "\n".join(lines)


# ─── api_lookup ──────────────────────────────────────────────────────────────────────────────────

_index = None  # [(kind, key lowercased, display name, payload)] — built on first use, once per session


def _build_index():
    entries = []
    for name in dir(bpy.types):
        cls = getattr(bpy.types, name, None)
        rna = getattr(cls, "bl_rna", None)
        if rna is None:
            continue
        description = getattr(rna, "description", "") or ""
        entries.append(("type", name.lower(), name, (cls, description.lower(), (rna.name or "").lower())))
        base = getattr(rna, "base", None)
        inherited = set(base.properties.keys()) if base is not None else set()
        for prop in rna.properties:
            key = prop.identifier
            if key == "rna_type" or key in inherited:
                continue
            words = f"{prop.name} {prop.description}".lower()
            entries.append(("property", f"{name}.{key}".lower(), f"{name}.{key}", (prop, words, key.lower())))
    for module_name in dir(bpy.ops):
        if module_name.startswith("_"):
            continue
        module = getattr(bpy.ops, module_name)
        for op_name in dir(module):
            if op_name.startswith("_"):
                continue
            ident = f"{module_name}.{op_name}"
            try:
                op = getattr(module, op_name)
                rna = op.get_rna_type()
                words = f"{rna.name} {rna.description}".lower()
            except (AttributeError, KeyError, RuntimeError):
                continue
            entries.append(("operator", ident.lower(), ident, (op, words, op_name.lower())))
    return entries


def _score(entry, query, tokens, compact):
    kind, key, display, payload = entry
    words = payload[1]
    short = payload[2]
    bare = key.replace("_", "")
    if key == query or short == query or bare == compact:
        return 100
    if key.endswith("." + query):
        return 90
    if compact and compact in bare:
        return 70 - min(len(bare) - len(compact), 20) / 2
    if all(t in key for t in tokens):
        return 50 - min(len(key), 60) / 6
    if all(t in key or t in words for t in tokens):
        hits = sum(1 for t in tokens if t in key)
        return 20 + hits * 5
    return 0


def _range(prop):
    soft = ""
    try:
        low, high = prop.hard_min, prop.hard_max
        text = f"{values._num(low)} to {values._num(high)}"
        if (prop.soft_min, prop.soft_max) != (low, high):
            soft = f" (soft {values._num(prop.soft_min)} to {values._num(prop.soft_max)})"
        return text + soft
    except AttributeError:
        return ""


def _describe_property(prop, owner=None):
    kind = prop.type
    parts = [kind]
    subtype = getattr(prop, "subtype", "NONE")
    if subtype and subtype != "NONE":
        parts.append(subtype.lower())
    length = getattr(prop, "array_length", 0) or 0
    if length:
        parts.append(f"[{length}]")
    if kind in {"INT", "FLOAT"}:
        parts.append(_range(prop))
        try:
            default = list(prop.default_array) if length else prop.default
            parts.append(f"default {_show(default)}")
        except AttributeError:
            pass
    elif kind == "ENUM":
        items = [item.identifier for item in prop.enum_items]
        if items:
            parts.append(("flags " if prop.is_enum_flag else "") + ", ".join(items[:30]) +
                         (f" … {len(items)} in all" if len(items) > 30 else ""))
        else:
            parts.append("items depend on context")
    elif kind == "BOOLEAN":
        try:
            parts.append(f"default {prop.default}")
        except AttributeError:
            pass
    elif kind in {"POINTER", "COLLECTION"}:
        fixed = getattr(prop, "fixed_type", None)
        if fixed is not None:
            parts.append(f"of {fixed.identifier}")
    if prop.is_readonly:
        parts.append("read-only")
    text = ", ".join(p for p in parts if p)
    description = (prop.description or prop.name or "").strip()
    return f"{text}" + (f" — {description}" if description else "")


def _describe(entry):
    kind, _, display, payload = entry
    if kind == "type":
        cls, _, _ = payload
        rna = cls.bl_rna
        base = rna.base.identifier if getattr(rna, "base", None) is not None else ""
        description = rna.description or rna.name
        props = [p.identifier for p in rna.properties if p.identifier != "rna_type"]
        own = props[:50]
        line = f"type bpy.types.{display}" + (f" ({base})" if base else "") + f" — {description}"
        if own:
            line += f"\n    properties: {', '.join(own)}" + (f" … {len(props)} in all" if len(props) > 50 else "")
        return line
    if kind == "property":
        prop = payload[0]
        return f"property {display}: {_describe_property(prop)}"
    op = payload[0]
    try:
        rna = op.get_rna_type()
    except (AttributeError, RuntimeError):
        return f"operator bpy.ops.{display}()"
    arguments = []
    for prop in rna.properties:
        if prop.identifier == "rna_type":
            continue
        arguments.append(f"{prop.identifier}: {_describe_property(prop).split(' — ')[0]}")
    description = rna.description or rna.name
    args_text = ("\n      " + "\n      ".join(arguments[:30])) if arguments else " (no arguments)"
    return f"operator bpy.ops.{display}() — {description}{args_text}"


@tool("api_lookup")
def api_lookup(args, ctx):
    global _index
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ToolError("query is the words or dotted name to look for, e.g. \"bevel width\".")
    kind = args.get("kind") or "any"
    if kind not in {"type", "property", "operator", "any"}:
        raise ToolError("kind is type, property, operator or any.")
    limit = _clamp(args.get("limit"), 1, 50, 20)
    if _index is None:
        _index = _build_index()
    query = query.strip()
    for head in ("bpy.types.", "bpy.ops."):
        if query.startswith(head):
            query = query[len(head):]
    query = query.rstrip("()").lower()
    tokens = [t for t in re.split(r"[\s.]+", query) if t]
    compact = re.sub(r"[\s_]+", "", query)
    scored = []
    for entry in _index:
        if kind != "any" and entry[0] != kind:
            continue
        score = _score(entry, query, tokens, compact)
        if score > 0:
            scored.append((score, entry))
    if not scored:
        return (f"Nothing in Blender {bpy.app.version_string}'s API matches {query!r}. Try fewer or other words "
                "(\"bevel\", \"subdivision\"), or the type's name.")
    scored.sort(key=lambda pair: (-pair[0], len(pair[1][2])))
    shown = [_describe(entry) for _, entry in scored[:limit]]
    head = f"Blender {bpy.app.version_string} — {len(scored)} matches" + (f", best {limit}:" if len(scored) > limit
                                                                          else ":")
    return "\n".join([head] + shown)
