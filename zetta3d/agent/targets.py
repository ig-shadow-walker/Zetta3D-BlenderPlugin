"""Targets: how the agent names things in the open file (BlenderAgentProtocol.md › Targets and values).

- an object by its name: "Chair"
- any other data-block as "<kind>:<name>": "material:Oak", "collection:Props", "node_group:Scatter"
- a part of an object by path: "Chair/modifiers/Bevel", "Chair/constraints/Track To", "Rig/bones/spine"
- node owners: "material:Oak", "world", "compositor", "node_group:Scatter", "Rocks/modifiers/GeometryNodes"
- files relative to the .blend: "//textures/oak.png", or "attached:<name>" for a picture the user attached

A name that does not resolve raises ToolError naming the closest matches, so the model can correct itself.
"""

import difflib
import os

import bpy

from .tools import ToolError

# kind → the bpy.data collection holding it
KINDS = {
    "object": "objects",
    "collection": "collections",
    "material": "materials",
    "mesh": "meshes",
    "curve": "curves",
    "armature": "armatures",
    "camera": "cameras",
    "light": "lights",
    "image": "images",
    "texture": "textures",
    "node_group": "node_groups",
    "world": "worlds",
    "scene": "scenes",
    "action": "actions",
    "text": "texts",
    "brush": "brushes",
    "grease_pencil": "grease_pencils",
    "lattice": "lattices",
    "metaball": "metaballs",
    "font": "fonts",
    "light_probe": "lightprobes",
    "speaker": "speakers",
    "volume": "volumes",
    "point_cloud": "pointclouds",
    "hair_curves": "hair_curves",
    "particle": "particles",
    "palette": "palettes",
    "sound": "sounds",
    "movie_clip": "movieclips",
    "mask": "masks",
    "workspace": "workspaces",
}

# The collections an object path may name, after "<Object>/"
PARTS = {"modifiers", "constraints", "bones", "pose_bones", "material_slots", "shape_keys", "vertex_groups"}


def _collection(kind):
    attr = KINDS.get(kind)
    if attr is None:
        raise ToolError(f'"{kind}" is not a kind of data. Use one of: {", ".join(sorted(KINDS))}.')
    data = getattr(bpy.data, attr, None)
    if data is None:
        raise ToolError(f"This Blender has no {kind} data.")
    return data


def _missing(what, name, candidates):
    close = difflib.get_close_matches(name, list(candidates), n=5, cutoff=0.5)
    hint = f" Did you mean: {', '.join(close)}?" if close else ""
    return ToolError(f'There is no {what} "{name}".{hint}')


def split(target):
    """("object", "Chair") or ("material", "Oak") — no lookup."""
    if not isinstance(target, str) or not target.strip():
        raise ToolError("A target is a name, like \"Chair\" or \"material:Oak\".")
    target = target.strip()
    kind, sep, rest = target.partition(":")
    if sep and kind in KINDS and rest:
        return kind, rest
    return "object", target


def datablock(target):
    """The object or data-block a target names."""
    kind, name = split(target)
    if kind == "object":
        obj = bpy.data.objects.get(name)
        if obj is None:
            # A path to a part is resolved by part(); here the whole string is the object's name.
            raise _missing("object", name, bpy.data.objects.keys())
        return obj
    data = _collection(kind)
    found = data.get(name)
    if found is None:
        raise _missing(kind.replace("_", " "), name, data.keys())
    return found


def obj(target):
    """An object, refusing other data-blocks with a clear line."""
    found = datablock(target)
    if not isinstance(found, bpy.types.Object):
        raise ToolError(f"{target} is not an object.")
    return found


def objects(targets):
    if not isinstance(targets, list):
        raise ToolError("targets is a list of names.")
    return [obj(target) for target in targets]


def part(target):
    """(owner, item) for a path into an object ("Chair/modifiers/Bevel"), or (datablock, None) for a plain
    target. An object whose own name contains "/" still resolves as itself."""
    if isinstance(target, str) and "/" in target and target.strip() not in bpy.data.objects:
        head, _, tail = target.strip().partition("/")
        collection, _, name = tail.partition("/")
        if collection in PARTS and name:
            owner = obj(head)
            if collection == "bones":
                if owner.type != "ARMATURE":
                    raise ToolError(f"{head} is not an armature, so it has no bones.")
                items = owner.data.bones
            elif collection == "pose_bones":
                if owner.pose is None:
                    raise ToolError(f"{head} has no pose.")
                items = owner.pose.bones
            elif collection == "shape_keys":
                keys = getattr(owner.data, "shape_keys", None)
                if keys is None:
                    raise ToolError(f"{head} has no shape keys.")
                items = keys.key_blocks
            else:
                items = getattr(owner, collection)
            item = items.get(name)
            if item is None:
                raise _missing(collection.rstrip("s").replace("_", " "), name, items.keys())
            return owner, item
    return datablock(target), None


def name_of(block):
    """The target string for a data-block or object."""
    if isinstance(block, bpy.types.Object):
        return block.name
    for kind, attr in KINDS.items():
        data = getattr(bpy.data, attr, None)
        if data is not None and kind != "object":
            try:
                if data.get(block.name) == block:
                    return f"{kind}:{block.name}"
            except (TypeError, AttributeError):
                continue
    return getattr(block, "name", str(block))


