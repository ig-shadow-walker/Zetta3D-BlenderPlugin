"""Image Studio: generate, edit and compose pictures — textures, references, concept art — and the recent ones.

Results land in the 2D library on zetta3d.ai. Saving one writes it into the images folder and loads it as a
Blender image; "Use for 3D" hands it to Generate 3D.
"""

import base64

from .. import importer
from ..api import catalog, zetta
from ..core import tasks
from . import common, context, pictures

STYLE_PREFIX = "img_style"
EDIT_SLOT = "img_edit0"
COMPOSE_PREFIX = "img_compose"
MAX_STYLE = 3
MAX_COMPOSE = 14

results = []  # [{picture, aspect, resolution, prompt}], newest first
recent = {"items": [], "loading": False, "error": None, "loaded": False}


def chosen_model(state):
    """The image model the user picked, or None for the server's default."""
    return None if state.img_model in ("", "default") else state.img_model


def locked():
    return context.gate2("allowsImageEditing") is False


LOCK_TEXT = "Editing and composing are available on a paid plan. Generating stays free."


def resolution_lock(state, resolution):
    model = context.image_model(chosen_model(state) or context.default_image_model())
    allowed = (model or {}).get("resolutions") or []
    if allowed and resolution not in allowed:
        return f"Not available on {(model or {}).get('label') or 'this model'}."
    if resolution == "4K" and context.gate2("allows4kImages") is False:
        return "4K is available on a paid plan. 1K and 2K stay free."
    return None


def problem(state):
    status = common.status("images")
    if status.busy:
        return status.busy
    if state.img_mode in ("edit", "compose") and locked():
        return LOCK_TEXT
    if not state.img_prompt.strip():
        return "Write a prompt first."
    if state.img_mode == "edit" and pictures.get(EDIT_SLOT) is None:
        return "Add the image to edit."
    if state.img_mode == "compose" and len(pictures.with_prefix(COMPOSE_PREFIX)) < 2:
        return "Add at least two images."
    if chosen_model(state):
        model = context.image_model(chosen_model(state))
        if model and model.get("locked"):
            return model.get("lockedReason") or f"{model.get('label')} is available on a paid plan."
    return None


def add_picture(prefix, picture, limit):
    """Appends to a numbered list of pictures. Returns an error, or None."""
    existing = pictures.with_prefix(prefix)
    if picture.post_id is not None and any(p.post_id == picture.post_id for p in existing):
        return None
    reason = picture.problem()
    if reason:
        return reason
    if len(existing) >= limit:
        return f"Only {limit} images are allowed here, so one was left out."
    pictures.set_list(prefix, existing + [picture])
    return None


def remove_picture(prefix, index):
    existing = pictures.with_prefix(prefix)
    if 0 <= index < len(existing):
        existing.pop(index)
        pictures.set_list(prefix, existing)


def submit(state):
    status = common.status("images")
    reason = problem(state)
    if reason:
        status.fail(common.Problem(reason))
        return
    mode, prompt = state.img_mode, state.img_prompt.strip()
    aspect = state.img_aspect
    resolution = state.img_resolution
    if resolution == "4K" and context.gate2("allows4kImages") is False:
        resolution = "2K"
    model = chosen_model(state)
    status.start("Generating…" if mode == "generate" else "Working…")
    styles = [p.inline() for p in pictures.with_prefix(STYLE_PREFIX)]
    edit = pictures.get(EDIT_SLOT)
    compose = pictures.with_prefix(COMPOSE_PREFIX)

    def work():
        if mode == "generate":
            return zetta.generate_image(prompt, aspect, resolution, model, styles)
        if mode == "edit":
            if edit.post_id is not None:
                return zetta.edit_image(prompt, source_post_id=edit.post_id, aspect_ratio=aspect,
                                        resolution=resolution, model=model)
            return zetta.edit_image(prompt, image=edit.inline(), aspect_ratio=aspect, resolution=resolution,
                                    model=model)
        inline = [p.inline() for p in compose if p.post_id is None]
        ids = [p.post_id for p in compose if p.post_id is not None]
        return zetta.compose_images(prompt, inline, ids, aspect, resolution, model)

    def done(result):
        found = [i for i in result.get("images") or [] if i.get("base64")]
        if not found:
            status.fail(common.Problem(result.get("text") or "No picture came back. Try a different prompt."))
            return
        for image in found:
            data = base64.b64decode(image["base64"])
            content_type = image.get("contentType") or pictures.sniff(data)
            picture = pictures.Picture(data, content_type, importer.slug(prompt) + pictures.extension_for(content_type),
                                       post_id=result.get("postId"))
            results.insert(0, {"picture": picture, "aspect": aspect, "resolution": resolution, "prompt": prompt})
        del results[8:]
        status.finish(None)
        context.refresh_credits()
        load_recent()

    tasks.run(work, done, status.fail, name="zetta3d-images")


def size_label(picture):
    size = len(picture.data)
    return f"{size / (1024 * 1024):.1f} MB" if size >= 1024 * 1024 else f"{size // 1024} KB"


def save(picture, prompt=None):
    """Writes the picture into the images folder and loads it as a Blender image."""
    image = importer.save_image(picture.data, prompt or picture.name or "image", picture.content_type)
    if picture.post_id is not None:
        image["zetta_image_id"] = int(picture.post_id)
    return image


def use_for_3d(state, picture):
    form = state.gen
    form.source_mode = "single"
    clean = pictures.Picture(picture.data, picture.content_type, picture.name)
    reason = pictures.put("generate_single", clean)
    state.tool = "generate"
    return reason


def edit_this(state, picture):
    pictures.put(EDIT_SLOT, picture, for_3d=False)
    state.img_mode = "edit"


def compose_with(state, picture):
    state.img_mode = "compose"
    return add_picture(COMPOSE_PREFIX, picture, MAX_COMPOSE)


# ─── Recent pictures (the 2D library's first page) ────────────────────────────────────────────────


def load_recent():
    if recent["loading"]:
        return
    recent.update(loading=True, error=None)

    def done(answer):
        recent.update(items=answer.get("items") or [], loading=False, loaded=True)

    def fail(error):
        recent.update(loading=False, loaded=True, error="Your library could not be loaded. " + catalog.explain(error))

    tasks.run(lambda: zetta.list_images(page=1, page_size=12), done, fail, name="zetta3d-recent-images")


def open_stored(post, on_done=None):
    """Downloads a library picture and shows it as a result."""
    stored = (post.get("images") or [{}])[0]
    url = stored.get("url")
    if not url:
        return
    status = common.status("images")
    status.start("Opening…")

    def done(data):
        content_type = stored.get("contentType") or pictures.sniff(data)
        prompt = post.get("prompt") or f"image-{post.get('id')}"
        picture = pictures.Picture(data, content_type, importer.slug(prompt) + pictures.extension_for(content_type),
                                   post_id=post.get("id"))
        results.insert(0, {"picture": picture, "aspect": post.get("aspectRatio") or "",
                           "resolution": post.get("resolution") or "", "prompt": prompt})
        del results[8:]
        status.finish(None)
        if on_done:
            on_done(picture)

    tasks.run(lambda: zetta.http.fetch_bytes(url, timeout=60), done, status.fail, name="zetta3d-open-image")
