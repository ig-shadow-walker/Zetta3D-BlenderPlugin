"""Account data every Studio tool reads: plan gates, the price list and the image models.

Loaded once per account, all four calls independently (one failing does not fail the others). A null gate, or a
gate field that is missing, means "unknown" and never locks anything: every check is written `is False` (locks)
or `is not False` (allows), never `not gate`.
"""

from .. import auth, config
from ..api import catalog, zetta
from ..core import tasks

gates_3d = None  # /3d/entitlements
gates_2d = None  # /2d/entitlements
prices = None  # /3d/pricing items
image_models = None  # {models, default}
load_error = None
_loaded_for = None
_loading = False
_listeners = []


def on_loaded(listener):
    """listener() runs on the main thread each time the account data lands."""
    _listeners.append(listener)


def _account_key():
    user = auth.user or {}
    return f"{config.api_base()}|{user.get('id')}"


def ensure_loaded():
    """Loads (or reloads after an error / an account switch). Safe to call from draw code."""
    global _loading, gates_3d, gates_2d, prices, image_models, load_error, _loaded_for
    if not auth.signed_in() or _loading:
        return
    key = _account_key()
    if _loaded_for == key:
        return  # loaded — or failed, and waiting for Retry
    if _loaded_for is not None:
        # A different account: never show the previous account's plan.
        gates_3d = gates_2d = prices = image_models = None
    _loading = True
    load_error = None

    def work():
        results, errors = {}, []
        for name, call in (
            ("prices", zetta.pricing),
            ("gates_3d", zetta.entitlements_3d),
            ("image_models", zetta.image_models),
            ("gates_2d", zetta.entitlements_2d),
        ):
            try:
                results[name] = call()
            except Exception as error:  # noqa: BLE001 — collected and shown once
                errors.append(error)
        return results, errors

    def done(answer):
        global _loading, gates_3d, gates_2d, prices, image_models, load_error, _loaded_for
        results, errors = answer
        _loading = False
        gates_3d = results.get("gates_3d", gates_3d)
        gates_2d = results.get("gates_2d", gates_2d)
        prices = results.get("prices", prices)
        image_models = results.get("image_models", image_models)
        load_error = catalog.explain(errors[0]) if errors else None
        _loaded_for = key  # after an error, loading again waits for Retry
        for listener in list(_listeners):
            listener()

    tasks.run(work, done, name="zetta3d-context")


def retry():
    global _loaded_for
    _loaded_for = None
    ensure_loaded()


def reset():
    global gates_3d, gates_2d, prices, image_models, load_error, _loaded_for, _loading
    gates_3d = gates_2d = prices = image_models = load_error = _loaded_for = None
    _loading = False


def loading():
    return _loading


def gate3(name):
    """A 3D gate's value, or None when unknown."""
    return (gates_3d or {}).get(name)


def gate2(name):
    return (gates_2d or {}).get(name)


def credits():
    return auth.credits("threeD")


def refresh_credits():
    """After any charged job or picture: re-read the account for the balance."""
    auth.refresh()


def price(code, model=None):
    from ..api import requests

    return requests.price_of(prices, code, model)


def price_label(value):
    if value is None:
        return "—"
    if value == 0:
        return "Free"
    return f"{value:,} credits"


def price_hint(code, model=None):
    """"Free" / "+N" — the suffix next to an option that costs extra."""
    value = price(code, model)
    if value is None:
        return ""
    return "Free" if value == 0 else f"+{value:,}"


def image_model_list():
    return (image_models or {}).get("models") or []


def default_image_model():
    models = image_model_list()
    default = (image_models or {}).get("default")
    if default:
        return default
    return models[0]["id"] if models else None


def image_model(model_id):
    return next((m for m in image_model_list() if m.get("id") == model_id), None)
