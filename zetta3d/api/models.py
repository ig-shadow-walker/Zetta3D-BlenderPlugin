"""Helpers over the API's plain-dict rows (a generation, its result files)."""

from . import catalog

TERMINAL = frozenset({"success", "failed", "cancelled"})

STATUS_LABELS = {
    "pending": "Waiting",
    "queued": "Queued",
    "processing": "Generating",
    "saving": "Saving",
    "success": "Ready",
    "failed": "Failed",
    "cancelled": "Cancelled",
}

_PICTURE_TYPES = {"IMAGE", "WEBP", "PNG", "JPG", "JPEG", "UV_LAYOUT"}
_SPLAT_TYPES = {"SPLAT", "SPZ", "PLY"}


def is_terminal(gen):
    return (gen or {}).get("status") in TERMINAL


def is_success(gen):
    return (gen or {}).get("status") == "success"


def status_label(gen):
    status = (gen or {}).get("status") or ""
    return STATUS_LABELS.get(status, status)


def _shorten(text, limit):
    return text if len(text) <= limit else text[: limit - 1] + "…"


def display_title(gen):
    title = (gen.get("title") or "").strip()
    if title:
        return title
    prompt = (gen.get("inputPrompt") or "").strip()
    if prompt:
        return _shorten(prompt, 48)
    return f"{catalog.feature_label(gen.get('selectedFeature'))} #{gen.get('id')}"


# ─── Result files ─────────────────────────────────────────────────────────────────────────────────


def file_extension(f):
    name = f.get("key") or f.get("url") or ""
    name = name.split("?", 1)[0]
    if "." in name.rsplit("/", 1)[-1]:
        return name.rsplit(".", 1)[1].lower()
    return (f.get("folder") or "").lower()


def file_name(f):
    return (f.get("key") or "").rsplit("/", 1)[-1]


def is_picture(f):
    return (
        f.get("folder") in ("thumbnail", "gifs", "uv")
        or f.get("label") == "UV layout"
        or (f.get("type") or "").upper() in _PICTURE_TYPES
    )


def is_splat(f):
    return (
        f.get("folder") == "splat"
        or (f.get("type") or "").upper() in _SPLAT_TYPES
        or file_extension(f) in ("splat", "spz")
    )


def files(gen):
    return ((gen or {}).get("result") or {}).get("files") or []


def mesh_files(gen):
    return [f for f in files(gen) if not is_picture(f) and not is_splat(f)]


def uv_layout(gen):
    for f in files(gen):
        if f.get("label") == "UV layout" or f.get("folder") == "uv":
            return f
    return None


def is_splat_row(gen):
    if catalog.is_splat_feature(gen.get("selectedFeature")):
        return True
    all_files = files(gen)
    return bool(all_files) and not mesh_files(gen) and any(is_splat(f) for f in all_files)


def is_pipeline_made(gen):
    return gen.get("pipelineRunId") is not None


def spec_base(gen):
    value = ((gen.get("jobSpec") or {}).get("generation") or {}).get("base")
    return value if isinstance(value, str) else None


def spec_modifiers(gen):
    values = ((gen.get("jobSpec") or {}).get("generation") or {}).get("modifiers") or []
    return {v for v in values if isinstance(v, str)}


def thumbnail_url(gen):
    url = gen.get("thumbnailURL")
    if url:
        return url
    thumb = ((gen.get("result") or {}).get("thumbnail")) or {}
    return thumb.get("url")


def pick_mesh_file(gen, preferred="glb"):
    """The mesh file to import: the preferred format when the job made it, else GLB, else FBX, else the first."""
    meshes = mesh_files(gen)
    if not meshes:
        return None
    for ext in (preferred, "glb", "gltf", "fbx", "obj"):
        for f in meshes:
            if file_extension(f) == ext:
                return f
    return meshes[0]
