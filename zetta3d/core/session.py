"""The sign-in token, kept in a private file on this machine (never in a .blend or the preferences)."""

import base64
import json
import time

from . import state

_token = None
_expiry = 0
_loaded = False


def _load():
    global _token, _expiry, _loaded
    if _loaded:
        return
    _loaded = True
    saved = state.read("session", {}) or {}
    _token = saved.get("token") or None
    _expiry = int(saved.get("expiry") or 0)


def token():
    _load()
    return _token


def expired():
    _load()
    return bool(_expiry) and time.time() > _expiry


def save(new_token):
    global _token, _expiry, _loaded
    _loaded = True
    _token = new_token
    _expiry = read_expiry(new_token)
    state.write("session", {"token": _token, "expiry": _expiry}, private=True)


def clear():
    global _token, _expiry, _loaded
    _loaded = True
    _token = None
    _expiry = 0
    state.delete("session")


def read_expiry(jwt):
    """The JWT's `exp` claim, unverified (the server does that); 0 when unknown."""
    try:
        payload = jwt.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return int(json.loads(base64.urlsafe_b64decode(payload)).get("exp") or 0)
    except (IndexError, ValueError, TypeError, AttributeError):
        return 0
