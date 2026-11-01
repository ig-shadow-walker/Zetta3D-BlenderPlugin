"""Follows every 3D job the add-on started until it finishes, then (optionally) imports it.

The list is kept on disk, so closing Blender while a model generates loses nothing: the next session picks the
job up where it was and imports it when it lands. Jobs run on the server either way.
"""

import time

from .api import models, zetta
from .core import state, tasks

POLL_SECONDS = 2.5
KEEP = 50

_jobs = []  # newest first: dicts, see track()
_followers = {}  # id → Token of the thread polling it
_listeners = []
_loaded = False


def on_finish(listener):
    """listener(job) runs on the main thread when a job reaches a terminal state."""
    _listeners.append(listener)


def all_jobs():
    _load()
    return _jobs


def find(job_id):
    return next((j for j in all_jobs() if j["id"] == job_id), None)


def running():
    return [j for j in all_jobs() if not j.get("finished")]


def _load():
    global _jobs, _loaded
    if _loaded:
        return
    _loaded = True
    saved = state.read("jobs", [])
    _jobs = [j for j in saved if isinstance(j, dict) and "id" in j] if isinstance(saved, list) else []


def _save():
    keep = [{k: v for k, v in j.items() if k != "latest"} for j in _jobs[:KEEP]]
    state.write("jobs", keep)


def track(generation, label, auto_import=True, place=True, location=None, kind="model"):
    """Starts following a job the server just accepted. location: where to place it (the 3D cursor)."""
    _load()
    job = {
        "id": int(generation["id"]),
        "label": label,
        "kind": kind,
        "status": generation.get("status") or "pending",
        "queuePosition": None,
        "autoImport": auto_import,
        "place": place,
        "location": list(location) if location is not None else None,
        "imported": None,
        "error": None,
        "progress": None,
        "startedAt": time.time(),
        "finished": False,
        "latest": generation,
    }
    _jobs[:] = [j for j in _jobs if j["id"] != job["id"]]
    _jobs.insert(0, job)
    _save()
    _follow(job)
    return job


def resume():
    """Follows every job a previous session left running."""
    for job in all_jobs():
        if not job.get("finished"):
            _follow(job)


def _follow(job):
    job_id = job["id"]
    if job_id in _followers:
        return
    token = tasks.Token()
    _followers[job_id] = token

    def work(token):
        failures = 0
        while True:
            token.check()
            try:
                generation, position = zetta.get_generation(job_id)
                failures = 0
            except zetta.ApiError as error:
                if error.status == 404:
                    return {"id": job_id, "status": "failed", "errorMessage": "This job no longer exists."}, None
                failures += 1
                # Offline for a while: keep the job, look again less often.
                token.sleep(min(30, POLL_SECONDS * (failures + 1)))
                continue
            tasks.post(_update, job_id, generation, position)
            if models.is_terminal(generation):
                return generation, position
            token.sleep(POLL_SECONDS)

    def done(result):
        _followers.pop(job_id, None)
        generation, position = result
        _update(job_id, generation, position)
        _finish(job_id)

    def fail(error):
        _followers.pop(job_id, None)
        current = find(job_id)
        if current:
            current["error"] = str(error)
            _save()

    tasks.run(work, done, fail, token=token, name=f"zetta3d-job-{job_id}")


def _update(job_id, generation, position):
    job = find(job_id)
    if job is None or not generation:
        return
    changed = job.get("status") != generation.get("status") or job.get("queuePosition") != position
    job["latest"] = generation
    job["status"] = generation.get("status")
    job["queuePosition"] = position
    if generation.get("status") == "failed":
        job["error"] = generation.get("errorMessage") or "The job failed."
    if changed:
        _save()


def _finish(job_id):
    job = find(job_id)
    if job is None or job.get("finished"):
        return
    job["finished"] = True
    _save()
    if job["status"] == "success" and job.get("autoImport") and job.get("latest"):
        from . import importer

        importer.import_generation(job["latest"], place=job.get("place", True), location=job.get("location"),
                                   job=job)
    for listener in list(_listeners):
        try:
            listener(job)
        except Exception as error:  # noqa: BLE001
            print(f"[Zetta 3D] {error!r}")


def cancel(job_id):
    """Asks the server to cancel; the follower sees it land."""
    job = find(job_id)
    if job is None or job.get("finished"):
        return

    def fail(error):
        job["error"] = str(error)

    tasks.run(lambda: zetta.cancel_generation(job_id), None, fail, name="zetta3d-cancel")


def dismiss(job_id):
    _load()
    token = _followers.pop(job_id, None)
    if token:
        token.cancel()
    _jobs[:] = [j for j in _jobs if j["id"] != job_id]
    _save()


def clear_finished():
    _load()
    _jobs[:] = [j for j in _jobs if not j.get("finished")]
    _save()


def stop():
    """Stops polling (the add-on is being disabled); the jobs stay on disk for next time."""
    for token in _followers.values():
        token.cancel()
    _followers.clear()
