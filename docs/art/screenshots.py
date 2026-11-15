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
    "pipeline_nodes": {"ZETTA_PT_main", "ZETTA_PT_pipelines"},
    "agent": {"ZETTA_PT_main"},
    "agent_card": {"ZETTA_PT_main"},
    "agent_tag": {"ZETTA_PT_main"},
}[SHOT]
TAB = "Zetta Agent" if SHOT.startswith("agent") else "Zetta 3D"
for cls in (panels.ZETTA_PT_main, panels.ZETTA_PT_studio, panels.ZETTA_PT_jobs, panels.ZETTA_PT_library,
            panels.ZETTA_PT_pipelines):
    bpy.utils.unregister_class(cls)
    cls.bl_options = set() if cls.__name__ in OPEN else {"DEFAULT_CLOSED"}
for cls in (panels.ZETTA_PT_main, panels.ZETTA_PT_studio, panels.ZETTA_PT_jobs, panels.ZETTA_PT_library,
            panels.ZETTA_PT_pipelines):
    bpy.utils.register_class(cls)

# The tab clicker ends on the lowest tab, and tabs stack in registration order: the agent's goes last for its shots.
if TAB == "Zetta Agent":
    agent_ui = module("ui.agent_panel")
    chat_host = module("ui.chat.host")
    for cls in (agent_ui.ZETTA_PT_agent, chat_host.ZETTA_PT_agent_chat):
        bpy.utils.unregister_class(cls)
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
    if SHOT == "pipeline_nodes":  # a branch, so the canvas shows a graph rather than a line
        g, step = graph.add_step(g, "smart_uv", after=gen)
    pipes.editor.update(pipeline={"id": 9, "title": "Game-ready character"}, graph=g, selected=gen)
    pipes.listing.update(items=[{"id": 9, "title": "Game-ready character"}], loaded=True)
    bpy.context.window_manager.zetta.pipe_title = "Game-ready character"
    bpy.context.window_manager.zetta.pipe_view = "recipe"


def conversation():
    """A finished exchange with the agent — and, for agent_card, the card it stops on before running Python; for
    agent_tag, a message being typed with the @ list open."""
    runtime = module("agent.runtime")
    agent_changes = module("agent.changes")
    tools = module("agent.tools")
    runtime.options.update(loaded=True, loading=False, error=None, providers=[],
                           tiers=[{"key": "auto", "label": "Auto", "available": True}],
                           thinking=[{"key": "auto", "label": "Auto", "available": True}])
    monkey = bpy.data.objects.get("Suzanne")
    if monkey is not None:
        monkey.name = "Statue"
    rows = [
        {"role": "user", "text": "Give the statue a soft bevel and a polished bronze look", "mentions": ["Statue"]},
        {"role": "step", "name": "inspect", "toolUseId": "a", "status": "done", "label": "Inspected Statue"},
        {"role": "step", "name": "add_modifier", "toolUseId": "b", "status": "done", "label": "Added Bevel to Statue"},
        {"role": "step", "name": "create_material", "toolUseId": "c", "status": "done",
         "label": "Made material Bronze"},
        {"role": "step", "name": "capture_view", "toolUseId": "d", "status": "done", "label": "Captured the view"},
        {"role": "assistant", "text": "Done — the statue has a 2 mm Bevel (3 segments) and a new **Bronze** "
                                      "material: metallic 1.0, roughness 0.25, a warm base colour.\n\n"
                                      "Switch the viewport to Material Preview (Z › 2) to see it. Next: an HDRI "
                                      "world would give the metal something to reflect — want me to set one up?"},
    ]
    for row in rows:
        runtime._add(row)
    turn = agent_changes.begin_turn("bronze statue")
    for label in ("Added Bevel to Statue", "Made material Bronze"):
        agent_changes.scene_step(label)
    if SHOT == "agent_tag":  # typing a message, with the @ list open
        chat = module("ui.chat.view")
        chat.box.set_text("Now put a soft area light above @st")  # focused just before the shot (repaint)
        bpy.context.window_manager.zetta_agent.input = chat.box.text
    if SHOT == "agent_card":
        runtime._add({"role": "user", "text": "Scatter 40 rocks around it, random sizes"})
        runtime._add({"role": "step", "name": "run_python", "toolUseId": "e", "status": "running",
                      "label": "Run python"})
        runtime._open_card("approve", {
            "title": "Run Python: scatter 40 rocks around the statue",
            "summary": "import random\nrocks = bpy.data.collections.new(\"Rocks\")\n"
                       "bpy.context.scene.collection.children.link(rocks)\nfor i in range(40):\n"
                       "    angle = random.uniform(0, 6.283)\n    ...",
            "reasons": [], "copy": True, "copy_ticked": True, "copy_note": None, "button": "Allow"},
            lambda _answer: None)
        tools.Result  # noqa: B018 — imported for the card's tool, nothing to run


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
        if region.active_panel_category == TAB:
            return
        click(window, x, top - offset)


@guarded
def start():
    scene()
    account()
    if SHOT.startswith("pipeline"):
        pipeline()
    if SHOT.startswith("agent"):
        conversation()
    window, area = sidebar()
    if SHOT == "pipeline_nodes":  # the 3D View becomes the pipeline's node editor
        nodes = module("ui.pipeline_nodes")
        nodes.refresh()
        nodes.show_in(area, nodes.get_tree())
        bpy.app.timers.register(lambda: frame_nodes(window, area), first_interval=1.0)
        bpy.app.timers.register(lambda: repaint(window), first_interval=2.4)
        bpy.app.timers.register(lambda: shoot(window, area), first_interval=3.0)
        return
    bpy.app.timers.register(lambda: widen(window, area), first_interval=0.6)
    bpy.app.timers.register(lambda: zetta_tab(window, area), first_interval=1.2)
    bpy.app.timers.register(lambda: repaint(window), first_interval=2.4)
    bpy.app.timers.register(lambda: shoot(window, area), first_interval=3.0)


@guarded
def frame_nodes(window, area):
    region = next(r for r in area.regions if r.type == "WINDOW")
    with bpy.context.temp_override(window=window, area=area, region=region):
        bpy.ops.node.view_all()


@guarded
def repaint(window):
    """Draws the window now: a Blender window that is not in front may not repaint by itself, and the shot is
    taken of what was last drawn."""
    if SHOT == "agent_tag":  # after the tab clicks, which take the keyboard away from the message box
        module("ui.chat.view").state["focus"] = True
    with bpy.context.temp_override(window=window):
        bpy.ops.wm.redraw_timer(type="DRAW_WIN_SWAP", iterations=2)


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
