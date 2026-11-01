"""3D Pipelines in Blender: the open recipe (undo, autosave, "Describe it"), its runs, new runs, and the
follower that polls live runs and imports what they make.

Runs keep going on the server with Blender closed; following them is only for showing progress and importing.
The runs being followed are kept on disk, so the next session picks them up again.
"""

import copy
import time

import bpy

from .. import importer
from ..api import catalog, models, zetta
from ..core import state as store
from ..core import tasks
from ..studio import context, pictures
from . import api, graph as g

LIVE = ("running", "awaiting", "paused")
OVER = ("done", "failed", "cancelled")
DELETABLE = ("draft", "done", "failed", "cancelled")
RUN_LABELS = {"draft": "Not started", "running": "Running", "awaiting": "Needs you", "paused": "Paused",
              "failed": "Stopped", "done": "Done", "cancelled": "Cancelled"}
STEP_LABELS = {"waiting": "Waiting for you", "ready": "Starting", "running": "Running", "submitted": "Running",
               "review": "Choose parts", "success": "Done", "failed": "Failed", "cancelled": "Cancelled",
               "idle": "Not run"}
RUN_FILTERS = {"all": None, "running": ("running", "paused"), "attention": ("draft", "awaiting"), "done": ("done",),
               "stopped": ("failed", "cancelled")}
UNTITLED = "Untitled pipeline"

listing = {"items": [], "loading": False, "error": None, "loaded": False}
editor = {"pipeline": None, "graph": None, "selected": None, "undo": [], "redo": [], "saving": False,
          "dirty": False, "save_error": None, "message": None, "error": None, "loading": False,
          "describe_busy": False, "describe_note": None, "describe_error": None, "last_edit": (None, 0.0)}
runs = {"items": [], "loading": False, "error": None}
run_view = {"run": None, "selected": None, "busy": None, "error": None, "needs": [], "review": {}, "extras": {}}
new_run = {"open": False, "seeds": [], "mode": "manual", "import": True, "uploading": 0, "busy": False,
           "error": None}
notices = []  # recent "needs you" / finished messages


def _wm_state():
    return bpy.context.window_manager.zetta


def entitlements():
    return context.gates_3d


# ─── The list ─────────────────────────────────────────────────────────────────────────────────────


def load_list():
    if listing["loading"]:
        return
    listing.update(loading=True, error=None)

    def done(items):
        listing.update(items=items or [], loading=False, loaded=True)

    def fail(error):
        listing.update(loading=False, loaded=True, error=catalog.explain(error))

    tasks.run(api.list_pipelines, done, fail, name="zetta3d-pipelines")


def ensure_list():
    if not listing["loaded"] and not listing["loading"]:
        load_list()


def run_counts_label(summary):
    counts = summary.get("runCounts") or {}
    parts = []
    if counts.get("running"):
        parts.append(f"{counts['running']} running")
    attention = int(counts.get("awaiting") or 0) + int(counts.get("draft") or 0)
    if attention:
        parts.append(f"{attention} need{'s' if attention == 1 else ''} you")
    if counts.get("paused"):
        parts.append(f"{counts['paused']} paused")
    return " · ".join(parts)


def create_pipeline():
    def done(pipeline):
        load_list()
        _open_loaded(pipeline)

    tasks.run(lambda: api.create_pipeline(UNTITLED), done, _list_error, name="zetta3d-new-pipeline")


def duplicate_pipeline(pipeline_id):
    tasks.run(lambda: api.duplicate_pipeline(pipeline_id), lambda _: load_list(), _list_error,
              name="zetta3d-duplicate-pipeline")


def delete_pipeline(pipeline_id):
    def done(_):
        if editor["pipeline"] and editor["pipeline"].get("id") == pipeline_id:
            close()
        load_list()

    tasks.run(lambda: api.delete_pipeline(pipeline_id), done, _list_error, name="zetta3d-delete-pipeline")


def _list_error(error):
    listing["error"] = catalog.explain(error)


