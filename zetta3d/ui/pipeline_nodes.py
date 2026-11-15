"""3D Pipelines as nodes, in Blender's own Node Editor (the "Zetta 3D Pipeline" editor).

A pipeline is a graph of steps, as on zetta3d.ai and in the Unity plugin: each step is a node, its inputs on the
left and outputs on the right, coloured by what they carry (a picture, pictures of each side, a model, a file).
Blender's node editor gives the rest the way Blender users know it: drag, wire, Shift+A to add, X to delete,
box select, zoom and pan; the step's settings are in the editor's sidebar (N › Pipeline).

THE RECIPE STAYS THE SOURCE OF TRUTH (`pipelines.state.editor["graph"]`): it is what is saved, what the pipeline's
own Undo walks back, and what Describe it and the 3D View's sidebar change. The node tree is its view:
- every change on the canvas — a node added, deleted or duplicated, a wire drawn or removed, a node moved — is
  turned into the same edit the sidebar would make (graph.add_step, connect, …) and committed, so it is checked
  by the same rules (a wire that carries the wrong thing is refused with the reason, a second seed too);
- every change to the recipe — from anywhere — redraws the canvas from it. Graph changes are new objects, so a
  quarter-second check of "is it the same object?" notices them without touching the state module.

A RUN can be shown on the same canvas: while one is open (Runs), the canvas shows the graph it ran with, each
node coloured by what it is doing, and the sidebar has that step's results. A run is not edited there.

The tree ("Zetta 3D Pipeline") is taken out of the file while it saves and put back afterwards — the pipeline lives
on zetta3d.ai, not in the .blend.
"""

import copy
import textwrap

import bpy
from bpy.props import StringProperty

from ..pipelines import graph as g
from ..pipelines import state as pipes
from ..studio import context as studio

TREE = "ZettaPipelineTree"
TREE_NAME = "Zetta 3D Pipeline"
CATEGORY = "Pipeline"
NODE_WIDTH = 250
SUMMARY_CHARS = 38

PORT_COLOURS = {  # the web editor's (node-visuals.ts), so a wire looks the same everywhere
    g.PICTURE: "#e57cf5",
    g.VIEWS: "#f8c6ff",
    g.MESH: "#5ea4ff",
    g.SPLAT: "#42d3ba",
    g.FILE: "#a8de52",
}
GROUP_COLOURS = {  # the plates of each area's hue on the web — dark enough for Blender's node text
    "Seed": "#3a1640",
    "Picture": "#2a2152",
    "Make": "#14295a",
    "Refine": "#103a37",
    "Output": "#3d2b0e",
}
STATUS_COLOURS = {
    "running": "#1d3f86",
    "success": "#1d4a2a",
    "failed": "#5a1d1d",
    "waiting": "#5a4012",
    "review": "#5a4012",
    "cancelled": "#333333",
}
STATUS_TEXT = {"running": ("Running", "TIME"), "success": ("Done", "CHECKMARK"), "failed": ("Failed", "ERROR"),
               "waiting": ("Waiting for you", "PAUSE"), "review": ("Needs your review", "QUESTION"),
               "cancelled": ("Cancelled", "CANCEL"), "idle": ("Not reached", "DOT")}
ICONS = {
    "seed_picture": "IMAGE_DATA", "seed_model": "OBJECT_DATA", "seed_views": "IMAGE_REFERENCE",
    "remove_background": "IMAGE_RGB_ALPHA", "ai_edit": "GREASEPENCIL", "draw_views": "CAMERA_DATA",
    "isolate_parts": "MOD_EXPLODE", "generate_3d": "MESH_ICOSPHERE", "generate_smart_3d": "MESH_ICOSPHERE",
    "generate_3d_multiview": "MESH_ICOSPHERE", "generate_smart_3d_multiview": "MESH_ICOSPHERE",
    "generate_splat": "OUTLINER_OB_POINTCLOUD", "retopology": "MOD_REMESH", "segmentation": "MOD_EDGESPLIT",
    "smart_segmentation": "MOD_EDGESPLIT", "texture": "TEXTURE", "texture_multiview": "TEXTURE",
    "smart_uv": "UV", "rigging": "ARMATURE_DATA", "export": "EXPORT",
}

view = {"message": None, "seen": None, "pending": False, "undoing": False, "saved_spaces": []}
_cache = {"graph": None, "problems": {}, "fan_out": set()}


