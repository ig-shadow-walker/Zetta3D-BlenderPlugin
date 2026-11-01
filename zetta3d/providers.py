"""The providers the user can bring their own API key for, and the keys' state.

NOTHING HARDCODED. The server's list (GET /unityAgent/options → providers) is the truth, so a new or retired
provider or model needs no add-on update. The list is cached on disk so Preferences can show it offline. The
keys themselves live in the system key store (core.keychain), never here.

Phase 1 manages the keys; the Zetta Agent (phase 2) is what runs on them.
"""

from .api import zetta
from .core import keychain, state, tasks

ZETTA = "zetta"  # run the agent on the Zetta account
OWN = "own"  # run it on the user's own keys

_providers = None
_loading = False
checks = {}  # provider → (ok: bool | None, message) of the last check, for Preferences


def providers():
    """[{key, label, keyUrl, models: [{key, label, blurb, vision, documents, thinking}]}], or None when the
    server offers none (or has not answered yet)."""
    global _providers
    if _providers is None:
        cached = state.read("providers")
        if isinstance(cached, list):
            _providers = cached
    return _providers


def provider(key):
    return next((p for p in providers() or () if p.get("key") == key), None)


def keyed():
    """Providers with a saved key, in the server's order."""
    return [p for p in providers() or () if keychain.has(p.get("key"))]


def loading():
    return _loading


def refresh():
    global _loading
    if _loading:
        return
    _loading = True

    def done(options):
        global _providers, _loading
        _loading = False
        found = options.get("providers") if isinstance(options, dict) else None
        _providers = found if isinstance(found, list) else None
        state.write("providers", _providers or [])

    def fail(_error):
        global _loading
        _loading = False  # offline or an older server: keep what we had

    tasks.run(zetta.agent_options, done, fail, name="zetta3d-providers")


def save_key(provider_key, key):
    """Saves the key and checks it. Returns an error to show, or None."""
    error = keychain.set(provider_key, key)
    if error:
        return error
    check_key(provider_key)
    return None


def remove_key(provider_key):
    keychain.delete(provider_key)
    checks.pop(provider_key, None)


def check_key(provider_key):
    key = keychain.get(provider_key)
    checks[provider_key] = (None, "Checking…")

    def work():
        return zetta.check_provider_key(provider_key, key)

    def done(answer):
        ok = bool((answer or {}).get("ok"))
        checks[provider_key] = (ok, "The key works." if ok else (answer or {}).get("message") or "The key didn't work.")

    def fail(error):
        checks[provider_key] = (False, str(error))

    tasks.run(work, done, fail, name="zetta3d-check-key")
