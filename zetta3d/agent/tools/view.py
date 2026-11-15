"""The agent's eyes and pointer: pictures of the scene, the 3D View, the interface, and renders.

TWO WAYS TO LOOK. "current" is the 3D View exactly as the user sees it — drawn off screen from that view's own
matrices, so it needs a window. Everything else (around, top, through the camera, and any look while Blender
runs in the background) is a Workbench render in a throwaway scene that links the objects, the way the
Studio's capture does: the user's scene, camera and render settings are never touched.

`viewport` and `ui_navigate` change what Blender shows, never the file — they are `read` tools, so the agent
can use them in Ask mode to show the user where things are. Neither is an undo step.

`render` runs the user's own render. In a window it renders the way F12 does (INVOKE), so Blender stays
usable, and the result reaches the model when the render_complete handler fires; in the background it simply
blocks. The scene's settings it overrides for the render are put back afterwards.
"""

import math
import os
import tempfile
import time

import bpy
from mathutils import Vector

from .. import changes, targets, values
from ...studio import capture
from . import LATER, Result, ToolError, tool

NO_VIEW = "No 3D View is open."
GEOMETRY = {"MESH", "CURVE", "SURFACE", "META", "FONT", "CURVES", "POINTCLOUD", "VOLUME", "GPENCIL",
            "GREASEPENCIL"}


# ─── Finding the 3D View ─────────────────────────────────────────────────────────────────────────


def _windows():
    # Background Blender still loads the startup file's windows and screens, but nothing draws them.
    if bpy.app.background:
        return []
    manager = bpy.context.window_manager
    return list(manager.windows) if manager is not None else []


def _view3d():
    """(window, area, region, space) of the biggest 3D View on screen, or None without a window."""
    best = None
    for window in _windows():
        for area in window.screen.areas:
            if area.type != "VIEW_3D":
                continue
            region = next((r for r in area.regions if r.type == "WINDOW"), None)
            if region is None:
                continue
            size = area.width * area.height
            # The window the user is working in wins over a bigger one elsewhere.
            if window == bpy.context.window:
                size *= 4
            if best is None or size > best[0]:
                best = (size, window, area, region, area.spaces.active)
    return best[1:] if best else None


def _require_view():
    found = _view3d()
    if found is None:
        raise ToolError(NO_VIEW + (" Blender runs in the background, without a window." if bpy.app.background
                                   else " Ask the user to open one, or use angles \"around\" or \"top\"."))
    return found


# ─── What a view shows ───────────────────────────────────────────────────────────────────────────


def _visible_objects(scene=None):
    scene = scene or bpy.context.scene
    found = []
    for obj in scene.objects:
        try:
            if obj.visible_get():
                found.append(obj)
        except RuntimeError:
            continue
    return found


def _subjects(target):
    """The objects a capture or frame is about: a target object (with its children), a collection, or None
    for everything visible."""
    if not target:
        return None
    block = targets.datablock(target)
    if isinstance(block, bpy.types.Object):
        return [block] + list(block.children_recursive)
    if isinstance(block, bpy.types.Collection):
        return list(block.all_objects)
    raise ToolError(f"{target} is not an object or a collection, so there is nothing to look at.")


def _bounds(objects):
    centre, radius = capture._bounds(objects)
    if centre is None:
        points = [o.matrix_world.translation for o in objects]
        if not points:
            return None, None
        centre = sum(points, Vector()) / len(points)
        radius = max([(p - centre).length for p in points] + [1.0])
    return centre, radius


def _in_view(region, r3d, objects):
    from bpy_extras.view3d_utils import location_3d_to_region_2d

    seen = []
    for obj in objects:
        if obj.type not in GEOMETRY and obj.type not in {"LIGHT", "CAMERA", "EMPTY", "ARMATURE"}:
            continue
        point = location_3d_to_region_2d(region, r3d, obj.matrix_world.translation)
        if point is not None and 0 <= point.x <= region.width and 0 <= point.y <= region.height:
            seen.append(obj.name)
    return seen