def _rgba(hex_colour, alpha=1.0):
    value = hex_colour.lstrip("#")
    srgb = [int(value[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    return (*srgb, alpha)


def _rgb_linear(hex_colour):
    """Node colours are set as linear values; the web's are sRGB."""
    def lin(c):
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    return tuple(lin(c) for c in _rgba(hex_colour)[:3])


# ─── What the canvas shows ───────────────────────────────────────────────────────────────────────


def shown_run():
    return pipes.run_view["run"] if pipes.is_open() else None


def shown_graph():
    """The graph on the canvas: the open run's, or the recipe."""
    run = shown_run()
    if run is not None:
        return run.get("graph") or g.empty()
    return pipes.editor["graph"] if pipes.is_open() else None


def _problems(graph):
    if _cache["graph"] is not graph:
        by_node = {}
        if graph is not None and shown_run() is None:
            for node_id, message in g.problems(graph, pipes.entitlements()):
                by_node.setdefault(node_id, []).append(message)
        _cache.update(graph=graph, problems=by_node, fan_out=g.fan_out_nodes(graph) if graph else set())
    return _cache["problems"]


def get_tree(create=False):
    tree = bpy.data.node_groups.get(TREE_NAME)
    if tree is not None and tree.bl_idname != TREE:
        tree = None
    if tree is None and create:
        tree = bpy.data.node_groups.new(TREE_NAME, TREE)
        tree.use_fake_user = False
    return tree


# ─── Sockets ─────────────────────────────────────────────────────────────────────────────────────


def _socket_class(port):
    colour = _rgba(PORT_COLOURS[port])

    def draw(self, _context, layout, _node, text):
        layout.label(text=text)

    def draw_color(self, _context, _node):
        return colour

    return type(f"ZETTA_NS_pipe_{port}", (bpy.types.NodeSocket,), {
        "bl_idname": f"ZettaPipeSocket_{port}",
        "bl_label": g.PORT_LABELS[port],
        "draw": draw,
        "draw_color": draw_color,
    })


SOCKETS = {port: _socket_class(port) for port in PORT_COLOURS}


def _socket_idname(port):
    return f"ZettaPipeSocket_{port}" if port in SOCKETS else "ZettaPipeSocket_picture"


# ─── The tree ────────────────────────────────────────────────────────────────────────────────────


class ZETTA_NT_pipeline(bpy.types.NodeTree):
    """A Zetta 3D pipeline: the steps a picture or a model goes through, each run"""

    bl_idname = TREE
    bl_label = "Zetta 3D Pipeline"
    bl_icon = "NODETREE"

    def update(self):
        # Blender calls this after a node or a wire is added or removed. Changing the tree from inside it is
        # not safe, so the edit is read a moment later.
        if view["undoing"] or _syncing[0]:
            return
        if not view["pending"]:
            view["pending"] = True
            bpy.app.timers.register(_apply_canvas, first_interval=0.0)


_syncing = [False]


# ─── The nodes ───────────────────────────────────────────────────────────────────────────────────


def _graph_node(node):
    graph = shown_graph()
    return g.node(graph, node.step_id) if graph is not None and node.step_id else None


class _StepNode:
    step_id: StringProperty(options={"HIDDEN"})
    copied_from: StringProperty(options={"HIDDEN"})
    zetta_kind = ""

    @classmethod
    def poll(cls, ntree):
        return ntree.bl_idname == TREE

    def init(self, _context):
        self.width = NODE_WIDTH
        _ensure_sockets(self, {"kind": self.zetta_kind, "settings": {}}, None)
        _colour(self, {"kind": self.zetta_kind}, None)

    def copy(self, node):
        # Shift+D: a new step with the same settings — it gets its own id when the canvas is read.
        self.copied_from = node.step_id
        self.step_id = ""

    def draw_label(self):
        n = _graph_node(self)
        return g.node_name(n) if n else g.label(self.zetta_kind or "Step")

    def draw_buttons(self, _context, layout):
        n = _graph_node(self)
        if n is None:
            layout.label(text=g.label(self.zetta_kind), icon=ICONS.get(self.zetta_kind, "NODE"))
            return
        _draw_body(layout, n)

    def draw_buttons_ext(self, context, layout):
        draw_step_panel(context, layout)


def _node_class(kind):
    name = f"ZETTA_PipeNode_{kind}"
    return type(name, (_StepNode, bpy.types.Node), {
        "bl_idname": f"ZettaPipeNode_{kind}",
        "bl_label": g.label(kind),
        "bl_icon": ICONS.get(kind, "NODE"),
        "zetta_kind": kind,
        "__doc__": g.blurb(kind) or g.label(kind),
        "__annotations__": dict(_StepNode.__annotations__),
    })


class ZETTA_PipeNode_other(_StepNode, bpy.types.Node):
    """A step this version of the add-on does not know — kept as it is"""

    bl_idname = "ZettaPipeNode_other"
    bl_label = "Other step"
    bl_icon = "QUESTION"

    @classmethod
    def poll(cls, ntree):
        return False  # never offered in Add; made only for steps the recipe already has


NODE_CLASSES = {kind: _node_class(kind) for kind in g.KINDS}


def _idname(kind):
    return f"ZettaPipeNode_{kind}" if kind in NODE_CLASSES else "ZettaPipeNode_other"


def _ensure_sockets(node, n, graph):
    """The node's sockets match the step: its inputs, and its outputs (Isolate parts adds one per part)."""
    kind = n.get("kind")
    wanted_in = [(handle, accepts[0] if accepts else g.PICTURE, "")
                 for handle, (accepts, _required) in g.inputs(kind).items()]
    wanted_out = [(handle, port) for handle, port in g.outputs(n).items()]
    if not g.known(kind) and graph is not None:  # unknown kinds: whatever their wires use
        wanted_in = sorted({(e.get("targetHandle") or "in", g.PICTURE, "") for e in graph.get("edges") or []
                            if e.get("target") == n.get("id")})
        wanted_out = sorted({(e.get("sourceHandle") or "out", g.PICTURE) for e in graph.get("edges") or []
                             if e.get("source") == n.get("id")})
    for sockets, wanted, is_input in ((node.inputs, wanted_in, True), (node.outputs, wanted_out, False)):
        keys = [w[0] for w in wanted]
        for socket in list(sockets):
            match = next((w for w in wanted if w[0] == socket.identifier), None)
            if match is None or socket.bl_idname != _socket_idname(match[1]):
                sockets.remove(socket)
        for index, entry in enumerate(wanted):
            handle, port = entry[0], entry[1]
            existing = next((s for s in sockets if s.identifier == handle), None)
            name = _socket_name(n, handle, port, is_input)
            if existing is None:
                existing = sockets.new(_socket_idname(port), name, identifier=handle)
            elif existing.name != name:
                existing.name = name
        # Keep the catalogue's order.
        for index, handle in enumerate(keys):
            current = next((i for i, s in enumerate(sockets) if s.identifier == handle), None)
            if current is not None and current != index:
                sockets.move(current, index)


def _socket_name(n, handle, port, is_input):
    if is_input:
        accepts = g.inputs(n.get("kind")).get(handle, ((port,), True))[0]
        return " or ".join(g.PORT_LABELS.get(a, a) for a in accepts) if handle == "in" else \
            g.PORT_LABELS.get(port, handle).replace("Views", "Pictures of each side")
    return g.output_label(n, handle)


def _colour(node, n, run):
    if run is not None:
        status, _steps = pipes.node_state(run, n.get("id")) if n.get("id") else ("idle", [])
        hex_colour = STATUS_COLOURS.get(status)
    else:
        hex_colour = GROUP_COLOURS.get(g.group(n.get("kind")))
    use = hex_colour is not None
    if node.use_custom_color != use:  # only when it changes: every write tells Blender the tree changed
        node.use_custom_color = use
    if use:
        colour = _rgb_linear(hex_colour)
        if any(abs(a - b) > 1e-4 for a, b in zip(node.color, colour)):
            node.color = colour


def _draw_body(layout, n):
    """What a step shows on the canvas: what it is set to and costs — or, in a run, what it is doing."""
    run = shown_run()
    kind = n.get("kind")
    col = layout.column(align=True)
    if not g.in_scope(n):
        col.label(text="Not available in Blender", icon="INFO")
        col.label(text="Kept as it is — open it on zetta3d.ai")
        return
    if run is not None:
        status, steps = pipes.node_state(run, n["id"])
        text, icon = STATUS_TEXT.get(status, (status, "DOT"))
        if len(steps) > 1:
            done = sum(1 for s in steps if s.get("status") == "success")
            text += f" · {done} of {len(steps)}"
        col.label(text=text, icon=icon)
        credits = sum(int(s.get("credits") or 0) for s in steps)
        if credits:
            col.label(text=f"{credits:,} credits", icon="FUND")
        failed = next((s for s in steps if s.get("status") == "failed" and s.get("errorMessage")), None)
        if failed:
            for line in textwrap.wrap(failed["errorMessage"], SUMMARY_CHARS)[:2]:
                sub = col.row()
                sub.alert = True
                sub.label(text=line)
        return
    graph = pipes.editor["graph"]
    summary = g.summary(n)
    lines = textwrap.wrap(summary, SUMMARY_CHARS)[:2] or [""]
    col.label(text=lines[0], icon=ICONS.get(kind, "DOT"))
    for line in lines[1:]:
        col.label(text=line, icon="BLANK1")
    price = g.price_for(graph, n, studio.prices, pipes.entitlements())
    if price is not None or not g.is_seed(kind):
        col.label(text=g.price_label(price, n["id"] in _cache["fan_out"]), icon="FUND")
    problems = _problems(graph).get(n["id"]) or []
    if problems:
        for line in textwrap.wrap(problems[0], SUMMARY_CHARS)[:2]:
            sub = col.row()
            sub.alert = True
            sub.label(text=line, icon="ERROR" if line == textwrap.wrap(problems[0], SUMMARY_CHARS)[0] else "BLANK1")


# ─── Recipe → canvas ─────────────────────────────────────────────────────────────────────────────


def refresh():
    """Redraws the canvas from what it shows (the recipe or the open run)."""
    graph = shown_graph()
    tree = get_tree(create=graph is not None)
    if tree is None:
        return
    _syncing[0] = True
    try:
        _build(tree, graph or g.empty(), shown_run())
    finally:
        _syncing[0] = False
    view["seen"] = _fingerprint()


def _socket(sockets, handle):
    return next((s for s in sockets if s.identifier == handle), None)


def _build(tree, graph, run):
    _problems(graph)
    ours = {}
    for node in list(tree.nodes):
        step_id = getattr(node, "step_id", None)
        if step_id is None or not step_id or step_id in ours or g.node(graph, step_id) is None:
            tree.nodes.remove(node)  # frames, reroutes and stray copies don't map to a step
            continue
        ours[step_id] = node
    for n in graph.get("nodes") or []:
        node = ours.get(n["id"])
        idname = _idname(n.get("kind"))
        if node is not None and node.bl_idname != idname:
            tree.nodes.remove(node)
            node = None
        if node is None:
            node = tree.nodes.new(idname)
            node.step_id = n["id"]
            node.width = NODE_WIDTH
            node.select = False  # Blender selects every new node; a redrawn canvas starts with none
            ours[n["id"]] = node
        position = n.get("position") or {}
        location = (float(position.get("x", 0)), -float(position.get("y", 0)))
        if abs(node.location[0] - location[0]) > 0.5 or abs(node.location[1] - location[1]) > 0.5:
            node.location = location
        _ensure_sockets(node, n, graph)
        _colour(node, n, run)
    wanted = set()
    for edge in graph.get("edges") or []:
        wanted.add((edge.get("source"), edge.get("sourceHandle") or "out", edge.get("target"),
                    edge.get("targetHandle") or "in"))
    for link in list(tree.links):
        key = (getattr(link.from_node, "step_id", None), link.from_socket.identifier,
               getattr(link.to_node, "step_id", None), link.to_socket.identifier)
        if key in wanted:
            wanted.discard(key)
        else:
            tree.links.remove(link)
    for source, source_handle, target, target_handle in wanted:
        a, b = ours.get(source), ours.get(target)
        if a is None or b is None:
            continue
        out, into = _socket(a.outputs, source_handle), _socket(b.inputs, target_handle)
        if out is not None and into is not None:
            tree.links.new(out, into)
    selected = (pipes.run_view["selected"] if run is not None else pipes.editor["selected"])
    if selected and selected in ours and tree.nodes.active is not ours[selected]:
        # Chosen elsewhere (the 3D View's sidebar, a run's step list): selected on the canvas too.
        for node in tree.nodes:
            if node.select:
                node.select = False
        ours[selected].select = True
        tree.nodes.active = ours[selected]


# ─── Canvas → recipe ─────────────────────────────────────────────────────────────────────────────


def _link_key(link):
    return (getattr(link.from_node, "step_id", ""), link.from_socket.identifier,
            getattr(link.to_node, "step_id", ""), link.to_socket.identifier)


def _apply_canvas():
    """Turns what changed on the canvas into recipe edits — the same ones the sidebar makes."""
    view["pending"] = False
    tree = get_tree()
    if tree is None or not pipes.is_open():
        return None
    if shown_run() is not None:
        view["message"] = "This is a run: it shows what it ran. Back to the recipe (Runs › All runs) to change it."
        refresh()
        return None
    graph = pipes.editor["graph"]
    entitlements = pipes.entitlements()
    new = graph
    messages = []
    in_tree = {}
    _syncing[0] = True
    try:
        for node in list(tree.nodes):
            step_id = getattr(node, "step_id", None)
            if step_id is None:
                continue
            if step_id and g.node(new, step_id) is not None and step_id not in in_tree:
                in_tree[step_id] = node
                continue
            kind = getattr(node, "zetta_kind", "")
            source = g.node(new, node.copied_from) if node.copied_from else None
            try:
                new, new_id = g.add_step(new, kind, entitlements=entitlements)
                if source is not None and g.in_scope(source):
                    new = g.patch_settings(new, new_id, copy.deepcopy(g.settings_of(source)), entitlements)
            except ValueError as error:
                messages.append(str(error))
                continue
            node.step_id, node.copied_from = new_id, ""
            new = copy.deepcopy(new)
            added = g.node(new, new_id)
            added["position"] = {"x": round(node.location[0]), "y": round(-node.location[1])}
            in_tree[new_id] = node
        for n in list(new.get("nodes") or []):
            if n["id"] not in in_tree:
                new = g.remove_step(new, n["id"], entitlements)
        on_canvas = {_link_key(link) for link in tree.links
                     if link.from_node.bl_idname.startswith("ZettaPipeNode_")
                     and link.to_node.bl_idname.startswith("ZettaPipeNode_")}
        in_graph = {(e.get("source"), e.get("sourceHandle") or "out", e.get("target"), e.get("targetHandle") or "in")
                    for e in new.get("edges") or []}
        # A wire dropped on an input that had one replaces it on the canvas. When the new wire is refused, the
        # input keeps the wire it had.
        refused = set()
        for key in on_canvas - in_graph:
            reason = g.connection_problem(new, *key)
            if reason:
                messages.append(reason)
                refused.add((key[2], key[3]))
        for source, source_handle, target, target_handle in in_graph - on_canvas:
            if (target, target_handle) not in refused:
                new = g.disconnect(new, target, target_handle, entitlements)
        for source, source_handle, target, target_handle in on_canvas - in_graph:
            if (target, target_handle) in refused:
                continue
            try:
                new = g.connect(new, source, source_handle, target, target_handle, entitlements)
            except ValueError as error:
                messages.append(str(error))
    finally:
        _syncing[0] = False
    view["message"] = messages[-1] if messages else None
    if not g.same(new, graph):
        pipes.commit(new)
    refresh()  # also drops what was refused (a wrong wire, a second seed)
    _redraw()
    return None


def _read_positions():
    """Nodes moved on the canvas move in the recipe (one undo step for a drag)."""
    tree = get_tree()
    graph = pipes.editor["graph"]
    if tree is None or graph is None or shown_run() is not None:
        return
    moved = {}
    for node in tree.nodes:
        n = g.node(graph, getattr(node, "step_id", "") or "")
        if n is None:
            continue
        position = n.get("position") or {}
        x, y = round(node.location[0]), round(-node.location[1])
        if abs(position.get("x", 0) - x) > 1 or abs(position.get("y", 0) - y) > 1:
            moved[n["id"]] = {"x": x, "y": y}
    if moved:
        new = copy.deepcopy(graph)
        for n in new["nodes"]:
            if n["id"] in moved:
                n["position"] = moved[n["id"]]
        pipes.commit(new, coalesce="move")
        view["seen"] = _fingerprint()


def _read_selection():
    tree = get_tree()
    if tree is None:
        return
    active = tree.nodes.active
    step_id = getattr(active, "step_id", "") if active is not None and active.select else ""
    target = pipes.run_view if shown_run() is not None else pipes.editor
    if step_id and target["selected"] != step_id:
        target["selected"] = step_id
        view["seen"] = _fingerprint()
        _redraw()


def _fingerprint():
    return (id(pipes.editor["graph"]), id(pipes.run_view["run"]), pipes.editor["selected"],
            pipes.run_view["selected"], pipes.is_open())


def _assign_spaces(tree):
    for window in bpy.context.window_manager.windows:
        screen = window.screen
        if screen is None:
            continue
        for area in screen.areas:
            if area.type == "NODE_EDITOR":
                space = area.spaces.active
                if space.tree_type == TREE and space.node_tree is not tree:
                    space.node_tree = tree


def _editor_shown():
    for window in bpy.context.window_manager.windows:
        screen = window.screen
        if screen is None:
            continue
        for area in screen.areas:
            if area.type == "NODE_EDITOR" and area.spaces.active.tree_type == TREE:
                return True
    return False


def _redraw():
    for window in bpy.context.window_manager.windows:
        screen = window.screen
        if screen is None:
            continue
        for area in screen.areas:
            if area.type == "NODE_EDITOR":
                area.tag_redraw()


def _tick():
    """Every quarter second while Blender runs: keeps the canvas and the recipe in step."""
    try:
        if view["undoing"] or not _editor_shown():
            return 0.5
        tree = get_tree(create=pipes.is_open())
        if tree is not None:
            _assign_spaces(tree)
            if view["seen"] != _fingerprint():
                refresh()
                _redraw()
            elif not view["pending"]:
                _read_positions()
                _read_selection()
    except (ReferenceError, RuntimeError, AttributeError):
        pass
    return 0.25


# ─── Opening the editor ──────────────────────────────────────────────────────────────────────────


def open_editor(context):
    """Shows the open pipeline in a Node Editor: one already showing it, or a new window."""
    tree = get_tree(create=True)
    refresh()
    wm = context.window_manager
    for window in wm.windows:
        for area in window.screen.areas:
            if area.type == "NODE_EDITOR" and area.spaces.active.tree_type == TREE:
                area.spaces.active.node_tree = tree
                return
    bpy.ops.wm.window_new()
    window = wm.windows[-1]
    area = max(window.screen.areas, key=lambda a: a.width * a.height)
    show_in(area, tree)
    region = next((r for r in area.regions if r.type == "WINDOW"), None)
    if region is not None:
        def frame():
            try:
                with bpy.context.temp_override(window=window, area=area, region=region):
                    bpy.ops.node.view_all()
            except (RuntimeError, ReferenceError):
                pass
            return None
        bpy.app.timers.register(frame, first_interval=0.2)


def show_in(area, tree):
    """Turns an area into the pipeline editor, its sidebar open on the Pipeline tab."""
    area.ui_type = TREE
    space = area.spaces.active
    space.node_tree = tree
    space.show_region_ui = True
    def pick_tab():
        # After the sidebar has drawn once — before that it has no tabs to pick from. Settable from Blender 5.x;
        # before, the sidebar keeps its tab.
        try:
            for region in area.regions:
                if region.type == "UI" and region.active_panel_category != CATEGORY:
                    region.active_panel_category = CATEGORY
                    area.tag_redraw()
        except (AttributeError, TypeError, ValueError, ReferenceError):
            pass
        return None

    area.tag_redraw()
    bpy.app.timers.register(pick_tab, first_interval=0.3)


class ZETTA_OT_pipeline_nodes(bpy.types.Operator):
    """Edit this pipeline as nodes in Blender's Node Editor"""

    bl_idname = "zetta.pipeline_nodes"
    bl_label = "Edit as nodes"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, _context):
        return pipes.is_open()

    def execute(self, context):
        open_editor(context)
        return {"FINISHED"}


def _ours(context):
    space = getattr(context, "space_data", None)
    return space is not None and getattr(space, "tree_type", None) == TREE


class ZETTA_OT_pipeline_undo(bpy.types.Operator):
    """Undo the last change to the pipeline"""

    bl_idname = "zetta.pipeline_canvas_undo"
    bl_label = "Undo pipeline change"
    bl_options = {"INTERNAL"}

    redo: bpy.props.BoolProperty(options={"SKIP_SAVE"})

    @classmethod
    def poll(cls, context):
        return _ours(context) and pipes.is_open() and shown_run() is None

    def execute(self, _context):
        pipes.redo() if self.redo else pipes.undo()
        refresh()
        _redraw()
        return {"FINISHED"}


# ─── The editor's sidebar (N › Pipeline) and header ──────────────────────────────────────────────


class _PipePanel:
    bl_space_type = "NODE_EDITOR"
    bl_region_type = "UI"
    bl_category = CATEGORY

    @classmethod
    def poll(cls, context):
        return _ours(context)


class ZETTA_PT_pipe_nodes(_PipePanel, bpy.types.Panel):
    bl_label = "Pipeline"
    bl_idname = "ZETTA_PT_pipe_nodes"

    def draw(self, context):
        from . import panels

        layout = self.layout
        zetta = context.window_manager.zetta
        if pipes.editor["loading"]:
            layout.label(text="Opening…", icon="TIME")
            return
        if not pipes.is_open():
            panels.draw_pipeline_list(layout)
            return
        pipeline = pipes.editor["pipeline"]
        row = layout.row(align=True)
        panels.btn(row, "pipeline_close", "Pipelines", "BACK")
        panels.btn(row, "open_site", "", "URL", arg=f"/pipelines?pipeline={pipeline['id']}", hint="Open on zetta3d.ai")
        layout.prop(zetta, "pipe_title", text="")
        if shown_run() is not None:
            panels.wrap(layout, "Showing a run: each step is coloured by what it did. Runs › All runs goes back to "
                                "the recipe.", "INFO")
            return
        graph = pipes.editor["graph"] or g.empty()
        row = layout.row(align=True)
        sub = row.row(align=True)
        sub.enabled = bool(pipes.editor["undo"])
        panels.btn(sub, "pipeline_undo", "", "LOOP_BACK", hint="Undo (Ctrl+Z over the canvas)")
        sub = row.row(align=True)
        sub.enabled = bool(pipes.editor["redo"])
        panels.btn(sub, "pipeline_redo", "", "LOOP_FORWARDS", hint="Redo")
        row.label(text=pipes.save_status())
        panels.btn(row, "pipeline_save", "", "FILE_TICK", hint="Save now")
        if view["message"]:
            panels.wrap(layout, view["message"], "ERROR", alert=True)
        if pipes.editor["error"]:
            panels.wrap(layout, pipes.editor["error"], "ERROR", alert=True)

        box = layout.box()
        box.label(text="Describe it", icon="OUTLINER_DATA_LIGHTPROBE")
        panels.text_prop(box, zetta, "pipe_describe", "Change the recipe — e.g. add AI texturing at the end"
                         if pipes.editing() else "e.g. a game-ready character from a photo, rigged")
        row = box.row()
        row.enabled = not pipes.editor["describe_busy"] and len(zetta.pipe_describe.strip()) >= 3
        label = ("Changing your recipe…" if pipes.editing() else "Designing your recipe…") if pipes.editor[
            "describe_busy"] else ("Change it" if pipes.editing() else "Build it")
        panels.btn(row, "pipeline_describe", label, "PLAY")
        if pipes.editor["describe_note"]:
            panels.wrap(box, pipes.editor["describe_note"], "CHECKMARK")
        if pipes.editor["describe_error"]:
            panels.wrap(box, pipes.editor["describe_error"], "ERROR", alert=True)

        problems = pipes.recipe_problems()
        box = layout.box()
        box.label(text=g.estimate_label(graph, studio.prices, pipes.entitlements()), icon="FUND")
        if not graph.get("nodes"):
            panels.wrap(box, "Shift+A › Seed adds what each run starts from: a picture, a model, or pictures of each "
                             "side. Then add steps and wire them from left to right.", "INFO")
        elif problems:
            count = len(problems)
            panels.wrap(box, f"{count} thing{'s' if count != 1 else ''} to finish before it can run — "
                             f"{problems[0][1]}", "INFO")
        row = box.row()
        row.scale_y = 1.4
        row.enabled = not problems and g.seed(graph) is not None
        panels.btn(row, "pipeline_new_run", "New run…", "PLAY",
                   hint="Choose the pictures or models to run on — one run each")
        if pipes.new_run["open"]:
            panels.draw_new_run(layout, zetta, graph)


def draw_step_panel(context, layout):
    from . import panels

    graph = shown_graph()
    run = shown_run()
    selected = pipes.run_view["selected"] if run is not None else pipes.editor["selected"]
    n = g.node(graph, selected) if graph is not None and selected else None
    if n is None:
        panels.wrap(layout, "Select a step on the canvas to see its settings.", "INFO")
        return
    row = layout.row()
    row.label(text=g.node_name(n), icon=ICONS.get(n.get("kind"), "NODE"))
    if run is not None:
        _status, steps = pipes.node_state(run, n["id"])
        panels.draw_run_step(layout, run, n, steps)
        return
    problems = _problems(graph).get(n["id"]) or []
    panels.draw_step_settings(layout, graph, n, problems)


class ZETTA_PT_pipe_nodes_step(_PipePanel, bpy.types.Panel):
    bl_label = "Step"
    bl_idname = "ZETTA_PT_pipe_nodes_step"

    @classmethod
    def poll(cls, context):
        return _ours(context) and pipes.is_open()

    def draw(self, context):
        draw_step_panel(context, self.layout)


class ZETTA_PT_pipe_nodes_runs(_PipePanel, bpy.types.Panel):
    bl_label = "Runs"
    bl_idname = "ZETTA_PT_pipe_nodes_runs"

    @classmethod
    def poll(cls, context):
        return _ours(context) and pipes.is_open()

    def draw(self, context):
        from . import panels

        run = shown_run()
        if run is not None:
            row = self.layout.row(align=True)
            panels.btn(row, "run_close", "Back to the recipe", "BACK")
            panels.draw_run(self.layout, context.window_manager.zetta, run)
            return
        panels.draw_runs(self.layout, context.window_manager.zetta)


def _header(self, context):
    if not _ours(context):
        return
    layout = self.layout
    if not pipes.is_open():
        layout.label(text="Open a pipeline in the sidebar (N › Pipeline)", icon="INFO")
        return
    run = shown_run()
    title = (pipes.editor["pipeline"] or {}).get("title") or pipes.UNTITLED
    if run is not None:
        layout.label(text=f"{title} · Run #{run.get('id')} · {pipes.RUN_LABELS.get(run.get('status'), '')}",
                     icon="PLAY")
        op = layout.operator("zetta.do", text="Back to the recipe", icon="BACK")
        op.action = "run_close"
    else:
        layout.label(text=f"{title} · {pipes.save_status()}", icon="NODETREE")


# ─── Shift+A ─────────────────────────────────────────────────────────────────────────────────────


def _add_menu_class(group):
    def draw(self, _context):
        layout = self.layout
        layout.operator_context = "INVOKE_REGION_WIN"
        for kind in g.KINDS:
            if g.group(kind) == group and g.offered(kind):
                op = layout.operator("node.add_node", text=g.label(kind), icon=ICONS.get(kind, "NODE"))
                op.type = _idname(kind)
                op.use_transform = True

    return type(f"ZETTA_MT_pipe_add_{group.lower()}", (bpy.types.Menu,), {
        "bl_idname": f"ZETTA_MT_pipe_add_{group.lower()}",
        "bl_label": group,
        "draw": draw,
    })


ADD_MENUS = [_add_menu_class(group) for group in g.GROUPS]


def _add_menu(self, context):
    if not _ours(context):
        return
    layout = self.layout
    for menu in ADD_MENUS:
        layout.menu(menu.bl_idname)


# ─── Keeping the tree out of the file, and Blender's own undo ────────────────────────────────────


@bpy.app.handlers.persistent
def _save_pre(*_args):
    tree = get_tree()
    if tree is None:
        return
    view["saved_spaces"] = True
    _syncing[0] = True
    try:
        bpy.data.node_groups.remove(tree)
    finally:
        _syncing[0] = False


@bpy.app.handlers.persistent
def _save_post(*_args):
    if view["saved_spaces"]:
        view["saved_spaces"] = False
        view["seen"] = None  # rebuilt on the next tick


@bpy.app.handlers.persistent
def _undo_pre(*_args):
    view["undoing"] = True


@bpy.app.handlers.persistent
def _undo_post(*_args):
    # Blender's undo (Edit › Undo) restored an older canvas; the recipe is what counts — redraw from it.
    view["undoing"] = False
    view["seen"] = None


@bpy.app.handlers.persistent
def _load_post(*_args):
    view["seen"] = None


HANDLERS = (("save_pre", _save_pre), ("save_post", _save_post), ("undo_pre", _undo_pre), ("undo_post", _undo_post),
            ("redo_pre", _undo_pre), ("redo_post", _undo_post), ("load_post", _load_post))

CLASSES = (*SOCKETS.values(), ZETTA_NT_pipeline, *NODE_CLASSES.values(), ZETTA_PipeNode_other,
           ZETTA_OT_pipeline_nodes, ZETTA_OT_pipeline_undo, ZETTA_PT_pipe_nodes, ZETTA_PT_pipe_nodes_step,
           ZETTA_PT_pipe_nodes_runs, *ADD_MENUS)
_keymaps = []


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.NODE_MT_add.append(_add_menu)
    bpy.types.NODE_HT_header.append(_header)
    for name, fn in HANDLERS:
        getattr(bpy.app.handlers, name).append(fn)
    keyconfig = bpy.context.window_manager.keyconfigs.addon
    if keyconfig is not None:
        # Ctrl+Z over the canvas walks back the pipeline's own history (the recipe is what is saved).
        keymap = keyconfig.keymaps.new(name="Node Editor", space_type="NODE_EDITOR")
        for modifiers, redo in (({"ctrl": True}, False), ({"oskey": True}, False),
                                ({"ctrl": True, "shift": True}, True), ({"oskey": True, "shift": True}, True)):
            item = keymap.keymap_items.new("zetta.pipeline_canvas_undo", "Z", "PRESS", **modifiers)
            item.properties.redo = redo
            _keymaps.append((keymap, item))
    bpy.app.timers.register(_tick, first_interval=1.0, persistent=True)


def unregister():
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
    if bpy.app.timers.is_registered(_apply_canvas):
        bpy.app.timers.unregister(_apply_canvas)
    for keymap, item in _keymaps:
        try:
            keymap.keymap_items.remove(item)
        except (ReferenceError, RuntimeError):
            pass
    _keymaps.clear()
    for name, fn in HANDLERS:
        handlers = getattr(bpy.app.handlers, name)
        if fn in handlers:
            handlers.remove(fn)
    bpy.types.NODE_HT_header.remove(_header)
    bpy.types.NODE_MT_add.remove(_add_menu)
    tree = get_tree()
    if tree is not None:
        bpy.data.node_groups.remove(tree)
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
