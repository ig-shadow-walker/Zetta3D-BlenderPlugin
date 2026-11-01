"""Zetta 3D for Blender — generate, finish and import 3D models and pictures with your Zetta 3D account.

The sidebar (3D View › N › Zetta 3D) holds the 3D Studio (Generate 3D, Smart 3D, Segment, Retopology, Smart UV,
Texture, Auto Rig, Image Studio), the jobs you started, My Generations and 3D Pipelines.
"""

import bpy

from . import auth, jobs, library, props, providers, updates
from .core import http, tasks
from .pipelines import state as pipelines
from .studio import context as studio
from .studio import generate, pictures
from .ui import ops, panels, preferences


def _apply_plan_defaults():
    wm = getattr(bpy.context, "window_manager", None)
    if wm is None:
        return
    generate.apply_plan_defaults(wm.zetta.gen)
    generate.apply_plan_defaults(wm.zetta.smart)


def _account_changed():
    if auth.signed_in():
        providers.refresh()
        library.reset()
        pipelines.reset()
    elif auth.current == auth.SIGNED_OUT:
        studio.reset()
        library.reset()
        pipelines.reset()


@bpy.app.handlers.persistent
def _load_post(_dummy):
    props.initialise()
    _apply_plan_defaults()


def _start():
    """After registration, once Blender's context is usable."""
    props.initialise()
    auth.start()
    jobs.resume()
    return None


def register():
    props.register()
    ops.register()
    panels.register()
    preferences.register()
    tasks.register()
    http.read_host_version()
    auth.on_change(_account_changed)
    studio.on_loaded(_apply_plan_defaults)
    updates.register()
    pipelines.register()
    bpy.app.handlers.load_post.append(_load_post)
    bpy.app.timers.register(_start, first_interval=0.2)


def unregister():
    auth._listeners.clear()
    studio._listeners.clear()
    http._unauthorized_listeners.clear()
    if _load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_load_post)
    if bpy.app.timers.is_registered(_start):
        bpy.app.timers.unregister(_start)
    pipelines.flush()
    pipelines.unregister()
    updates.unregister()
    jobs.stop()
    auth.stop()
    tasks.unregister()
    pictures.unregister()
    preferences.unregister()
    panels.unregister()
    ops.unregister()
    props.unregister()
