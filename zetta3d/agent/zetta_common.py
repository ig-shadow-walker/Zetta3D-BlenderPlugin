"""What the agent's Zetta 3D tools share: sign-in, finishing a call from a worker thread, the credit card, the
account's plan, and bringing finished models into the file — for the tools themselves (`import_generation`,
`import_pipeline_results`) and for the jobs and pipeline runs the agent started, which import when they finish and
then tell the conversation (`events.emit`).

THE AGENT'S OWN JOBS AND RUNS ARE KEPT APART. A job the agent started is tracked by the add-on's jobs module like any
other (the Jobs list shows it), but with auto-import off: this module imports it when it finishes — where `place`
said — and emits `job_finished`. Which jobs are the agent's is kept on disk (`agent_jobs`), so a job still running
when Blender closes is imported, and reported, in the next session. Runs the agent started are followed here, not by
the Pipelines panel's follower (which would import them a second time, unplaced): `agent_runs` on disk, polled by a
timer while there is one, their final models imported as they land, and `pipeline_run_finished` emitted when a run
ends or needs the user.

UNDO. A tool that imports (`import_generation`, `import_pipeline_results`) is registered undoable and finishes only
once the import has landed, so the dispatcher names the undo step. Imports that land later, outside any tool call
(a finished job or run), push their own step here: `tools.push_undo` + `changes.scene_step`.
"""

import traceback

import bpy

from .. import auth, importer, jobs
from ..api import catalog, models, zetta
from ..core import http
from ..core import state as store
from ..core import tasks
from ..studio import common, context
from . import changes, errors, events, targets
from .tools import LATER, Result, ToolError, push_undo

SPLAT_REFUSAL = "Gaussian splats are not supported in Blender here — open it on zetta3d.ai."
DECLINED = "The user declined."
BUILD_IN_BLENDER = ("Not generated: the user chose Build in Blender. Model it yourself with Blender's own tools — "
                    "primitives, modifiers, mesh edits, Geometry Nodes or run_python for procedural shapes, then "
                    "materials — in clear steps, and capture_view to check it as you go. Don't propose "
                    "generate_3d_model for it again unless they ask.")

# ─── Calls that finish later ─────────────────────────────────────────────────────────────────────


def require_signed_in():
    if not auth.signed_in():
        raise ToolError("Not signed in to Zetta 3D. Ask the user to sign in from the Zetta 3D sidebar, then try again.")


def explain(error):
    """One line the model can act on, for anything a worker raised."""
    from ..pipelines import api as pipeline_api

    if isinstance(error, (ToolError, common.Problem)):
        return str(error)
    if isinstance(error, pipeline_api.PipelineError) and error.problems:
        found = " ".join(f"[{node}] {message}" if node else message for node, message in error.problems[:6])
        return f"{catalog.explain(error)} {found}".strip()
    if isinstance(error, http.ApiError):
        return catalog.explain(error)
    text = str(error).strip().splitlines()
    return f"{type(error).__name__}: {text[0] if text else ''}".rstrip(": ")


def settle(ctx, fn):
    """Runs fn() on the main thread and finishes the call with what it returns (unless LATER)."""
    if ctx.finished:
        return
    try:
        out = fn()
    except ToolError as error:
        ctx.finish(Result(str(error), error=True))
        return
    except Exception as error:  # noqa: BLE001 — a bug is the model's error line, never a lost turn
        traceback.print_exc()
        name = ctx.call.get("name", "tool")
        errors.record(f"tool {name}", f"{type(error).__name__}: {error}")
        ctx.finish(Result(f"{name} failed: {type(error).__name__}: {error}", error=True))
        return
    if out is not LATER:
        ctx.finish(out)


def later(ctx, work, then, name="zetta3d-agent"):
    """work() on a worker thread, then(value) on the main thread finishing the call. Returns LATER."""

    def done(value):
        settle(ctx, lambda: then(value))

    def fail(error):
        if not ctx.finished:
            ctx.finish(Result(explain(error), error=True))

    tasks.run(work, done, fail, name=name)
    return LATER


