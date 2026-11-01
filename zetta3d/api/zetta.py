"""Typed calls for every Zetta 3D route the add-on uses. Run them on worker threads (core.tasks.run)."""

import base64
import uuid

from .. import config
from ..core import http
from ..core.http import ApiError

# ─── Account ──────────────────────────────────────────────────────────────────────────────────────


def me():
    return (http.get("/auth/me") or {}).get("user")


# ─── 3D: catalogue, gates, prices ─────────────────────────────────────────────────────────────────


def entitlements_3d():
    return (http.get("/3d/entitlements") or {}).get("entitlements") or {}


def pricing():
    return (http.get("/3d/pricing") or {}).get("items") or []


def quote(base, modifiers, model=None):
    body = {"generation": {"base": base, "modifiers": list(dict.fromkeys(modifiers))}}
    if model:
        body["model"] = model
    return http.post("/3d/quote", body) or {}


# ─── 3D: uploads ──────────────────────────────────────────────────────────────────────────────────


def create_upload(content_type):
    return http.post("/3d/uploads", {"contentType": content_type}) or {}


def upload(data, content_type, progress=None, token=None):
    """Uploads through the API's relay; returns the ticket, whose `key` names the stored file and whose
    `fileUrl` the engine reads for 24h. An expired ticket is renewed once."""
    ticket = create_upload(content_type)
    try:
        http.put_bytes(ticket["uploadUrl"], data, ticket.get("headers"), progress, token)
    except ApiError as error:
        if error.code != "UPLOAD_TICKET_EXPIRED":
            raise
        ticket = create_upload(content_type)
        http.put_bytes(ticket["uploadUrl"], data, ticket.get("headers"), progress, token)
    return ticket


def create_upload_post(key, size, title=None, feature="upload"):
    """Turns an uploaded GLB into a finished library row (free)."""
    body = {"key": key, "bytes": int(size), "feature": feature}
    if title and title.strip():
        body["title"] = title.strip()[:120]
    return (http.post("/3d/uploads/post", body) or {}).get("generation")


# ─── 3D: jobs ─────────────────────────────────────────────────────────────────────────────────────


def create_generation(body, idempotency_key=None):
    """Creates a generation or post-process job (a CreateGenerationDto). The origin is stamped here so every job
    the add-on starts is attributed to Blender. Returns {generation, quote, charged}."""
    body = dict(body)
    body["origin"] = config.CLIENT_NAME
    headers = {"Idempotency-Key": idempotency_key or str(uuid.uuid4())}
    return http.post("/3d/generations", body, headers=headers) or {}


def get_generation(generation_id):
    """(generation, queuePosition or None)."""
    answer = http.get(f"/3d/generations/{int(generation_id)}") or {}
    position = answer.get("queuePosition")
    return answer.get("generation"), position if isinstance(position, int) else None


def list_generations(page=1, page_size=24, search=None, feature=None, statuses=None, group_runs=False):
    params = {"page": page, "pageSize": page_size, "sortDir": "DESC"}
    # Each pipeline run as one entry carrying its models (runGroup), so a run is never split across pages.
    if group_runs:
        params["groupRuns"] = True
    if search and search.strip():
        params["search"] = search.strip()
    if feature:
        params["feature"] = feature
    if statuses:
        params["status"] = list(statuses)
    return http.get("/3d/generations?" + http.query(params)) or {}


def download_url(generation_id, key=None):
    """A fresh signed URL (15 min) for one of a job's files; no key = the main mesh."""
    path = f"/3d/generations/{int(generation_id)}/download"
    if key:
        path += "?" + http.query({"key": key})
    return (http.get(path) or {}).get("url")


def update_generation(generation_id, title=None, tags=None, availability=None):
    body = {}
    if title is not None:
        body["title"] = title
    if tags is not None:
        body["tags"] = list(tags)
    if availability is not None:
        body["availability"] = availability
    return (http.patch(f"/3d/generations/{int(generation_id)}", body) or {}).get("generation")


def cancel_generation(generation_id):
    return (http.post(f"/3d/generations/{int(generation_id)}/cancel") or {}).get("generation")


def delete_generation(generation_id):
    http.delete(f"/3d/generations/{int(generation_id)}")


# ─── Tools & AI helpers ───────────────────────────────────────────────────────────────────────────


