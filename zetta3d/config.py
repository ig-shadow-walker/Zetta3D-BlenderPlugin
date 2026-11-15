"""Where the add-on talks to, and what it calls itself.

ALWAYS PRODUCTION: https://api.zetta3d.ai (every route is under /api) and https://zetta3d.ai for sign-in. There
is no setting, switch or variable for it — the addresses below are the only ones the add-on uses.
"""

VERSION = "0.1.0"

# What the API knows this client as: X-Zetta-Client, the jobs' `origin`, and the
# `client` the sign-in page is opened with.
CLIENT_NAME = "blender"

PRODUCTION_API = "https://api.zetta3d.ai/api"
PRODUCTION_SITE = "https://zetta3d.ai"

REPOSITORY_URL = "https://github.com/ig-shadow-walker/Zetta3D-BlenderPlugin"
RELEASES_URL = REPOSITORY_URL + "/releases"
RAW_URL = "https://raw.githubusercontent.com/ig-shadow-walker/Zetta3D-BlenderPlugin/main"

PRICING_URL = PRODUCTION_SITE + "/pricing"


def api_base():
    return PRODUCTION_API


def site_base():
    return PRODUCTION_SITE


def is_production():
    return api_base() == PRODUCTION_API
