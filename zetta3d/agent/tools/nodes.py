"""The node tools: edit any node tree, make a material in one call, put a material on an object.

edit_nodes, create_material, assign_material (BlenderAgentProtocol.md › Client tools).

edit_nodes IS ALL OR NOTHING. The model sends up to 40 changes at once; if the twelfth names a socket that
does not exist, a half-edited tree with no undo step of its own would be worse than no change — the model would
resend the call and get "Image Texture.001". So every change is journalled as it is made and taken back when a
later one fails, and nodes asked to be removed are only parked (unlinked, renamed out of the way) until the whole
call has succeeded. (A tree the call had to create — a material's nodes, a new Geometry Nodes group — stays.)

SOCKETS ARE WRITTEN "Node.Socket", and both halves may contain dots ("Image Texture.001.Color"), so the split
is the first dot that leaves the name of a node in the tree and a socket that node has. A socket is found by
the name the node shows, or by its position among the sockets the node shows (Mix's "A" is a different socket
for floats and colours; the shown one is the one the user means).

COLOURS on sockets are linear, like every colour property in Blender; the agent writes sRGB "#rrggbb" and
values.convert makes it linear. Image textures holding colour (base colour, emission) are read as sRGB; those
holding data (roughness, metallic, alpha, normals) as Non-Color — the classic cause of washed-out normal maps.
"""

import difflib
import math

import bmesh
import bpy

from .. import targets, values
from . import Result, ToolError, tool
from .scene import _clean, _snapshot, alive, listing, put_material

MAX_CHANGES = 40
_OPS = ("add", "link", "set", "unlink", "remove")

# ─── Node types and sockets ──────────────────────────────────────────────────────────────────────


def _node_types():
    return [name for name in dir(bpy.types)
            if isinstance(getattr(bpy.types, name, None), type) and issubclass(getattr(bpy.types, name), bpy.types.Node)
            and name not in {"Node", "NodeInternal", "ShaderNode", "GeometryNode", "CompositorNode", "TextureNode",
                             "FunctionNode", "NodeCustomGroup"}]


def check_node_type(kind):
    """The node type identifier, or a ToolError naming the close ones."""
    if not isinstance(kind, str) or not kind.strip():
        raise ToolError("add needs type, a node type identifier like ShaderNodeTexImage.")
    kind = kind.strip()
    found = getattr(bpy.types, kind, None)
    if isinstance(found, type) and issubclass(found, bpy.types.Node):
        return kind
    close = difflib.get_close_matches(kind, _node_types(), n=5, cutoff=0.6)
    hint = f" Did you mean: {', '.join(close)}?" if close else " Look it up with api_lookup."
    raise ToolError(f"There is no node type {kind}.{hint}")


def shown(socket):
    """True for a socket the node shows: Mix's colour inputs are hidden while it mixes floats."""
    return getattr(socket, "enabled", True) and not getattr(socket, "is_unavailable", False)


def find_socket(node, key, output):
    """A socket of node by name, identifier or position among the shown ones; None when there is none."""
    sockets = node.outputs if output else node.inputs
    visible = [s for s in sockets if shown(s)]
    key = str(key).strip()
    if key.isdigit():
        index = int(key)
        return visible[index] if index < len(visible) else None
    for pool in (visible, list(sockets)):
        for socket in pool:
            if socket.name == key:
                return socket
    for socket in sockets:
        if socket.identifier == key:
            return socket
    lower = key.lower()
    for pool in (visible, list(sockets)):
        for socket in pool:
            if socket.name.lower() == lower:
                return socket
    return None


def socket_names(node, output):
    sockets = [s for s in (node.outputs if output else node.inputs) if shown(s)]
    return ", ".join(f"{i} {s.name}" for i, s in enumerate(sockets)) or "none"


def _degrees_to_radians(value):
    if isinstance(value, (list, tuple)):
        return [math.radians(v) for v in value]
    return math.radians(value)


