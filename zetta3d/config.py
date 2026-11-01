"""Where the add-on talks to, and what it calls itself.

Production by default; a developer can point it at a local backend from
Preferences › Add-ons › Zetta 3D › Developer without touching code.
"""

VERSION = "0.1.0"

# What the API knows this client as: X-Zetta-Client, the jobs' `origin`, and the
# `client` the sign-in page is opened with.
CLIENT_NAME = "blender"

PRODUCTION_API = "https://api.zetta3d.ai/api"
PRODUCTION_SITE = "https://zetta3d.ai"

REPOSITORY_URL = "https://github.com/ig-shadow-walker/Zetta3D---BlenderPlugin"
RELEASES_URL = REPOSITORY_URL + "/releases"
RAW_URL = "https://raw.githubusercontent.com/ig-shadow-walker/Zetta3D---BlenderPlugin/main"

PRICING_URL = PRODUCTION_SITE + "/pricing"


def _trim(value, fallback):
    value = (value or "").strip().rstrip("/")
    return value or fallback


def api_base():
    from .core import prefs
    return _trim(prefs.get("api_base", ""), PRODUCTION_API)


def site_base():
    from .core import prefs
    return _trim(prefs.get("site_base", ""), PRODUCTION_SITE)


def is_production():
    return api_base() == PRODUCTION_API
