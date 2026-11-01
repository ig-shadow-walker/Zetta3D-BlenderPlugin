"""My Generations: everything made on Zetta 3D — on the web, in Unity, in Blender — and the 2D pictures.

Pipeline runs come as one entry each (groupRuns), so a run is never split across pages. Thumbnails download in
the background into the add-on's folder and show as preview icons.
"""

import hashlib
import os
import queue
import threading

import bpy

from . import importer
from .api import catalog, models, zetta
from .core import http, state, tasks
from .studio import pictures

PAGE_SIZE = 24
IMAGE_PAGE_SIZE = 24

page = {"items": [], "total": 0, "page": 1, "loading": False, "error": None, "loaded": False}
images = {"items": [], "total": 0, "page": 0, "loading": False, "error": None, "loaded": False}
selected = set()  # generation ids picked in select mode
open_run = {"group": None}  # the run group whose models are shown
notice = {"text": None, "error": None}
_request = 0

STATUS_FILTERS = {
    "all": None,
    "ready": ["success"],
    "running": ["pending", "queued", "processing", "saving"],
    "failed": ["failed", "cancelled"],
}


def _state():
    return bpy.context.window_manager.zetta


def reload(reset_page=False):
    global _request
    zetta_state = _state()
    if reset_page:
        page["page"] = 1
    _request += 1
    ticket = _request
    page.update(loading=True, error=None)
    number = page["page"]
    search = zetta_state.lib_search
    feature = None if zetta_state.lib_feature == "all" else zetta_state.lib_feature
    statuses = STATUS_FILTERS.get(zetta_state.lib_status)

    def work():
        return zetta.list_generations(page=number, page_size=PAGE_SIZE, search=search, feature=feature or None,
                                      statuses=statuses, group_runs=True)

    def done(answer):
        if ticket != _request:
            return  # a newer request is on its way
        total = int(answer.get("total") or 0)
        pages = max(1, -(-total // PAGE_SIZE))
        if number > pages and total > 0:
            page["page"] = pages
            reload()
            return
        page.update(items=answer.get("items") or [], total=total, loading=False, loaded=True)
        group = open_run["group"]
        if group is not None:
            runs = [g for g in page["items"] if (g.get("runGroup") or {}).get("runId") == group.get("runId")]
            open_run["group"] = runs[0]["runGroup"] if runs else None

    def fail(error):
        if ticket == _request:
            page.update(loading=False, loaded=True, error=catalog.explain(error))

    tasks.run(work, done, fail, name="zetta3d-library")


def pages():
    return max(1, -(-page["total"] // PAGE_SIZE))


def go_to(number):
    page["page"] = max(1, min(number, pages()))
    reload()


def ensure_loaded():
    if not page["loaded"] and not page["loading"]:
        reload()


def flatten(items):
    """Every model on the page, run members included."""
    found = []
    for gen in items:
        group = gen.get("runGroup")
        if group and group.get("members"):
            found.extend(group["members"])
        else:
            found.append(gen)
    return found


def find(generation_id):
    for gen in flatten(page["items"]):
        if gen.get("id") == generation_id:
            return gen
    return None


# ─── Run groups ───────────────────────────────────────────────────────────────────────────────────


def run_name(group):
    members = group.get("members") or []
    if members:
        title = (members[-1].get("title") or "").split(" - ", 1)[0].strip()
        if title:
            return title
    return group.get("pipelineTitle") or f"Pipeline run #{group.get('runId')}"


def run_detail(group):
    count = run_count(group)
    title = group.get("pipelineTitle")
    parts = [f"{count} model{'s' if count != 1 else ''}"]
    if title:
        parts.append(title)
    parts.append(f"run #{group.get('runId')}")
    return " · ".join(parts)


def run_count(group):
    return int(group.get("count") or 0) or len(group.get("members") or [])


def in_run_order(members):
    def key(gen):
        step = gen.get("runStep") or {}
        position = step.get("position")
        return (0 if isinstance(position, int) else 1, position or 0, gen.get("createdAt") or "", gen.get("id") or 0)

    return sorted(members, key=key)


def step_text(gen, index):
    step = gen.get("runStep") or {}
    parts = [f"Step {step['position']}" if step.get("position") else f"Step {index + 1}", step.get("label"),
             step.get("part")]
    return " · ".join(p for p in parts if p)


# ─── Thumbnails ───────────────────────────────────────────────────────────────────────────────────

_thumb_queue = queue.Queue()
_thumb_requested = set()
_thumb_ready = {}  # key → path on disk
_thumb_thread = None


def _thumb_folder():
    folder = state.path("thumbnails")
    os.makedirs(folder, exist_ok=True)
    return folder


def _thumb_worker():
    while True:
        key, url, path = _thumb_queue.get()
        try:
            data = http.fetch_bytes(url, timeout=30)
            with open(path, "wb") as handle:
                handle.write(data)
            tasks.post(_thumb_ready.__setitem__, key, path)
        except Exception:  # noqa: BLE001 — a missing thumbnail just shows the placeholder
            pass


def thumbnail(key, url):
    """An icon id for a thumbnail, or 0 while it downloads (the panel redraws when it lands)."""
    global _thumb_thread
    if not url:
        return 0
    if pictures.has_icon(key):
        return pictures.icon_for_file(key, "")
    path = _thumb_ready.get(key)
    if path:
        try:
            return pictures.icon_for_file(key, path)
        except (RuntimeError, KeyError):
            return 0
    if key in _thumb_requested:
        return 0
    _thumb_requested.add(key)
    # Signed URLs change; the file name follows the object, not the signature.
    digest = hashlib.sha1(url.split("?", 1)[0].encode("utf-8")).hexdigest()[:16]
    path = os.path.join(_thumb_folder(), f"{key}-{digest}")
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        _thumb_ready[key] = path
        return pictures.icon_for_file(key, path)
    if not getattr(bpy.app, "online_access", True):
        return 0
    if _thumb_thread is None:
        for index in range(3):
            thread = threading.Thread(target=_thumb_worker, name=f"zetta3d-thumbs-{index}", daemon=True)
            thread.start()
        _thumb_thread = True
    _thumb_queue.put((key, url, path))
    return 0


def generation_icon(gen):
    return thumbnail(f"g{gen.get('id')}", models.thumbnail_url(gen))


def image_icon(post):
    stored = (post.get("images") or [{}])[0]
    return thumbnail(f"i{post.get('id')}", stored.get("url"))


# ─── Actions ──────────────────────────────────────────────────────────────────────────────────────


def import_row(gen, place, location=None, parent=None):
    title = models.display_title(gen)
    notice.update(text=None, error=None)

    def done(_objects):
        notice["text"] = f"Placed {title}" if place else f"Imported {title}"

    def fail(message):
        notice["error"] = f"{title}: {message}"

    importer.import_generation(gen, place=place, location=location, parent=parent, done=done, fail=fail)


def rename(gen, title):
    title = (title or "").strip()[:255]
    if not title or title == gen.get("title"):
        return

    def done(updated):
        gen["title"] = (updated or {}).get("title", title)

    def fail(error):
        notice["error"] = catalog.explain(error)

    tasks.run(lambda: zetta.update_generation(gen["id"], title=title), done, fail, name="zetta3d-rename")


def delete(ids, on_done=None):
    """Deletes models, four at a time. Failed ids stay selected."""
    ids = list(ids)
    total = len(ids)
    if not total:
        return
    progress = {"done": 0}
    notice.update(text=f"Deleting 0 of {total}…", error=None)

    def work():
        failed = []
        lock = threading.Lock()
        pending = list(ids)

        def worker():
            while True:
                with lock:
                    if not pending:
                        return
                    gen_id = pending.pop()
                try:
                    zetta.delete_generation(gen_id)
                except zetta.ApiError:
                    with lock:
                        failed.append(gen_id)
                with lock:
                    progress["done"] += 1
                    tasks.post(notice.__setitem__, "text", f"Deleting {progress['done']} of {total}…")

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(min(4, total))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return failed

    def done(failed):
        selected.difference_update(set(ids) - set(failed))
        if failed:
            notice.update(text=None, error=f"{len(failed)} of {total} could not be deleted — they are still selected. "
                                          "Try again in a moment.")
        else:
            notice.update(text=f"Deleted {total} model{'s' if total != 1 else ''}.", error=None)
        reload()
        if on_done:
            on_done(failed)

    tasks.run(work, done, name="zetta3d-delete")


def import_run(group):
    """Imports every finished model of a run, in run order, one at a time and without placing them."""
    members = [g for g in in_run_order(group.get("members") or []) if models.is_success(g)
               and not models.is_splat_row(g) and not importer.in_project(int(g["id"]))
               and int(g["id"]) not in importer.importing]
    if not members:
        notice.update(text="Every model from this run is already in this file.", error=None)
        return
    total, results = len(members), {"ok": 0, "failed": 0, "last": None}

    def step(index):
        if index >= total:
            if results["failed"]:
                notice.update(text=None, error=f"{results['failed']} of {total} could not be imported. {results['last']}")
            else:
                notice.update(text=f"Imported {results['ok']} of {total} models", error=None)
            return
        notice.update(text=f"Importing {index + 1} of {total}…", error=None)

        def done(_objects):
            results["ok"] += 1
            step(index + 1)

        def fail(message):
            results["failed"] += 1
            results["last"] = message
            step(index + 1)

        importer.import_generation(members[index], place=False, done=done, fail=fail)

    step(0)


def save_uv_layout(gen):
    def done(path):
        notice.update(text=f"UV layout picture saved to {path}." if path else None, error=None)
        if path:
            try:
                bpy.data.images.load(path, check_existing=True)
            except RuntimeError:
                pass

    def fail(error):
        notice.update(text=None, error=f"The UV layout picture could not be downloaded: {error}")

    tasks.run(lambda: importer.download_uv_layout(gen), done, fail, name="zetta3d-uv-layout")


def send_to_options(gen):
    """[(tool, enabled)] for the Send to menu."""
    processable = (gen.get("selectedFeature") in catalog.PROCESSABLE_FEATURES and models.is_success(gen)
                   and not models.is_splat_row(gen))
    feature = gen.get("selectedFeature")
    return [
        ("segment", processable),
        ("retopology", processable and feature != "retopology"),
        ("smart_uv", processable and feature != "uvUnwrap"),
        ("texture", processable),
        ("rig", processable),
    ]


# ─── Images ───────────────────────────────────────────────────────────────────────────────────────


def reload_images(reset=False, more=False):
    if images["loading"]:
        return
    zetta_state = _state()
    if reset or not more:
        images.update(items=[], page=0, total=0)
    number = images["page"] + 1
    feature = None if zetta_state.lib_image_feature == "all" else zetta_state.lib_image_feature
    images.update(loading=True, error=None)

    def done(answer):
        images["items"].extend(answer.get("items") or [])
        images.update(total=int(answer.get("total") or 0), page=number, loading=False, loaded=True)

    def fail(error):
        images.update(loading=False, loaded=True, error=catalog.explain(error))

    tasks.run(lambda: zetta.list_images(page=number, page_size=IMAGE_PAGE_SIZE, feature=feature), done, fail,
              name="zetta3d-images-library")


def ensure_images_loaded():
    if not images["loaded"] and not images["loading"]:
        reload_images()


def delete_image(post):
    def done(_):
        images["items"] = [p for p in images["items"] if p.get("id") != post.get("id")]
        images["total"] = max(0, images["total"] - 1)

    def fail(error):
        notice["error"] = catalog.explain(error)

    tasks.run(lambda: zetta.delete_image(post["id"]), done, fail, name="zetta3d-delete-image")


def reset():
    page.update(items=[], total=0, page=1, loading=False, error=None, loaded=False)
    images.update(items=[], total=0, page=0, loading=False, error=None, loaded=False)
    selected.clear()
    open_run["group"] = None