def set_input(socket, value):
    """Sets an input socket's value, checked against its type. Returns a way to put the old value back."""
    if not hasattr(socket, "default_value"):
        raise ToolError(f"{socket.node.name}.{socket.name} is a {socket.type.lower()} socket and takes no value; "
                        "link something into it.")
    old = socket.default_value
    if not isinstance(old, (str, bpy.types.ID)) and old is not None:
        try:
            old = tuple(old)
        except TypeError:
            pass
    if socket.type == "ROTATION" and isinstance(value, (list, tuple, int, float)) and not isinstance(value, bool):
        value = _degrees_to_radians(value)  # the agent writes rotations in degrees
    socket.default_value = values.convert(socket, "default_value", value)

    def restore():
        socket.default_value = old
    return restore


# ─── edit_nodes ──────────────────────────────────────────────────────────────────────────────────


class _Edit:
    """One edit_nodes call on a tree: the changes so far, and how to take each back."""

    def __init__(self, tree):
        self.tree = tree
        self.journal = []  # functions that undo a change, newest last
        self.parked = []  # nodes to remove once the call succeeds
        self.lines = []

    # Lookups ignore parked nodes: within the call they are already gone.

    def find(self, name):
        node = self.tree.nodes.get(name)
        if node is not None and node not in self.parked:
            return node
        matches = [n for n in self.tree.nodes if n.label == name and n not in self.parked]
        return matches[0] if len(matches) == 1 else None

    def node(self, name):
        if not isinstance(name, str) or not name.strip():
            raise ToolError("node is the node's name (inspect_nodes lists them).")
        found = self.find(name.strip())
        if found is None:
            names = [n.name for n in self.tree.nodes if n not in self.parked]
            raise targets._missing("node", name.strip(), names)
        return found

    def socket(self, text, output):
        side = "output" if output else "input"
        if not isinstance(text, str) or "." not in text:
            raise ToolError(f'{"from" if output else "to"} is "Node.Socket", like "Principled BSDF.Base Color" '
                            'or "Mix.1".')
        candidates = []
        for i, ch in enumerate(text):
            if ch == ".":
                node = self.find(text[:i])
                if node is not None:
                    candidates.append((node, text[i + 1:]))
        if not candidates:
            names = [n.name for n in self.tree.nodes if n not in self.parked]
            head = text.split(".")[0]
            raise targets._missing("node", head, names)
        for node, key in candidates:
            found = find_socket(node, key, output)
            if found is not None:
                return found
        node, key = candidates[-1]
        raise ToolError(f'{node.name} has no {side} "{key}". Its {side}s: {socket_names(node, output)}.')

    # The changes

    def add(self, change):
        kind = change["type"].strip()
        try:
            node = self.tree.nodes.new(kind)
        except RuntimeError as error:
            raise ToolError(f"{kind} cannot go in this {self.tree.bl_idname}: {_clean(error)}.") from None
        self.journal.append(lambda: self.tree.nodes.remove(node))
        if change.get("name"):
            node.name = str(change["name"])
        if change.get("label"):
            node.label = str(change["label"])
        if change.get("location") is not None:
            node.location = values.vector(change["location"], "location", size=2)
        done = self._configure(node, change)
        taken = f' ("{change["name"]}" was taken)' if change.get("name") and node.name != change["name"] else ""
        self.lines.append(f"added {node.name} ({kind}){taken}" + (" with " + ", ".join(done) if done else ""))

    def set(self, change):
        node = self.node(change.get("node"))
        if not change.get("inputs") and not change.get("properties"):
            raise ToolError("set needs inputs or properties.")
        self.lines.append(f"set {node.name}: " + ", ".join(self._configure(node, change)))

    def _configure(self, node, change):
        properties = change.get("properties") or {}
        inputs = change.get("inputs") or {}
        if not isinstance(properties, dict) or not isinstance(inputs, dict):
            raise ToolError("inputs and properties are objects: {\"Roughness\": 0.4}, {\"data_type\": \"RGBA\"}.")
        done = []
        # Properties first: data_type, operation and the like decide which inputs exist.
        for path, value in properties.items():
            restore = _snapshot(node, path)
            now = values.assign(node, path, value)
            if restore is not None:
                self.journal.append(restore)
            done.append(f"{path} = {values.show(now)}")
        for key, value in inputs.items():
            socket = find_socket(node, key, output=False)
            if socket is None:
                raise ToolError(f'{node.name} has no input "{key}". Its inputs: {socket_names(node, False)}.')
            self.journal.append(set_input(socket, value))
            linked = " (linked, so the link wins)" if socket.is_linked else ""
            done.append(f"{socket.name} = {values.show(socket.default_value)}{linked}")
        return done

    def _relink(self, pairs):
        def restore():
            for out, inp in pairs:
                self.tree.links.new(out, inp)
        return restore

    def _drop(self, out, inp):
        def remove():
            for link in list(self.tree.links):
                if link.from_socket == out and link.to_socket == inp:
                    self.tree.links.remove(link)
        return remove

    def link(self, change):
        out = self.socket(change.get("from"), output=True)
        inp = self.socket(change.get("to"), output=False)
        if out.node == inp.node:
            raise ToolError(f"{out.node.name} cannot be linked to itself.")
        replaced = [] if inp.is_multi_input else [(l.from_socket, l.to_socket) for l in self.tree.links
                                                  if l.to_socket == inp]
        self.tree.links.new(out, inp)
        self.journal.append(self._relink(replaced))
        self.journal.append(self._drop(out, inp))
        was = f" (replacing {replaced[0][0].node.name}.{replaced[0][0].name})" if replaced and \
            replaced[0][0] != out else ""
        self.lines.append(f"linked {out.node.name}.{out.name} → {inp.node.name}.{inp.name}{was}")

    def unlink(self, change):
        inp = self.socket(change.get("to"), output=False)
        out = self.socket(change["from"], output=True) if change.get("from") else None
        found = [l for l in self.tree.links if l.to_socket == inp and (out is None or l.from_socket == out)]
        if not found:
            raise ToolError(f"Nothing is linked into {inp.node.name}.{inp.name}"
                            + (f" from {out.node.name}.{out.name}." if out else "."))
        pairs = [(l.from_socket, l.to_socket) for l in found]
        for link in found:
            self.tree.links.remove(link)
        self.journal.append(self._relink(pairs))
        self.lines.append("unlinked " + ", ".join(f"{a.node.name}.{a.name} → {b.node.name}.{b.name}"
                                                  for a, b in pairs))

    def remove(self, change):
        node = self.node(change.get("node"))
        pairs = [(l.from_socket, l.to_socket) for l in self.tree.links if l.from_node == node or l.to_node == node]
        for link in [l for l in self.tree.links if l.from_node == node or l.to_node == node]:
            self.tree.links.remove(link)
        self.journal.append(self._relink(pairs))
        name = node.name
        # Parked under another name, so a node added later in the call can take this name.
        node.name = f"zetta-removed {name}"

        def unpark():
            self.parked.remove(node)
            node.name = name
        self.journal.append(unpark)
        self.parked.append(node)
        self.lines.append(f"removed {name}")

    def rollback(self):
        for undo in reversed(self.journal):
            try:
                undo()
            except Exception:  # noqa: BLE001 — best effort; the error being reported matters more
                pass
        self.journal.clear()

    def commit(self):
        for node in self.parked:
            if alive(node):
                self.tree.nodes.remove(node)
        self.parked.clear()