# ─── The open recipe ──────────────────────────────────────────────────────────────────────────────


def open_pipeline(pipeline_id, then=None):
    flush()
    editor.update(loading=True, error=None)

    def done(pipeline):
        editor["loading"] = False
        _open_loaded(pipeline)
        if then:
            then()

    def fail(error):
        editor.update(loading=False, error=catalog.explain(error))

    tasks.run(lambda: api.get_pipeline(pipeline_id), done, fail, name="zetta3d-open-pipeline")


def _open_loaded(pipeline):
    normalized, needs_rebuild = g.normalize(pipeline.get("graph"))
    editor.update(pipeline=pipeline, graph=normalized, selected=None, undo=[], redo=[], dirty=False,
                  save_error=None, message=None, error=None, describe_note=None, describe_error=None)
    _wm_state().pipe_title = pipeline.get("title") or UNTITLED
    _wm_state().pipe_view = "recipe"
    runs.update(items=[], error=None)
    run_view.update(run=None, selected=None, busy=None, error=None)
    load_runs()
    if needs_rebuild:
        editor["dirty"] = True
        save()


def close():
    flush()
    editor.update(pipeline=None, graph=None, selected=None, undo=[], redo=[])
    run_view.update(run=None, selected=None)
    runs.update(items=[])
    new_run["open"] = False


def is_open():
    return editor["pipeline"] is not None


def commit(new_graph, coalesce=None):
    """Replaces the recipe as one undoable step; edits with the same coalesce key within a second merge."""
    key, at = editor["last_edit"]
    now = time.time()
    if not (coalesce and key == coalesce and now - at < 1.0):
        editor["undo"].append(copy.deepcopy(editor["graph"]))
        del editor["undo"][:-100]
    editor["redo"].clear()
    editor["last_edit"] = (coalesce, now)
    editor["graph"] = new_graph
    editor["dirty"] = True
    schedule_save()


def undo():
    if editor["undo"]:
        editor["redo"].append(editor["graph"])
        editor["graph"] = editor["undo"].pop()
        editor["dirty"] = True
        schedule_save()


def redo():
    if editor["redo"]:
        editor["undo"].append(editor["graph"])
        editor["graph"] = editor["redo"].pop()
        editor["dirty"] = True
        schedule_save()


def edit(fn, coalesce=None):
    """Applies a graph edit; its ValueError becomes the editor's message."""
    try:
        commit(fn(editor["graph"]), coalesce)
        editor["error"] = None
    except ValueError as error:
        editor["error"] = str(error)


def _autosave():
    if editor["dirty"] and not editor["saving"]:
        save()
    return None


def schedule_save():
    if bpy.app.timers.is_registered(_autosave):
        bpy.app.timers.unregister(_autosave)
    bpy.app.timers.register(_autosave, first_interval=0.8)


def save(then=None):
    pipeline = editor["pipeline"]
    if pipeline is None:
        return
    if editor["saving"]:
        schedule_save()
        return
    title = _wm_state().pipe_title.strip() or UNTITLED
    body = g.with_requests(editor["graph"], entitlements())
    editor.update(saving=True, dirty=False)
    pipeline_id = pipeline["id"]

    def done(saved):
        editor.update(saving=False, save_error=None)
        if editor["pipeline"] and editor["pipeline"].get("id") == pipeline_id:
            editor["pipeline"]["title"] = (saved or {}).get("title", title)
        if then:
            then()

    def fail(error):
        editor.update(saving=False, dirty=True, save_error="Not saved — retrying on the next change")
        print(f"[Zetta 3D] The pipeline could not be saved: {error}")

    tasks.run(lambda: api.update_pipeline(pipeline_id, title, body), done, fail, name="zetta3d-save-pipeline")


def flush():
    if bpy.app.timers.is_registered(_autosave):
        bpy.app.timers.unregister(_autosave)
    if editor["dirty"] and editor["pipeline"] is not None:
        save()


