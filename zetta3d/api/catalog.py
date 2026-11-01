"""What the Studio offers, in Zetta's own vocabulary.

Mirrors the web Studio (Zetta3D-Frontend src/lib/model-capabilities.ts, post-process.ts) and the backend's
model-capabilities.ts / face-limits.ts. Engine vendors are never named to users.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelInfo:
    id: str
    label: str
    blurb: str
    paid: bool = False
    low_poly: bool = False
    quad_any_base: bool = False
    detailed_geometry: bool = False
    detailed_texture: bool = False
    extreme_texture: bool = False
    image_tuning: bool = False
    parts: bool = False
    bare_prompt: bool = False


N1 = ModelInfo("n1", "Zetta N1", "Base model", low_poly=True, bare_prompt=True)
N25 = ModelInfo(
    "n25", "Zetta N2.5", "Highest quality of mesh and texture", paid=True, low_poly=True, quad_any_base=True,
    detailed_geometry=True, detailed_texture=True, image_tuning=True, parts=True,
)
NP1 = ModelInfo(
    "np1", "Zetta Np1", "Clean topology, balanced quality", paid=True, detailed_texture=True,
    extreme_texture=True, image_tuning=True,
)
NP15 = ModelInfo(
    "np15", "Zetta Np1.5", "Clean topology — quad native", paid=True, quad_any_base=True, detailed_texture=True,
    extreme_texture=True, image_tuning=True,
)

GENERATE_MODELS = (N1, N25)
SMART_MODELS = (NP1, NP15)
_MODELS = {m.id: m for m in (N1, N25, NP1, NP15)}


def model(model_id):
    return _MODELS.get(model_id, N1)


SMART_UV_MAX_TRIANGLES = 80_000


def is_splat_feature(feature):
    """Rows that are Gaussian splats (made on the web) rather than meshes — Blender can't use them here."""
    return feature in ("imageToSplat", "worldSplatExport")


MODEL_LABELS = {
    "n1": "Zetta N1",
    "n25": "Zetta N2.5",
    "np1": "Zetta Np1",
    "np15": "Zetta Np1.5",
    "r1": "Zetta R1",
    "zs2": "Zetta Splat 2",
    "zs21": "Zetta Splat 2.1",
}


def model_label(model_id):
    return MODEL_LABELS.get(model_id, model_id or "")


def face_range(model_id, quality, quads):
    """Face-count bounds per model / quality rung / polygon type (backend face-limits.ts)."""
    if model_id == "n25":
        if quality == "low-poly":
            return (500, 10_000) if quads else (1_000, 20_000)
        if quality == "ultra":
            return (1_000, 150_000) if quads else (1_000, 2_000_000)
        return (1_000, 150_000) if quads else (1_000, 1_000_000)
    if model_id == "np1":
        return (48, 20_000)
    if model_id == "np15":
        return (48, 25_000) if quads else (48, 50_000)
    return (3_100, 1_500_000)


def _quality_preset(quality):
    return {"low-poly": 9_000, "ultra": 1_000_000}.get(quality, 200_000)


def face_preset(model_id, quality, quads):
    """The count the web Studio pre-fills for a model / rung / topology (defaultPolygonCount) — the baseline the
    paid custom_face_count modifier is measured against."""
    if model_id == "n25":
        return face_range(model_id, quality, quads)[1]
    if model_id == "np1":
        return 20_000
    if model_id == "np15":
        return 25_000 if quads else 50_000
    return _quality_preset(quality)


QUALITIES = (("low-poly", "Low-Poly"), ("high-res", "High-Res"), ("ultra", "Ultra"))

RIG_TYPES = (
    ("biped", "Humanoid"),
    ("quadruped", "Four legs"),
    ("avian", "Bird"),
    ("serpentine", "Snake"),
    ("aquatic", "Fish"),
    ("hexapod", "Six legs"),
    ("octopod", "Eight legs"),
    ("others", "Other creature"),
)
HUMANOID_RIG_TYPE = "biped"

# The bone namings a humanoid can be rigged with — every preset is a humanoid skeleton.
RIG_SPECS = (
    ("standard", "Standard", "The rigger's own skeleton, finger bones included."),
    ("mixamo", "Mixamo", "For Mixamo and libraries that follow it."),
    ("unity", "Unity Humanoid", "Maps straight onto a Unity Humanoid avatar."),
    ("unreal", "UE5 Mannequin", "Unreal Engine 5's Mannequin skeleton."),
    ("vrm", "VRM 1.0", "For VRM avatars and VTuber tools."),
    ("actorcore", "ActorCore", "For ActorCore motion libraries."),
)

ORIENTATIONS = ("+x", "-x", "+y", "-y")

ASPECT_RATIOS = ("1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "4:5", "5:4", "21:9")
RESOLUTIONS = ("1K", "2K", "4K")

# Features whose rows can be post-processed (post-process.ts).
PROCESSABLE_FEATURES = frozenset(
    {"txt23d", "img23d", "multiView23d", "retopology", "upload", "uvUnwrap", "worldMeshExport"}
)

FEATURE_LABELS = {
    "img23d": "Image to 3D",
    "txt23d": "Text to 3D",
    "multiView23d": "Multi-view to 3D",
    "retopology": "Retopology",
    "segmentation": "Segmentation",
    "smartSegmentation": "Smart Segmentation",
    "uvUnwrap": "Smart UV",
    "imageToSplat": "Gaussian Splat",
    "worldSplatExport": "Splat export (PLY)",
    "worldMeshExport": "World mesh",
    "retexture": "AI Texturing",
    "autoRigging": "AI Rigging",
    "convertFormat": "Converted",
    "upload": "Uploaded model",
}


def feature_label(feature):
    return FEATURE_LABELS.get(feature, "Model")


def explain(error):
    """Plain-language messages for the API's error codes."""
    body = getattr(error, "body", None) or {}
    code = getattr(error, "code", None)
    if code == "INSUFFICIENT_CREDITS":
        required, available = body.get("required"), body.get("available")
        if required is not None:
            return f"This needs {required} credits and you have {available}. Top up or upgrade at zetta3d.ai/pricing."
        return "Not enough credits. Top up or upgrade at zetta3d.ai/pricing."
    if code == "JOB_LIMIT_REACHED":
        return (f"You already have {body.get('limit')} job(s) running — the most your plan runs at once. "
                "Wait for one to finish, or upgrade.")
    if code == "PLAN_CAPABILITY_REQUIRED":
        violations = body.get("violations") or []
        first = violations[0].get("message") if violations and isinstance(violations[0], dict) else None
        return (first or "Your plan does not include this option.") + " Upgrade at zetta3d.ai/pricing."
    if code == "GEN3D_NOT_READY":
        return "That model is not ready yet."
    if code == "EARLY_ACCESS_REQUIRED":
        return ("This feature is in early access. Request access and we'll let you know the moment you're in. "
                "Nothing was charged.")
    if code == "FEATURE_UNAVAILABLE":
        return ("This kind of generation is temporarily unavailable while we work on it. Nothing was charged — "
                "please try again later.")
    return str(getattr(error, "message", None) or error)