def _check_change(index, change):
    """The static checks, before the tree is touched."""
    where = f"Change {index + 1}"
    if not isinstance(change, dict):
        raise ToolError(f"{where} is not an object.")
    op = change.get("op")
    if op not in _OPS:
        raise ToolError(f"{where}: op is one of {', '.join(_OPS)}.")
    if op == "add":
        try:
            check_node_type(change.get("type"))
        except ToolError as error:
            raise ToolError(f"{where} (add): {error}") from None
    elif op in {"set", "remove"} and not change.get("node"):
        raise ToolError(f"{where} ({op}) needs node, the node's name.")
    elif op == "link" and not (change.get("from") and change.get("to")):
        raise ToolError(f'{where} (link) needs from and to: "Node.Socket".')
    elif op == "unlink" and not change.get("to"):
        raise ToolError(f'{where} (unlink) needs to: "Node.Socket" (and from to remove only that link).')
    return op


# ─── Arranging ───────────────────────────────────────────────────────────────────────────────────


def _absolute(node):
    if hasattr(node, "location_absolute"):  # 4.4+
        return tuple(node.location_absolute)
    x, y = node.location
    parent = node.parent
    while parent is not None:
        x, y = x + parent.location.x, y + parent.location.y
        parent = parent.parent
    return x, y