def save_status():
    if editor["saving"]:
        return "Saving…"
    if editor["save_error"]:
        return editor["save_error"]
    return "Not saved yet" if editor["dirty"] else "Saved"


def recipe_problems():
    if editor["graph"] is None:
        return []
    return g.problems(editor["graph"], entitlements())


def editing():
    """The recipe has steps beyond its seed: Describe it changes it rather than starting over."""
    return any(not g.is_seed(n.get("kind")) for n in (editor["graph"] or {}).get("nodes") or [])


def describe(text):
    text = (text or "").strip()
    if len(text) < 3:
        editor["describe_error"] = "Say a little more about what the recipe should do."
        return
    current = g.with_requests(editor["graph"], entitlements()) if editing() else None
    editor.update(describe_busy=True, describe_error=None, describe_note=None)

    def done(answer):
        editor["describe_busy"] = False
        drafted, _ = g.normalize((answer or {}).get("graph"))
        commit(g.with_requests(drafted, entitlements()))
        title = _wm_state().pipe_title.strip()
        if (not title or title == UNTITLED) and answer.get("title"):
            _wm_state().pipe_title = answer["title"]
        summary = (answer.get("summary") or "").strip()
        editor["describe_note"] = f"{summary} Undo brings back what was there." if summary else \
            "Done. Undo brings back what was there."
        _wm_state().pipe_describe = ""

    def fail(error):
        editor["describe_busy"] = False
        if isinstance(error, zetta.ApiError) and error.status == 429:
            editor["describe_error"] = "That's a lot of recipes in a minute — wait a moment and try again."
        else:
            editor["describe_error"] = catalog.explain(error)

    tasks.run(lambda: api.draft(text, current), done, fail, name="zetta3d-describe")


# ─── Runs ─────────────────────────────────────────────────────────────────────────────────────────


def load_runs():
    pipeline = editor["pipeline"]
    if pipeline is None or runs["loading"]:
        return
    runs.update(loading=True, error=None)
    pipeline_id = pipeline["id"]

    def done(items):
        runs.update(loading=False)
        if editor["pipeline"] and editor["pipeline"].get("id") == pipeline_id:
            runs["items"] = items or []

    def fail(error):
        runs.update(loading=False, error=catalog.explain(error))

    tasks.run(lambda: api.list_runs(pipeline_id), done, fail, name="zetta3d-runs")


def filtered_runs(name):
    wanted = RUN_FILTERS.get(name)
    return [r for r in runs["items"] if wanted is None or r.get("status") in wanted]


def run_name(summary):
    seed = summary.get("seed") or {}
    return seed.get("name") or f"Run #{summary.get('id')}"


def run_progress(summary):
    if summary.get("status") == "draft":
        needed = int(summary.get("inputsNeeded") or 0)
        return f"{needed} step{'s' if needed != 1 else ''} need{'s' if needed == 1 else ''} input" if needed \
            else "Ready to start"
    progress = summary.get("progress") or {}
    total = int(progress.get("total") or 0)
    text = f"{progress.get('done') or 0} of {total} step{'s' if total != 1 else ''}"
    if progress.get("waiting"):
        text += f" · {progress['waiting']} waiting"
    return text


def open_run(run_id):
    run_view.update(busy="Opening…", error=None, selected=None)

    def done(run):
        run_view["busy"] = None
        _apply_run(run)
        if run.get("status") in LIVE or run.get("status") == "draft":
            follow(run, import_results=False)

    def fail(error):
        run_view.update(busy=None, error=catalog.explain(error))

    tasks.run(lambda: api.get_run(run_id), done, fail, name="zetta3d-open-run")


def close_run():
    run_view.update(run=None, selected=None, busy=None, error=None, needs=[])


def _apply_run(run):
    """Shows a run (when it is the open one) and learns the parts it found."""
    current = run_view["run"]
    if current is None or current.get("id") == run.get("id"):
        run_view["run"] = run
    if editor["pipeline"] and editor["pipeline"].get("id") == run.get("pipelineId") and editor["graph"]:
        learned, changed = g.learn_parts(editor["graph"], run)
        if changed:
            editor["graph"] = learned
            editor["dirty"] = True
            schedule_save()


