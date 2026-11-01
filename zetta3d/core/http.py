"""The one HTTP client for the add-on. Call it from worker threads only (see tasks.run).

Standard library only. The API's edge refuses Python's default user agent, so every request names the add-on.
"""

import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

import bpy

from .. import config
from . import session, tasks

# Above this, an upload goes up in pieces and a download comes down in ranges: the edge in front of the API
# ends any request after 100 seconds.
PIECE_BYTES = 8 * 1024 * 1024
RETRY_DELAYS = (0.6, 1.8)

# Read once on the main thread as the add-on registers.
_host_version = ""
_unauthorized_listeners = []

try:
    import certifi

    _ssl = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _ssl = ssl.create_default_context()


class ApiError(Exception):
    """An error the API answered with. Branch on `code` (INSUFFICIENT_CREDITS, JOB_LIMIT_REACHED, …);
    `message` is for people."""

    def __init__(self, status, message, code=None, body=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code
        self.body = body or {}

    @property
    def unauthorized(self):
        return self.status == 401


def on_unauthorized(listener):
    """listener() runs on the main thread whenever the API says the token is no good."""
    _unauthorized_listeners.append(listener)


def read_host_version():
    global _host_version
    _host_version = bpy.app.version_string


def _check_online():
    # Blender's own switch (Preferences › System › Network). An extension must not go online without it.
    if not getattr(bpy.app, "online_access", True):
        raise ApiError(
            0,
            "Online access is turned off in Blender. Turn on Allow Online Access in "
            "Preferences › System › Network to use Zetta 3D.",
            "OFFLINE",
        )


def url_for(path):
    return path if path.lower().startswith("http") else config.api_base() + path


def _headers(auth, extra=None):
    headers = {
        "User-Agent": f"Zetta3D-Blender/{config.VERSION}",
        "Accept": "application/json",
        "X-Zetta-Client": config.CLIENT_NAME,
        "X-Zetta-Client-Version": config.VERSION,
    }
    if _host_version:
        headers["X-Zetta-Host-Version"] = _host_version
    token = session.token()
    if auth and token:
        headers["Authorization"] = "Bearer " + token
    if extra:
        headers.update(extra)
    return headers


def _failure(status, text):
    try:
        body = json.loads(text) if text else None
    except ValueError:
        body = None
    if not isinstance(body, dict):
        body = {}
    message = body.get("message")
    if isinstance(message, list):
        message = " ".join(str(m) for m in message)
    code = body.get("code")
    if not message:
        if status == 401:
            message = "Sign in to Zetta 3D to carry on."
        elif status == 403:
            message = "Your plan does not include this."
        elif status == 404:
            message = "Not found."
        elif status == 413:
            message = "That is too large to send."
        elif status == 429:
            message = "Too many requests — wait a moment and try again."
        elif status >= 500:
            message = "Zetta 3D had a problem. Try again in a moment."
        else:
            message = f"Request failed ({status})."
    if status == 401:
        for listener in _unauthorized_listeners:
            tasks.post(listener)
    return ApiError(status, str(message), code, body)


def request(method, path, body=None, headers=None, auth=True, timeout=60):
    """Sends a JSON request; returns the parsed answer (dict/list), a string, or None. Raises ApiError."""
    _check_online()
    data = None
    all_headers = _headers(auth, headers)
    # A bodyless POST with a JSON content type is a 400 on the API — only send it with a body.
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        all_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url_for(path), data=data, method=method, headers=all_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl) as response:
            text = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        raise _failure(error.code, error.read().decode("utf-8", "replace")) from None
    except TimeoutError:
        raise ApiError(0, "Zetta 3D did not answer in time. Check your connection and try again.", "TIMEOUT") from None
    except (urllib.error.URLError, OSError) as error:
        reason = getattr(error, "reason", error)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason):
            raise ApiError(0, "Zetta 3D did not answer in time. Check your connection and try again.", "TIMEOUT") from None
        raise ApiError(0, f"Could not reach Zetta 3D ({reason}).", "UNREACHABLE") from None
    if not text.strip():
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


def get(path, **kw):
    return request("GET", path, **kw)


def post(path, body=None, **kw):
    return request("POST", path, body, **kw)


def patch(path, body=None, **kw):
    return request("PATCH", path, body, **kw)


def put(path, body=None, **kw):
    return request("PUT", path, body, **kw)


def delete(path, **kw):
    return request("DELETE", path, **kw)


def query(params):
    """Builds a query string; list values repeat the key (the API rejects `key[]=`)."""
    pairs = []
    for key, value in params.items():
        if value is None or value == "":
            continue
        if isinstance(value, (list, tuple)):
            pairs.extend((key, str(v)) for v in value)
        elif isinstance(value, bool):
            pairs.append((key, "true" if value else "false"))
        else:
            pairs.append((key, str(value)))
    return urllib.parse.urlencode(pairs)


# ─── Raw transfers (uploads to the relay, downloads of signed files) ────────────────────────────────


