"""The forms' state, on the window manager: kept for the session, never saved into a .blend (a prompt is not
scene data). Pictures and chosen source models live in Python (studio.pictures, studio.tools), not here.
"""

import bpy
from bpy.props import BoolProperty, EnumProperty, IntProperty, PointerProperty, StringProperty

from .api import catalog

_guard = set()


def _guarded(name):
    """Runs an update callback once, ignoring the updates it causes itself."""

    def wrap(fn):
        def update(self, context):
            key = (name, self.as_pointer())
            if key in _guard:
                return
            _guard.add(key)
            try:
                fn(self, context)
            finally:
                _guard.discard(key)

        return update

    return wrap


# ─── Generate / Smart 3D ──────────────────────────────────────────────────────────────────────────


def _model_items(self, _context):
    models = catalog.SMART_MODELS if self.is_smart else catalog.GENERATE_MODELS
    return [(m.id, m.label + (" · New" if m.id == "np15" else ""), m.blurb) for m in models]


@_guarded("model")
def _model_changed(self, context):
    from .studio import generate

    self.model_picked = True
    generate.apply_model(self)


@_guarded("quality")
def _quality_changed(self, context):
    from .studio import generate

    generate.apply_quality(self)


@_guarded("polygon")
def _polygon_changed(self, context):
    from .studio import generate

    generate.apply_polygon_type(self)


@_guarded("count")
def _count_changed(self, context):
    from .studio import generate

    self.face_typed = True
    generate.apply_count(self)


@_guarded("parts")
def _parts_changed(self, context):
    from .studio import generate

    generate.apply_invariants(self)


def _output_changed(self, context):
    self.output_picked = True


def _private_changed(self, context):
    self.private_touched = True


def _title_changed(self, context):
    self.title_touched = True


class ZETTA_PG_generate(bpy.types.PropertyGroup):
    is_smart: BoolProperty(default=False)
    source_mode: EnumProperty(
        name="Input",
        items=[("single", "Text / Image", "A prompt or one picture"), ("multi", "Multi-View", "Pictures of each side")],
        default="single",
    )
    prompt: StringProperty(name="Describe the model", maxlen=1024,
                           description="e.g. A weathered medieval helmet, battle-worn steel")
    title: StringProperty(name="Name", maxlen=255, description="Name this generation", update=_title_changed)
    title_touched: BoolProperty(default=False)
    remove_background: BoolProperty(
        name="Remove background", default=True,
        description="The background is removed from the picture before it is uploaded")
    autofix_image: BoolProperty(name="Enhance input image · Free", default=False)
    model: EnumProperty(name="Generation Engine", items=_model_items, update=_model_changed)
    model_picked: BoolProperty(default=False)
    output: EnumProperty(
        name="Output",
        items=[("geometry", "Geometry", "Mesh only, no texture"), ("textured", "Textured", "Mesh and textures")],
        default="textured", update=_output_changed,
    )
    output_picked: BoolProperty(default=False)
    segment_parts: BoolProperty(name="Segmented mesh", default=False, description="Split mesh to separate parts",
                                update=_parts_changed)
    quality: EnumProperty(
        name="Quality",
        items=[(q, label, "") for q, label in catalog.QUALITIES],
        default="high-res", update=_quality_changed,
    )
    face_count: StringProperty(name="Polygon count", maxlen=12, update=_count_changed,
                               description="Empty = the engine's default for this quality")
    face_typed: BoolProperty(default=False)
    polygon_type: EnumProperty(
        name="Polygons",
        items=[("triangle", "Triangles", ""), ("quadrilateral", "Quads", "")],
        default="triangle", update=_polygon_changed,
    )
    pbr: BoolProperty(name="PBR", default=True,
                      description="Physically based materials: roughness, metalness and normal maps")
    texture_quality: EnumProperty(
        name="Texture quality",
        items=[("standard", "Standard", "Up to 2K"), ("detailed", "Detailed", "Up to 4K"), ("extreme", "8K", "Up to 8K")],
        default="standard",
    )
    private: BoolProperty(name="Keep this model private", default=False, update=_private_changed)
    private_touched: BoolProperty(default=False)
    real_world_scale: BoolProperty(name="Real-world scale · Free", default=False)
    compress_geometry: BoolProperty(name="Smaller file · Free", default=False)
    export_uv: BoolProperty(name="Export UV map · Free", default=True)
    delight: BoolProperty(name="Remove baked-in lighting · Free", default=False)
    texture_alignment: EnumProperty(
        name="Texture fit",
        items=[("geometry", "Fit texture to mesh", ""), ("original_image", "Fit texture to picture", "")],
        default="geometry",
    )
    control_mode: BoolProperty(
        name="Control Mode", default=True,
        description="Draw a picture from the prompt first and stop to show it, before any 3D credits are spent")
    show_polygons: BoolProperty(name="Polygon control", default=False)
    show_advanced: BoolProperty(name="Advanced controls", default=False)


# ─── Everything else ──────────────────────────────────────────────────────────────────────────────

