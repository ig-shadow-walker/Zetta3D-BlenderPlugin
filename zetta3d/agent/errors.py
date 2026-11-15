"""Python errors since Blender started — what `recent_errors` and the snapshot's `recentErrors` report.

The agent's own failures are recorded here directly (run_python, a tool's bug). Blender prints errors from
handlers, drivers and other add-ons to the console only, so those are caught from `sys.stderr` as lines that
look like a traceback's last line or a Blender error report.
"""

import re
import sys
import time

_errors = []  # newest last: {"source", "message", "at"}
MAX = 200

_LAST_LINE = re.compile(r"^(?:[A-Z][A-Za-z]*(?:Error|Exception|Warning)|Error|Warning)\b[:\s].{0,500}$")


def record(source, message):
    message = " ".join(str(message).split())[:600]
    if not message:
        return
    if _errors and _errors[-1]["message"] == message and _errors[-1]["source"] == source:
        return
    _errors.append({"source": source, "message": message, "at": time.time()})
    del _errors[:-MAX]


def recent(limit=20):
    return list(reversed(_errors[-limit:]))


class _Tee:
    """stderr, unchanged, with error lines noted on the way through."""

    def __init__(self, stream):
        self._stream = stream
        self._buffer = ""

    def write(self, text):
        try:
            self._buffer += text
            *lines, self._buffer = self._buffer.split("\n")
            for line in lines:
                if _LAST_LINE.match(line.strip()):
                    record("Blender", line.strip())
        except Exception:  # noqa: BLE001 — never break Blender's own output
            pass
        return self._stream.write(text)

    def flush(self):
        return self._stream.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


_original = None


def register():
    global _original
    if _original is None and not isinstance(sys.stderr, _Tee):
        _original = sys.stderr
        sys.stderr = _Tee(sys.stderr)


def unregister():
    global _original
    if _original is not None and isinstance(sys.stderr, _Tee):
        sys.stderr = _original
    _original = None
