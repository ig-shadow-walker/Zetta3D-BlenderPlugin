"""The scene tools: add, change, organise, animate and delete things in the open file.

create_object, modify_object, set_properties, add_modifier, add_constraint, keyframes, collection_op, select,
set_mode, mesh_op, delete, purge_unused, undo (BlenderAgentProtocol.md › Client tools).

WHY THE DATA API FIRST: the agent's calls arrive from a timer, not from a click in a 3D View, so an operator
may find no window, no area and the wrong selection — and background Blender (the tests) has no window at all.
Wherever the data API does the job (transforms, shading, normals, merging, origins) it is used directly; the
operators that have no data equivalent (applying a modifier, joining, separating, UV unwrapping) run inside a
context override built here, with a 3D View when one is open.

WHY EVERYTHING IS PUT BACK: an operator acts on the selection and the active object, and most need Object
Mode. A tool that changes those to do its work puts the user's mode, selection and active object back
afterwards (`kept`), so the user finds Blender as they left it. Only select and set_mode change them for good.

Rotations from the agent are in degrees; Blender stores radians. Names are unique per kind, so a result always
gives the name Blender actually used ("Chair.001").
"""

import math
from contextlib import contextmanager

import bmesh
import bpy
from mathutils import Euler, Matrix, Vector

from .. import changes, targets, values
from . import Result, ToolError, tool

# ─── Small helpers ───────────────────────────────────────────────────────────────────────────────


def listing(names, limit=12):
    """"A, B and C" — the first few, then how many more."""
    names = [str(n) for n in names]
    if len(names) > limit:
        return ", ".join(names[:limit]) + f" and {len(names) - limit} more"
    if len(names) > 1:
        return ", ".join(names[:-1]) + " and " + names[-1]
    return names[0] if names else "nothing"


def _number(value):
    return f"{value:.4g}" if isinstance(value, float) else str(value)


def _vec(values_):
    return "[" + ", ".join(_number(round(v, 4)) for v in values_) + "]"


def alive(block):
    """False for a data-block Python still holds but Blender has removed (joined away, deleted)."""
    try:
        block.name  # noqa: B018 — raises ReferenceError once it is gone
    except ReferenceError:
        return False
    return True