def _run_action(fn, busy):
    run = run_view["run"]
    if run is None:
        return
    run_view.update(busy=busy, error=None)

    def done(updated):
        run_view["busy"] = None
        if isinstance(updated, dict) and updated.get("id"):
            _apply_run(updated)
            if updated.get("status") in LIVE:
                follow(updated, import_results=None)
        load_runs()

    def fail(error):
        run_view["busy"] = None
        if isinstance(error, api.PipelineError):
            run_view["needs"] = error.problems
            if error.problems and error.problems[0][0]:
                run_view["selected"] = error.problems[0][0]
        run_view["error"] = catalog.explain(error)

    tasks.run(lambda: fn(run["id"]), done, fail, name="zetta3d-run-action")


def local_needs(run):
    return g.run_input_problems(run.get("graph") or {})


def start_run():
    run = run_view["run"]
    if run is None:
        return
    needs = local_needs(run)
    if needs:
        run_view["needs"] = needs
        run_view["selected"] = needs[0][0]
        count = len(needs)
        run_view["error"] = (f"This run can't start yet — {'one step needs' if count == 1 else f'{count} steps need'} "
                             "something from you. Select the step marked Needs input, fill it in, then press Start "
                             "again.")
        return
    run_view["needs"] = []
    _run_action(lambda run_id: api.run_action(run_id, "start"), "Starting…")


def pause_run():
    _run_action(lambda run_id: api.run_action(run_id, "pause"), "Pausing…")


def resume_run():
    _run_action(lambda run_id: api.run_action(run_id, "resume"), "Resuming…")


def cancel_run():
    _run_action(lambda run_id: api.run_action(run_id, "cancel"), "Cancelling…")


def run_waiting():
    _run_action(lambda run_id: api.run_action(run_id, "run-waiting"), "Running…")


def set_mode(mode):
    _run_action(lambda run_id: api.run_action(run_id, "mode", {"mode": mode}), "Switching…")


def run_step(step_id):
    _run_action(lambda run_id: api.run_step(run_id, step_id), "Running the step…")


def retry_step(step_id, with_settings=False):
    run = run_view["run"]
    step = next((s for s in run.get("steps") or [] if s.get("id") == step_id), None) if run else None
    if step is None:
        return
    request = settings = None
    if with_settings:
        n = g.node(run.get("graph") or {}, step.get("nodeId"))
        if n is not None:
            settings = g.with_defaults(n["kind"], n.get("settings"))
            request, _ = g.request_for(n["kind"], settings, g.incoming_type(run["graph"], n["id"]), entitlements())
    _run_action(lambda run_id: api.retry_step(run_id, step_id, request, settings), "Retrying…")


def choose_parts(step_id):
    keys = sorted(run_view["review"].get(step_id, set()))
    extras = run_view["extras"].get(step_id, [])
    _run_action(lambda run_id: api.choose_parts(run_id, step_id, keys, extras), "Isolating…")


def delete_run(run_id):
    def done(_):
        unfollow(run_id)
        if run_view["run"] and run_view["run"].get("id") == run_id:
            close_run()
        load_runs()

    def fail(error):
        run_view["error"] = catalog.explain(error)

    tasks.run(lambda: api.delete_run(run_id), done, fail, name="zetta3d-delete-run")


def patch_run_node(node_id, patch):
    """A per-run setting on a draft (or a step not reached yet): applied to the view at once, then sent."""
    run = run_view["run"]
    if run is None:
        return
    graph = run.get("graph") or {}
    settings, request = g.run_node_change(graph, node_id, patch, entitlements())
    n = g.node(graph, node_id)
    if n is not None:
        n["settings"], n["request"] = settings, request
    run_id = run["id"]

    def done(updated):
        if isinstance(updated, dict) and run_view["run"] and run_view["run"].get("id") == run_id:
            run_view["run"] = updated

    def fail(error):
        run_view["error"] = catalog.explain(error)

    tasks.run(lambda: api.patch_run_node(run_id, node_id, request, settings), done, fail, name="zetta3d-run-node")


