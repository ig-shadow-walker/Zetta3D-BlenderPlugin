"""What every Studio tool shares: its status line (busy / error / done) and starting a tracked job."""

import bpy

from .. import jobs
from ..api import catalog, zetta
from . import context


class Status:
    def __init__(self):
        self.busy = None  # the button's stage label while working ("Uploading… 40%")
        self.error = None
        self.done = None
        self.note = None  # a neutral line (fork note, detect result…)

    def start(self, label):
        self.busy, self.error, self.done = label, None, None

    def stage(self, label):
        self.busy = label

    def fail(self, error):
        self.busy = None
        self.error = catalog.explain(error) if isinstance(error, zetta.ApiError) else str(error)

    def finish(self, message=None):
        self.busy = None
        self.done = message


_status = {}


def status(name):
    if name not in _status:
        _status[name] = Status()
    return _status[name]


class Problem(Exception):
    """An expected user-input problem: its message is shown as is, never logged."""


def shorten(text, limit=60):
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def cursor_location():
    scene = bpy.context.scene
    return tuple(scene.cursor.location) if scene else None


def track(result, label, state):
    """Follows the job the server just accepted; returns the "Started" message."""
    generation = result.get("generation") or {}
    place = state.place and state.auto_import
    jobs.track(generation, label, auto_import=state.auto_import, place=place, location=cursor_location())
    quote = result.get("quote") or {}
    context.refresh_credits()
    if result.get("charged") and int(quote.get("credits") or 0) > 0:
        return f"Started — {int(quote['credits']):,} credits spent. Follow it in Jobs."
    return "Started. Follow it in Jobs."


def upload_picture(picture, remove_background, progress=None, what="Uploading"):
    """Checks a picture, optionally removes its background, uploads it; returns the engine-readable URL.
    Worker thread."""
    problem = picture.problem()
    if problem:
        raise Problem(problem)
    data, content_type = picture.data, picture.content_type
    if remove_background:
        if progress:
            progress("Removing background…")
        data = zetta.remove_background(data)
        if not data:
            raise Problem("Could not remove the background. Try again, or turn Remove background off.")
        content_type = "image/png"

    def report(fraction):
        if progress:
            progress(f"{what}… {int(fraction * 100)}%")

    ticket = zetta.upload(data, content_type, report)
    return ticket["fileUrl"]