def approved(answer):
    return answer is True or (isinstance(answer, dict) and bool(answer.get("allow") or answer.get("approved")))


def confirm(ctx, title, lines, credits, button, then, alternative=False):
    """The credit card ("generate"): then() runs on approval; a decline finishes with "The user declined.".

    alternative: the card also offers Build in Blender (making a 3D model), which finishes with BUILD_IN_BLENDER —
    the agent then models it itself. Such a card is always shown, in full autonomy too (runtime._tool_card)."""

    def answered(answer):
        if alternative and answer == "blender":
            if not ctx.finished:
                ctx.finish(Result(BUILD_IN_BLENDER, label="Build in Blender instead"))
            return
        if not approved(answer):
            if not ctx.finished:
                ctx.finish(Result(DECLINED, error=True))
            return
        settle(ctx, then)

    payload = {"title": title[:120], "lines": [line for line in lines if line], "credits": credits, "button": button,
               "alternative": "blender" if alternative else None, "always_ask": bool(alternative)}
    ctx.card("generate", payload, answered)
    return LATER


def plan():
    """(3D gates, price list) — the account data the Studio panels use, fetched when they have not loaded it yet.
    Worker thread."""
    gates, prices = context.gates_3d, context.prices
    if gates is None:
        try:
            gates = zetta.entitlements_3d()
        except http.ApiError:
            gates = None
    if prices is None:
        try:
            prices = zetta.pricing()
        except http.ApiError:
            prices = None
    return gates, prices


def refresh_credits():
    try:
        context.refresh_credits()
    except Exception:  # noqa: BLE001 — the balance is cosmetic
        pass


def credits_text(value):
    return f" ({int(value):,} credits)" if isinstance(value, (int, float)) and value > 0 else ""


def fetch_generation(generation_id):
    """A library row (worker thread), with a line the model can act on when it is not the user's."""
    try:
        generation, _ = zetta.get_generation(generation_id)
    except http.ApiError as error:
        if error.status == 404:
            raise ToolError(f"Generation #{generation_id} is not in the user's library. Use list_generations to find "
                            "the id.") from None
        raise
    if not generation:
        raise ToolError(f"Generation #{generation_id} was not found.")
    return generation


def positive_id(args, key, hint=""):
    value = args.get(key)
    if isinstance(value, str) and value.strip().lstrip("#").isdigit():
        value = int(value.strip().lstrip("#"))
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ToolError(f"'{key}' is required: a positive number{hint}.")
    return value


def pick(args, key, allowed, fallback):
    value = args.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        return fallback
    value = str(value).strip().lower()
    if value not in allowed:
        raise ToolError(f"'{key}' must be one of {', '.join(allowed)}.")
    return value


def flag(args, key, fallback=None):
    value = args.get(key)
    if value is None:
        return fallback
    if not isinstance(value, bool):
        raise ToolError(f"'{key}' must be true or false.")
    return value


def short(text, limit):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def plural(count, one, many=None):
    return f"1 {one}" if count == 1 else f"{count:,} {many or one + 's'}"


def date(value):
    """"2026-11-14 10:32" from the API's ISO time."""
    text = str(value or "")
    return text[:16].replace("T", " ") if len(text) >= 16 else text or "—"


# ─── Where an import goes ────────────────────────────────────────────────────────────────────────


def parse_place(place):
    """The `place` argument, checked now: {parent: object name, location: [x, y, z], collection: name}."""
    if place is None:
        return {}
    if not isinstance(place, dict):
        raise ToolError("place is an object: { parent?, location?, collection? }.")
    unknown = set(place) - {"parent", "location", "position", "collection"}
    if unknown:
        raise ToolError(f"place takes parent, location and collection — not {', '.join(sorted(unknown))}.")
    found = {}
    if place.get("parent"):
        found["parent"] = targets.obj(place["parent"]).name
    location = place.get("location", place.get("position"))
    if location is not None:
        if (not isinstance(location, (list, tuple)) or len(location) != 3
                or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in location)):
            raise ToolError("place.location is [x, y, z].")
        found["location"] = [float(v) for v in location]
    if place.get("collection"):
        name = place["collection"]
        block = targets.datablock(name if str(name).startswith("collection:") else f"collection:{name}")
        found["collection"] = block.name
    return found