def remove_background(image_bytes):
    """Background removal; returns PNG bytes."""
    answer = http.post("/tools/rembg", {"image": base64.b64encode(image_bytes).decode("ascii")}, timeout=120) or {}
    return base64.b64decode(answer.get("image") or "")


def _frames(jpegs):
    return [{"data": base64.b64encode(b).decode("ascii"), "mediaType": "image/jpeg"} for b in list(jpegs)[:4]]


def suggest_tags(jpeg_frames):
    answer = http.post("/ai/tags", {"frames": _frames(jpeg_frames)}, timeout=90) or {}
    category = answer.get("category") if isinstance(answer.get("category"), str) else None
    return category, answer.get("tags") or []


def rigging_precheck(jpeg_frames):
    answer = http.post("/ai/rigging-precheck", {"frames": _frames(jpeg_frames)}, timeout=90) or {}
    return answer.get("bodyType"), answer.get("confidence"), answer.get("reason")


# ─── Image Studio (2D) ────────────────────────────────────────────────────────────────────────────


def entitlements_2d():
    return (http.get("/2d/entitlements") or {}).get("entitlements") or {}


def image_models():
    """{models: [{id, label, resolutions, locked, lockedReason}], default}."""
    return http.get("/2d/models") or {}


def inline_image(data, content_type="image/png"):
    """An image going up in a request body: raw base64, no data: prefix."""
    return {"data": base64.b64encode(data).decode("ascii"), "contentType": content_type}


def _image_options(body, aspect_ratio, resolution, model, for_3d):
    # for3D pictures are forced square 1K (an object on the converter's square), so no frame is sent.
    if for_3d:
        body["for3D"] = True
    else:
        if aspect_ratio:
            body["aspectRatio"] = aspect_ratio
        if resolution:
            body["resolution"] = resolution
    if model:
        body["model"] = model
    return body


def _idempotent():
    return {"Idempotency-Key": str(uuid.uuid4())}


def generate_image(prompt, aspect_ratio="1:1", resolution="1K", model=None, style_references=None, for_3d=False):
    """Returns {postId, images: [{base64, contentType, bytes}], text, model}."""
    body = _image_options({"prompt": prompt}, aspect_ratio, resolution, model, for_3d)
    refs = list(style_references or [])[:3]
    if refs:
        body["styleReferences"] = refs
    return http.post("/2d/generate", body, headers=_idempotent(), timeout=180) or {}


def edit_image(prompt, image=None, source_post_id=None, aspect_ratio="1:1", resolution="1K", model=None,
               pipeline_step=False, for_3d=False):
    body = _image_options({"prompt": prompt}, aspect_ratio, resolution, model, for_3d)
    if image is not None:
        body["image"] = image
    elif source_post_id is not None:
        body["sourcePostId"] = int(source_post_id)
    if pipeline_step:
        body["pipelineStep"] = True
    return http.post("/2d/img-edit", body, headers=_idempotent(), timeout=180) or {}


def compose_images(prompt, images, source_post_ids=None, aspect_ratio="1:1", resolution="1K", model=None):
    body = _image_options({"prompt": prompt}, aspect_ratio, resolution, model, False)
    images = list(images or [])[:14]
    if images:
        body["images"] = images
    ids = [int(i) for i in (source_post_ids or [])]
    if ids:
        body["sourcePostIds"] = ids
    return http.post("/2d/img-compose", body, headers=_idempotent(), timeout=240) or {}


def list_images(page=1, page_size=24, search=None, feature=None):
    params = {"page": page, "pageSize": page_size}
    if search and search.strip():
        params["search"] = search.strip()
    if feature:
        params["feature"] = feature
    return http.get("/2d?" + http.query(params)) or {}


def delete_image(image_id):
    http.delete(f"/2d/{int(image_id)}")


# ─── Own API keys (used by the agent; managed in Preferences) ─────────────────────────────────────


def agent_options():
    """{providers: [{key, label, keyUrl, models: [...]}], …} — the own-key providers the server offers."""
    return http.get("/unityAgent/options") or {}


def check_provider_key(provider, key):
    """{ok, message}. The key travels in a header for this one call and is never stored by the server."""
    return http.post("/unityAgent/provider-key/check", {"provider": provider},
                     headers={"X-Zetta-Provider-Key": key or ""}, timeout=30) or {}
