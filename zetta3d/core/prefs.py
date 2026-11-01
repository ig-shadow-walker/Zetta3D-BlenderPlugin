"""Reads the add-on's preferences from anywhere, including before they are registered."""

import bpy

# The add-on's own package: "zetta3d" as a legacy add-on, "bl_ext.<repo>.zetta3d" as an extension.
PACKAGE = __package__.rsplit(".", 1)[0]


def addon_prefs():
    try:
        addon = bpy.context.preferences.addons.get(PACKAGE)
    except AttributeError:
        return None
    return addon.preferences if addon else None


def get(name, default=None):
    prefs = addon_prefs()
    if prefs is None:
        return default
    return getattr(prefs, name, default)