def node_state(run, node_id):
    """(status, steps) for one recipe step in a run."""
    steps = [s for s in run.get("steps") or [] if s.get("nodeId") == node_id]
    statuses = [s.get("status") for s in steps]
    if not steps:
        return "idle", steps
    for wanted, states in (("running", ("running", "submitted", "ready")), ("review", ("review",)),
                           ("waiting", ("waiting",)), ("failed", ("failed",)), ("cancelled", ("cancelled",))):
        if any(s in states for s in statuses):
            return wanted, steps
    return "success", steps


def node_editable(run, node_id):
    n = g.node(run.get("graph") or {}, node_id)
    if n is None or g.is_seed(n.get("kind")) or run.get("status") == "cancelled" or not g.in_scope(n):
        return False
    _, steps = node_state(run, node_id)
    return all(s.get("status") == "waiting" for s in steps)


def part_label(run, part_key):
    last = (part_key or "").split("/")[-1]
    for step in run.get("steps") or []:
        output = step.get("output") or {}
        if output.get("type") == "isolated":
            for part in output.get("parts") or []:
                if part.get("key") == last:
                    return part.get("label") or last
    return last


def waiting_steps(run):
    return [s for s in run.get("steps") or [] if s.get("status") == "waiting"]


# ─── Importing what runs make ─────────────────────────────────────────────────────────────────────


def importable_steps(run, final_only=True):
    graph = run.get("graph") or {}
    found = []
    for step in run.get("steps") or []:
        output = step.get("output") or {}
        gen = step.get("generation")
        n = g.node(graph, step.get("nodeId"))
        if (step.get("status") != "success" or output.get("type") != "mesh" or n is None or g.is_seed(n.get("kind"))
                or not gen or gen.get("status") != "success" or models.is_splat_row(gen)):
            continue
        if final_only:
            feeds_mesh = False
            for edge in g.children(graph, n["id"]):
                target = g.node(graph, edge.get("target"))
                spec = g.inputs(target.get("kind")).get(edge.get("targetHandle") or "in") if target else None
                if spec and g.MESH in spec[0]:
                    feeds_mesh = True
            if feeds_mesh:
                continue
        found.append(step)
    return found


def import_step(step, place=False):
    gen = step.get("generation")
    if gen:
        importer.import_generation(gen, place=place)


# ─── Following runs (polled; kept on disk) ────────────────────────────────────────────────────────

_followed = None  # run id (str) → {pipelineId, import, imported: [step ids], status, polledAt}


def _followed_runs():
    global _followed
    if _followed is None:
        saved = store.read("pipeline_runs", {})
        _followed = saved if isinstance(saved, dict) else {}
    return _followed


def _save_followed():
    store.write("pipeline_runs", _followed_runs())


def follow(run, import_results=None):
    """Polls a run until it is over. import_results None keeps the current choice."""
    followed = _followed_runs()
    key = str(run["id"])
    entry = followed.get(key) or {"pipelineId": run.get("pipelineId"), "import": False, "imported": [],
                                  "polledAt": 0}
    if import_results is not None:
        entry["import"] = bool(import_results)
    entry["status"] = run.get("status")
    followed[key] = entry
    _save_followed()


def unfollow(run_id):
    if _followed_runs().pop(str(run_id), None) is not None:
        _save_followed()


def following(run_id):
    return str(run_id) in _followed_runs()


def importing_results(run_id):
    return bool((_followed_runs().get(str(run_id)) or {}).get("import"))


_polling = set()
_runs_polled_at = 0.0


