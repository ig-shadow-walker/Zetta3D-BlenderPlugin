"""Per-machine state the add-on keeps between sessions: the sign-in, running jobs, what the update check found.

NEVER IN THE .BLEND. A scene file is shared and committed; a session token is not. Everything here lives in
the extension's user folder (or ~/.config/zetta3d-blender for a legacy install), one JSON file per concern,
written whole and atomically.
"""

import json
import os
import threading

import bpy

from .prefs import PACKAGE

_lock = threading.Lock()
_folder = None


def folder():
    """The add-on's private folder on this machine, created on first use."""
    global _folder
    if _folder:
        return _folder
    path = None
    try:
        path = bpy.utils.extension_path_user(PACKAGE, path="", create=True)
    except (ValueError, AttributeError, TypeError):
        # A legacy add-on (not an extension) has no extension folder.
        path = None
    if not path:
        path = os.path.join(os.path.expanduser("~"), ".config", "zetta3d-blender")
    os.makedirs(path, exist_ok=True)
    _folder = path
    return path


def path(name):
    return os.path.join(folder(), name)


def read(name, default=None):
    file = path(name + ".json")
    with _lock:
        try:
            with open(file, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return default


def write(name, value, private=False):
    file = path(name + ".json")
    temp = file + ".tmp"
    with _lock:
        try:
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump(value, handle)
            if private:
                # Owner read/write only: a session token is a password for as long as it lasts.
                try:
                    os.chmod(temp, 0o600)
                except OSError:
                    pass
            os.replace(temp, file)
        except OSError as error:
            print(f"[Zetta 3D] Could not write {file}: {error}")


def delete(name):
    file = path(name + ".json")
    with _lock:
        try:
            os.remove(file)
        except OSError:
            pass