TOOLS = [
    ("generate", "Generate 3D", "Turn a prompt or a picture into a textured 3D model", "MESH_MONKEY", 0),
    ("smart", "Smart 3D", "Clean, production-ready topology from a picture or a prompt", "MOD_REMESH", 1),
    ("segment", "Segment", "Split a model into separate, named parts", "MOD_EXPLODE", 2),
    ("retopology", "Retopology", "Rebuild the mesh with clean, even topology", "MOD_DECIM", 3),
    ("smart_uv", "Smart UV", "Lay the model's UVs out again as a clean new layout", "UV", 4),
    ("texture", "Texture", "Paint a new texture onto a model", "TEXTURE", 5),
    ("rig", "Auto Rig", "Add a skeleton and skin weights", "ARMATURE_DATA", 6),
    ("images", "Image Studio", "Generate, edit and compose pictures", "IMAGE_DATA", 7),
]


def _image_model_items(self, _context):
    from .studio import context as studio

    items = [("default", "Default", "The server's default image model")]
    for model in studio.image_model_list():
        label = model.get("label") or model.get("id")
        if model.get("locked"):
            label += " (locked)"
        items.append((model["id"], label, model.get("lockedReason") or ""))
    return items


def _feature_items():
    features = ("txt23d", "img23d", "multiView23d", "retopology", "segmentation", "smartSegmentation", "uvUnwrap",
                "retexture", "autoRigging", "convertFormat", "upload")
    return [("all", "All kinds", "")] + [(f, catalog.feature_label(f), "") for f in features]


def _library_search_changed(self, context):
    from . import library

    library.reload(reset_page=True)


def _library_filter_changed(self, context):
    from . import library

    library.reload(reset_page=True)


def _image_filter_changed(self, context):
    from . import library

    library.reload_images(reset=True)


