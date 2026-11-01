"""Background work for a single-threaded host.

Blender's data and UI may only be touched from the main thread, and a network call must never block it. So:
work runs on a worker thread, and everything it wants to tell the UI is posted back here and run on the
main thread by a timer. Panels redraw after anything lands.
"""

import queue
import threading
import traceback

import bpy

_inbox = queue.Queue()
_registered = False


class Cancelled(Exception):
    """Raised inside work whose token was cancelled."""


class Token:
    """A cancellation flag shared between the UI and one piece of work."""

    def __init__(self):
        self._event = threading.Event()

    def cancel(self):
        self._event.set()

    @property
    def cancelled(self):
        return self._event.is_set()

    def check(self):
        if self._event.is_set():
            raise Cancelled()

    def sleep(self, seconds):
        """Waits, waking early (and raising) if cancelled."""
        if self._event.wait(seconds):
            raise Cancelled()


def post(callback, *args):
    """Runs callback(*args) on the main thread, soon."""
    _inbox.put((callback, args))


def run(work, done=None, fail=None, token=None, name="zetta3d"):
    """Runs work() on a worker thread; done(result) or fail(error) follow on the main thread.

    work may take a `token` keyword when one is given. A Cancelled error is not reported to fail.
    """

    def body():
        try:
            result = work(token) if token is not None else work()
        except Cancelled:
            return
        except Exception as error:  # noqa: BLE001 — every failure is reported to the UI, never lost
            if fail is not None:
                post(fail, error)
            else:
                traceback.print_exc()
            return
        if done is not None:
            post(done, result)

    thread = threading.Thread(target=body, name=name, daemon=True)
    thread.start()
    return thread


def redraw():
    """Tags every area the add-on draws in."""
    wm = getattr(bpy.context, "window_manager", None)
    if wm is None:
        return
    for window in wm.windows:
        screen = window.screen
        if screen is None:
            continue
        for area in screen.areas:
            if area.type in {"VIEW_3D", "PREFERENCES", "NODE_EDITOR", "IMAGE_EDITOR"}:
                area.tag_redraw()


def _pump():
    ran = False
    while True:
        try:
            callback, args = _inbox.get_nowait()
        except queue.Empty:
            break
        ran = True
        try:
            callback(*args)
        except Exception:  # noqa: BLE001 — one bad callback must not stop the rest
            traceback.print_exc()
    if ran:
        redraw()
    return 0.1


def register():
    global _registered
    if not _registered:
        bpy.app.timers.register(_pump, first_interval=0.1, persistent=True)
        _registered = True


def unregister():
    global _registered
    if _registered and bpy.app.timers.is_registered(_pump):
        bpy.app.timers.unregister(_pump)
    _registered = False