def _describe_view(area, region, space):
    r3d = space.region_3d
    # From the view's own location, rotation and distance: view_matrix is only refreshed when it redraws.
    eye = r3d.view_location + r3d.view_rotation @ Vector((0, 0, r3d.view_distance))
    camera = bpy.context.scene.camera
    if r3d.view_perspective == "CAMERA" and camera:
        lines = [f"3D View: through camera {camera.name} at {values.show(camera.matrix_world.translation)}, "
                 f"shading {space.shading.type.lower()}"]
    else:
        lines = [f"3D View: {r3d.view_perspective.lower()} view, shading {space.shading.type.lower()}, "
                 f"looking at {values.show(r3d.view_location)} from {values.show(eye)} "
                 f"(distance {r3d.view_distance:.3g})"]
    seen = _in_view(region, r3d, _visible_objects())
    lines.append(f"In view ({len(seen)}): {', '.join(seen[:60]) or 'nothing'}" + (" …" if len(seen) > 60 else ""))
    return lines


# ─── Pictures ────────────────────────────────────────────────────────────────────────────────────


def _jpeg_from_pixels(pixels, width, height, quality=85):
    """JPEG bytes from float RGBA pixels, through a throwaway Image (Blender bundles no other encoder)."""
    image = bpy.data.images.new(".zetta-capture", width, height, alpha=False)
    path = os.path.join(tempfile.mkdtemp(prefix="zetta3d-view-"), "view.jpg")
    try:
        image.pixels.foreach_set(pixels)
        image.filepath_raw = path
        image.file_format = "JPEG"
        try:
            image.save(filepath=path, quality=quality)
        except TypeError:  # an Image.save without arguments (older Blender)
            image.save()
        with open(path, "rb") as handle:
            return handle.read()
    finally:
        bpy.data.images.remove(image)
        try:
            os.remove(path)
        except OSError:
            pass


def _grab_view(window, area, region, space, width):
    """The 3D View as drawn on screen, off screen at `width` pixels wide — JPEG bytes."""
    import gpu
    import numpy as np

    height = max(16, round(width * region.height / max(region.width, 1)))
    r3d = space.region_3d
    offscreen = gpu.types.GPUOffScreen(width, height)
    try:
        with bpy.context.temp_override(window=window, area=area, region=region):
            offscreen.draw_view3d(window.scene, window.view_layer, space, region, r3d.view_matrix,
                                  r3d.window_matrix, do_color_management=True)
            with offscreen.bind():
                framebuffer = gpu.state.active_framebuffer_get()
                buffer = framebuffer.read_color(0, 0, width, height, 4, 0, "FLOAT")
    finally:
        offscreen.free()
    try:
        pixels = np.frombuffer(buffer, dtype=np.float32)
    except (TypeError, ValueError):  # a Buffer without the buffer protocol
        pixels = np.array(buffer.to_list(), dtype=np.float32).ravel()
    return _jpeg_from_pixels(pixels.ravel(), width, height)


def _grab_view_opengl(window, area, region, space, width):
    """The fallback: Blender's own Viewport Render (View › Viewport Render Image), scaled to width."""
    with bpy.context.temp_override(window=window, area=area, region=region):
        bpy.ops.render.opengl(view_context=True)
    result = bpy.data.images.get("Render Result")
    if result is None:
        raise ToolError("Blender could not draw the 3D View.")
    folder = tempfile.mkdtemp(prefix="zetta3d-view-")
    raw = os.path.join(folder, "view.png")
    result.save_render(raw)
    image = bpy.data.images.load(raw)
    try:
        w, h = image.size
        image.scale(width, max(16, round(width * h / max(w, 1))))
        out = os.path.join(folder, "view.jpg")
        image.filepath_raw = out
        image.file_format = "JPEG"
        image.save()
        with open(out, "rb") as handle:
            return handle.read()
    finally:
        bpy.data.images.remove(image)


