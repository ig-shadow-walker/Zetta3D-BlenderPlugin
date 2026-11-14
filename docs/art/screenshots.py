"""Takes one README screenshot of the Zetta 3D sidebar in a real Blender window. Run by screenshots.sh.

The account, prices, jobs and pipeline are made up and set directly on the add-on's state; its background
work is switched off, so nothing is sent to zetta3d.ai and nothing a request answers can replace them.

    ZETTA_SHOT=studio|rig|pipelines  ZETTA_OUT=/path/to/shot.png
"""

import functools
import importlib
import os
import traceback

import bpy

SHOT = os.environ.get("ZETTA_SHOT", "studio")
OUT = os.environ["ZETTA_OUT"]
PACKAGE = next(name for name in bpy.context.preferences.addons.keys() if name.endswith("zetta3d"))


def module(name):
    return importlib.import_module(f"{PACKAGE}.{name}")


tasks = module("core.tasks")
tools = module("studio.tools")
auth = module("auth")
studio = module("studio.context")
jobs = module("jobs")
graph = module("pipelines.graph")
pipes = module("pipelines.state")
panels = module("ui.panels")

# No background work at all: the shot shows the state set below, and only that.
tasks.run = lambda *args, **kwargs: None

# Which panels are open in this shot. Panels open or closed by their bl_options the first time a region draws
# them, and the sidebar has not drawn yet, so re-registering them with new options is enough.
OPEN = {
    "studio": {"ZETTA_PT_main", "ZETTA_PT_studio", "ZETTA_PT_jobs"},
    "rig": {"ZETTA_PT_main", "ZETTA_PT_studio", "ZETTA_PT_jobs"},
    "pipelines": {"ZETTA_PT_main", "ZETTA_PT_pipelines"},
}[SHOT]
for cls in (panels.ZETTA_PT_main, panels.ZETTA_PT_studio, panels.ZETTA_PT_jobs, panels.ZETTA_PT_library,
            panels.ZETTA_PT_pipelines):
    bpy.utils.unregister_class(cls)
    cls.bl_options = set() if cls.__name__ in OPEN else {"DEFAULT_CLOSED"}
for cls in (panels.ZETTA_PT_main, panels.ZETTA_PT_studio, panels.ZETTA_PT_jobs, panels.ZETTA_PT_library,
            panels.ZETTA_PT_pipelines):
    bpy.utils.register_class(cls)


def guarded(fn):
    """A timer that fails says why and closes Blender, rather than leaving the window open."""
    @functools.wraps(fn)
    def run(*args):
        try:
            return fn(*args)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            bpy.ops.wm.quit_blender()
    return run


def scene():
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj)
    bpy.ops.mesh.primitive_plane_add(size=8)
    bpy.ops.mesh.primitive_monkey_add(location=(0, 0, 1), rotation=(0.0, 0.0, 0.5))
    monkey = bpy.context.active_object
    monkey.modifiers.new("Smooth", "SUBSURF").levels = 2
    bpy.ops.object.shade_smooth()
    monkey.select_set(False)
    bpy.context.scene.cursor.location = (2.4, 0.6, 0)


def account():
    auth.user = {"id": 1, "email": "demo@example.com", "displayName": "Demo Studio", "paymentPlan": "pro",
                 "credits": {"threeD": {"monthly": 2450, "purchased": 0, "bonusMonthly": 0, "bonus": 0}}}
    auth.current = auth.SIGNED_IN
    studio.gates_3d = {"allowsZettaN25": True, "allowsZettaNp1": True, "maxMultiViewImages": 4,
                       "allowsCustomFaceCount": True, "maxFaceCount": 2000000, "allowsPostProcess": True,
                       "allowsQuadMesh": True, "allowsPrivateGenerations": True, "allowsLowPoly": True}
    studio.gates_2d = {"allowsImageEditing": True, "allows4kImages": True}
    studio.prices = [{"code": code, "model": None, "credits": credits} for code, credits in (
        ("textured", 20), ("pbr", 5), ("smart_topology", 10), ("smart_uv", 10), ("retexture", 10),
        ("auto_rigging", 25), ("ai_segmentation", 10), ("smart_segmentation", 15), ("lowpoly", 20),
        ("geometry_only", 10), ("multi_view", 5), ("detailed_geometry", 10), ("detailed_texture", 10),
        ("extreme_texture", 20), ("generate_parts", 10), ("custom_face_count", 5))]
    studio.image_models = {"models": [{"id": "standard", "label": "Standard", "resolutions": ["1K", "2K", "4K"]}],
                           "default": "standard"}
    studio._loaded_for = studio._account_key()

    jobs.all_jobs()[:] = [
        {"id": 201, "label": "Sci-fi helmet with a cracked visor", "status": "processing"},
        {"id": 202, "label": "Wooden barrel, iron hoops", "status": "queued", "queuePosition": 2},
    ]

    zetta = bpy.context.window_manager.zetta
    zetta.tool = "generate"
    zetta.gen.source_mode = "single"
    zetta.gen.prompt = "A pirate treasure chest, stylized"
    if SHOT == "rig":
        zetta.tool = "rig"
        tools.sources["rig"] = {
            "id": 301, "status": "success", "selectedFeature": "img23d", "selectedModel": "n25",
            "title": "Armored knight", "faceCount": 20000, "polygonType": "triangle", "inputImages": [],
            "thumbnailURL": None, "result": {"files": [{"key": "x/model.glb", "type": "GLB"}]},
        }


