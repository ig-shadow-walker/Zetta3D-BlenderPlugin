"""The Zetta Agent's drawn chat in the 3D View sidebar (see view.py for why it is drawn)."""

import bpy

from . import host, paint, view

_handle = None
_keymaps = []


def register():
    global _handle
    for cls in host.CLASSES:
        bpy.utils.register_class(cls)
    _handle = bpy.types.SpaceView3D.draw_handler_add(view.draw_callback, (), "UI", "POST_PIXEL")
    keyconfig = bpy.context.window_manager.keyconfigs.addon
    if keyconfig is not None:
        # Every region of the 3D View has this key map, its sidebar included; the poll keeps it to the chat.
        keymap = keyconfig.keymaps.new(name="3D View Generic", space_type="VIEW_3D", region_type="WINDOW")
        for kind, value in host.WAKE_EVENTS:
            _keymaps.append((keymap, keymap.keymap_items.new("zetta.agent_wake", kind, value)))


def unregister():
    global _handle
    for keymap, item in _keymaps:
        try:
            keymap.keymap_items.remove(item)
        except (ReferenceError, RuntimeError):
            pass
    _keymaps.clear()
    if _handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_handle, "UI")
        _handle = None
    view.frames.clear()
    view.state["running"].clear()
    view.state["starting"].clear()
    paint.forget_pictures()
    for cls in reversed(host.CLASSES):
        bpy.utils.unregister_class(cls)