class ZETTA_PG_state(bpy.types.PropertyGroup):
    tool: EnumProperty(name="Tool", items=TOOLS, default="generate")
    auto_import: BoolProperty(name="Import when ready", default=True,
                              description="Download the finished model into this file automatically")
    place: BoolProperty(name="Place at the 3D cursor", default=True,
                        description="Put the imported model at the 3D cursor, sitting on the ground")

    gen: PointerProperty(type=ZETTA_PG_generate)
    smart: PointerProperty(type=ZETTA_PG_generate)

    # Segment
    seg_type: EnumProperty(
        name="Segmentation",
        items=[("segment", "Segment", "Splits the model you have into named parts"),
               ("smart", "Smart Segment", "Rebuilds as it splits, for cleaner parts on a rough or incomplete mesh")],
        default="segment",
    )
    seg_granularity: EnumProperty(
        name="How many parts",
        items=[("simple", "Few parts", ""), ("balanced", "Balanced", ""), ("detailed", "Max parts", "")],
        default="balanced",
    )
    seg_split: BoolProperty(name="Also split pieces that are not joined", default=True)
    seg_hint: StringProperty(name="What is this?", maxlen=1024,
                             description="Optional. e.g. game character with sword and armor")

    # Retopology
    retopo_engine: EnumProperty(
        name="Engine",
        items=[("r1", "Zetta R1", "Up to 20,000 polygons. Faster, and less than half the price."),
               ("n1", "Zetta N1", "No polygon ceiling — the option for a dense mesh.")],
        default="r1",
    )
    retopo_polygon: EnumProperty(name="Polygons", items=[("triangle", "Triangles", ""), ("quadrilateral", "Quads", "")],
                                 default="triangle")
    retopo_faces: StringProperty(name="Polygon count", default="8000", maxlen=12)
    retopo_bake: BoolProperty(name="Bake textures onto the rebuilt mesh", default=True,
                              description="Carries the original colours over to the new mesh. Off returns bare geometry.")
    retopo_level: EnumProperty(
        name="Level of detail",
        items=[("low", "Low", "Fewest polygons — game-ready."), ("medium", "Medium", "A balance of the two."),
               ("high", "High", "Keeps the most detail.")],
        default="medium",
    )

    # Smart UV
    uv_orientation: EnumProperty(
        name="Orientation", description="Which way the model faces in the exported file",
        items=[("as-is", "As is", ""), ("+x", "+X", ""), ("-x", "−X", ""), ("+y", "+Y", ""), ("-y", "−Y", "")],
        default="as-is",
    )
    uv_bake: BoolProperty(name="Bake textures", default=True)
    uv_compress: BoolProperty(name="Compress geometry", default=False,
                              description="A smaller file, through mesh compression")

    # Texture
    tex_mode: EnumProperty(name="Reference",
                           items=[("single", "Single image", ""), ("multi", "Multi-view", "")], default="single")
    tex_prompt: StringProperty(name="Describe the surface", maxlen=1000,
                               description="e.g. worn brass with green patina settling in the recesses")
    tex_tier: EnumProperty(
        name="Texture quality",
        items=[("retexture", "Standard", "Up to 2K"), ("retexture_hd", "Detailed", "Up to 4K"),
               ("retexture_8k", "8K", "Up to 8K")],
        default="retexture",
    )
    tex_fast: BoolProperty(name="Faster, at some detail — same price", default=False)
    tex_alignment: EnumProperty(
        name="Texture fit",
        items=[("geometry", "Fit texture to mesh", ""), ("original_image", "Fit texture to picture", "")],
        default="original_image",
    )
    tex_pbr: BoolProperty(name="PBR materials", default=True)
    tex_delight: BoolProperty(name="Remove the reference's baked-in lighting", default=True)
    tex_bake: BoolProperty(name="Bake effects into the textures", default=True)
    tex_remove_bg: BoolProperty(name="Remove background before applying", default=True)

    # Auto Rig
    rig_type: EnumProperty(name="Body", items=[(k, label, "") for k, label in catalog.RIG_TYPES], default="biped")
    rig_spec: EnumProperty(name="Bone naming", items=[(k, label, hint) for k, label, hint in catalog.RIG_SPECS],
                           default="standard")
    rig_format: EnumProperty(name="Rigged file", items=[("glb", "GLB", ""), ("fbx", "FBX", "")], default="glb")

    # Image Studio
    img_mode: EnumProperty(
        name="Mode",
        items=[("generate", "Generate", "A new picture from your prompt."),
               ("edit", "Edit", "The picture is changed. Describe what should be different."),
               ("compose", "Compose", "Several pictures are combined into one new picture.")],
        default="generate",
    )
    img_prompt: StringProperty(name="Prompt", maxlen=5000)
    img_aspect: EnumProperty(
        name="Aspect ratio",
        items=[("9:16", "9:16 · Story", ""), ("2:3", "2:3", ""), ("3:4", "3:4", ""), ("4:5", "4:5 · Post", ""),
               ("1:1", "1:1 · Square", ""), ("5:4", "5:4", ""), ("4:3", "4:3", ""), ("3:2", "3:2", ""),
               ("16:9", "16:9 · Wide", ""), ("21:9", "21:9 · Cinema", "")],
        default="1:1",
    )
    img_resolution: EnumProperty(name="Resolution", items=[(r, r, "") for r in catalog.RESOLUTIONS], default="1K")
    img_model: EnumProperty(name="Model", items=_image_model_items)

    # AI edit (inline, for a Generate / multi-view picture)
    aiedit_instruction: StringProperty(name="Change", maxlen=2000,
                                       description="Make the stripes blue, add a flag on top…")

    # Source picker
    src_search: StringProperty(name="Search", description="Search your library")

    # Library
    lib_kind: EnumProperty(name="Library", items=[("models", "3D models", ""), ("images", "Images", "")],
                           default="models")
    lib_search: StringProperty(name="Search", description="Search by title or prompt", update=_library_search_changed)
    lib_feature: EnumProperty(name="Kind", items=_feature_items(), update=_library_filter_changed)
    lib_status: EnumProperty(
        name="Status",
        items=[("all", "All", ""), ("ready", "Ready", ""), ("running", "In progress", ""), ("failed", "Failed", "")],
        default="all", update=_library_filter_changed,
    )
    lib_image_feature: EnumProperty(
        name="Images",
        items=[("all", "All images", ""), ("genImg", "Generated", ""), ("editImg", "Edited", ""),
               ("composeImg", "Composed", "")],
        update=_image_filter_changed,
    )
    lib_columns: IntProperty(name="Columns", default=2, min=1, max=6)
    lib_selecting: BoolProperty(name="Select", default=False)

    # Pipelines
    pipe_describe: StringProperty(name="Describe it", maxlen=2000)
    pipe_title: StringProperty(name="Title", maxlen=200)
    pipe_view: EnumProperty(name="View", items=[("recipe", "Recipe", ""), ("runs", "Runs", "")], default="recipe")
    pipe_run_filter: EnumProperty(
        name="Runs",
        items=[("all", "All", ""), ("running", "Running", ""), ("attention", "Needs you", ""), ("done", "Done", ""),
               ("stopped", "Stopped", "")],
        default="all",
    )
    pipe_extra_part: StringProperty(name="Another part", maxlen=80)


CLASSES = (ZETTA_PG_generate, ZETTA_PG_state)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.WindowManager.zetta = PointerProperty(type=ZETTA_PG_state)


def unregister():
    del bpy.types.WindowManager.zetta
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


def state(context=None):
    return (context or bpy.context).window_manager.zetta


def initialise():
    """Marks the Smart 3D form as such (not allowed from draw code, so done once the add-on is up and after a file
    loads — a new window manager comes with each file)."""
    wm = getattr(bpy.context, "window_manager", None)
    if wm is None:
        return
    zetta = wm.zetta
    if not zetta.smart.is_smart:
        zetta.smart.is_smart = True
        zetta.smart.quality = "high-res"
    from .core import prefs

    zetta.place = bool(prefs.get("place_in_scene", True))
