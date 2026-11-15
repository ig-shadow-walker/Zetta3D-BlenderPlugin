"""Files the user attached to a message, kept for a week in the add-on's folder — never next to their .blend.

A picture attached from outside the file (or a capture of the viewport) is named "[attached:<name>]" in the
message, and the agent passes "attached:<name>" as a path (BlenderAgentProtocol.md › Pictures). Other files the
user attaches are copied next to the .blend under //zetta3d/attachments/ instead, so the agent can import them.
"""

import os
import re
import time

from ..core import state

KEEP_SECONDS = 7 * 24 * 3600


def folder():
    path = state.path("attached")
    os.makedirs(path, exist_ok=True)
    return path


def _safe(name):
    name = re.sub(r"[^\w.\- ]+", "_", os.path.basename(name or "file")).strip(" .") or "file"
    return name[:120]


def store(name, data):
    """Keeps data under a free name; returns the name to write as "attached:<name>"."""
    clean = _safe(name)
    base, ext = os.path.splitext(clean)
    candidate, n = clean, 1
    while os.path.exists(os.path.join(folder(), candidate)):
        n += 1
        candidate = f"{base}-{n}{ext}"
    with open(os.path.join(folder(), candidate), "wb") as handle:
        handle.write(data)
    return candidate


def path(name):
    """The stored file for "attached:<name>", or None."""
    full = os.path.join(folder(), _safe(name))
    return full if os.path.isfile(full) else None


def prune():
    """Drops attachments older than a week."""
    cutoff = time.time() - KEEP_SECONDS
    try:
        for entry in os.scandir(folder()):
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                os.remove(entry.path)
    except OSError:
        pass