def _place(node, x, y):
    """Sets where a node is in the editor, whatever frame it sits in (a framed node's location is relative)."""
    if hasattr(node, "location_absolute"):
        node.location_absolute = (x, y)
        return
    px = py = 0.0
    parent = node.parent
    while parent is not None:
        px, py = px + parent.location.x, py + parent.location.y
        parent = parent.parent
    node.location = (x - px, y - py)


def _height(node):
    """Background Blender never draws nodes, so their dimensions are 0: estimate from the shown sockets."""
    if node.dimensions[1] > 0:
        return node.dimensions[1]
    rows = sum(1 for s in node.outputs if shown(s)) + sum(1 for s in node.inputs if shown(s))
    return 60 + 24 * rows


def arrange(tree):
    """Lays the tree out left to right by link depth: what feeds the output sits left of it, in columns,
    each column ordered to follow the nodes it feeds so links cross as little as they can. Frames shrink
    around their nodes by themselves."""
    nodes = [n for n in tree.nodes if n.type != "FRAME"]
    if not nodes:
        return
    feeds = {n.name: set() for n in nodes}
    fed = {n.name: set() for n in nodes}
    for link in tree.links:
        a, b = link.from_node.name, link.to_node.name
        if a in feeds and b in feeds and a != b:
            feeds[a].add(b)
            fed[b].add(a)
    depth = {}

    def walk(name, seen=()):
        if name in depth:
            return depth[name]
        if name in seen:
            return 0
        depth[name] = value = 1 + max((walk(n, seen + (name,)) for n in feeds[name]), default=-1)
        return value

    for n in nodes:
        walk(n.name)
    deepest = max(depth.values())
    column = {}
    for n in nodes:
        lonely = not feeds[n.name] and not fed[n.name]
        column[n.name] = 0 if lonely and len(nodes) > 1 else deepest - depth[n.name]
    count = max(column.values()) + 1
    by_column = [[n for n in nodes if column[n.name] == c] for c in range(count)]
    widths = [max((n.width for n in col), default=140) for col in by_column]
    gap_x, gap_y = 80, 40
    xs = []
    x = -sum(widths) - gap_x * (count - 1)
    for width in widths:
        xs.append(x)
        x += width + gap_x
    placed = {}
    for c in reversed(range(count)):
        col = by_column[c]

        def key(node):
            targets_ = [placed[t] for t in feeds[node.name] if t in placed]
            return (-(sum(targets_) / len(targets_)) if targets_ else -_absolute(node)[1])
        col.sort(key=key)
        total = sum(_height(n) for n in col) + gap_y * (len(col) - 1)
        y = total / 2
        for node in col:
            _place(node, xs[c], y)
            placed[node.name] = y - _height(node) / 2
            y -= _height(node) + gap_y


