"""The agent's routes on the Zetta 3D API (BlenderAgentProtocol.md) — called from worker threads only.

`chat` streams one request of a turn: the answer is a stream of server-sent events, each handed to the main
thread as it arrives, so the transcript fills in while the model writes. Everything else is a plain request.
"""

import json
import socket
import urllib.error
import urllib.request

from ..core import http, tasks

ROOT = "/blenderAgent"
# A turn can think for a while before its first word; the socket waits that long between events.
READ_TIMEOUT = 300


def _key_header(provider_key):
    return {"X-Zetta-Provider-Key": provider_key} if provider_key else {}


def chat(body, on_event, provider_key=None, token=None):
    """POST /blenderAgent/chat and hand every event to on_event(event) on the main thread. Returns when the
    stream ends. Raises ApiError for an HTTP failure (before the stream opens)."""
    http._check_online()
    headers = http._headers(True, {"Content-Type": "application/json", "Accept": "text/event-stream",
                                   **_key_header(provider_key)})
    data = json.dumps(body, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(http.url_for(ROOT + "/chat"), data=data, method="POST", headers=headers)
    try:
        response = urllib.request.urlopen(request, timeout=READ_TIMEOUT, context=http._ssl)
    except urllib.error.HTTPError as error:
        raise http._failure(error.code, error.read().decode("utf-8", "replace")) from None
    except (urllib.error.URLError, OSError) as error:
        raise http.ApiError(0, f"Could not reach Zetta 3D ({getattr(error, 'reason', error)}).",
                            "UNREACHABLE") from None
    frame = []
    try:
        with response:
            for raw in response:
                if token is not None and token.cancelled:
                    return
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("data:"):
                    frame.append(line[5:].lstrip())
                elif not line and frame:
                    _deliver("\n".join(frame), on_event)
                    frame = []
            if frame:
                _deliver("\n".join(frame), on_event)
    except (socket.timeout, TimeoutError):
        raise http.ApiError(0, "The agent stopped answering. Try again.", "TIMEOUT") from None
    except OSError as error:
        if token is not None and token.cancelled:
            return
        raise http.ApiError(0, f"The connection dropped ({error}).", "UNREACHABLE") from None


def _deliver(text, on_event):
    try:
        event = json.loads(text)
    except ValueError:
        return  # a frame that is not JSON is not ours to act on
    if isinstance(event, dict):
        tasks.post(on_event, event)


def options():
    return http.get(ROOT + "/options")


def sessions(project_id):
    return (http.get(ROOT + "/sessions?" + http.query({"projectId": project_id})) or {}).get("sessions") or []


def session(session_id):
    return http.get(f"{ROOT}/sessions/{int(session_id)}")


def delete_session(session_id):
    return http.delete(f"{ROOT}/sessions/{int(session_id)}")


def stop(session_id):
    return http.post(f"{ROOT}/sessions/{int(session_id)}/stop")


def memories(project_id):
    return http.get(ROOT + "/memories?" + http.query({"projectId": project_id})) or {}


def add_memory(text, scope, category, project_id):
    body = {"text": text, "scope": scope, "category": category}
    if scope == "project":
        body["projectId"] = project_id
    return http.post(ROOT + "/memories", body)


def delete_memory(memory_id):
    return http.delete(f"{ROOT}/memories/{int(memory_id)}")


def put_brief(project_id, brief):
    return http.put(ROOT + "/brief", {"projectId": project_id, "brief": brief})


def image(image_id):
    """{url, contentType} for a picture the server made (generate_image), signed again."""
    return http.get(f"{ROOT}/images/{int(image_id)}")