def place_text(place):
    if not place:
        return "at the 3D cursor, standing on the ground, in its own collection under \"Zetta 3D\""
    parts = []
    if place.get("location"):
        parts.append("at [" + ", ".join(f"{v:g}" for v in place["location"]) + "]")
    else:
        parts.append("at the 3D cursor")
    if place.get("parent"):
        parts.append(f"parented to {place['parent']}")
    parts.append(f"in collection \"{place['collection']}\"" if place.get("collection")
                 else "in its own collection under \"Zetta 3D\"")
    return ", ".join(parts)


def collection_path(collection):
    """'collection "Zetta 3D/Helmet #501"' — what the events and results name."""
    root = bpy.data.collections.get(importer.ROOT_COLLECTION)
    if root is not None and collection.name in root.children:
        return f'collection "{importer.ROOT_COLLECTION}/{collection.name}"'
    return f'collection "{collection.name}"'


def _roots(objects):
    chosen = set(objects)
    return [o for o in objects if o.parent is None or o.parent not in chosen]


def _settle_collection(objects, name):
    """Moves an import into the collection place named (dropping the one the importer made); returns its path."""
    made = next((c for o in objects for c in o.users_collection), None)
    target = bpy.data.collections.get(name) if name else None
    if target is None or target == made:
        return collection_path(made) if made is not None else None
    for obj in objects:
        for owner in list(obj.users_collection):
            owner.objects.unlink(obj)
        target.objects.link(obj)
    if made is not None and not made.objects and not made.children:
        bpy.data.collections.remove(made)
    return collection_path(target)


def describe_import(objects):
    """"Helmet (MESH) at [0, 0, 0], size [0.4, 0.3, 0.5]" for an import's top objects."""
    roots = _roots(objects)
    names = ", ".join(f"{o.name} ({o.type})" for o in roots[:6])
    if len(roots) > 6:
        names += f", +{len(roots) - 6} more"
    corners = []
    for obj in objects:
        if obj.type == "MESH":
            from mathutils import Vector

            corners.extend(obj.matrix_world @ Vector(c) for c in obj.bound_box)
    if not corners:
        return names
    low = [min(c[i] for c in corners) for i in range(3)]
    high = [max(c[i] for c in corners) for i in range(3)]
    where = roots[0].matrix_world.translation if roots else None
    text = names
    if where is not None:
        text += " at [" + ", ".join(f"{v:.3g}" for v in where) + "]"
    return text + ", size [" + ", ".join(f"{h - lo:.3g}" for lo, h in zip(low, high)) + "]"


def import_for_agent(gen, place, done, fail, job=None):
    """Downloads and imports a finished row (the add-on's importer), then moves it where place says.
    done(objects, path) / fail(message) on the main thread."""
    gen_id = int(gen["id"])
    if models.is_splat_row(gen):
        fail(SPLAT_REFUSAL)
        return
    current = importer.importing.get(gen_id)
    if current is not None and current.get("error") is None:
        fail(f"#{gen_id} is already being imported — wait a moment, then look for it in the scene.")
        return
    place = place or {}
    parent = bpy.data.objects.get(place["parent"]) if place.get("parent") else None

    def imported(objects):
        try:
            with changes.running():
                path = _settle_collection(objects, place.get("collection"))
        except Exception as error:  # noqa: BLE001 — the model landed; only the move failed
            traceback.print_exc()
            path = None
            print(f"[Zetta 3D] Could not move the import into {place.get('collection')}: {error}")
        done(objects, path)

    importer.import_generation(gen, place=True, location=place.get("location"), job=job, done=imported, fail=fail,
                               parent=parent, around=changes.running)


def _scene_step(label):
    push_undo(label)
    changes.scene_step(label)