class _Shots:
    """Workbench (or the scene's engine, for "rendered") renders of some objects in a throwaway scene."""

    def __init__(self, objects, shading):
        self.user = bpy.context.scene
        self.objects = set(objects)
        for obj in objects:
            self.objects.update(obj.children_recursive)
        self.shading = shading

    def __enter__(self):
        user = self.user
        self.scene = scene = bpy.data.scenes.new(".Zetta capture")
        self.camera_data = bpy.data.cameras.new(".Zetta capture")
        self.camera = bpy.data.objects.new(".Zetta capture", self.camera_data)
        self.folder = tempfile.mkdtemp(prefix="zetta3d-capture-")
        for obj in self.objects:
            scene.collection.objects.link(obj)
        scene.collection.objects.link(self.camera)
        scene.camera = self.camera
        scene.world = user.world
        render = scene.render
        render.resolution_percentage = 100
        render.film_transparent = False
        render.image_settings.file_format = "JPEG"
        render.image_settings.quality = 85
        if self.shading == "rendered" and user.render.engine != "BLENDER_WORKBENCH":
            render.engine = user.render.engine
            # The look of the user's renders, but fast: few samples.
            for key in ("view_transform", "look", "exposure", "gamma"):
                try:
                    setattr(scene.view_settings, key, getattr(user.view_settings, key))
                except (TypeError, AttributeError):
                    pass
            if render.engine == "CYCLES" and hasattr(scene, "cycles"):
                scene.cycles.samples = 16
                scene.cycles.device = user.cycles.device
            elif hasattr(scene, "eevee"):
                scene.eevee.taa_render_samples = 16
        else:
            render.engine = "BLENDER_WORKBENCH"
            shading = scene.display.shading
            shading.light = "STUDIO"
            shading.color_type = "TEXTURE" if self.shading in {"material", "rendered"} else "MATERIAL"
        return self

    def shoot(self, width, height, place, lens_from=None):
        """One JPEG. place(camera) positions the throwaway camera; lens_from copies a real camera's lens."""
        render = self.scene.render
        render.resolution_x, render.resolution_y = width, height
        if lens_from is not None:
            self.camera.matrix_world = lens_from.matrix_world
            for key in ("type", "lens", "lens_unit", "ortho_scale", "sensor_width", "sensor_height", "sensor_fit",
                        "shift_x", "shift_y", "clip_start", "clip_end"):
                setattr(self.camera_data, key, getattr(lens_from.data, key))
        else:
            place(self.camera, self.camera_data)
        render.filepath = os.path.join(self.folder, f"shot-{time.time_ns()}.jpg")
        bpy.ops.render.render(write_still=True, scene=self.scene.name)
        with open(render.filepath, "rb") as handle:
            data = handle.read()
        os.remove(render.filepath)
        return data

    def __exit__(self, *_):
        bpy.data.objects.remove(self.camera)
        bpy.data.cameras.remove(self.camera_data)
        bpy.data.scenes.remove(self.scene)


def _orbit(centre, radius, yaw, pitch, fov=math.radians(35)):
    """A place() for _Shots.shoot: the camera on a sphere around centre, framing radius."""

    def place(camera, data):
        data.type = "PERSP"
        data.angle = fov
        distance = radius / math.sin(fov / 2) * 1.1
        data.clip_start = max(distance * 0.005, 0.0005)
        data.clip_end = distance + radius * 4
        if pitch >= 89.9:
            camera.location = centre + Vector((0, 0, distance))
            camera.rotation_euler = (0, 0, 0)  # straight down, +Y up in the picture
            return
        y, p = math.radians(yaw), math.radians(pitch)
        offset = Vector((math.sin(y) * math.cos(p), -math.cos(y) * math.cos(p), math.sin(p))) * distance
        camera.location = centre + offset
        camera.rotation_euler = (centre - camera.location).to_track_quat("-Z", "Y").to_euler()

    return place


AROUND = (("front-left", -45, 20), ("front-right", 45, 20), ("back", 180, 20), ("above", -30, 60))


# ─── capture_view ────────────────────────────────────────────────────────────────────────────────