def _tick():
    global _runs_polled_at
    from .. import auth

    if not auth.signed_in():
        return 3.0
    now = time.time()
    for key, entry in list(_followed_runs().items()):
        every = 15.0 if entry.get("status") == "draft" else 2.75
        if key in _polling or now - float(entry.get("polledAt") or 0) < every:
            continue
        entry["polledAt"] = now
        _poll(int(key))
    if editor["pipeline"] and any(r.get("status") in LIVE for r in runs["items"]) and now - _runs_polled_at > 2.75:
        _runs_polled_at = now
        load_runs()
    return 1.0


def _poll(run_id):
    key = str(run_id)
    _polling.add(key)

    def done(run):
        _polling.discard(key)
        entry = _followed_runs().get(key)
        if entry is None:
            return
        previous = entry.get("status")
        entry["status"] = run.get("status")
        _apply_run(run)
        if entry.get("import"):
            for step in importable_steps(run):
                if step["id"] not in entry["imported"]:
                    entry["imported"].append(step["id"])
                    import_step(step)
        if run.get("status") == "awaiting" and previous != "awaiting":
            _notify(f"Pipeline run #{run_id} needs you.")
        if run.get("status") in OVER:
            _notify(f"Pipeline run #{run_id}: {RUN_LABELS.get(run['status'], run['status'])}.")
            unfollow(run_id)
            load_runs()
        else:
            _save_followed()

    def fail(error):
        _polling.discard(key)
        if isinstance(error, zetta.ApiError) and error.status == 404:
            unfollow(run_id)

    tasks.run(lambda: api.get_run(run_id), done, fail, name=f"zetta3d-run-{run_id}")


def _notify(message):
    notices.insert(0, message)
    del notices[5:]
    print(f"[Zetta 3D] {message}")


def register():
    bpy.app.timers.register(_tick, first_interval=2.0, persistent=True)


def unregister():
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
    if bpy.app.timers.is_registered(_autosave):
        bpy.app.timers.unregister(_autosave)


# ─── New runs ─────────────────────────────────────────────────────────────────────────────────────


def seed_kind():
    found = g.seed(editor["graph"] or {})
    return found.get("kind") if found else None


def open_new_run():
    new_run.update(open=True, seeds=[], uploading=0, busy=False, error=None)


def _add_seed(seed):
    if len(new_run["seeds"]) + new_run["uploading"] >= g.MAX_BATCH:
        new_run["error"] = f"Up to {g.MAX_BATCH} runs at once."
        return False
    for existing in new_run["seeds"]:
        for key in ("gen3dPostId", "gen2dPostId"):
            if seed.get(key) is not None and existing.get(key) == seed.get(key):
                new_run["seeds"].remove(existing)  # picking it again takes it off
                return False
    new_run["seeds"].append(seed)
    return True


def _upload_input(picture, done, fail):
    """Checks, downscales and uploads a run's picture. done({uploadKey, name, picture})."""
    reason = picture.problem()
    if reason:
        fail(reason)
        return
    small = pictures.downscaled(picture)
    new_run["uploading"] += 1

    def work():
        return zetta.upload(small.data, small.content_type)["key"]

    def finished(key):
        new_run["uploading"] -= 1
        done({"uploadKey": key, "name": picture.name or "picture", "picture": small})

    def failed(error):
        new_run["uploading"] -= 1
        fail(catalog.explain(error) if isinstance(error, zetta.ApiError) else str(error))

    tasks.run(work, finished, failed, name="zetta3d-run-upload")


def add_picture_seed(picture):
    def done(entry):
        _add_seed({"type": "picture", "uploadKey": entry["uploadKey"], "name": entry["name"],
                   "picture": entry["picture"]})

    _upload_input(picture, done, lambda message: new_run.__setitem__("error", message))


def add_image_seed(post):
    name = (post.get("prompt") or "")[:60] or f"Picture #{post.get('id')}"
    _add_seed({"type": "picture", "gen2dPostId": int(post["id"]), "name": name, "thumb": f"i{post['id']}",
               "thumbUrl": ((post.get("images") or [{}])[0]).get("url")})