def node_tree(owner, create=None):
    """The node tree an owner names. create "shader" / "geometry" makes one when it is missing."""
    if not isinstance(owner, str) or not owner.strip():
        raise ToolError("owner names a node tree: \"material:Oak\", \"world\", \"compositor\", "
                        "\"node_group:Name\" or \"Object/modifiers/GeometryNodes\".")
    owner = owner.strip()
    scene = bpy.context.scene
    if owner == "world":
        world = scene.world
        if world is None:
            if not create:
                raise ToolError("The scene has no world.")
            world = scene.world = bpy.data.worlds.new("World")
        # Reading never changes the file: nodes are switched on only when asked to create. (Blender 5.x keeps
        # use_nodes only for old files; a world there always has its tree.)
        if getattr(world, "use_nodes", True) is False:
            if not create:
                raise ToolError("The world does not use nodes. Pass create: \"shader\" to switch them on.")
            world.use_nodes = True
        return world.node_tree
    if owner == "compositor":
        tree = getattr(scene, "compositing_node_group", None)  # Blender 5.0+
        if tree is None and hasattr(scene, "compositing_node_group") and create:
            tree = bpy.data.node_groups.new("Compositing", "CompositorNodeTree")
            scene.compositing_node_group = tree
        if tree is None and hasattr(scene, "node_tree"):
            if not scene.use_nodes:
                scene.use_nodes = True
            tree = scene.node_tree
        if tree is None:
            raise ToolError("The compositor has no node tree yet. Pass create to make one.")
        return tree
    host, item = part(owner)
    if item is not None:
        if getattr(item, "type", "") != "NODES":
            raise ToolError(f"{owner} is not a Geometry Nodes modifier.")
        if item.node_group is None:
            if create != "geometry":
                raise ToolError(f"{owner} has no node group. Pass create: \"geometry\" to make one.")
            item.node_group = new_geometry_group(f"{host.name} Nodes")
        return item.node_group
    if isinstance(host, bpy.types.Material):
        if getattr(host, "use_nodes", True) is False:
            if not create:
                raise ToolError(f"{owner} does not use nodes. Pass create: \"shader\" to switch it on.")
            host.use_nodes = True
        return host.node_tree
    if isinstance(host, bpy.types.NodeTree):
        return host
    if isinstance(host, bpy.types.Object) and create == "geometry":
        modifier = host.modifiers.new("GeometryNodes", "NODES")
        modifier.node_group = new_geometry_group(f"{host.name} Nodes")
        return modifier.node_group
    raise ToolError(f"{owner} has no node tree.")


def new_geometry_group(name):
    """An empty Geometry Nodes group with the usual Geometry in and out, wired through."""
    group = bpy.data.node_groups.new(name, "GeometryNodeTree")
    group.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    group.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    inp = group.nodes.new("NodeGroupInput")
    out = group.nodes.new("NodeGroupOutput")
    inp.location = (-300, 0)
    out.location = (300, 0)
    group.links.new(inp.outputs[0], out.inputs[0])
    return group


# ─── Files ───────────────────────────────────────────────────────────────────────────────────────


def blend_folder():
    """The .blend's folder, or None for a file that was never saved."""
    path = bpy.data.filepath
    return os.path.dirname(path) if path else None


def file_path(path, must_exist=False, write=False):
    """An absolute path for a "//" path (or an attached picture), kept inside the .blend's folder.

    Reading also allows the add-on's attachment store (attached:<name>); nothing else outside the folder is
    touched — the agent works on the user's project, not their disk.
    """
    from . import attachments  # the store for files the user attached

    if not isinstance(path, str) or not path.strip():
        raise ToolError("A path is \"//folder/name.ext\", relative to the .blend.")
    path = path.strip()
    if path.startswith("attached:"):
        if write:
            raise ToolError("Attached files are read-only. Save to a \"//\" path instead.")
        found = attachments.path(path[len("attached:"):])
        if found is None:
            raise ToolError(f"There is no attached file {path[len('attached:'):]} — attachments last a week.")
        return found
    folder = blend_folder()
    if folder is None:
        raise ToolError(f"{path} needs the file saved first — ask the user to save it (or use file_op save).")
    if not path.startswith("//"):
        raise ToolError(f"Use a path relative to the .blend, starting \"//\" — not {path}.")
    full = os.path.normpath(bpy.path.abspath(path))
    root = os.path.normpath(folder)
    # realpath on both: a symlink inside the folder must not lead the agent outside it.
    real, real_root = os.path.realpath(full), os.path.realpath(root)
    if os.path.commonpath([full, root]) != root or os.path.commonpath([real, real_root]) != real_root:
        raise ToolError(f"{path} is outside the .blend's folder, which the agent does not touch.")
    if must_exist and not os.path.exists(full):
        raise ToolError(f"There is no file {path}.")
    return full


def relative(full):
    """"//…" for a path inside the .blend's folder, else the path as it is."""
    try:
        return bpy.path.relpath(full)
    except ValueError:
        return full


# Public name for the tool modules.
missing = _missing
