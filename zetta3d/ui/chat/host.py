"""What Blender itself provides for the drawn chat: the tab's (empty) panel, the operator that wakes the chat when
the mouse comes over it, the event modal, the settings popover and the + menu."""

import traceback

import bpy

from .. import agent_panel as classic
from . import view
from .theme import Sizes


class ZETTA_PT_agent_chat(bpy.types.Panel):
    """The Zetta Agent tab when the chat is drawn: an empty panel — it only puts the tab in the sidebar.
    Everything in it is drawn over it (view.py)."""

    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = classic.CATEGORY
    bl_label = "Zetta Agent"
    bl_idname = "ZETTA_PT_agent_chat"
    # A normal header (drawn over, like the rest): a panel without one puts its tab first in every 3D View's
    # sidebar, and makes it the tab a new sidebar opens on.
    bl_options = set()

    @classmethod
    def poll(cls, _context):
        return not classic.classic_panel()

    def draw(self, context):
        # As tall as the sidebar. With region overlap (Blender's default) the sidebar is see-through: a click or a
        # hover over a part with no panel under it goes to the viewport behind — the chat drawn there would not
        # get it, and the viewport would select what is behind. So the panel covers it all, down to the bottom.
        region = context.region
        col = self.layout.column()
        if region is not None:
            view.hosted[region.as_pointer()] = True
            col.scale_y = max(1.0, region.height / Sizes().widget_unit)
        col.label(text="")


def _over_chat(context):
    area, region = context.area, context.region
    return (area is not None and area.type == "VIEW_3D" and region is not None and region.type == "UI"
            and not classic.classic_panel() and view.tab_active(region))


class ZETTA_OT_agent_wake(bpy.types.Operator):
    """Starts the chat's event modal when the mouse moves, clicks or scrolls over it. Bound in the sidebar's key
    map (View2D Buttons List), which sees every event over a sidebar — so the chat answers from the first move,
    whatever happened before (a menu, a popover, another window)."""

    bl_idname = "zetta.agent_wake"
    bl_label = "Zetta Agent chat"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, context):
        return _over_chat(context)

    def invoke(self, context, event):
        window = context.window
        if window is None or window.as_pointer() in view.state["running"]:
            return {"PASS_THROUGH"}
        _key, frame = view.frame_under(window, event.mouse_x, event.mouse_y)
        if frame is None:
            return {"PASS_THROUGH"}
        bpy.ops.zetta.agent_events("INVOKE_DEFAULT")
        verdict = view.handle(context, event)
        if event.type in {"MOUSEMOVE", "INBETWEEN_MOUSEMOVE"}:
            return {"PASS_THROUGH"}
        return {"FINISHED"} if verdict == "consume" else {"PASS_THROUGH"}


class ZETTA_OT_agent_events(bpy.types.Operator):
    """Clicks, scrolling and typing for the Zetta Agent's chat. Runs only while the mouse is over the chat or
    its message box has focus"""

    bl_idname = "zetta.agent_events"
    bl_label = "Zetta Agent chat"
    bl_options = {"INTERNAL"}

    def invoke(self, context, _event):
        window = context.window
        if window is None:
            return {"CANCELLED"}
        key = window.as_pointer()
        if key in view.state["running"]:
            return {"CANCELLED"}
        self._key = key
        self._timer = context.window_manager.event_timer_add(0.25, window=window)
        view.state["running"].add(key)
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        try:
            verdict = view.handle(context, event)
        except Exception:  # noqa: BLE001 — a bug here must not swallow every event in the window
            traceback.print_exc()
            verdict = "finish"
        if verdict == "finish":
            self._end(context)
            return {"CANCELLED", "PASS_THROUGH"}
        return {"RUNNING_MODAL"} if verdict == "consume" else {"PASS_THROUGH"}

    def cancel(self, context):
        self._end(context)

    def _end(self, context):
        view.state["running"].discard(getattr(self, "_key", None))
        timer = getattr(self, "_timer", None)
        if timer is not None:
            try:
                context.window_manager.event_timer_remove(timer)
            except (ReferenceError, RuntimeError, ValueError):
                pass
            self._timer = None


class ZETTA_PT_agent_settings(bpy.types.Panel):
    """The settings the composer's ▾ opens: which model, how hard it thinks, and when it asks first."""

    bl_space_type = "VIEW_3D"
    bl_region_type = "WINDOW"
    bl_label = "Zetta Agent settings"
    bl_options = {"INSTANCED"}
    bl_ui_units_x = 14

    def draw(self, context):
        from ... import providers
        from ...core import prefs

        layout = self.layout
        props = classic.state(context)
        layout.label(text="Model")
        if prefs.get("agent_account", providers.ZETTA) == providers.OWN:
            layout.prop(props, "provider", text="")
            if props.provider and props.provider != "zetta":
                layout.prop(props, "model", text="")
            else:
                layout.prop(props, "tier", text="")
        else:
            layout.prop(props, "tier", text="")
        layout.label(text="Thinking")
        layout.prop(props, "thinking", text="")
        layout.separator()
        layout.label(text="Before risky steps")
        layout.prop(props, "approval", expand=True)
        layout.separator()
        layout.operator("zetta.do", text="Keys and preferences…", icon="PREFERENCES").action = "agent_settings"


class ZETTA_MT_agent_add(bpy.types.Menu):
    bl_label = "Add to the message"
    bl_idname = "ZETTA_MT_agent_add"

    def draw(self, _context):
        layout = self.layout
        # A menu opened with wm.call_menu runs its items with execute, not invoke: Attach a file… would never open
        # the file browser.
        layout.operator_context = "INVOKE_DEFAULT"
        layout.operator("zetta.agent_add_selection", text="Tag the selected objects", icon="RESTRICT_SELECT_OFF")
        op = layout.operator("zetta.do", text="Picture of the viewport", icon="SCENE")
        op.action = "agent_attach_view"
        layout.operator("zetta.agent_attach", text="Attach a file…", icon="FILE_FOLDER")
        layout.operator("zetta.agent_write_more", text="Write a long message…", icon="TEXT")
        layout.separator()
        op = layout.operator("zetta.do", text="New chat", icon="ADD")
        op.action = "agent_new"


CLASSES = (ZETTA_PT_agent_chat, ZETTA_OT_agent_wake, ZETTA_OT_agent_events, ZETTA_PT_agent_settings,
           ZETTA_MT_agent_add)
WAKE_EVENTS = (("MOUSEMOVE", "ANY"), ("LEFTMOUSE", "PRESS"), ("WHEELUPMOUSE", "PRESS"), ("WHEELDOWNMOUSE", "PRESS"),
               ("TRACKPADPAN", "ANY"))