@tool("capture_view")
def capture_view(args, ctx):
    view = args.get("view") or "viewport"
    if view not in {"viewport", "camera"}:
        raise ToolError("view is \"viewport\" or \"camera\".")
    target = args.get("target")
    angles = args.get("angles") or ("around" if target else "current")
    if angles not in {"current", "around", "top"}:
        raise ToolError("angles is \"current\", \"around\" or \"top\".")
    shading = args.get("shading")
    if shading is not None and shading not in {"solid", "material", "rendered"}:
        raise ToolError("shading is \"solid\", \"material\" or \"rendered\".")
    width = args.get("width") or 768
    if isinstance(width, bool) or not isinstance(width, int) or not 256 <= width <= 1280:
        raise ToolError("width is a whole number from 256 to 1280.")
    scene = bpy.context.scene

    if view == "camera":
        camera = scene.camera
        if camera is None:
            raise ToolError("The scene has no camera. Add one with create_object kind \"camera\", or capture "
                            "the viewport instead.")
        everything = _visible_objects()
        render = scene.render
        height = max(16, round(width * render.resolution_y / max(render.resolution_x, 1)))
        with _Shots(everything, shading or "solid") as shots:
            picture = shots.shoot(width, height, None, lens_from=camera)
        text = [f"Through camera {camera.name} ({camera.data.lens:g}mm) at {values.show(camera.location)}, "
                f"shading {shading or 'solid'}, {width}×{height}.",
                f"In the scene ({len(everything)}): {', '.join(o.name for o in everything[:60])}"]
        if angles != "current":
            text.append(f"(angles {angles} does not apply to the camera view.)")
        return Result("\n".join(text), images=[(picture, "image/jpeg")])

    if angles == "current":
        window, area, region, space = _require_view()
        old = space.shading.type
        wanted = {"solid": "SOLID", "material": "MATERIAL", "rendered": "RENDERED"}.get(shading, old)
        try:
            space.shading.type = wanted  # the view's own setting (screen data), put back below
            try:
                picture = _grab_view(window, area, region, space, width)
            except Exception:  # noqa: BLE001 — the off-screen draw is not available everywhere: fall back
                picture = _grab_view_opengl(window, area, region, space, width)
        finally:
            space.shading.type = old
        text = _describe_view(area, region, space)
        if shading:
            text[0] = text[0].replace(f"shading {old.lower()}", f"shading {wanted.lower()}")
        return Result("\n".join(text), images=[(picture, "image/jpeg")])

    subjects = _subjects(target) or _visible_objects()
    centre, radius = _bounds(subjects)
    if centre is None:
        raise ToolError("There is nothing to look at: the scene has no visible objects.")
    pictures, names = [], []
    with _Shots(subjects, shading or "solid") as shots:
        for name, yaw, pitch in (AROUND if angles == "around" else (("top", 0, 90),)):
            pictures.append((shots.shoot(width, width, _orbit(centre, radius, yaw, pitch)), "image/jpeg"))
            names.append(name)
        shown = sorted(o.name for o in shots.objects)
    what = target or "the whole scene"
    text = [f"{len(pictures)} picture{'s' if len(pictures) > 1 else ''} of {what} ({', '.join(names)}), "
            f"shading {shading or 'solid'}, centre {values.show(centre)}, size {radius * 2:.3g} m across.",
            f"Shown ({len(shown)}): {', '.join(shown[:60])}" + (" …" if len(shown) > 60 else "")]
    if shading == "material":
        text.append("(Rendered without a window, \"material\" is Workbench with textures, not Material Preview.)")
    return Result("\n".join(text), images=pictures)


# ─── viewport ────────────────────────────────────────────────────────────────────────────────────

# The view rotations of Blender's own axis views (numpad 1, 3, 7 …). Set directly rather than through
# view3d.view_axis, whose smooth-view animation would still be running when the result is written.
AXES = {"front": (90, 0, 0), "back": (90, 0, 180), "right": (90, 0, 90), "left": (90, 0, -90), "top": (0, 0, 0),
        "bottom": (180, 0, 0)}


def _redraw(window, area, region):
    """Draws the view once so its matrices (and so what is in view) match the change just made."""
    try:
        with bpy.context.temp_override(window=window, area=area, region=region):
            bpy.ops.wm.redraw_timer(type="DRAW", iterations=1)
    except RuntimeError:
        area.tag_redraw()


