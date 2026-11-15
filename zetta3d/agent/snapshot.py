"""The `editor` snapshot: what Blender reports about the open file with every request (BlenderAgentProtocol.md).

The server writes it into the facts the model reads first, so the agent starts from where the user is — the
version, the file, the mode, the selection, recent errors, an outline of the scene. Each field is gathered on
its own, so one that fails is left out rather than costing the request. It describes; the server never
trusts it for anything else.
"""

import bpy

from . import errors

MAX_CHARS = 60_000
MAX_SELECTION = 20
MAX_OUTLINE = 20_000


def _field(snapshot, key, gather):
    try:
        value = gather()
    except Exception:  # noqa: BLE001 — a field that fails is left out, never the request
        return
    if value not in (None, "", [], {}):
        snapshot[key] = value


def _file():
    path = bpy.data.filepath
    return {"path": path, "name": bpy.path.basename(path) if path else "", "dirty": bool(bpy.data.is_dirty),
            "saved": bool(path)}


def _units(scene):
    units = scene.unit_settings
    return {"system": units.system, "scale": round(units.scale_length, 6), "length": units.length_unit}


def _device(scene):
    if scene.render.engine == "CYCLES":
        return getattr(scene.cycles, "device", None)
    return None


def _frame(scene):
    return {"current": scene.frame_current, "start": scene.frame_start, "end": scene.frame_end,
            "fps": round(scene.render.fps / (scene.render.fps_base or 1), 3)}


def _selection(context):
    picked = getattr(context, "selected_objects", None) or []
    return [{"name": obj.name, "type": obj.type} for obj in picked[:MAX_SELECTION]]


def _edit_mesh(context):
    obj = context.active_object
    if obj is None or obj.mode != "EDIT" or obj.type != "MESH":
        return None
    import bmesh

    mesh = bmesh.from_edit_mesh(obj.data)
    return {"verts": sum(v.select for v in mesh.verts), "edges": sum(e.select for e in mesh.edges),
            "faces": sum(f.select for f in mesh.faces)}


def _outline():
    try:
        from .tools import read
    except ImportError:
        return None
    text = read.outline_text(depth=3)
    return text[:MAX_OUTLINE] if text else None


def _gpu():
    import gpu

    return {"backend": gpu.platform.backend_type_get(), "device": gpu.platform.renderer_get()[:80]}


def gather():
    """The snapshot as a dict; at most MAX_CHARS of JSON (the outline is dropped first when it is too big)."""
    import json

    context = bpy.context
    scene = context.scene
    snapshot = {}
    _field(snapshot, "blenderVersion", lambda: bpy.app.version_string.split(" ")[0])
    _field(snapshot, "file", _file)
    _field(snapshot, "units", lambda: _units(scene))
    _field(snapshot, "renderEngine", lambda: scene.render.engine)
    _field(snapshot, "device", lambda: _device(scene))
    _field(snapshot, "colorManagement", lambda: {"view": scene.view_settings.view_transform,
                                                 "look": scene.view_settings.look})
    _field(snapshot, "frame", lambda: _frame(scene))
    _field(snapshot, "mode", lambda: context.mode)
    _field(snapshot, "scene", lambda: scene.name)
    _field(snapshot, "viewLayer", lambda: context.view_layer.name)
    _field(snapshot, "workspace", lambda: context.workspace.name if context.workspace else None)
    _field(snapshot, "active", lambda: context.active_object.name if context.active_object else None)
    _field(snapshot, "selection", lambda: _selection(context))
    _field(snapshot, "editMesh", lambda: _edit_mesh(context))
    _field(snapshot, "recentErrors", lambda: [{"source": e["source"], "message": e["message"]}
                                             for e in errors.recent(20)])
    _field(snapshot, "addons", lambda: sorted(context.preferences.addons.keys())[:80])
    _field(snapshot, "gpu", _gpu)
    _field(snapshot, "outline", _outline)
    if len(json.dumps(snapshot)) > MAX_CHARS:
        snapshot.pop("outline", None)
    if len(json.dumps(snapshot)) > MAX_CHARS:
        snapshot = {key: snapshot[key] for key in ("blenderVersion", "file", "mode", "active") if key in snapshot}
    return snapshot
