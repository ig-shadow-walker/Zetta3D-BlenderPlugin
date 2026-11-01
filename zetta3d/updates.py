"""Tells the user when a newer release of the add-on is out, and installs it.

Checks as Blender starts and every 6 hours while it stays open (an hour after a failed check). Asks the API
first (GET /blender-plugin/version, which reads this repository and lets the server override it), then the
repository itself — for a developer's own server without the route: main's blender_manifest.toml for the
latest version and release/update.json for a required minimum and a message.

Installing downloads the release's zip from GitHub and hands it to Blender's own extension installer.
"""

import json
import os
import re
import tempfile
import time
import webbrowser

import bpy

from . import config
from .core import http, state, tasks
from .core.prefs import PACKAGE

EVERY = 6 * 3600
RETRY_AFTER_FAILURE = 3600

known = None  # {latest, minimum, message, notesUrl}
problem = None
installing = False
installed_version = None  # set once an update is installed, until Blender restarts
_pending = False
_last_checked = 0.0


def _version_tuple(text):
    parts = re.findall(r"\d+", str(text or ""))[:3]
    return tuple(int(p) for p in parts) + (0,) * (3 - len(parts)) if parts else None


def newer(a, b):
    """True when version a is newer than b."""
    va, vb = _version_tuple(a), _version_tuple(b)
    return va is not None and vb is not None and va > vb


def available():
    return bool(known) and newer(known.get("latest"), installed_version or config.VERSION)


def required():
    return available() and bool(known.get("minimum")) and newer(known["minimum"], config.VERSION)


def _load():
    global known, _last_checked
    saved = state.read("updates") or {}
    known = saved.get("known")
    _last_checked = float(saved.get("checkedAt") or 0)


def _save():
    state.write("updates", {"known": known, "checkedAt": _last_checked})


def _release_from(answer):
    if not isinstance(answer, dict):
        return None
    latest = str(answer.get("latest") or "").strip()
    if not _version_tuple(latest):
        return None

    def text(key):
        value = answer.get(key)
        return value.strip() if isinstance(value, str) and value.strip() else None

    return {
        "latest": latest,
        "minimum": text("minimum"),
        "message": text("message"),
        "notesUrl": text("notesUrl") or f"{config.RELEASES_URL}/tag/v{latest}",
    }


def _fetch():
    try:
        answer = http.request("GET", "/blender-plugin/version?" + http.query({"current": config.VERSION}),
                              auth=False, timeout=10)
        release = _release_from(answer)
        if release:
            return release
    except http.ApiError:
        pass

    # Straight from the repository: the server doesn't serve this, is down, or is a developer's own.
    try:
        manifest = http.fetch_bytes(config.RAW_URL + "/zetta3d/blender_manifest.toml", timeout=10).decode("utf-8")
    except http.ApiError:
        return None
    match = re.search(r'^\s*version\s*=\s*"([^"]+)"', manifest, re.MULTILINE)
    if not match:
        return None
    update = {}
    try:
        update = json.loads(http.fetch_bytes(config.RAW_URL + "/release/update.json", timeout=10).decode("utf-8"))
    except (http.ApiError, ValueError):
        pass  # no release file: nothing is required
    return _release_from({"latest": match.group(1), **{k: update.get(k) for k in ("minimum", "message", "notesUrl")}})


def check(force=False, done=None):
    """Asks for the newest release (now when forced, else when the last check is old enough)."""
    global _pending
    if _pending:
        return
    now = time.time()
    if not force and now < _last_checked + EVERY:
        return
    _pending = True

    def finished(release):
        global known, _pending, _last_checked
        _pending = False
        if release:
            known = release
            _last_checked = time.time()
        else:
            _last_checked = time.time() - EVERY + RETRY_AFTER_FAILURE
        _save()
        if done:
            done(release)

    def failed(_error):
        finished(None)

    tasks.run(_fetch, finished, failed, name="zetta3d-updates")


def checking():
    return _pending


def _repo_module():
    # An extension's package is "bl_ext.<repo>.zetta3d"; a legacy add-on has no repository.
    parts = PACKAGE.split(".")
    return parts[1] if len(parts) == 3 and parts[0] == "bl_ext" else None


def can_install():
    return _repo_module() is not None


def install():
    """Downloads the release zip and installs it over this copy. Blender keeps it enabled."""
    global installing, problem
    if not available() or installing:
        return
    repo = _repo_module()
    if repo is None:
        webbrowser.open(known.get("notesUrl") or config.RELEASES_URL)
        return
    latest = known["latest"]
    url = f"{config.RELEASES_URL}/download/v{latest}/zetta3d-{latest}.zip"
    installing, problem = True, None

    def work():
        data = http.download(url)
        path = os.path.join(tempfile.mkdtemp(prefix="zetta3d-update-"), f"zetta3d-{latest}.zip")
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def done(path):
        global installing, problem, installed_version
        installing = False
        try:
            result = bpy.ops.extensions.package_install_files(filepath=path, repo=repo, enable_on_install=True)
        except (RuntimeError, TypeError) as error:
            problem = f"The update didn't finish: {error}"
            return
        if "FINISHED" in result:
            installed_version = latest
        else:
            problem = "The update didn't finish. Download it from the releases page instead."

    def fail(error):
        global installing, problem
        installing = False
        problem = f"The update could not be downloaded: {error}"

    tasks.run(work, done, fail, name="zetta3d-update-install")


def open_notes():
    webbrowser.open((known or {}).get("notesUrl") or config.RELEASES_URL)


def _tick():
    check()
    return 600.0


def register():
    _load()
    bpy.app.timers.register(_tick, first_interval=3.0, persistent=True)


def unregister():
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