@tool("viewport")
def viewport(args, ctx):
    action = args.get("action")
    if action not in {"frame", "view", "shading", "camera"}:
        raise ToolError("action is \"frame\", \"view\", \"shading\" or \"camera\".")
    window, area, region, space = _require_view()
    r3d = space.region_3d
    scene = bpy.context.scene
    if action == "frame":
        names = args.get("targets")
        if names:
            subjects = []
            for name in names:
                subjects.extend(_subjects(name))
        else:
            layer = bpy.context.view_layer
            subjects = [o for o in layer.objects if o.select_get(view_layer=layer)] or _visible_objects()
        centre, radius = _bounds(subjects)
        if centre is None:
            raise ToolError("There is nothing to frame.")
        if r3d.view_perspective == "CAMERA":
            r3d.view_perspective = "PERSP"  # framing moves the view, not the scene's camera
        half = math.atan(16 / max(space.lens, 1))  # the view's lens on a 32 mm sensor
        r3d.view_location = centre
        r3d.view_distance = radius / math.sin(half) * 1.15
    elif action in {"view", "camera"}:
        side = "camera" if action == "camera" else args.get("view")
        if side == "camera":
            if scene.camera is None:
                raise ToolError("The scene has no camera to look through.")
            r3d.view_perspective = "CAMERA"
        elif side in AXES:
            from mathutils import Euler

            r3d.view_rotation = Euler([math.radians(a) for a in AXES[side]]).to_quaternion()
            if r3d.view_perspective == "CAMERA":
                r3d.view_perspective = "PERSP"
            if bpy.context.preferences.inputs.use_auto_perspective:
                r3d.view_perspective = "ORTHO"  # what the numpad views do with Auto Perspective on
        else:
            raise ToolError("view is front, back, left, right, top, bottom or camera.")
    else:
        shading = args.get("shading")
        if shading not in {"wireframe", "solid", "material", "rendered"}:
            raise ToolError("shading is wireframe, solid, material or rendered.")
        space.shading.type = shading.upper()
    _redraw(window, area, region)
    return "\n".join(_describe_view(area, region, space))


# ─── ui_navigate ─────────────────────────────────────────────────────────────────────────────────

EDITORS = {
    "properties": ("PROPERTIES", "Properties"), "outliner": ("OUTLINER", "Outliner"),
    "shader": ("ShaderNodeTree", "Shader Editor"), "geometry_nodes": ("GeometryNodeTree", "Geometry Node Editor"),
    "compositor": ("CompositorNodeTree", "Compositor"), "uv": ("UV", "UV Editor"),
    "image": ("IMAGE_EDITOR", "Image Editor"), "text": ("TEXT_EDITOR", "Text Editor"),
    "graph": ("FCURVES", "Graph Editor"), "dope_sheet": ("DOPESHEET", "Dope Sheet"),
    "nla": ("NLA_EDITOR", "Nonlinear Animation"), "timeline": ("TIMELINE", "Timeline"),
    "spreadsheet": ("SPREADSHEET", "Spreadsheet"), "asset_browser": ("ASSETS", "Asset Browser"),
    "file_browser": ("FILES", "File Browser"),
}
TABS = {
    "tool": ("TOOL", "Tool"), "render": ("RENDER", "Render"), "output": ("OUTPUT", "Output"),
    "view_layer": ("VIEW_LAYER", "View Layer"), "scene": ("SCENE", "Scene"), "world": ("WORLD", "World"),
    "collection": ("COLLECTION", "Collection"), "object": ("OBJECT", "Object"),
    "modifiers": ("MODIFIER", "Modifiers (wrench)"), "particles": ("PARTICLES", "Particles"),
    "physics": ("PHYSICS", "Physics"), "constraints": ("CONSTRAINT", "Object Constraints"),
    "data": ("DATA", "Object Data (green icon)"), "bone": ("BONE", "Bone"),
    "bone_constraints": ("BONE_CONSTRAINT", "Bone Constraints"), "material": ("MATERIAL", "Material"),
    "texture": ("TEXTURE", "Texture"),
}
_KEEP = {"VIEW_3D", "PROPERTIES", "OUTLINER", "TOPBAR", "STATUSBAR"}