def _clean(error):
    """An operator's RuntimeError as one line ("Error: Modifier is disabled\n" → "Modifier is disabled")."""
    text = " ".join(str(error).split())
    for prefix in ("Error:", "Warning:"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    return text.rstrip(".")


def collection_named(text, create=False):
    """A collection from "collection:Name" (or a bare name). create makes it under the scene's collection."""
    if not isinstance(text, str) or not text.strip():
        raise ToolError('A collection is written "collection:Name".')
    name = text.strip()
    if name.startswith("collection:"):
        name = name[len("collection:"):]
    scene = bpy.context.scene
    if name in {scene.collection.name, "Scene Collection"}:
        return scene.collection
    found = bpy.data.collections.get(name)
    if found is None:
        if not create:
            return targets.datablock(f"collection:{name}")  # raises, naming the closest ones
        found = bpy.data.collections.new(name)
        scene.collection.children.link(found)
    return found


def layer_collection(collection, layer=None):
    """The view layer's entry for a collection (where hide and exclude live), or None."""
    layer = layer or bpy.context.view_layer.layer_collection
    if layer.collection == collection:
        return layer
    for child in layer.children:
        found = layer_collection(collection, child)
        if found is not None:
            return found
    return None


def move_to_collection(obj, collection):
    """Links obj into collection and out of every other one."""
    if obj.name not in collection.objects:
        collection.objects.link(obj)
    for other in list(obj.users_collection):
        if other != collection:
            other.objects.unlink(obj)


def put_material(obj, material, slot=None):
    """Puts material in a slot of obj — the given one, else the first — adding a slot when there is none
    there yet. Returns the slot index used."""
    data = getattr(obj, "data", None)
    if data is None or not hasattr(data, "materials"):
        raise ToolError(f"{obj.name} is a {obj.type.lower()}, which has no materials.")
    slots = obj.material_slots
    if slot is None:
        slot = 0
    if slot >= len(slots):
        data.materials.append(material)
        return len(obj.material_slots) - 1
    # Through the slot, not data.materials, so a slot linked to the object (not its mesh) is respected.
    slots[slot].material = material
    return slot


def set_rotation(obj, degrees_xyz):
    """Sets an object's rotation from Euler degrees, whatever rotation mode it uses."""
    euler = Euler(values.degrees(degrees_xyz), "XYZ")
    mode = obj.rotation_mode
    if mode == "QUATERNION":
        obj.rotation_quaternion = euler.to_quaternion()
    elif mode == "AXIS_ANGLE":
        axis, angle = euler.to_quaternion().to_axis_angle()
        obj.rotation_axis_angle = (angle, *axis)
    else:
        obj.rotation_euler = Euler(values.degrees(degrees_xyz), mode)


def rotation_degrees(obj):
    if obj.rotation_mode in {"QUATERNION", "AXIS_ANGLE"}:
        euler = obj.matrix_basis.to_euler("XYZ")
    else:
        euler = obj.rotation_euler
    return [math.degrees(a) for a in euler]


# ─── Context: a 3D View when there is one, the right objects, and putting things back ────────────


def _view3d():
    """(window, area, region) of an open 3D View, or None — background Blender has none, and the agent's
    calls come from a timer, which has no area of its own."""
    wm = bpy.context.window_manager
    for window in getattr(wm, "windows", []):
        screen = window.screen
        if screen is None:
            continue
        for area in screen.areas:
            if area.type == "VIEW_3D":
                for region in area.regions:
                    if region.type == "WINDOW":
                        return window, area, region
    return None


def override(active=None, selected=None):
    """A temp_override with a 3D View (when open) and the given active and selected objects."""
    kwargs = {}
    found = _view3d()
    if found:
        window, area, region = found
        kwargs.update(window=window, screen=window.screen, area=area, region=region)
    if active is not None:
        kwargs.update(object=active, active_object=active)
    if selected is not None:
        kwargs.update(selected_objects=list(selected), selected_editable_objects=list(selected))
    return bpy.context.temp_override(**kwargs)


def operator(fn, what, active=None, selected=None, **props):
    """Runs an operator in a proper context; its failure becomes the tool's error line."""
    with override(active, selected):
        try:
            result = fn(**props)
        except RuntimeError as error:
            raise ToolError(f"{what}: {_clean(error)}.") from None
    if "FINISHED" not in result:
        raise ToolError(f"{what} did nothing (Blender cancelled it).")
    return result


def mode_set(obj, mode):
    """Puts obj (made active) into mode."""
    layer = bpy.context.view_layer
    if layer.objects.active != obj:
        layer.objects.active = obj
    if obj.mode == mode:
        return
    operator(bpy.ops.object.mode_set, f"Switching {obj.name} to {mode.replace('_', ' ').title()} Mode",
             active=obj, mode=mode)


def object_mode():
    """Leaves Edit Mode (or any other) so the data API sees the real mesh — Edit Mode keeps its own copy and
    would overwrite our change when it exits."""
    active = bpy.context.view_layer.objects.active
    if active is not None and active.mode != "OBJECT":
        mode_set(active, "OBJECT")


def select_only(objs, active=None):
    layer = bpy.context.view_layer
    for each in layer.objects:
        if each.select_get():
            each.select_set(False)
    for each in objs:
        try:
            each.select_set(True)
        except RuntimeError:
            raise ToolError(f"{each.name} is not in the current view layer, so it cannot be selected.") from None
    layer.objects.active = active if active is not None else (objs[0] if objs else None)


@contextmanager
def kept():
    """with kept(): the user's mode, selection and active object come back afterwards — minus anything the
    tool removed (a join, a delete)."""
    layer = bpy.context.view_layer
    active = layer.objects.active
    selected = [o for o in layer.objects if o.select_get()]
    mode = active.mode if active is not None else "OBJECT"
    object_mode()
    try:
        yield
    finally:
        layer = bpy.context.view_layer
        try:
            object_mode()
        except ToolError:
            pass
        for each in layer.objects:
            if each.select_get():
                each.select_set(False)
        for each in selected:
            if alive(each) and layer.objects.get(each.name) == each:
                each.select_set(True)
        layer.objects.active = active if active is not None and alive(active) else None
        if mode != "OBJECT" and active is not None and alive(active):
            try:
                mode_set(active, mode)
            except ToolError:
                pass  # the object can no longer take that mode (converted, emptied): Object Mode it is


# ─── create_object ───────────────────────────────────────────────────────────────────────────────

# kind → (operator, its size arguments for a size s, or None for the default)
_PRIMITIVES = {
    "cube": ("mesh", "primitive_cube_add", lambda s: {"size": s or 2.0}),
    "uv_sphere": ("mesh", "primitive_uv_sphere_add", lambda s: {"radius": s or 1.0}),
    "ico_sphere": ("mesh", "primitive_ico_sphere_add", lambda s: {"radius": s or 1.0, "subdivisions": 2}),
    "cylinder": ("mesh", "primitive_cylinder_add", lambda s: {"radius": s or 1.0, "depth": 2 * (s or 1.0)}),
    "cone": ("mesh", "primitive_cone_add", lambda s: {"radius1": s or 1.0, "depth": 2 * (s or 1.0)}),
    "torus": ("mesh", "primitive_torus_add",
              lambda s: {"major_radius": s or 1.0, "minor_radius": 0.25 * (s or 1.0)}),
    "plane": ("mesh", "primitive_plane_add", lambda s: {"size": s or 2.0}),
    "grid": ("mesh", "primitive_grid_add", lambda s: {"size": s or 2.0}),
    "circle": ("mesh", "primitive_circle_add", lambda s: {"radius": s or 1.0}),
    "monkey": ("mesh", "primitive_monkey_add", lambda s: {"size": s or 2.0}),
    "bezier": ("curve", "primitive_bezier_curve_add", lambda s: {"radius": s or 1.0}),
    "armature": ("object", "armature_add", lambda s: {"radius": s or 1.0}),
    "text": ("object", "text_add", lambda s: {"radius": s or 1.0}),
}
_KINDS = sorted(list(_PRIMITIVES) + ["empty", "camera", "light"])


def _new_from_operator(kind, size):
    group, name, sizes = _PRIMITIVES[kind]
    before = set(bpy.data.objects)
    # Made at the origin, aligned to the world: the tool sets the transform itself afterwards, the same way for
    # every kind (and relative to a parent when there is one).
    operator(getattr(getattr(bpy.ops, group), name), f"Adding a {kind}", align="WORLD",
             location=(0, 0, 0), rotation=(0, 0, 0), **sizes(size))
    made = [o for o in bpy.data.objects if o not in before]
    if not made:
        raise ToolError(f"Blender did not add the {kind}.")
    return made[0]


def _new_from_data(kind, name, light, size, collection):
    if kind == "empty":
        obj = bpy.data.objects.new(name, None)
        obj.empty_display_type = "PLAIN_AXES"
        obj.empty_display_size = size or 1.0
    elif kind == "camera":
        obj = bpy.data.objects.new(name, bpy.data.cameras.new(name))
    else:
        data = bpy.data.lights.new(name, light or "POINT")
        if size:
            if data.type == "AREA":
                data.size = size
            elif hasattr(data, "shadow_soft_size"):
                data.shadow_soft_size = size
        obj = bpy.data.objects.new(name, data)
    collection.objects.link(obj)
    return obj


@tool("create_object", undoable=True)
def create_object(args, ctx):
    name = args.get("name")
    kind = args.get("kind")
    if not isinstance(name, str) or not name.strip():
        raise ToolError("name is the new object's name.")
    if kind not in _KINDS:
        raise ToolError(f"kind is one of: {', '.join(_KINDS)}.")
    light = args.get("light")
    if light is not None and light not in {"POINT", "SUN", "SPOT", "AREA"}:
        raise ToolError("light is POINT, SUN, SPOT or AREA.")
    size = args.get("size")
    if size is not None and (not isinstance(size, (int, float)) or isinstance(size, bool) or size <= 0):
        raise ToolError("size is a number above 0 (metres).")

    # Everything that can be wrong is checked before anything is made.
    parent = targets.obj(args["parent"]) if args.get("parent") else None
    material = _material(args["material"]) if args.get("material") else None
    location = values.vector(args["location"], "location") if args.get("location") is not None else None
    if args.get("rotation") is not None:
        values.degrees(args["rotation"])
    scale = values.vector(args["scale"], "scale") if args.get("scale") is not None else None
    modifiers = args.get("modifiers") or []
    if not isinstance(modifiers, list):
        raise ToolError("modifiers is a list of {type, name?, properties?}.")
    for each in modifiers:
        if not isinstance(each, dict):
            raise ToolError("modifiers is a list of {type, name?, properties?}.")
        _modifier_type(each.get("type"))
    if args.get("collection") is not None and (not isinstance(args["collection"], str)
                                               or not args["collection"].strip()):
        raise ToolError('collection is "collection:Name" (made if missing).')

    with kept():
        if kind in _PRIMITIVES:
            obj = _new_from_operator(kind, float(size) if size else None)
            obj.name = name.strip()
            if obj.data is not None and obj.data.users == 1:
                obj.data.name = obj.name
        else:
            obj = _new_from_data(kind, name.strip(), light, float(size) if size else None,
                                 bpy.context.view_layer.active_layer_collection.collection)
    try:
        if args.get("collection"):
            move_to_collection(obj, collection_named(args["collection"], create=True))
        if parent is not None:
            obj.parent = parent
        obj.location = location if location is not None else (
            bpy.context.scene.cursor.location if parent is None else (0, 0, 0))
        if args.get("rotation") is not None:
            set_rotation(obj, args["rotation"])
        if scale is not None:
            obj.scale = scale
        added = [_add_modifier(obj, each.get("type"), each.get("name"), each.get("properties"))
                 for each in modifiers]
        if material is not None:
            put_material(obj, material)
    except ToolError:
        # Nothing half-made is left behind: the object goes again, and the call changed nothing.
        data = obj.data
        bpy.data.objects.remove(obj, do_unlink=True)
        if data is not None and data.users == 0:
            _remove_datablock(data)
        raise

    where = obj.users_collection[0].name if obj.users_collection else "no collection"
    lines = [f'Created "{obj.name}" ({obj.type}, {kind}) in collection "{where}" at {_vec(obj.location)}'
             + (f", parented to {parent.name}" if parent else "") + "."]
    if obj.name != name.strip():
        lines.append(f'"{name.strip()}" was taken, so Blender named it "{obj.name}".')
    if added:
        lines.append("Modifiers: " + ", ".join(f"{m.name} ({m.type})" for m in added) + ".")
    if material is not None:
        lines.append(f"Material: {material.name}.")
    return Result("\n".join(lines), label=f"Added {obj.name}")


def _material(text):
    if not isinstance(text, str) or not text.strip():
        raise ToolError('material is "material:Name".')
    text = text.strip()
    if not text.startswith("material:") and text in bpy.data.materials:
        return bpy.data.materials[text]
    if not text.startswith("material:"):
        text = f"material:{text}"
    return targets.datablock(text)


def _remove_datablock(block):
    for attr in targets.KINDS.values():
        data = getattr(bpy.data, attr, None)
        if data is not None and data.get(block.name) == block:
            data.remove(block)
            return


# ─── modify_object ───────────────────────────────────────────────────────────────────────────────


@tool("modify_object", undoable=True)
def modify_object(args, ctx):
    obj = targets.obj(args.get("target"))
    new_parent = None
    if "parent" in args and args["parent"] is not None:
        new_parent = targets.obj(args["parent"])
        walker = new_parent
        while walker is not None:
            if walker == obj:
                raise ToolError(f"{new_parent.name} is {obj.name} or one of its children, so it cannot be its parent.")
            walker = walker.parent
    collection = collection_named(args["collection"]) if args.get("collection") else None
    for key in ("location", "scale"):
        if args.get(key) is not None:
            values.vector(args[key], key)
    if args.get("rotation") is not None:
        values.degrees(args["rotation"])
    for key in ("hide_viewport", "hide_render"):
        if key in args and not isinstance(args[key], bool):
            raise ToolError(f"{key} is true or false.")
    if args.get("display_type") is not None:
        values.convert(obj, "display_type", args["display_type"])
    if args.get("name") is not None and (not isinstance(args["name"], str) or not args["name"].strip()):
        raise ToolError("name is the object's new name.")

    done = []
    old_name = obj.name
    if args.get("name"):
        obj.name = args["name"].strip()
        done.append(f'renamed to "{obj.name}"' + (f' ("{args["name"].strip()}" was taken)'
                                                   if obj.name != args["name"].strip() else ""))
    if "parent" in args:
        keep = args.get("keepTransform", True) is not False
        world = obj.matrix_world.copy()
        obj.parent = new_parent
        if keep:
            obj.matrix_world = world  # the same place in the world, under the new parent
        else:
            obj.matrix_parent_inverse = Matrix.Identity(4)
        done.append(f"parented to {new_parent.name}" if new_parent else "parent cleared")
    if args.get("location") is not None:
        obj.location = values.vector(args["location"], "location")
        done.append(f"location {_vec(obj.location)}")
    if args.get("rotation") is not None:
        set_rotation(obj, args["rotation"])
        done.append(f"rotation {_vec(rotation_degrees(obj))}°")
    if args.get("scale") is not None:
        obj.scale = values.vector(args["scale"], "scale")
        done.append(f"scale {_vec(obj.scale)}")
    if collection is not None:
        move_to_collection(obj, collection)
        done.append(f'moved to collection "{collection.name}"')
    if "hide_viewport" in args:
        # Hidden the way H hides (the eye, per view layer); shown again from either kind of hiding.
        obj.hide_set(args["hide_viewport"])
        if not args["hide_viewport"]:
            obj.hide_viewport = False
        done.append("hidden in the viewport" if args["hide_viewport"] else "shown in the viewport")
    if "hide_render" in args:
        obj.hide_render = args["hide_render"]
        done.append("hidden in renders" if args["hide_render"] else "shown in renders")
    if args.get("display_type") is not None:
        values.assign(obj, "display_type", args["display_type"])
        done.append(f"displayed as {obj.display_type}")
    if not done:
        raise ToolError("Nothing to change: give name, location, rotation, scale, parent, collection, "
                        "hide_viewport, hide_render or display_type.")
    return Result(f"{old_name}: " + "; ".join(done) + ".", label=f"Changed {obj.name}")


# ─── set_properties ──────────────────────────────────────────────────────────────────────────────


def _owner(target):
    """The struct a target names: an object, a data-block, or a part ("Chair/modifiers/Bevel")."""
    owner, item = targets.part(target)
    return item if item is not None else owner


def _snapshot(owner, path):
    """A way to put one property back as it was, or None when the path does not resolve (assign then fails
    with the reason)."""
    try:
        head, attr, index = values._split_path(path)  # the same split assign() makes
        container = owner.path_resolve(head, False) if head else owner
    except (ValueError, AttributeError, TypeError):
        return None
    if attr.startswith("["):
        key = attr[2:-2]
        had = key in container.keys() if hasattr(container, "keys") else False
        old = container.get(key) if had else None
        old = old.to_list() if hasattr(old, "to_list") else old.to_dict() if hasattr(old, "to_dict") else old

        def restore():
            if had:
                container[key] = old
            elif key in container.keys():
                del container[key]
        return restore
    try:
        current = getattr(container, attr)
    except AttributeError:
        return None
    if index is not None:
        try:
            old = current[index]
        except (IndexError, TypeError):
            return None

        def restore():
            getattr(container, attr)[index] = old
        return restore
    if isinstance(current, (set, frozenset)):
        old = set(current)
    elif isinstance(current, (str, bpy.types.ID, bpy.types.bpy_struct)) or current is None:
        old = current
    else:
        try:
            old = tuple(current)
        except TypeError:
            old = current

    def restore():
        setattr(container, attr, old)
    return restore


@tool("set_properties", undoable=True)
def set_properties(args, ctx):
    """All or nothing: every path is tried, and if any is refused the ones already set are put back, so the
    model can fix the call and send it again without wondering which half landed."""
    owner = _owner(args.get("target"))
    props = args.get("properties")
    if not isinstance(props, dict) or not props:
        raise ToolError('properties is {"<data path>": value}, 1 to 40 of them.')
    if len(props) > 40:
        raise ToolError(f"At most 40 properties per call; this one has {len(props)}.")
    undo = []
    shown = []
    refused = []
    for path, value in props.items():
        restore = _snapshot(owner, path)
        try:
            now = values.assign(owner, path, value)
        except ToolError as error:
            refused.append(f"{path}: {error}")
            continue
        if restore is not None:
            undo.append(restore)
        shown.append(f"{path} = {values.show(now)}")
    if refused:
        for restore in reversed(undo):
            try:
                restore()
            except Exception:  # noqa: BLE001 — putting back is best effort; the refusal is what matters
                pass
        raise ToolError(f"Nothing was changed — {len(refused)} of {len(props)} refused: " + " | ".join(refused))
    name = targets.name_of(owner) if isinstance(owner, bpy.types.ID) else args.get("target")
    return Result(f"{name}:\n" + "\n".join(shown),
                  label=f"Set {len(shown)} propert{'y' if len(shown) == 1 else 'ies'} on {name}")


# ─── add_modifier ────────────────────────────────────────────────────────────────────────────────

# Blender 4.1+ has no auto smooth flag: "Smooth by Angle" is a Geometry Nodes modifier from the bundled
# assets, which the shade_auto_smooth operator adds. The agent asks for it as a modifier type.
SMOOTH_BY_ANGLE = "SMOOTH_BY_ANGLE"


def _modifier_types():
    return [item.identifier for item in bpy.types.Modifier.bl_rna.properties["type"].enum_items]


def _modifier_type(kind):
    if not isinstance(kind, str) or not kind.strip():
        raise ToolError("A modifier type is like BEVEL, SUBSURF, MIRROR, ARRAY, SOLIDIFY or NODES.")
    kind = kind.strip().upper()
    if kind == SMOOTH_BY_ANGLE:
        return kind
    kinds = _modifier_types()
    if kind not in kinds:
        import difflib

        close = difflib.get_close_matches(kind, kinds + [SMOOTH_BY_ANGLE], n=4, cutoff=0.5)
        hint = f" Did you mean: {', '.join(close)}?" if close else ""
        raise ToolError(f"There is no modifier type {kind}.{hint}")
    return kind


def _default_modifier_name(kind):
    if kind == "NODES":
        return "GeometryNodes"  # what Blender's own Add Modifier calls it
    for item in bpy.types.Modifier.bl_rna.properties["type"].enum_items:
        if item.identifier == kind:
            return item.name
    return kind.title()


def auto_smooth(objs, angle_degrees):
    """Adds (or updates) the "Smooth by Angle" modifier on mesh objects. Returns the modifiers."""
    for each in objs:
        if each.type != "MESH":
            raise ToolError(f"{each.name} is not a mesh; auto smooth is for meshes.")
    angle = math.radians(angle_degrees)
    if hasattr(bpy.ops.object, "shade_auto_smooth"):
        with kept():
            select_only(objs)
            props = {"angle": angle}
            # 4.1–4.2's operator also has use_auto_smooth (True adds the modifier); later ones may not.
            rna = bpy.ops.object.shade_auto_smooth.get_rna_type().properties
            if "use_auto_smooth" in rna.keys():
                props["use_auto_smooth"] = True
            operator(bpy.ops.object.shade_auto_smooth, "Auto smooth", active=objs[0], selected=objs, **props)
    else:  # before 4.1 there was no modifier: the mesh's own flag did it
        for each in objs:
            each.data.use_auto_smooth = True
            each.data.auto_smooth_angle = angle
        return []
    found = []
    for each in objs:
        modifier = next((m for m in reversed(each.modifiers)
                         if m.type == "NODES" and m.node_group is not None
                         and m.node_group.name.startswith("Smooth by Angle")), None)
        if modifier is None:
            raise ToolError(f"Blender did not add Smooth by Angle to {each.name}.")
        found.append(modifier)
    return found


def _add_modifier(obj, kind, name=None, properties=None, index=None):
    kind = _modifier_type(kind)
    if properties is not None and not isinstance(properties, dict):
        raise ToolError("properties is {\"<property>\": value}.")
    if kind == SMOOTH_BY_ANGLE:
        props = dict(properties or {})
        angle = props.pop("angle", 30)
        if props:
            raise ToolError("SMOOTH_BY_ANGLE takes one property, angle (degrees).")
        if not isinstance(angle, (int, float)) or isinstance(angle, bool):
            raise ToolError("SMOOTH_BY_ANGLE's angle is a number of degrees.")
        made = auto_smooth([obj], float(angle))
        if not made:
            raise ToolError("This Blender has no Smooth by Angle modifier (it came in 4.1).")
        modifier = made[0]
        if name:
            modifier.name = name
        return modifier
    try:
        modifier = obj.modifiers.new(name or _default_modifier_name(kind), kind)
    except (RuntimeError, TypeError) as error:
        raise ToolError(f"{obj.name} ({obj.type}) cannot take a {kind} modifier: {_clean(error)}.") from None
    if modifier is None:
        raise ToolError(f"{obj.name} is a {obj.type.lower()}, which cannot take a {kind} modifier.")
    try:
        for path, value in (properties or {}).items():
            values.assign(modifier, path, value)
        if index is not None:
            if not isinstance(index, int) or isinstance(index, bool) or index < 0:
                raise ToolError("index is 0 or more (0 is the top of the stack).")
            index = min(index, len(obj.modifiers) - 1)
            if index != len(obj.modifiers) - 1:
                # ObjectModifiers has no move() in 5.x; the operator is the one way that works everywhere.
                operator(bpy.ops.object.modifier_move_to_index, "Moving the modifier", active=obj,
                         modifier=modifier.name, index=index)
    except ToolError:
        obj.modifiers.remove(modifier)
        raise
    return modifier


@tool("add_modifier", undoable=True)
def add_modifier(args, ctx):
    obj = targets.obj(args.get("target"))
    modifier = _add_modifier(obj, args.get("type"), args.get("name"), args.get("properties"), args.get("index"))
    position = list(obj.modifiers).index(modifier)
    lines = [f"Added {modifier.name} ({modifier.type}) to {obj.name}, position {position} of "
             f"{len(obj.modifiers)} (0 is first). Target it as \"{obj.name}/modifiers/{modifier.name}\"."]
    if args.get("properties"):
        set_ = []
        for path in args["properties"]:
            try:
                set_.append(f"{path} = {values.show(modifier.path_resolve(path))}")
            except (ValueError, TypeError):
                continue
        if set_:
            lines.append("Set: " + ", ".join(set_) + ".")
    if modifier.type == "NODES" and modifier.node_group is None:
        lines.append(f'It has no node group yet: edit_nodes owner "{obj.name}/modifiers/{modifier.name}" '
                     'with create "geometry" makes one.')
    return Result("\n".join(lines), label=f"Added {modifier.name} to {obj.name}")


# ─── add_constraint ──────────────────────────────────────────────────────────────────────────────


def _constraint_types():
    return [item.identifier for item in bpy.types.Constraint.bl_rna.properties["type"].enum_items]


@tool("add_constraint", undoable=True)
def add_constraint(args, ctx):
    host, item = targets.part(args.get("target"))
    bone = args.get("bone")
    if item is not None:
        # "Rig/bones/spine" or "Rig/pose_bones/spine": the pose bone of that name.
        if not isinstance(item, (bpy.types.Bone, bpy.types.PoseBone)):
            raise ToolError("A constraint goes on an object, or a bone of an armature (bone).")
        bone = item.name
    if not isinstance(host, bpy.types.Object):
        raise ToolError(f"{args.get('target')} is not an object.")
    owner = host
    if bone:
        if host.type != "ARMATURE" or host.pose is None:
            raise ToolError(f"{host.name} is not an armature, so it has no bone {bone}.")
        owner = host.pose.bones.get(bone)
        if owner is None:
            raise targets._missing("bone", bone, host.pose.bones.keys())
    kind = args.get("type")
    kinds = _constraint_types()
    if not isinstance(kind, str) or kind.strip().upper() not in kinds:
        import difflib

        close = difflib.get_close_matches(str(kind).upper(), kinds, n=4, cutoff=0.5)
        raise ToolError(f"There is no constraint type {kind}."
                        + (f" Did you mean: {', '.join(close)}?" if close else
                           " Use one like TRACK_TO, DAMPED_TRACK, COPY_LOCATION, CHILD_OF, LIMIT_ROTATION, IK."))
    kind = kind.strip().upper()
    properties = args.get("properties") or {}
    if not isinstance(properties, dict):
        raise ToolError("properties is {\"<property>\": value}.")
    constraint = owner.constraints.new(kind)
    try:
        if args.get("name"):
            constraint.name = args["name"]
        for path, value in properties.items():
            values.assign(constraint, path, value)
        if kind == "CHILD_OF" and "inverse_matrix" not in properties and hasattr(constraint, "set_inverse_pending"):
            # What Blender's own Add Constraint does: the child stays where it is when the constraint starts.
            constraint.set_inverse_pending = True
    except ToolError:
        owner.constraints.remove(constraint)
        raise
    where = f"{host.name}, bone {owner.name}" if bone else host.name
    target_ = getattr(constraint, "target", None)
    lines = [f"Added {constraint.name} ({kind}) to {where}"
             + (f", target {target_.name}" if target_ is not None else "") + "."]
    if hasattr(constraint, "target") and target_ is None:
        lines.append('It has no target yet: set_properties with {"constraints[\\"' + constraint.name
                     + '\\"].target": {"id": "<object>"}}' + (" on the pose bone." if bone else "."))
    return Result("\n".join(lines), label=f"Added {constraint.name} to {where}")


# ─── keyframes ───────────────────────────────────────────────────────────────────────────────────


def _first_dot(path):
    """Index of the first "." outside brackets and quotes, or -1 ('modifiers["Bevel.001"].width' → 21)."""
    depth = 0
    quote = None
    for i, ch in enumerate(path):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        elif ch == "." and depth == 0:
            return i
    return -1


def _key_owner(base, path):
    """(struct, path) to key: a keyframe belongs to the data-block that owns the property, so "data.energy"
    on a light object is keyed on the light as "energy"."""
    while True:
        cut = _first_dot(path)
        if cut < 0:
            return base, path
        head = path[:cut]
        try:
            child = base.path_resolve(head)
        except (ValueError, TypeError):
            return base, path
        if not isinstance(child, bpy.types.ID):
            return base, path
        base, path = child, path[cut + 1:]


def fcurves_of(anim):
    """The F-curves an ID's animation uses. Blender 4.4+ keeps them in a layered action's channel bag for the
    ID's slot (5.0 removed action.fcurves); earlier versions on the action itself."""
    action = getattr(anim, "action", None) if anim is not None else None
    if action is None:
        return []
    slot = getattr(anim, "action_slot", None)
    layers = getattr(action, "layers", None)
    if layers and slot is not None:
        found = []
        for layer in layers:
            for strip in layer.strips:
                try:
                    bag = strip.channelbag(slot)
                except (AttributeError, TypeError, RuntimeError):
                    bag = None
                if bag is not None:
                    found.extend(bag.fcurves)
        if found:
            return found
    try:
        return list(action.fcurves)
    except AttributeError:
        return []


@tool("keyframes", undoable=True)
def keyframes(args, ctx):
    base = _owner(args.get("target"))
    data_path = args.get("data_path")
    if not isinstance(data_path, str) or not data_path.strip():
        raise ToolError('data_path is the property to key: "location", "rotation_euler", "data.energy"…')
    keys = args.get("keys")
    if not isinstance(keys, list) or not keys or len(keys) > 500:
        raise ToolError("keys is a list of 1 to 500 {frame, value}.")
    for key in keys:
        if not isinstance(key, dict) or "frame" not in key or "value" not in key or \
                not isinstance(key["frame"], (int, float)) or isinstance(key["frame"], bool):
            raise ToolError("Each key is {\"frame\": number, \"value\": …}.")
    interpolation = args.get("interpolation")
    if interpolation is not None and interpolation not in {"CONSTANT", "LINEAR", "BEZIER"}:
        raise ToolError("interpolation is CONSTANT, LINEAR or BEZIER.")

    owner, path = _key_owner(base, data_path.strip())
    head, attr, path_index = values._split_path(path)
    index = args.get("index")
    if path_index is not None:
        if index not in (None, -1, path_index):
            raise ToolError(f"data_path already says [{path_index}]; leave index out.")
        index = path_index
    if index is None:
        index = -1
    try:
        container = owner.path_resolve(head, False) if head else owner
    except ValueError:
        raise ToolError(f"{data_path} does not resolve on {args.get('target')}. Check the path with inspect.") from None
    if attr.startswith("["):
        raise ToolError("Custom properties are keyed by their path without quotes changing: use set_properties "
                        "then run_python's keyframe_insert, or key a built-in property.")
    rna = container.bl_rna.properties.get(attr) if hasattr(container, "bl_rna") else None
    if rna is None:
        raise ToolError(values._unknown(container, attr) if hasattr(container, "bl_rna")
                        else f"{data_path} is not a property.")
    if not rna.is_animatable:
        raise ToolError(f"{attr} cannot be animated.")
    length = getattr(rna, "array_length", 0) or 0
    if index >= 0 and (not length or index >= length):
        raise ToolError(f"{attr} has {length or 'no'} channels; index {index} is not one of them.")
    rotation = getattr(rna, "subtype", "") == "EULER"
    key_path = f"{head}.{attr}" if head else attr  # what keyframe_insert takes, without an index

    if args.get("action"):
        name = str(args["action"]).strip()
        action = bpy.data.actions.get(name) or bpy.data.actions.new(name)
        anim = owner.id_data.animation_data_create()
        if anim.action != action:
            anim.action = action  # 4.4+: keyframe_insert below makes or picks the slot

    value_path = f"{key_path}[{index}]" if index >= 0 else key_path
    frames = []
    for key in sorted(keys, key=lambda k: k["frame"]):
        value = key["value"]
        if rotation:
            try:
                value = [math.radians(v) for v in value] if isinstance(value, (list, tuple)) else math.radians(value)
            except TypeError:
                raise ToolError("Rotation values are numbers in degrees.") from None
        values.assign(owner, value_path, value)
        try:
            ok = owner.keyframe_insert(data_path=key_path, index=index, frame=float(key["frame"]))
        except (RuntimeError, TypeError) as error:
            raise ToolError(f"Could not key {data_path}: {_clean(error)}.") from None
        if not ok:
            raise ToolError(f"Blender would not key {data_path} (is it driven or locked?).")
        frames.append(float(key["frame"]))

    id_block = owner.id_data
    anim = id_block.animation_data
    try:
        full = owner.path_from_id(key_path) if not isinstance(owner, bpy.types.ID) else key_path
    except (ValueError, TypeError):
        full = key_path
    curves = [c for c in fcurves_of(anim) if c.data_path == full and (index < 0 or c.array_index == index)]
    if interpolation:
        for curve in curves:
            for point in curve.keyframe_points:
                if any(abs(point.co.x - f) < 1e-3 for f in frames):
                    point.interpolation = interpolation
            curve.update()
    scene = bpy.context.scene
    scene.frame_set(scene.frame_current)  # the property shows its animated value again, not the last key's

    action = anim.action if anim is not None else None
    channel = f"[{index}]" if index >= 0 else ""
    span = f"{_number(frames[0])}–{_number(frames[-1])}" if len(frames) > 1 else _number(frames[0])
    text = (f"Keyed {targets.name_of(id_block)} {full}{channel}: {len(frames)} key{'s' if len(frames) != 1 else ''}, "
            f"frame{'s' if len(frames) > 1 else ''} {span}"
            + (f", {interpolation}" if interpolation else "")
            + (f', in action "{action.name}"' if action else "") + "."
            + (" Values were read as degrees." if rotation else ""))
    return Result(text, label=f"Animated {getattr(id_block, 'name', '')} {attr}")


# ─── collection_op ───────────────────────────────────────────────────────────────────────────────

_COLLECTION_ACTIONS = ("create", "link", "unlink", "rename", "hide", "show", "exclude", "include")


@tool("collection_op", undoable=True)
def collection_op(args, ctx):
    action = args.get("action")
    if action not in _COLLECTION_ACTIONS:
        raise ToolError(f"action is one of: {', '.join(_COLLECTION_ACTIONS)}.")
    objs = targets.objects(args["objects"]) if args.get("objects") else []
    parent = collection_named(args["parent"]) if args.get("parent") else None

    if action == "create":
        text = args.get("collection")
        name = text[len("collection:"):] if isinstance(text, str) and text.startswith("collection:") else text
        if not isinstance(name, str) or not name.strip():
            raise ToolError('collection is the new collection\'s name: "collection:Props".')
        if name.strip() in bpy.data.collections:
            raise ToolError(f'There is already a collection "{name.strip()}". Use link to add objects to it.')
        collection = bpy.data.collections.new(name.strip())
        (parent or bpy.context.scene.collection).children.link(collection)
        for each in objs:
            collection.objects.link(each)
        text = f'Created collection "{collection.name}" under "{(parent or bpy.context.scene.collection).name}"'
        if objs:
            text += f", with {listing([o.name for o in objs])} linked in"
        return Result(text + ".", label=f"Created collection {collection.name}")

    collection = collection_named(args.get("collection"))
    name = collection.name
    if action == "link":
        if objs:
            added = [o for o in objs if o.name not in collection.objects]
            for each in added:
                collection.objects.link(each)
            return Result(f'Linked {listing([o.name for o in added]) if added else "nothing new"} into "{name}" '
                          "(they stay in their other collections too).", label=f"Linked objects into {name}")
        if parent is None:
            raise ToolError("link takes objects, or parent to put the collection under another one.")
        if collection.name in parent.children:
            raise ToolError(f'"{name}" is already under "{parent.name}".')
        if collection == parent or _is_inside(parent, collection):
            raise ToolError(f'"{parent.name}" is inside "{name}", so "{name}" cannot go under it.')
        parent.children.link(collection)
        return Result(f'Linked collection "{name}" under "{parent.name}".', label=f"Linked collection {name}")
    if action == "unlink":
        if objs:
            stranded = [o.name for o in objs if collection in o.users_collection and len(o.users_collection) == 1]
            if stranded:
                raise ToolError(f'{listing(stranded)} would be in no collection (gone from the scene). Link '
                                "it somewhere else first, or delete it.")
            removed = [o for o in objs if o.name in collection.objects]
            for each in removed:
                collection.objects.unlink(each)
            return Result(f'Unlinked {listing([o.name for o in removed]) if removed else "nothing"} from "{name}".',
                          label=f"Unlinked objects from {name}")
        holders = [parent] if parent else [c for c in [bpy.context.scene.collection, *bpy.data.collections]
                                          if collection.name in c.children]
        holders = [c for c in holders if collection.name in c.children]
        if not holders:
            raise ToolError(f'"{name}" is not under {parent.name if parent else "any collection"}.')
        for holder in holders:
            holder.children.unlink(collection)
        note = " It is no longer in the scene (still in the file until purged)." if collection.users == 0 else ""
        return Result(f'Unlinked collection "{name}" from {listing([h.name for h in holders])}.{note}',
                      label=f"Unlinked collection {name}")
    if action == "rename":
        new = args.get("name")
        if not isinstance(new, str) or not new.strip():
            raise ToolError("rename takes name, the new name.")
        collection.name = new.strip()
        extra = f' ("{new.strip()}" was taken)' if collection.name != new.strip() else ""
        return Result(f'Renamed collection "{name}" to "{collection.name}"{extra}.',
                      label=f"Renamed collection {collection.name}")

    layer = layer_collection(collection)
    if layer is None:
        raise ToolError(f'"{name}" is not in the current view layer (link it under the scene first).')
    if action == "hide":
        layer.hide_viewport = True
        text = f'Hid "{name}" in the viewport.'
    elif action == "show":
        layer.hide_viewport = False
        collection.hide_viewport = False  # the global "disable in viewports" too, or it would stay hidden
        text = f'Showed "{name}" in the viewport.'
    elif action == "exclude":
        layer.exclude = True
        text = f'Excluded "{name}" from the view layer (its objects are out of the scene until included).'
    else:
        layer.exclude = False
        text = f'Included "{name}" in the view layer.'
    return Result(text, label=f"{action.title()} collection {name}")


def _is_inside(inner, outer):
    """True when collection inner is somewhere under outer."""
    return any(child == inner or _is_inside(inner, child) for child in outer.children)


# ─── select, set_mode ────────────────────────────────────────────────────────────────────────────


@tool("select", undoable=True)
def select(args, ctx):
    names = args.get("targets")
    if not isinstance(names, list):
        raise ToolError("targets is a list of objects ([] deselects everything).")
    if len(names) > 200:
        raise ToolError("At most 200 targets.")
    objs = [targets.obj(name) for name in names]
    active = targets.obj(args["active"]) if args.get("active") else None
    mode = args.get("mode") or "replace"
    if mode not in {"replace", "add"}:
        raise ToolError("mode is replace or add.")
    layer = bpy.context.view_layer
    missing = [o.name for o in objs + ([active] if active else []) if layer.objects.get(o.name) != o]
    if missing:
        raise ToolError(f"{listing(missing)} is not in the current view layer (an excluded collection?).")
    note = ""
    current = layer.objects.active
    if current is not None and current.mode != "OBJECT":
        object_mode()
        note = " (Switched to Object Mode first.)"
    if mode == "replace":
        for each in layer.objects:
            if each.select_get():
                each.select_set(False)
    for each in objs:
        each.select_set(True)
    if active is not None:
        active.select_set(True)
        layer.objects.active = active
    elif mode == "replace":
        layer.objects.active = objs[0] if objs else None
    selected = [o.name for o in layer.objects if o.select_get()]
    hidden = [o.name for o in objs if not o.visible_get()]
    if not selected:
        return Result("Nothing is selected now." + note, label="Deselected all")
    text = f"Selected ({len(selected)}): {listing(selected, 20)}; active {layer.objects.active.name if layer.objects.active else 'none'}."
    if hidden:
        text += f" Hidden in the viewport: {listing(hidden)}."
    return Result(text + note, label=f"Selected {listing(selected, 3)}")


_MODES = {
    "OBJECT": None,
    "EDIT": {"MESH", "CURVE", "SURFACE", "META", "FONT", "LATTICE", "ARMATURE", "CURVES", "GREASEPENCIL",
             "GREASE_PENCIL", "POINTCLOUD"},
    "SCULPT": {"MESH", "CURVES"},
    "VERTEX_PAINT": {"MESH"},
    "WEIGHT_PAINT": {"MESH"},
    "TEXTURE_PAINT": {"MESH"},
    "POSE": {"ARMATURE"},
}


@tool("set_mode", undoable=True)
def set_mode(args, ctx):
    mode = args.get("mode")
    if mode not in _MODES:
        raise ToolError(f"mode is one of: {', '.join(_MODES)}.")
    layer = bpy.context.view_layer
    obj = targets.obj(args["target"]) if args.get("target") else layer.objects.active
    if obj is None:
        raise ToolError("There is no active object: pass target.")
    allowed = _MODES[mode]
    if allowed is not None and obj.type not in allowed:
        raise ToolError(f"{obj.name} is a {obj.type.lower()}; {mode.replace('_', ' ').title()} Mode is for "
                        f"{', '.join(sorted(t.lower() for t in allowed))} objects.")
    if layer.objects.get(obj.name) != obj:
        raise ToolError(f"{obj.name} is not in the current view layer.")
    if not obj.visible_get():
        raise ToolError(f"{obj.name} is hidden; show it first (modify_object hide_viewport false).")
    current = layer.objects.active
    if current is not None and current != obj and current.mode != "OBJECT":
        mode_set(current, "OBJECT")  # one object's Edit Mode does not carry over to another's
    obj.select_set(True)
    mode_set(obj, mode)
    inside = [o.name for o in layer.objects if o.mode == mode] if mode != "OBJECT" else [obj.name]
    label = mode.replace("_", " ").title()
    return Result(f"{listing(inside)} {'is' if len(inside) == 1 else 'are'} in {label} Mode.",
                  label=f"{label} Mode on {obj.name}")


# ─── mesh_op ─────────────────────────────────────────────────────────────────────────────────────

_MESH_OPS = ("apply_modifier", "apply_transform", "shade_smooth", "shade_auto_smooth", "shade_flat",
             "recalculate_normals", "merge_by_distance", "triangulate", "smart_uv_project", "set_origin", "join",
             "separate_loose", "convert_to_mesh", "make_single_user")


def _meshes(objs, op):
    bad = [o.name for o in objs if o.type != "MESH"]
    if bad:
        raise ToolError(f"{op} works on meshes; {listing(bad)} {'is' if len(bad) == 1 else 'are'} not "
                        "(convert_to_mesh first?).")
    return objs


def _single_users(objs, op):
    shared = [o.name for o in objs if o.data is not None and o.data.users > 1]
    if shared:
        raise ToolError(f"{listing(shared)} share{'s' if len(shared) == 1 else ''} its data with other objects, "
                        f"so {op} would change them too. mesh_op make_single_user first.")


def _with_bmesh(obj, fn):
    mesh = obj.data
    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        out = fn(bm)
        bm.to_mesh(mesh)
    finally:
        bm.free()
    mesh.update()
    return out


def _option_bool(options, key, default):
    value = options.get(key, default)
    if not isinstance(value, bool):
        raise ToolError(f"options.{key} is true or false.")
    return value


def _option_number(options, key, default, low=None):
    value = options.get(key, default)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or (low is not None and value < low):
        raise ToolError(f"options.{key} is a number" + (f" of at least {low}." if low is not None else "."))
    return float(value)


def _compensate_children(obj, applied):
    """Children stay put in the world when their parent's transform moves into (or out of) its data."""
    for child in obj.children:
        child.matrix_parent_inverse = applied @ child.matrix_parent_inverse


def _apply_transform(objs, options):
    location = _option_bool(options, "location", False)
    rotation = _option_bool(options, "rotation", False)
    scale = _option_bool(options, "scale", True)
    if not (location or rotation or scale):
        raise ToolError("Nothing to apply: set options.location, rotation or scale to true.")
    for each in objs:
        if each.type not in {"MESH", "CURVE", "SURFACE", "FONT", "LATTICE", "ARMATURE"} or each.data is None:
            raise ToolError(f"{each.name} is a {each.type.lower()}; apply_transform works on objects with "
                            "geometry (meshes, curves, lattices, armatures).")
        if not hasattr(each.data, "transform"):
            raise ToolError(f"{each.name}'s data cannot be transformed here.")
    _single_users(objs, "apply_transform")
    for each in objs:
        loc, rot, sca = each.matrix_basis.decompose()
        keep = (Matrix.Identity(4) if location else Matrix.Translation(loc)) @ \
               (Matrix.Identity(4) if rotation else rot.to_matrix().to_4x4()) @ \
               (Matrix.Identity(4) if scale else Matrix.Diagonal((*sca, 1.0)))
        # The data takes what the object gives up, so nothing moves in the world:
        # basis = keep @ applied  →  applied = keep⁻¹ @ basis.
        applied = keep.inverted_safe() @ each.matrix_basis
        each.data.transform(applied)
        each.matrix_basis = keep
        _compensate_children(each, applied)
        if hasattr(each.data, "update"):
            each.data.update()
    parts = [p for p, on in (("location", location), ("rotation", rotation), ("scale", scale)) if on]
    return f"Applied {listing(parts)} on {listing([o.name for o in objs])}.", f"Applied {parts[-1]}"


def _set_origin(objs, options):
    to = options.get("to", "geometry")
    if to not in {"geometry", "bounds_bottom", "cursor"}:
        raise ToolError('options.to is "geometry", "bounds_bottom" or "cursor".')
    _meshes(objs, "set_origin")
    _single_users(objs, "set_origin")
    cursor = bpy.context.scene.cursor.location
    for each in objs:
        verts = each.data.vertices
        if to == "cursor":
            offset = each.matrix_world.inverted_safe() @ cursor
        elif not len(verts):
            continue
        elif to == "geometry":
            offset = sum((v.co for v in verts), Vector()) / len(verts)
        else:
            lows = Vector((min(v.co.x for v in verts), min(v.co.y for v in verts), min(v.co.z for v in verts)))
            highs = Vector((max(v.co.x for v in verts), max(v.co.y for v in verts), max(v.co.z for v in verts)))
            offset = Vector(((lows.x + highs.x) / 2, (lows.y + highs.y) / 2, lows.z))
        shift = Matrix.Translation(-offset)
        each.data.transform(shift)
        each.matrix_basis = each.matrix_basis @ Matrix.Translation(offset)
        _compensate_children(each, shift)
        each.data.update()
    where = {"geometry": "the middle of its geometry", "bounds_bottom": "the bottom of its bounds",
             "cursor": "the 3D cursor"}[to]
    return f"Moved the origin of {listing([o.name for o in objs])} to {where}.", "Set origin"


def _restore_selection_flags(mesh):
    """The mesh's own vertex/edge/face selection, to put back after an Edit Mode operator that selects all."""
    saved = {}
    for name in ("vertices", "edges", "polygons"):
        items = getattr(mesh, name)
        flags = [False] * len(items)
        items.foreach_get("select", flags)
        saved[name] = flags

    def restore():
        for name, flags in saved.items():
            items = getattr(mesh, name)
            if len(items) == len(flags):
                items.foreach_set("select", flags)
        mesh.update()
    return restore


def _edit_all(objs, fn):
    """Runs fn in Edit Mode on objs with everything selected, then back to Object Mode."""
    select_only(objs)
    mode_set(objs[0], "EDIT")
    try:
        operator(bpy.ops.mesh.select_all, "Selecting the mesh", active=objs[0], action="SELECT")
        fn()
    finally:
        mode_set(objs[0], "OBJECT")


def _apply_modifier(objs, options):
    wanted = options.get("modifier")
    if wanted is not None and not isinstance(wanted, str):
        raise ToolError("options.modifier is the modifier's name.")
    for each in objs:
        if wanted and wanted not in each.modifiers:
            raise targets._missing(f"modifier on {each.name}", wanted, each.modifiers.keys())
        if not wanted and not len(each.modifiers):
            raise ToolError(f"{each.name} has no modifiers to apply.")
    done = []
    for each in objs:
        names = [wanted] if wanted else [m.name for m in each.modifiers]
        for name in names:
            props = {"modifier": name}
            rna = bpy.ops.object.modifier_apply.get_rna_type().properties
            if "single_user" in rna.keys():
                props["single_user"] = True  # 4.1+: a shared mesh is made this object's own, as the UI offers
            select_only([each])
            operator(bpy.ops.object.modifier_apply, f"Applying {name} on {each.name}", active=each, **props)
            done.append(f"{name} on {each.name}")
    return f"Applied {listing(done)}. The result is now plain geometry.", \
        f"Applied {wanted or 'modifiers'} on {objs[0].name}" if len(objs) == 1 else f"Applied {wanted or 'modifiers'}"


def _join(objs):
    if len(objs) < 2:
        raise ToolError("join needs at least two targets; the first is the one they join into.")
    kinds = {o.type for o in objs}
    if len(kinds) > 1:
        raise ToolError(f"join needs objects of one type; these are {', '.join(sorted(kinds))}.")
    into = objs[0]
    others = [o.name for o in objs[1:]]
    select_only(objs, active=into)
    operator(bpy.ops.object.join, "Joining", active=into, selected=objs)
    extra = f" ({len(into.data.vertices)} vertices)" if into.type == "MESH" else ""
    return f"Joined {listing(others)} into {into.name}{extra}; those objects are gone.", f"Joined into {into.name}"


def _separate_loose(objs):
    _meshes(objs, "separate_loose")
    made = []
    for each in objs:
        before = set(bpy.data.objects)
        _edit_all([each], lambda: operator(bpy.ops.mesh.separate, f"Separating {each.name}", active=each,
                                           type="LOOSE"))
        made.extend(o.name for o in bpy.data.objects if o not in before)
    if not made:
        return f"{listing([o.name for o in objs])} had only one connected part; nothing was separated.", \
            "Separate loose parts"
    return (f"Separated loose parts into {len(made)} new object{'s' if len(made) != 1 else ''}: {listing(made, 30)}.",
            "Separated loose parts")


def _convert(objs):
    before = {o: o.type for o in objs}
    select_only(objs)
    operator(bpy.ops.object.convert, "Converting to mesh", active=objs[0], selected=objs, target="MESH")
    lines = [f"{o.name} ({before[o].lower()} → {o.type.lower()})" for o in objs if alive(o)]
    return f"Converted {listing(lines)}; modifiers were applied.", "Converted to mesh"


def _make_single_user(objs):
    made = []
    for each in objs:
        if each.data is not None and each.data.users > 1:
            each.data = each.data.copy()
            made.append(f"{each.name} ({each.data.name})")
    if not made:
        return f"{listing([o.name for o in objs])} already had data of {'its' if len(objs) == 1 else 'their'} own.", \
            "Make single user"
    return f"Gave {listing(made)} {'its' if len(made) == 1 else 'their'} own copy of the data.", "Made single user"


@tool("mesh_op", undoable=True, risky=True)
def mesh_op(args, ctx):
    op = args.get("op")
    if op not in _MESH_OPS:
        raise ToolError(f"op is one of: {', '.join(_MESH_OPS)}.")
    names = args.get("targets")
    if not isinstance(names, list) or not names:
        raise ToolError("targets is a list of objects.")
    objs = []
    for each in targets.objects(names):
        if each not in objs:
            objs.append(each)
    options = args.get("options") or {}
    if not isinstance(options, dict):
        raise ToolError("options is an object.")
    layer = bpy.context.view_layer
    if op in {"apply_modifier", "join", "separate_loose", "convert_to_mesh", "smart_uv_project",
              "shade_auto_smooth"}:
        outside = [o.name for o in objs if layer.objects.get(o.name) != o]
        if outside:
            raise ToolError(f"{listing(outside)} is not in the current view layer, which {op} needs.")

    with kept():
        if op == "apply_transform":
            text, label = _apply_transform(objs, options)
        elif op == "set_origin":
            text, label = _set_origin(objs, options)
        elif op in {"shade_smooth", "shade_flat"}:
            for each in _meshes(objs, op):
                mesh = each.data
                smooth = op == "shade_smooth"
                method = getattr(mesh, "shade_smooth" if smooth else "shade_flat", None)
                if method is not None:  # 4.1+
                    method()
                else:
                    mesh.polygons.foreach_set("use_smooth", [smooth] * len(mesh.polygons))
                mesh.update()
            text = f"Shaded {listing([o.name for o in objs])} {'smooth' if op == 'shade_smooth' else 'flat'}."
            label = f"Shade {'smooth' if op == 'shade_smooth' else 'flat'}"
        elif op == "shade_auto_smooth":
            angle = _option_number(options, "angle", 30, low=0)
            auto_smooth(_meshes(objs, op), angle)
            text = (f"Auto smooth at {_number(angle)}° on {listing([o.name for o in objs])} "
                    '(a "Smooth by Angle" modifier; change its angle there or call this again).')
            label = "Auto smooth"
        elif op == "recalculate_normals":
            inside = _option_bool(options, "inside", False)
            for each in _meshes(objs, op):
                def recalc(bm):
                    bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
                    if inside:
                        bmesh.ops.reverse_faces(bm, faces=bm.faces[:])
                _with_bmesh(each, recalc)
            text = f"Recalculated normals of {listing([o.name for o in objs])} to point {'in' if inside else 'out'}."
            label = "Recalculate normals"
        elif op == "merge_by_distance":
            distance = _option_number(options, "distance", 0.0001, low=0)
            removed = []
            for each in _meshes(objs, op):
                count = len(each.data.vertices)
                _with_bmesh(each, lambda bm: bmesh.ops.remove_doubles(bm, verts=bm.verts[:], dist=distance))
                removed.append(f"{each.name} −{count - len(each.data.vertices)}")
            text = f"Merged vertices closer than {_number(distance)} m: {listing(removed)} vertices."
            label = "Merge by distance"
        elif op == "triangulate":
            for each in _meshes(objs, op):
                _with_bmesh(each, lambda bm: bmesh.ops.triangulate(bm, faces=bm.faces[:]))
            text = "Triangulated " + listing([f"{o.name} ({len(o.data.polygons)} triangles)" for o in objs]) + "."
            label = "Triangulate"
        elif op == "smart_uv_project":
            angle = _option_number(options, "angle", 66, low=0)
            margin = _option_number(options, "margin", 0.0, low=0)
            meshes = _meshes(objs, op)
            restores = [_restore_selection_flags(o.data) for o in meshes]
            _edit_all(meshes, lambda: operator(
                bpy.ops.uv.smart_project, "Smart UV Project", active=meshes[0],
                angle_limit=math.radians(min(angle, 89)), island_margin=margin))
            for restore in restores:
                restore()
            text = (f"Unwrapped {listing([o.name for o in meshes])} with Smart UV Project (angle {_number(angle)}°, "
                    f"margin {_number(margin)}) into UV map "
                    f"{listing(sorted({o.data.uv_layers.active.name for o in meshes if o.data.uv_layers.active}))}.")
            label = "Smart UV Project"
        elif op == "apply_modifier":
            text, label = _apply_modifier(objs, options)
        elif op == "join":
            text, label = _join(objs)
        elif op == "separate_loose":
            text, label = _separate_loose(objs)
        elif op == "convert_to_mesh":
            text, label = _convert(objs)
        else:
            text, label = _make_single_user(objs)
    if len(objs) == 1 and objs[0].name not in label and alive(objs[0]) and op not in {"join", "apply_modifier"}:
        label = f"{label} on {objs[0].name}"
    return Result(text, label=label)


# ─── delete ──────────────────────────────────────────────────────────────────────────────────────


def _resolve_deletion(target):
    """(kind, description, remove) for one target, resolved before anything is removed."""
    if not isinstance(target, str) or not target.strip():
        raise ToolError("Each target is a name, like \"Chair\", \"material:Oak\" or \"Chair/modifiers/Bevel\".")
    target = target.strip()
    if "/nodes/" in target and target not in bpy.data.objects:
        owner, _, node_name = target.rpartition("/nodes/")
        tree = targets.node_tree(owner)
        node = tree.nodes.get(node_name)
        if node is None:
            raise targets._missing(f"node in {owner}", node_name, tree.nodes.keys())
        return "part", f"node {node.name} in {owner}", lambda: tree.nodes.remove(node), node
    owner, item = targets.part(target)
    if item is not None:
        if isinstance(item, bpy.types.Modifier):
            return "part", f"modifier {item.name} on {owner.name}", lambda: owner.modifiers.remove(item), item
        if isinstance(item, bpy.types.Constraint):
            return "part", f"constraint {item.name} on {owner.name}", lambda: owner.constraints.remove(item), item
        if isinstance(item, bpy.types.VertexGroup):
            return "part", f"vertex group {item.name} on {owner.name}", \
                lambda: owner.vertex_groups.remove(item), item
        if isinstance(item, bpy.types.ShapeKey):
            return "part", f"shape key {item.name} on {owner.name}", \
                lambda: owner.shape_key_remove(item), item
        raise ToolError(f"delete does not remove {target.split('/')[1]}. It removes objects, collections, "
                        "data-blocks, modifiers, constraints, vertex groups, shape keys and nodes "
                        '("material:Oak/nodes/Mix").')
    if isinstance(owner, bpy.types.Object):
        return "object", f"object {owner.name}", lambda: _delete_object(owner), owner
    if isinstance(owner, bpy.types.Collection):
        return "collection", f"collection {owner.name}", lambda: _delete_collection(owner), owner
    if isinstance(owner, bpy.types.Scene) and len(bpy.data.scenes) == 1:
        raise ToolError("This is the file's only scene; Blender needs one.")
    kind, name = targets.split(target)
    data = getattr(bpy.data, targets.KINDS[kind])
    return "data", f"{kind.replace('_', ' ')} {owner.name}" + (
        f" (it had {owner.users} user{'s' if owner.users != 1 else ''})" if owner.users else ""), \
        lambda: data.remove(owner), owner


def _delete_object(obj):
    for child in obj.children:
        world = child.matrix_world.copy()
        child.parent = None
        child.matrix_world = world  # children stay where they are, as with Blender's own Delete
    bpy.data.objects.remove(obj, do_unlink=True)


def _delete_collection(collection):
    """Removes a collection; its objects and child collections move up to its parents, as Blender's Delete
    does — deleting the box never deletes what is in it. Delete those by name to remove them too."""
    parents = [c for c in [bpy.context.scene.collection, *bpy.data.collections] if collection.name in c.children
               and c != collection]
    if not parents:
        parents = [bpy.context.scene.collection]
    for obj in list(collection.objects):
        if len(obj.users_collection) == 1:
            for parent in parents:
                if obj.name not in parent.objects:
                    parent.objects.link(obj)
    for child in list(collection.children):
        for parent in parents:
            if child.name not in parent.children:
                parent.children.link(child)
    bpy.data.collections.remove(collection)


@tool("delete", undoable=True, risky=True)
def delete(args, ctx):
    names = args.get("targets")
    if not isinstance(names, list) or not names:
        raise ToolError("targets is a list of 1 to 100 things to delete.")
    if len(names) > 100:
        raise ToolError("At most 100 targets per call.")
    plan = []
    seen = set()
    for name in names:
        kind, text, remove, block = _resolve_deletion(name)
        key = block.as_pointer()
        if key in seen:
            continue
        seen.add(key)
        plan.append((kind, text, remove, block))
    # Parts first (their owner must still exist), then objects, collections, and other data last.
    order = {"part": 0, "object": 1, "collection": 2, "data": 3}
    plan.sort(key=lambda step: order[step[0]])
    active = bpy.context.view_layer.objects.active
    if active is not None and active.mode != "OBJECT":
        object_mode()  # removing what Edit Mode is holding would lose its edits or crash the session
    done = []
    for kind, text, remove, block in plan:
        if kind != "part" and not alive(block):
            continue
        remove()
        done.append(text)
    label = f"Deleted {done[0].split(' (')[0].split(' ', 1)[1]}" if len(done) == 1 else f"Deleted {len(done)} items"
    return Result(f"Deleted {len(done)}: " + "; ".join(done) + ".", label=label)


# ─── purge_unused ────────────────────────────────────────────────────────────────────────────────


def _all_ids():
    found = set()
    for kind, attr in targets.KINDS.items():
        data = getattr(bpy.data, attr, None)
        if data is None:
            continue
        for block in data:
            found.add(f"{kind}:{block.name}" if kind != "object" else block.name)
    return found


@tool("purge_unused", undoable=True, risky=True)
def purge_unused(args, ctx):
    recursive = args.get("recursive", True)
    if not isinstance(recursive, bool):
        raise ToolError("recursive is true or false.")
    before = _all_ids()
    if hasattr(bpy.data, "orphans_purge"):
        bpy.data.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=recursive)
    else:
        operator(bpy.ops.outliner.orphans_purge, "Purging unused data", do_recursive=recursive)
    gone = sorted(before - _all_ids())
    if not gone:
        return Result("Nothing was unused; nothing removed.", label="Purge unused data")
    return Result(f"Removed {len(gone)} unused data-block{'s' if len(gone) != 1 else ''}: {listing(gone, 30)}.",
                  label=f"Purged {len(gone)} unused data-block{'s' if len(gone) != 1 else ''}")