def pipeline():
    g = graph.empty()
    g, step = graph.add_step(g, "seed_picture")
    g, step = graph.add_step(g, "remove_background", after=step)
    g, gen = graph.add_step(g, "generate_3d", after=step)
    g, step = graph.add_step(g, "retopology", after=gen)
    g, step = graph.add_step(g, "texture", after=step)
    g, step = graph.add_step(g, "rigging", after=step)
    pipes.editor.update(pipeline={"id": 9, "title": "Game-ready character"}, graph=g, selected=gen)
    pipes.listing.update(items=[{"id": 9, "title": "Game-ready character"}], loaded=True)
    bpy.context.window_manager.zetta.pipe_title = "Game-ready character"
    bpy.context.window_manager.zetta.pipe_view = "recipe"


def sidebar():
    window = bpy.context.window_manager.windows[0]
    for area in window.screen.areas:
        if area.type == "VIEW_3D":
            space = area.spaces.active
            space.show_region_ui = True
            space.shading.type = "SOLID"
            space.shading.light = "MATCAP"
            space.overlay.show_floor = True
            space.overlay.show_object_origins = False
            view = space.region_3d
            view.view_location = (0.6, 0, 0.7)
            view.view_distance = 9
            return window, area


def click(window, x, y):
    window.event_simulate(type="MOUSEMOVE", value="NOTHING", x=x, y=y)
    window.event_simulate(type="LEFTMOUSE", value="PRESS", x=x, y=y)
    window.event_simulate(type="LEFTMOUSE", value="RELEASE", x=x, y=y)


@guarded
def widen(window, area):
    """Drags the sidebar's left edge outward, as you would by hand: the region's width has no setter."""
    region = next(r for r in area.regions if r.type == "UI")
    x, y = region.x + 2, region.y + region.height // 2
    window.event_simulate(type="MOUSEMOVE", value="NOTHING", x=x, y=y)
    window.event_simulate(type="LEFTMOUSE", value="PRESS", x=x, y=y)
    for step in range(1, 11):
        window.event_simulate(type="MOUSEMOVE", value="NOTHING", x=x - step * 14, y=y)
    window.event_simulate(type="LEFTMOUSE", value="RELEASE", x=x - 140, y=y)


@guarded
def zetta_tab(window, area):
    """Clicks the Zetta 3D tab: the active tab has no setter either. The add-on's comes after Blender's own, so
    it is tried from the bottom of the tab strip up until the sidebar says it is showing."""
    region = next(r for r in area.regions if r.type == "UI")
    x = region.x + region.width - 10
    top = region.y + region.height
    for offset in range(40, region.height, 12):
        if region.active_panel_category == "Zetta 3D":
            return
        click(window, x, top - offset)


@guarded
def start():
    scene()
    account()
    if SHOT == "pipelines":
        pipeline()
    window, area = sidebar()
    bpy.app.timers.register(lambda: widen(window, area), first_interval=0.6)
    bpy.app.timers.register(lambda: zetta_tab(window, area), first_interval=1.2)
    bpy.app.timers.register(lambda: shoot(window, area), first_interval=3.0)


@guarded
def shoot(window, area):
    with bpy.context.temp_override(window=window, area=area):
        bpy.ops.screen.screenshot_area(filepath=OUT)
    region = next(r for r in area.regions if r.type == "UI")
    print(f"ZETTA_SHOT {SHOT} tab={region.active_panel_category} sidebar_x={region.x - area.x} "
          f"sidebar_w={region.width} area={area.width}x{area.height}")
    bpy.app.timers.register(lambda: bpy.ops.wm.quit_blender(), first_interval=0.5)


# After the add-on's own start-up timer, which would otherwise set the account back to signed out.
bpy.app.timers.register(start, first_interval=1.5)