def _host_area(screen, ui_type):
    """The area that shows ui_type: one already showing it, else the biggest one the user can spare."""
    for area in screen.areas:
        if area.ui_type == ui_type:
            return area, False
    spare = [a for a in screen.areas if a.type not in _KEEP]
    if not spare:
        spare = [a for a in screen.areas if a.type not in {"VIEW_3D", "TOPBAR", "STATUSBAR"}]
    if not spare:
        raise ToolError("This screen has no area to turn into another editor besides the 3D View. Ask the user to "
                        "split one (View › Area › Vertical Split).")
    area = max(spare, key=lambda a: a.width * a.height)
    return area, True


def _set_editor(area, ui_type, label):
    try:
        area.ui_type = ui_type
    except (TypeError, ValueError):
        raise ToolError(f"This Blender has no {label} editor.") from None


def _section(name):
    sections = [item.identifier for item in bpy.types.Preferences.bl_rna.properties["active_section"].enum_items]
    key = name.strip().upper().replace("-", "").replace(" ", "_").replace("&", "")
    key = {"SAVE_LOAD": "SAVE_LOAD", "SAVELOAD": "SAVE_LOAD", "SAVE__LOAD": "SAVE_LOAD", "FILEPATHS": "FILE_PATHS",
           "ADD_ONS": "ADDONS", "KEYMAPS": "KEYMAP", "THEME": "THEMES", "LIGHT": "LIGHTS"}.get(key, key)
    if key not in sections:
        raise ToolError(f"Preferences has no section {name}. Sections: {', '.join(s.lower() for s in sections)}.")
    return key


@tool("ui_navigate")
def ui_navigate(args, ctx):
    workspace_name = args.get("workspace")
    editor = args.get("editor")
    tab = args.get("properties_tab")
    section = args.get("preferences_section")
    if not any((workspace_name, editor, tab, section)):
        raise ToolError("Say where to go: workspace, editor, properties_tab or preferences_section.")
    if editor and editor not in EDITORS:
        raise ToolError(f"editor is one of: {', '.join(EDITORS)}.")
    if tab and tab not in TABS:
        raise ToolError(f"properties_tab is one of: {', '.join(TABS)}.")
    windows = _windows()
    if not windows:
        raise ToolError("No Blender window is open (Blender runs in the background), so there is no interface "
                        "to show.")
    window = bpy.context.window if bpy.context.window in windows else windows[0]
    screen = window.screen
    done = []
    if workspace_name:
        wanted = workspace_name.strip().lower()
        found = next((w for w in bpy.data.workspaces if w.name.lower() == wanted), None)
        if found is None:
            names = ", ".join(w.name for w in bpy.data.workspaces)
            raise ToolError(f"This file has no {workspace_name} workspace. It has: {names}. (The user can add one "
                            "with the + at the end of the workspace tabs.)")
        if window.workspace != found:
            # Applied when Blender next handles events; the new workspace's own screen is edited below.
            window.workspace = found
        screen = found.screens[0] if len(found.screens) else screen
        done.append(f"Switched to the {found.name} workspace (the tabs along the top).")
    if section:
        key = _section(section)
        with bpy.context.temp_override(window=window):
            bpy.ops.screen.userpref_show(section=key)
        title = bpy.types.Preferences.bl_rna.properties["active_section"].enum_items[key].name
        done.append(f"Opened Preferences › {title} (Edit › Preferences).")
    if editor:
        ui_type, label = EDITORS[editor]
        area, changed = _host_area(screen, ui_type)
        if changed:
            before = next((name for key, name in EDITORS.values() if key == area.ui_type),
                          area.type.replace("_", " ").title())
            _set_editor(area, ui_type, label)
            done.append(f"Turned the {before} area into the {label} (the Editor Type menu at an area's top-left "
                        "corner switches it back).")
        else:
            done.append(f"The {label} is already open on this screen.")
        area.tag_redraw()
    if tab:
        context_key, label = TABS[tab]
        area, changed = _host_area(screen, "PROPERTIES")
        if changed:
            _set_editor(area, "PROPERTIES", "Properties")
        space = area.spaces.active
        try:
            space.context = context_key
        except (TypeError, ValueError):
            active = bpy.context.view_layer.objects.active
            what = f"the active object ({active.name}, {active.type})" if active else "no active object"
            raise ToolError(f"The {label} tab is not available now — it depends on the active object, and there is "
                            f"{what}. Select a fitting object first.") from None
        area.tag_redraw()
        done.append(f"Showing the {label} tab of the Properties editor (the tab column on its left).")
    return "\n".join(done)


