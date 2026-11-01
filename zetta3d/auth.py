"""Sign-in through the browser.

The add-on listens on 127.0.0.1, opens zetta3d.ai/plugin-auth, the user presses Connect, and the page POSTs
{token, state} to http://127.0.0.1:<port>/callback. The token is a plugin session (Bearer): Blender never
sees a password.
"""

import json
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config
from .core import http, session, tasks

SIGNED_OUT = "SIGNED_OUT"
WAITING = "WAITING"  # for the browser
CHECKING = "CHECKING"
SIGNED_IN = "SIGNED_IN"

EXPIRED = "Your Zetta 3D sign-in has expired. Sign in again."
LOGIN_TIMEOUT = 600

current = SIGNED_OUT
user = None  # the /auth/me user dict
last_error = None

_server = None
_state = None
_timer = None
_listeners = []


def on_change(listener):
    _listeners.append(listener)


def signed_in():
    return current == SIGNED_IN and user is not None


def _set(new_state, new_user):
    global current, user, last_error
    current = new_state
    user = new_user
    if new_state == SIGNED_IN:
        last_error = None
    for listener in list(_listeners):
        try:
            listener()
        except Exception as error:  # noqa: BLE001 — one listener must not stop the rest
            print(f"[Zetta 3D] {error!r}")
    tasks.redraw()


def start():
    """Called once the add-on is registered: picks up a saved sign-in."""
    http.on_unauthorized(_on_unauthorized)
    if session.token():
        _set(CHECKING, None)
        refresh()
    else:
        _set(SIGNED_OUT, None)


def refresh():
    """Re-reads the account (plan, credits). Safe to call often."""
    global last_error
    if not session.token():
        _set(SIGNED_OUT, None)
        return
    if session.expired():
        sign_out(EXPIRED)
        return

    def work():
        return (http.get("/auth/me", timeout=30) or {}).get("user")

    def done(me):
        if me:
            _set(SIGNED_IN, me)
        else:
            sign_out(EXPIRED)

    def fail(error):
        global last_error
        if isinstance(error, http.ApiError) and error.unauthorized:
            sign_out(EXPIRED)
            return
        # Offline or the API is down: keep the token, show the error, stay usable later.
        last_error = str(error)
        _set(SIGNED_IN if user else CHECKING, user)

    tasks.run(work, done, fail, name="zetta3d-me")


def sign_out(reason=None):
    global last_error
    cancel_login()
    session.clear()
    last_error = reason
    _set(SIGNED_OUT, None)


def _on_unauthorized():
    if current in (SIGNED_IN, CHECKING):
        sign_out(EXPIRED)


# ─── Browser handoff ──────────────────────────────────────────────────────────────────────────────


class _Handler(BaseHTTPRequestHandler):
    server_version = "Zetta3D-Blender"

    def log_message(self, *args):  # quiet: nothing of the handoff belongs in the console
        pass

    def _cors(self):
        origin = self.headers.get("Origin")
        # The page is the only caller; echo its origin rather than '*' so nothing else can read the reply.
        self.send_header("Access-Control-Allow-Origin", origin or config.site_base())
        self.send_header("Vary", "Origin")

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        # Chrome's Private Network Access: a public page calling 127.0.0.1 is preflighted.
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):  # noqa: N802
        if self.path.split("?", 1)[0].rstrip("/") != "/callback":
            self._reply(404, {"ok": False})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (ValueError, OSError):
            self._reply(400, {"ok": False, "message": "Bad request."})
            return
        token = body.get("token")
        if not token or not _state or not secrets.compare_digest(str(body.get("state") or ""), _state):
            self._reply(400, {"ok": False, "message": "State mismatch."})
            return
        self._reply(200, {"ok": True})
        tasks.post(_complete, token, self.server)

    def _reply(self, status, payload):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def begin_login():
    """Starts the listener and opens the sign-in page. Returns an error string, or None."""
    global _server, _state, _timer, last_error
    cancel_login()
    last_error = None
    try:
        _state = secrets.token_hex(24)
        _server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        port = _server.server_address[1]
        threading.Thread(target=_server.serve_forever, name="zetta3d-signin", daemon=True).start()
    except OSError as error:
        cancel_login()
        last_error = f"Could not start the sign-in listener: {error}"
        _set(SIGNED_OUT, None)
        return last_error

    _timer = threading.Timer(LOGIN_TIMEOUT, lambda: tasks.post(_timed_out))
    _timer.daemon = True
    _timer.start()
    url = f"{config.site_base()}/plugin-auth?port={port}&state={_state}&client={config.CLIENT_NAME}"
    webbrowser.open(url)
    _set(WAITING, None)
    return None


def login_url():
    """The page the browser was sent to, for copying when it did not open."""
    if _server is None or _state is None:
        return None
    return f"{config.site_base()}/plugin-auth?port={_server.server_address[1]}&state={_state}&client={config.CLIENT_NAME}"


def cancel_login():
    global _server, _state, _timer
    if _timer is not None:
        _timer.cancel()
        _timer = None
    server, _server = _server, None
    _state = None
    if server is not None:
        threading.Thread(target=_shutdown, args=(server,), daemon=True).start()
    if current == WAITING:
        _set(SIGNED_OUT, None)


def _shutdown(server):
    try:
        server.shutdown()
        server.server_close()
    except OSError:
        pass


def _timed_out():
    global last_error
    if current == WAITING:
        cancel_login()
        last_error = "Sign-in timed out. Try again."
        tasks.redraw()


def _complete(token, server):
    if server is not _server:
        return  # a listener that was cancelled meanwhile
    session.save(token)
    cancel_login()
    _set(CHECKING, None)
    refresh()


def stop():
    cancel_login()


# ─── What the UI shows ────────────────────────────────────────────────────────────────────────────


def display_name():
    if not user:
        return ""
    return user.get("displayName") or user.get("email") or ""


def plan_label():
    plan = (user or {}).get("paymentPlan")
    if plan == "creator_pro":
        return "Creator Pro"
    if not plan or plan == "none":
        return "Free"
    return plan[:1].upper() + plan[1:]


def is_free():
    return (user or {}).get("paymentPlan") in (None, "", "none", "free")


def credits(pool="threeD"):
    """The total of one credit pool (threeD or ai)."""
    values = (((user or {}).get("credits") or {}).get(pool)) or {}
    return sum(int(values.get(k) or 0) for k in ("monthly", "purchased", "bonusMonthly", "bonus"))