def _raw(method, url, data=None, headers=None, timeout=120):
    """Returns (status, headers, bytes). Raises ApiError with UPLOAD_/DOWNLOAD_ codes."""
    _check_online()
    all_headers = {"User-Agent": f"Zetta3D-Blender/{config.VERSION}"}
    if headers:
        all_headers.update(headers)
    req = urllib.request.Request(url, data=data, method=method, headers=all_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        text = error.read().decode("utf-8", "replace")
        raise _transfer_failure(error.code, text, method) from None
    except (urllib.error.URLError, OSError) as error:
        reason = getattr(error, "reason", error)
        code = "UPLOAD_FAILED" if method in ("PUT", "POST") else "DOWNLOAD_FAILED"
        raise ApiError(0, f"The transfer could not reach Zetta 3D ({reason}).", code) from None


def _transfer_failure(status, text, method):
    message = None
    try:
        message = json.loads(text).get("message")
    except (ValueError, AttributeError):
        pass
    if method == "GET":
        if status == 410:
            return ApiError(status, "The download link expired. Try again.", "DOWNLOAD_EXPIRED")
        return ApiError(status, message or f"Download failed ({status}).", "DOWNLOAD_FAILED")
    code = {410: "UPLOAD_TICKET_EXPIRED", 413: "UPLOAD_TOO_LARGE", 415: "UPLOAD_WRONG_TYPE"}.get(status, "UPLOAD_FAILED")
    if status == 413 and not message:
        message = "That file is too large to upload (pictures up to 32 MB, models up to 512 MB)."
    return ApiError(status, message or f"Upload failed ({status}).", code)


def _with_retry(attempt, token=None):
    for i in range(len(RETRY_DELAYS) + 1):
        try:
            return attempt()
        except ApiError as error:
            if (error.status == 0 or error.status >= 500) and i < len(RETRY_DELAYS):
                if token is not None:
                    token.sleep(RETRY_DELAYS[i])
                else:
                    time.sleep(RETRY_DELAYS[i])
                continue
            raise
    return None


def put_bytes(url, data, headers=None, progress=None, token=None):
    """Uploads to the URL a ticket (POST /3d/uploads) returned — the API's upload relay. The ticket is the
    authorisation: no Bearer header is sent. Up to 8 MB is one PUT; larger files use the relay's multipart
    protocol (start → 8 MB parts → complete, aborted on failure). Network drops and 5xx are retried twice."""
    headers = dict(headers or {})
    content_type = headers.pop("Content-Type", None) or headers.pop("content-type", None) or "application/octet-stream"
    parsed = urllib.parse.urlsplit(url)
    ticket = urllib.parse.parse_qs(parsed.query).get("ticket", [None])[0]

    if len(data) <= PIECE_BYTES or not ticket:
        _with_retry(lambda: _raw("PUT", url, data, {"Content-Type": content_type, **headers}, timeout=300), token)
        if progress:
            progress(1.0)
        return

    base = url.split("?", 1)[0]
    t = urllib.parse.quote(ticket, safe="")
    status, _, started = _with_retry(lambda: _raw("POST", f"{base}/multipart?ticket={t}"), token)
    upload_id = urllib.parse.quote(str(json.loads(started.decode("utf-8")).get("uploadId", "")), safe="")
    try:
        parts = (len(data) + PIECE_BYTES - 1) // PIECE_BYTES
        for part in range(1, parts + 1):
            if token is not None:
                token.check()
            piece = data[(part - 1) * PIECE_BYTES : part * PIECE_BYTES]
            part_url = f"{base}/multipart/part?ticket={t}&uploadId={upload_id}&part={part}"
            _with_retry(lambda: _raw("PUT", part_url, piece, {"Content-Type": content_type}, timeout=300), token)
            if progress:
                progress(part / parts)
        complete = f"{base}/multipart/complete?ticket={t}&uploadId={upload_id}&parts={parts}&bytes={len(data)}"
        _with_retry(lambda: _raw("POST", complete), token)
    except BaseException:
        try:
            _raw("DELETE", f"{base}/multipart?ticket={t}&uploadId={upload_id}")
        except ApiError:
            pass  # Best effort: an unfinished upload expires with its ticket anyway.
        raise


def download(url, progress=None, token=None):
    """GETs a file in 8 MB ranges (signed URLs carry their own auth, so none is sent)."""

    def fetch(start, end):
        return _raw("GET", url, headers={"Range": f"bytes={start}-{end}"}, timeout=120)

    status, headers, first = _with_retry(lambda: fetch(0, PIECE_BYTES - 1), token)
    if status != 206:
        if progress:
            progress(1.0)
        return first
    total = _content_range_total(headers.get("Content-Range")) or len(first)
    if total <= len(first):
        if progress:
            progress(1.0)
        return first
    buffer = bytearray(first)
    if progress:
        progress(len(buffer) / total)
    while len(buffer) < total:
        if token is not None:
            token.check()
        start = len(buffer)
        end = min(start + PIECE_BYTES, total) - 1
        _, _, piece = _with_retry(lambda: fetch(start, end), token)
        if not piece:
            raise ApiError(0, "The download stopped early. Try again.", "DOWNLOAD_FAILED")
        buffer.extend(piece)
        if progress:
            progress(len(buffer) / total)
    return bytes(buffer)


def fetch_bytes(url, timeout=30):
    """A small file in one request (thumbnails, pictures)."""
    _, _, data = _raw("GET", url, timeout=timeout)
    return data


def _content_range_total(value):
    # "bytes 0-8388607/23456789"
    try:
        return int(str(value).rsplit("/", 1)[1])
    except (IndexError, ValueError):
        return 0