# ─── undo ────────────────────────────────────────────────────────────────────────────────────────


@tool("undo")
def undo(args, ctx):
    """Not itself an undo step. Only this message's own steps, and only while they are the newest: after the
    user's own edits, undoing would take theirs back first."""
    steps = args.get("steps", 1)
    if not isinstance(steps, int) or isinstance(steps, bool) or not 1 <= steps <= 20:
        raise ToolError("steps is 1 to 20.")
    turn = changes.current()
    available = turn["steps"] if turn else 0
    if not available:
        raise ToolError("There is nothing of yours to undo in this message.")
    if turn.get("foreign"):
        raise ToolError("The user has changed the scene since your last step, so undo would take back their "
                        "work first. Ask them to use Edit › Undo, or fix it with another call.")
    count = min(steps, available)
    lines = [line for line in turn["lines"] if line.get("kind") == "scene" and line.get("state") == "done"]
    undone = []
    with changes.running():
        for _ in range(count):
            try:
                with override():
                    result = bpy.ops.ed.undo()
            except RuntimeError as error:
                if not undone:
                    raise ToolError(f"Blender could not undo: {_clean(error)}.") from None
                break
            if "FINISHED" not in result:
                break
            turn["steps"] -= 1
            if lines:
                line = lines.pop()
                line["state"] = "undone"
                undone.append(line.get("label", "a step"))
            else:
                undone.append("a step")
    if not undone:
        raise ToolError("Blender could not undo (its undo history may be off in Preferences).")
    text = f"Undid {len(undone)} step{'s' if len(undone) != 1 else ''}: {'; '.join(undone)}."
    if count < steps:
        text += f" Only {available} {'was' if available == 1 else 'were'} yours this message."
    return Result(text)