def _summary(tree, limit=60):
    nodes = [n for n in tree.nodes]
    links = [f"{l.from_node.name}.{l.from_socket.name} → {l.to_node.name}.{l.to_socket.name}" for l in tree.links]
    text = f"Nodes ({len(nodes)}): " + listing([n.name for n in nodes], limit) + "."
    if links:
        text += f"\nLinks ({len(links)}): " + "; ".join(links[:limit]) + (
            f"; … {len(links) - limit} more" if len(links) > limit else "") + "."
    else:
        text += "\nNo links."
    return text


@tool("edit_nodes", undoable=True)
def edit_nodes(args, ctx):
    owner = args.get("owner")
    create = args.get("create")
    if create not in (None, "shader", "geometry"):
        raise ToolError('create is "shader" or "geometry".')
    changes = args.get("changes")
    if not isinstance(changes, list) or not changes:
        raise ToolError("changes is a list of 1 to 40 {op: add | link | set | unlink | remove, …}.")
    if len(changes) > MAX_CHANGES:
        raise ToolError(f"At most {MAX_CHANGES} changes per call; this one has {len(changes)}. Split it.")
    arrange_after = args.get("arrange", True)
    if not isinstance(arrange_after, bool):
        raise ToolError("arrange is true or false.")
    ops = [_check_change(i, change) for i, change in enumerate(changes)]

    tree = targets.node_tree(owner, create)
    edit = _Edit(tree)
    for index, (op, change) in enumerate(zip(ops, changes)):
        try:
            getattr(edit, op)(change)
        except ToolError as error:
            edit.rollback()
            raise ToolError(f"Change {index + 1} ({op}): {error} Nothing was changed.") from None
        except Exception as error:  # noqa: BLE001 — a Blender error mid-call must still leave the tree as it was
            edit.rollback()
            raise ToolError(f"Change {index + 1} ({op}) failed: {type(error).__name__}: {error}. "
                            "Nothing was changed.") from None
    edit.commit()
    if arrange_after:
        arrange(tree)
    text = f"{owner.strip()} — {len(edit.lines)} change{'s' if len(edit.lines) != 1 else ''}:\n" + \
        "\n".join(f"- {line}" for line in edit.lines) + "\n" + _summary(tree)
    if arrange_after:
        text += "\nArranged left to right."
    short = owner.strip().split(":", 1)[-1]
    return Result(text, label=f"Edited nodes of {short}")


# ─── create_material ─────────────────────────────────────────────────────────────────────────────

# The Principled BSDF's sockets: the 4.0+ names first, the older ones after (Principled v1, before 4.0).
_SOCKETS = {
    "base_color": ("Base Color",),
    "metallic": ("Metallic",),
    "roughness": ("Roughness",),
    "alpha": ("Alpha",),
    "transmission": ("Transmission Weight", "Transmission"),
    "emission": ("Emission Color", "Emission"),
    "emission_strength": ("Emission Strength",),
    "normal": ("Normal",),
}
_COLOUR_TEXTURES = {"base_color", "emission"}  # read as sRGB; the rest is data, Non-Color
_TEXTURE_KEYS = ("base_color", "roughness", "metallic", "normal", "emission", "alpha")


def principled_input(node, key):
    for name in _SOCKETS[key]:
        socket = node.inputs.get(name)
        if socket is not None:
            return socket
    raise ToolError(f"This Blender's Principled BSDF has no {_SOCKETS[key][0]} input.")


def _texture_source(key, text):
    """("image", Image) or ("file", absolute path) — checked before the material is made."""
    if not isinstance(text, str) or not text.strip():
        raise ToolError(f'textures.{key} is a "//" path or "image:Name".')
    text = text.strip()
    if text.startswith("image:"):
        return "image", targets.datablock(text)
    return "file", targets.file_path(text, must_exist=True)


def _load_image(source, colour):
    kind, value = source
    image = value if kind == "image" else bpy.data.images.load(value, check_existing=True)
    wanted = "sRGB" if colour else "Non-Color"
    try:
        if image.colorspace_settings.name != wanted:
            image.colorspace_settings.name = wanted
    except TypeError:
        pass  # a custom colour configuration without that name: Blender's guess stands
    return image