# ─── The agent's jobs ────────────────────────────────────────────────────────────────────────────

_job_registry = None


def _agent_jobs():
    global _job_registry
    if _job_registry is None:
        saved = store.read("agent_jobs", {})
        _job_registry = saved if isinstance(saved, dict) else {}
    return _job_registry


def _save_jobs():
    store.write("agent_jobs", _agent_jobs())


def is_agent_job(generation_id):
    return str(int(generation_id)) in _agent_jobs()


def track_job(result, label, place, session=None):
    """Follows a job the agent just started (the add-on's jobs module), to be imported and reported by this module.
    Registered as the agent's BEFORE tracking, so a job that finishes at once is still recognised."""
    generation = result.get("generation") or {}
    if not generation.get("id"):
        raise ToolError("Zetta 3D did not return the new job.")
    _agent_jobs()[str(int(generation["id"]))] = {"place": place or {}, "session": (session or {}).get("id")}
    _save_jobs()
    listen()
    location = (place or {}).get("location") or common.cursor_location()
    jobs.track(generation, label, auto_import=False, place=True, location=location)
    refresh_credits()
    return generation


def _on_job_finished(job):
    entry = _agent_jobs().get(str(job.get("id")))
    if entry is None:
        return
    generation_id = int(job["id"])

    def forget():
        _agent_jobs().pop(str(generation_id), None)
        _save_jobs()

    if job.get("status") != "success" or not job.get("latest"):
        forget()
        events.emit("job_finished", {"generationId": generation_id, "status": job.get("status") or "failed"})
        return

    def done(objects, path):
        forget()
        title = models.display_title(job["latest"])
        _scene_step(f"Imported {title}"[:60])
        payload = {"generationId": generation_id, "status": "success"}
        if path:
            payload["importedPath"] = path
        events.emit("job_finished", payload)

    def fail(message):
        forget()
        print(f"[Zetta 3D] #{generation_id} finished but was not imported: {message}")
        events.emit("job_finished", {"generationId": generation_id, "status": "success"})

    place = dict(entry.get("place") or {})
    if place.get("location") is None and job.get("location") is not None:
        place["location"] = list(job["location"])  # the 3D cursor when the job started, as the panels place theirs
    import_for_agent(job["latest"], place, done, fail, job=job)


def listen():
    """Hooks the jobs module once (on import, and again after a reload)."""
    listeners = getattr(jobs, "_listeners", None)
    if listeners is None:
        jobs.on_finish(_on_job_finished)
        return
    for existing in list(listeners):
        if getattr(existing, "__name__", "") == "_on_job_finished" and existing is not _on_job_finished \
                and getattr(existing, "__module__", "") == __name__:
            listeners.remove(existing)
    if _on_job_finished not in listeners:
        jobs.on_finish(_on_job_finished)


def resume_jobs():
    """Agent jobs a previous session left: finished ones not yet imported are imported now."""
    for key in list(_agent_jobs()):
        job = jobs.find(int(key))
        if job is None:
            _agent_jobs().pop(key, None)
            continue
        if job.get("finished") and not job.get("imported"):
            _on_job_finished(job)
    _save_jobs()


# ─── The agent's pipeline runs ───────────────────────────────────────────────────────────────────

RUN_OVER = ("done", "failed", "cancelled")
POLL_SECONDS = 3.0
_run_registry = None
_polling = set()
_pending_imports = {}  # run id → imports still landing


def _agent_runs():
    global _run_registry
    if _run_registry is None:
        saved = store.read("agent_runs", {})
        _run_registry = saved if isinstance(saved, dict) else {}
    return _run_registry


def _save_runs():
    store.write("agent_runs", _agent_runs())


def following_run(run_id):
    return str(int(run_id)) in _agent_runs()


def follow_run(run, place=None, session=None):
    """Follows a run the agent started or manages: final models import as they land; the end is reported."""
    key = str(int(run["id"]))
    entry = _agent_runs().get(key) or {"pipelineId": run.get("pipelineId"), "imported": [], "paths": [],
                                       "place": place or {}, "reported": None,
                                       "session": (session or {}).get("id")}
    entry["status"] = run.get("status")
    _agent_runs()[key] = entry
    _save_runs()
    _start_timer()