# ─── render ──────────────────────────────────────────────────────────────────────────────────────

FORMATS = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".exr": "OPEN_EXR", ".tif": "TIFF", ".tiff": "TIFF",
           ".webp": "WEBP", ".bmp": "BMP"}
PREVIEW_SIZE = 512


def _engines():
    """Every render engine id this Blender has. The engine enum is filled in at run time and lists only the
    built-in EEVEE when read from Python, so Workbench and the registered engines (Cycles…) are added here."""
    found = [item.identifier for item in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items]
    found.append("BLENDER_WORKBENCH")
    for cls in bpy.types.RenderEngine.__subclasses__():
        ident = getattr(cls, "bl_idname", "")
        if ident and ident not in found:
            found.append(ident)
    return found


def _engine(name):
    """The engine id this Blender knows: EEVEE is BLENDER_EEVEE_NEXT in 4.2–4.4 and BLENDER_EEVEE again in 5.0+."""
    items = _engines()
    if name in items:
        return name
    if name.startswith("BLENDER_EEVEE"):
        for alias in ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"):
            if alias in items:
                return alias
    raise ToolError(f"This Blender has no {name} engine. It has: {', '.join(items)}.")


class _Overrides:
    """Scene settings changed for one render, put back in reverse order afterwards."""

    def __init__(self):
        self.saved = []

    def set(self, struct, key, value):
        self.saved.append((struct, key, getattr(struct, key)))
        setattr(struct, key, value)

    def keep(self, struct, key):
        """Remembers a setting that another change may move (a file format changes the colour mode)."""
        self.saved.append((struct, key, getattr(struct, key)))

    def restore(self):
        for struct, key, value in reversed(self.saved):
            try:
                setattr(struct, key, value)
            except (TypeError, ValueError, AttributeError, ReferenceError):
                pass
        self.saved.clear()