def add_model_seed(gen):
    _add_seed({"type": "model", "gen3dPostId": int(gen["id"]), "name": models.display_title(gen),
               "thumb": f"g{gen['id']}", "thumbUrl": models.thumbnail_url(gen)})


def add_views_set():
    if len(new_run["seeds"]) < g.MAX_BATCH:
        new_run["seeds"].append({"type": "views", "views": {}})


def set_view(seed_index, side, picture):
    def done(entry):
        if seed_index < len(new_run["seeds"]):
            new_run["seeds"][seed_index]["views"][side] = entry

    _upload_input(picture, done, lambda message: new_run.__setitem__("error", message))


def remove_seed(index):
    if 0 <= index < len(new_run["seeds"]):
        new_run["seeds"].pop(index)


def ready_seeds():
    found = []
    for seed in new_run["seeds"]:
        if seed["type"] == "views":
            views = g.views_request(seed.get("views"))
            if any(v["viewType"] == "front" for v in views):
                front = seed["views"].get("front") or {}
                found.append({"views": views, **({"name": front["name"]} if front.get("name") else {})})
        elif seed["type"] == "model":
            found.append({"gen3dPostId": seed["gen3dPostId"]})
        elif seed.get("uploadKey"):
            found.append({"uploadKey": seed["uploadKey"], "name": seed.get("name") or "picture"})
        elif seed.get("gen2dPostId") is not None:
            found.append({"gen2dPostId": seed["gen2dPostId"]})
    return found


def new_run_problem():
    if new_run["uploading"]:
        return "Uploading…"
    if any(s["type"] == "views" and s.get("views") and not s["views"].get("front") for s in new_run["seeds"]):
        return "Each set needs a front picture before it can be a run."
    if not ready_seeds():
        return "Choose the pictures or models to run on — one run each."
    return None


def create_runs():
    pipeline = editor["pipeline"]
    reason = new_run_problem()
    if pipeline is None or reason:
        new_run["error"] = reason
        return
    seeds = ready_seeds()
    graph = g.with_requests(g.for_new_run(editor["graph"]), entitlements())
    mode, import_results = new_run["mode"], new_run["import"]
    pipeline_id = pipeline["id"]
    new_run.update(busy=True, error=None)
    if bpy.app.timers.is_registered(_autosave):
        bpy.app.timers.unregister(_autosave)  # the graph goes with the batch

    def done(answer):
        created = (answer or {}).get("runs") or []
        editor["dirty"] = False
        new_run.update(busy=False, open=False, seeds=[])
        for run in created:
            follow({"id": run["id"], "pipelineId": pipeline_id, "status": run.get("status")}, import_results)
        load_runs()
        _wm_state().pipe_view = "runs"
        if created:
            open_run(created[0]["id"])

    def fail(error):
        new_run.update(busy=False, error=catalog.explain(error))
        if isinstance(error, api.PipelineError) and error.problems:
            new_run["error"] = error.problems[0][1]

    tasks.run(lambda: api.batch(pipeline_id, mode, seeds, graph), done, fail, name="zetta3d-new-runs")


def set_run_reference(node_id, picture):
    """A draft texture step's reference picture."""

    def done(entry):
        patch_run_node(node_id, {"reference": {"uploadKey": entry["uploadKey"], "name": entry["name"]}})

    _upload_input(picture, done, lambda message: run_view.__setitem__("error", message))


def set_run_view(node_id, side, picture):
    """A draft multi-view texture step's picture for one side."""
    run = run_view["run"]
    n = g.node((run or {}).get("graph") or {}, node_id)
    if n is None:
        return

    def done(entry):
        views = dict(g.with_defaults(n["kind"], n.get("settings")).get("referenceViews") or {})
        views[side] = {"uploadKey": entry["uploadKey"], "name": entry["name"]}
        patch_run_node(node_id, {"referenceViews": views})

    _upload_input(picture, done, lambda message: run_view.__setitem__("error", message))


def reset():
    close()
    listing.update(items=[], loading=False, error=None, loaded=False)