def _start_timer():
    try:
        if not bpy.app.timers.is_registered(_tick):
            bpy.app.timers.register(_tick, first_interval=1.0, persistent=True)
    except (AttributeError, ValueError):
        pass


def _tick():
    if not _agent_runs():
        return None
    if not auth.signed_in():
        return POLL_SECONDS
    for key in list(_agent_runs()):
        if key not in _polling:
            poll_run(int(key))
    return POLL_SECONDS


def poll_run(run_id):
    from ..pipelines import api as pipeline_api

    key = str(run_id)
    _polling.add(key)

    def done(run):
        _polling.discard(key)
        if isinstance(run, dict):
            run_landed(run)

    def fail(error):
        _polling.discard(key)
        if isinstance(error, http.ApiError) and error.status == 404:
            _agent_runs().pop(key, None)
            _save_runs()

    tasks.run(lambda: pipeline_api.get_run(run_id), done, fail, name=f"zetta3d-agent-run-{run_id}")


def run_landed(run):
    """A fresh copy of a followed run: import what is new, report the end (or a wait for the user)."""
    from ..pipelines import state as pipelines

    key = str(int(run["id"]))
    entry = _agent_runs().get(key)
    if entry is None:
        return
    entry["status"] = run.get("status")
    for step in pipelines.importable_steps(run):
        if step["id"] in entry["imported"]:
            continue
        entry["imported"].append(step["id"])
        _pending_imports[key] = _pending_imports.get(key, 0) + 1

        def done(objects, path, gen=step["generation"]):
            _pending_imports[key] -= 1
            _scene_step(f"Imported {models.display_title(gen)}"[:60])
            if path and path not in entry["paths"]:
                entry["paths"].append(path)
            _save_runs()
            _report(key)

        def fail(message, gen=step["generation"]):
            _pending_imports[key] -= 1
            print(f"[Zetta 3D] Run #{key}: {models.display_title(gen)} was not imported: {message}")
            _report(key)

        import_for_agent(step["generation"], entry.get("place") or {}, done, fail)
    if run.get("status") != "awaiting" and entry.get("reported") == "awaiting":
        entry["reported"] = None
    _save_runs()
    _report(key)


def _report(key):
    entry = _agent_runs().get(key)
    if entry is None or _pending_imports.get(key, 0) > 0:
        return
    status = entry.get("status")
    if status in RUN_OVER or (status == "awaiting" and entry.get("reported") != "awaiting"):
        entry["reported"] = status
        events.emit("pipeline_run_finished", {"runId": int(key), "status": status,
                                               "importedPaths": list(entry.get("paths") or [])[:50]})
        if status in RUN_OVER:
            _agent_runs().pop(key, None)
            _pending_imports.pop(key, None)
        _save_runs()


# ─── Registration ────────────────────────────────────────────────────────────────────────────────


def start():
    """On import: hook the jobs module now, pick up what a previous session left once Blender is running (data
    cannot be touched while the add-on registers)."""
    listen()
    try:
        if not bpy.app.timers.is_registered(_resume_later):
            bpy.app.timers.register(_resume_later, first_interval=1.0)
    except (AttributeError, ValueError):
        pass


def _resume_later():
    register()
    return None


def register():
    """Hooks the jobs module and picks up what a previous session left (also done on import)."""
    listen()
    try:
        resume_jobs()
    except Exception:  # noqa: BLE001 — never block the add-on from loading
        traceback.print_exc()
    if _agent_runs():
        _start_timer()


def unregister():
    listeners = getattr(jobs, "_listeners", None)
    if listeners is not None and _on_job_finished in listeners:
        listeners.remove(_on_job_finished)
    for timer in (_tick, _resume_later):
        try:
            if bpy.app.timers.is_registered(timer):
                bpy.app.timers.unregister(timer)
        except (AttributeError, ValueError):
            pass