def _prepare(args, scene):
    """Applies the render's settings. Returns (overrides, file to write, what the result says)."""
    kind = args.get("kind") or "preview"
    if kind not in {"preview", "still"}:
        raise ToolError("kind is \"preview\" or \"still\".")
    render = scene.render
    if kind == "still":
        output = args.get("output")
        if not output:
            stem = os.path.splitext(os.path.basename(bpy.data.filepath))[0] or "render"
            output = f"//renders/{stem}_{args.get('frame', scene.frame_current):04d}.png"
        path = targets.file_path(output, write=True)
        file_format = FORMATS.get(os.path.splitext(path)[1].lower())
        if file_format is None:
            raise ToolError(f"output must end in one of: {', '.join(FORMATS)}.")
        os.makedirs(os.path.dirname(path), exist_ok=True)
    else:
        output = None
        path = os.path.join(tempfile.mkdtemp(prefix="zetta3d-render-"), "preview.jpg")
        file_format = "JPEG"
    o = _Overrides()
    try:
        if args.get("engine"):
            o.set(render, "engine", _engine(args["engine"]))
        resolution = args.get("resolution")
        if resolution is not None:
            if not isinstance(resolution, list) or len(resolution) != 2 or not all(
                    isinstance(v, int) and 16 <= v <= 16384 for v in resolution):
                raise ToolError("resolution is [width, height], each 16–16384.")
            o.set(render, "resolution_x", resolution[0])
            o.set(render, "resolution_y", resolution[1])
        percentage = args.get("percentage")
        if percentage is not None:
            o.set(render, "resolution_percentage", max(1, min(100, int(percentage))))
        elif kind == "preview":
            longest = max(render.resolution_x, render.resolution_y)
            o.set(render, "resolution_percentage", max(1, min(100, PREVIEW_SIZE * 100 // longest)))
        samples = args.get("samples")
        cycles = render.engine == "CYCLES" and hasattr(scene, "cycles")
        eevee = render.engine.startswith("BLENDER_EEVEE") and hasattr(scene, "eevee")
        if samples is None and kind == "preview":
            samples = 32 if cycles else 16  # a preview is for checking light and materials, not noise
        if samples is not None:
            if cycles:
                o.set(scene.cycles, "samples", max(1, int(samples)))
            elif eevee:
                o.set(scene.eevee, "taa_render_samples", max(1, int(samples)))
        frame = args.get("frame")
        if frame is not None:
            o.set(scene, "frame_current", int(frame))
        settings = render.image_settings
        o.keep(settings, "color_mode")
        o.keep(settings, "color_depth")
        o.keep(settings, "quality")
        o.set(settings, "file_format", file_format)
        if file_format == "JPEG":
            settings.quality = 85
        o.set(render, "filepath", path)
        o.set(render, "use_file_extension", True)
    except Exception:
        o.restore()
        raise
    w = round(render.resolution_x * render.resolution_percentage / 100)
    h = round(render.resolution_y * render.resolution_percentage / 100)
    summary = f"frame {scene.frame_current} with {render.engine} at {w}×{h}"
    return kind, o, path, output, summary


def _finished(kind, path, output, summary, started, cancelled=False):
    """The render's result for the model, once Blender has written the file."""
    seconds = time.time() - started
    if cancelled:
        return Result("The render was cancelled before it finished (the user pressed Esc, or it failed — see "
                      "recent_errors).", error=True)
    if not os.path.isfile(path):
        return Result(f"The render finished but wrote no file to {output or 'the preview'} — see recent_errors.",
                      error=True)
    if kind == "preview":
        with open(path, "rb") as handle:
            picture = handle.read()
        try:
            os.remove(path)
        except OSError:
            pass
        return Result(f"Preview: {summary}, in {seconds:.1f}s.", images=[(picture, "image/jpeg")])
    shown = targets.relative(path)
    changes.note(f"Rendered {shown}")
    return Result(f"Rendered {summary} in {seconds:.1f}s; saved {shown}.", label=f"Rendered {shown}")


@tool("render")
def render(args, ctx):
    scene = bpy.context.scene
    if scene.camera is None:
        raise ToolError("The scene has no camera to render from. Add one with create_object kind \"camera\".")
    kind, overrides, path, output, summary = _prepare(args, scene)
    started = time.time()
    if bpy.app.background or not _windows():
        # No window to keep responsive: render here, then put the settings back.
        try:
            bpy.ops.render.render(write_still=True)
        finally:
            overrides.restore()
        return _finished(kind, path, output, summary, started)

    state = {"end": None}

    def on_complete(*_):
        state["end"] = "complete"  # may run on the render thread: only a flag here

    def on_cancel(*_):
        state["end"] = "cancel"

    handlers = bpy.app.handlers
    handlers.render_complete.append(on_complete)
    handlers.render_cancel.append(on_cancel)

    def unhook():
        for chain, fn in ((handlers.render_complete, on_complete), (handlers.render_cancel, on_cancel)):
            if fn in chain:
                chain.remove(fn)

    window = bpy.context.window if bpy.context.window in _windows() else _windows()[0]
    try:
        with bpy.context.temp_override(window=window):
            outcome = bpy.ops.render.render("INVOKE_DEFAULT", write_still=True)
    except RuntimeError as error:
        unhook()
        overrides.restore()
        raise ToolError(f"Blender could not start the render: {error}") from None
    if "CANCELLED" in outcome:
        unhook()
        overrides.restore()
        raise ToolError("Blender could not start the render — another render may be running.")

    def poll():
        running = bpy.app.is_job_running("RENDER") if hasattr(bpy.app, "is_job_running") else True
        if state["end"] is None and (running or time.time() - started < 2):
            return 0.25
        unhook()
        with changes.running():
            overrides.restore()
            ctx.finish(_finished(kind, path, output, summary, started, cancelled=state["end"] == "cancel"))
        return None

    bpy.app.timers.register(poll, first_interval=0.25)
    return LATER