def _check_number(args, key, low, high=None):
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < low or (high is not None and value > high):
        raise ToolError(f"{key} is a number from {low}" + (f" to {high}." if high is not None else " up."))
    return float(value)


@tool("create_material", undoable=True)
def create_material(args, ctx):
    name = args.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ToolError("name is the new material's name.")
    name = name.strip()
    colours = {key: values.colour(args[key]) for key in ("base_color", "emission") if args.get(key) is not None}
    numbers = {key: _check_number(args, key, 0, 1) for key in ("metallic", "roughness", "alpha", "transmission")}
    numbers["emission_strength"] = _check_number(args, "emission_strength", 0)
    textures = args.get("textures") or {}
    if not isinstance(textures, dict):
        raise ToolError("textures is {socket: \"//path\" or \"image:Name\"}.")
    unknown = [k for k in textures if k not in _TEXTURE_KEYS]
    if unknown:
        raise ToolError(f"textures takes {', '.join(_TEXTURE_KEYS)}; not {', '.join(unknown)}.")
    sources = {key: _texture_source(key, value) for key, value in textures.items()}
    assign_to = targets.objects(args["assign_to"]) if args.get("assign_to") else []
    for obj in assign_to:
        if obj.data is None or not hasattr(obj.data, "materials"):
            raise ToolError(f"{obj.name} is a {obj.type.lower()}, which has no materials.")

    material = bpy.data.materials.new(name)
    images_before = set(bpy.data.images)
    try:
        if material.node_tree is None:
            material.use_nodes = True  # 4.x: a new material has no nodes until asked; 5.x always has them
        tree = material.node_tree
        bsdf = next((n for n in tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
        output = next((n for n in tree.nodes if n.type == "OUTPUT_MATERIAL"), None)
        if bsdf is None:
            bsdf = tree.nodes.new("ShaderNodeBsdfPrincipled")
        if output is None:
            output = tree.nodes.new("ShaderNodeOutputMaterial")
        if not output.inputs["Surface"].is_linked:
            tree.links.new(bsdf.outputs[0], output.inputs["Surface"])

        said = []
        for key, value in colours.items():
            principled_input(bsdf, key).default_value = value
            said.append(f"{key.replace('_', ' ')} {args[key]}")
        for key, value in numbers.items():
            if value is not None:
                principled_input(bsdf, key).default_value = value
                said.append(f"{key.replace('_', ' ')} {value:g}")
        # Emission without a strength would not glow: 4.0+ starts Emission Strength at 0.
        if ("emission" in colours or "emission" in sources) and numbers["emission_strength"] is None:
            principled_input(bsdf, "emission_strength").default_value = 1.0
            said.append("emission strength 1")

        wired = []
        for key in _TEXTURE_KEYS:
            if key not in sources:
                continue
            colour = key in _COLOUR_TEXTURES
            image = _load_image(sources[key], colour)
            texture = tree.nodes.new("ShaderNodeTexImage")
            texture.image = image
            texture.label = key.replace("_", " ").title()
            texture.name = f"{texture.label} Texture"
            if key == "normal":
                normal_map = tree.nodes.new("ShaderNodeNormalMap")
                tree.links.new(texture.outputs["Color"], normal_map.inputs["Color"])
                tree.links.new(normal_map.outputs["Normal"], principled_input(bsdf, "normal"))
                wired.append(f"normal ← {image.name} (Non-Color, through {normal_map.name})")
            else:
                tree.links.new(texture.outputs["Color"], principled_input(bsdf, key))
                wired.append(f"{key.replace('_', ' ')} ← {image.name} ({'sRGB' if colour else 'Non-Color'})")
        arrange(tree)
        slots = [f"{obj.name} (slot {put_material(obj, material)})" for obj in assign_to]
    except Exception:
        # Nothing half-made stays: the material, and any image this call loaded, go again.
        for image in set(bpy.data.images) - images_before:
            bpy.data.images.remove(image)
        bpy.data.materials.remove(material)
        raise

    lines = [f'Created material "{material.name}" (Principled BSDF)'
             + (f': {", ".join(said)}' if said else "") + "."]
    if material.name != name:
        lines.append(f'"{name}" was taken, so Blender named it "{material.name}".')
    if wired:
        lines.append("Textures: " + "; ".join(wired) + ".")
    if slots:
        lines.append("Assigned to " + listing(slots) + ".")
    lines.append(f'Target it as "material:{material.name}"; edit_nodes on it for anything more.')
    return Result("\n".join(lines), label=f"Created material {material.name}")


# ─── assign_material ─────────────────────────────────────────────────────────────────────────────


def _material(text):
    if not isinstance(text, str) or not text.strip():
        raise ToolError('material is "material:Name".')
    text = text.strip()
    if not text.startswith("material:") and text in bpy.data.materials:
        return bpy.data.materials[text]
    return targets.datablock(text if text.startswith("material:") else f"material:{text}")


def _faces_to_slot(obj, slot, only_selected):
    """Points faces at a slot. In Edit Mode the edit mesh is the real one (the mesh is rewritten from it on
    leaving), so it is changed through bmesh there. Returns how many faces changed."""
    mesh = obj.data
    if obj.mode == "EDIT":
        bm = bmesh.from_edit_mesh(mesh)
        faces = [f for f in bm.faces if f.select] if only_selected else list(bm.faces)
        for face in faces:
            face.material_index = slot
        bmesh.update_edit_mesh(mesh)
        return len(faces)
    mesh.polygons.foreach_set("material_index", [slot] * len(mesh.polygons))
    mesh.update()
    return len(mesh.polygons)


@tool("assign_material", undoable=True)
def assign_material(args, ctx):
    obj = targets.obj(args.get("target"))
    material = _material(args.get("material"))
    slot = args.get("slot")
    if slot is not None and (not isinstance(slot, int) or isinstance(slot, bool) or slot < 0):
        raise ToolError("slot is 0 or more.")
    all_faces = args.get("all_faces", True)
    if not isinstance(all_faces, bool):
        raise ToolError("all_faces is true or false.")
    if obj.data is None or not hasattr(obj.data, "materials"):
        raise ToolError(f"{obj.name} is a {obj.type.lower()}, which has no materials.")

    if not all_faces:
        if obj.type != "MESH" or obj.mode != "EDIT":
            raise ToolError(f"all_faces false puts it on the selected faces, which needs {obj.name} in Edit Mode "
                            "with faces selected (set_mode EDIT, then the user selects them).")
        bm = bmesh.from_edit_mesh(obj.data)
        if not any(f.select for f in bm.faces):
            raise ToolError(f"No faces of {obj.name} are selected.")
        if slot is None:
            existing = [i for i, s in enumerate(obj.material_slots) if s.material == material]
            slot = existing[0] if existing else len(obj.material_slots)
        used = put_material(obj, material, slot)
        count = _faces_to_slot(obj, used, only_selected=True)
        return Result(f"Put {material.name} in slot {used} of {obj.name} and on its {count} selected "
                      f"face{'s' if count != 1 else ''}.", label=f"Assigned {material.name} to {obj.name}")

    if slot is not None:
        used = put_material(obj, material, slot)
        note = f" (slot {slot} did not exist, so it went into a new slot {used})" if used != slot else ""
        return Result(f"Put {material.name} in slot {used} of {obj.name}{note}; the faces using that slot show it.",
                      label=f"Assigned {material.name} to {obj.name}")
    used = put_material(obj, material, 0)
    text = f"Put {material.name} in slot 0 of {obj.name}"
    if obj.type == "MESH" and len(obj.material_slots) > 1:
        count = _faces_to_slot(obj, 0, only_selected=False)
        text += f" and pointed all {count} faces at it (the other slots stay, unused)"
    return Result(text + ".", label=f"Assigned {material.name} to {obj.name}")
