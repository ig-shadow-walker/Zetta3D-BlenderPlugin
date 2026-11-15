"""The conversation with the Zetta Agent: one turn at a time, streamed, with Blender's tools run in between.

THE SHAPE OF A TURN (BlenderAgentProtocol.md › Turn lifecycle). A message goes up; the reply streams back —
text, server tools, and `client_tool` calls for Blender. When the stream ends with calls awaiting, Blender
runs them one by one (asking the user first where the rules say so), then sends every result back in one
request, and the reply streams on. A turn is many requests; the transcript reads as one.

WHO DECIDES WHAT IS ASKED. The server tags each call with its effect: `read` never asks; `write` asks only
when the server's review flagged it; `destructive` asks unless the user chose full autonomy; `generate`
always shows its credit card (the tool shows it). Before a message's first risky call the add-on may keep a
copy of the file (decision D6): ticked on the card when there is one, Preferences decides when there is not.

STOP ENDS IT. Stop cancels the stream, closes any card, tells the server, and nothing resumes — not the
tools, not queued events — until the user sends a new message.

EVERYTHING HERE RUNS ON THE MAIN THREAD; the network is in `client`, on worker threads.
"""

import json
import os
import tempfile
import time
import uuid

import bpy

from .. import auth, providers
from ..core import http, keychain, prefs, tasks
from . import attachments, changes, client, events, snapshot
from . import tools as toolbox

IDLE = "idle"
STREAMING = "streaming"
TOOLS = "tools"
WAITING = "waiting"  # a card waits for the user

PROJECT_KEY = "zetta_project_id"
COPY_TOOLS_NOTE = "Keep a copy of the file first"

conversation = {
    "session_id": None,
    "title": None,
    "items": [],
    "phase": IDLE,
    "route": None,
    "halted": False,
    "charged": 0,
}
options = {"tiers": [], "thinking": [], "providers": [], "loaded": False, "loading": False, "error": None}
history = {"sessions": [], "loading": False, "error": None}
memory = {"memories": [], "brief": None, "loading": False, "error": None}

_pending = []  # client_tool events waiting to run, in the server's order
_results = []  # toolResults entries for the calls already run
_awaiting = []
_token = None
_queue = []  # app events waiting for the conversation to be idle
_assistant = None  # the transcript row the streaming text goes into
card = None  # {"kind", "payload", "answer", "call", "item"} — the one card waiting on the user


# ─── Small things ────────────────────────────────────────────────────────────────────────────────


def _props():
    return bpy.context.window_manager.zetta_agent


def phase():
    return conversation["phase"]


def busy():
    return conversation["phase"] != IDLE


def items():
    return conversation["items"]


def _add(item):
    item.setdefault("id", uuid.uuid4().hex[:10])
    item.setdefault("at", time.time())
    conversation["items"].append(item)
    del conversation["items"][:-400]
    return item


def project_id():
    """The id the agent's memory and history hang on: written into the .blend the first time (decision D4),
    so it travels with the file, survives renames and is the same for everyone who opens it."""
    scene = bpy.data.scenes[0] if bpy.data.scenes else None
    if scene is None:
        return "unsaved-file-000"
    value = scene.get(PROJECT_KEY)
    if not isinstance(value, str) or len(value) < 8:
        value = uuid.uuid4().hex
        scene[PROJECT_KEY] = value
    return value


def project_name():
    path = bpy.data.filepath
    return os.path.splitext(os.path.basename(path))[0][:120] if path else "Untitled"


def own_model():
    """(provider, model) when the user runs the agent on their own key, else None."""
    if prefs.get("agent_account", providers.ZETTA) != providers.OWN:
        return None
    props = _props()
    provider = props.provider
    if not provider or provider == "zetta" or not keychain.has(provider):
        return None
    return provider, props.model


def _body():
    """The fields every request of a turn carries. Returns (body, own key or None)."""
    props = _props()
    body = {"projectId": project_id(), "projectName": project_name(), "mode": props.mode,
            "approval": props.approval, "thinking": props.thinking or "auto", "editor": snapshot.gather()}
    if conversation["session_id"]:
        body["sessionId"] = conversation["session_id"]
    own = own_model()
    key = None
    if own:
        body["provider"], body["model"] = own
        key = keychain.get(own[0])
    else:
        body["tier"] = props.tier or "auto"
    return body, key


# ─── Sending ─────────────────────────────────────────────────────────────────────────────────────


def send(text, images=(), documents=(), mentions=(), shown=None):
    """A message from the user. images/documents: [{"data": base64, "mediaType", "name"?}]; mentions: target
    strings the user tagged. Returns an error line, or None."""
    text = (text or "").strip()
    if not text and not images:
        return "Write a message first."
    if not auth.signed_in():
        return "Sign in to Zetta 3D to use the agent."
    if conversation["phase"] == STREAMING:
        return "The agent is still answering — wait, or press Stop."
    if conversation["phase"] in (TOOLS, WAITING):
        # Typing while Blender's tools wait: those calls are dropped here, and the server closes them.
        _abandon("Not run: the user sent a new message instead.")
    conversation["halted"] = False
    changes.begin_turn(text or "(pictures)")
    message = text + _mentions_block(mentions)
    _add({"role": "user", "text": shown if shown is not None else text,
          "attachments": [d.get("name") or "picture" for d in list(images) + list(documents)],
          "mentions": list(mentions)})
    body, key = _body()
    body["message"] = message[:16000]
    if images:
        body["images"] = [{"data": i["data"], "mediaType": i["mediaType"]} for i in images][:4]
    if documents:
        body["documents"] = [{"data": d["data"], "mediaType": "application/pdf", "name": d.get("name", "")}
                             for d in documents][:4]
    _stream(body, key)
    return None


def _mentions_block(mentions):
    """The <mentions> block the model reads: each tagged thing as an address (BlenderAgentProtocol.md)."""
    from . import targets

    lines = []
    for target in mentions:
        if target.startswith("//") or target.startswith("attached:"):
            lines.append(f'- file "{target}"')
            continue
        try:
            block = targets.datablock(target)
        except toolbox.ToolError:
            continue
        if isinstance(block, bpy.types.Object):
            home = block.users_collection[0].name if block.users_collection else "Scene"
            lines.append(f'- object "{block.name}" ({block.type}, collection "{home}") — target it as "{block.name}"')
        else:
            name = targets.name_of(block)
            lines.append(f'- {name.split(":", 1)[0].replace("_", " ")} "{block.name}" — target it as "{name}"')
    return "\n\n<mentions>\n" + "\n".join(lines) + "\n</mentions>" if lines else ""


def answer_question(item, label):
    """The user picked one of an `ask_options` question's buttons: their pick is the next message."""
    item["answered"] = label
    send(label)


def _stream(body, key):
    global _token, _assistant
    conversation["phase"] = STREAMING
    _assistant = None
    _token = tasks.Token()
    token = _token

    def work():
        client.chat(body, _on_event, key, token)

    tasks.run(work, lambda _: _stream_ended(token), lambda error: _stream_failed(token, error),
              name="zetta3d-agent-stream")


# ─── The stream ──────────────────────────────────────────────────────────────────────────────────


def _on_event(event):
    global _assistant, _awaiting
    kind = event.get("type")
    if kind == "session":
        conversation["session_id"] = event.get("sessionId")
        return
    if conversation["halted"] and kind not in ("done", "error"):
        return
    if kind == "route":
        conversation["route"] = event.get("route") or {}
    elif kind == "text":
        if _assistant is None or event.get("start"):
            _assistant = _add({"role": "assistant", "text": ""})
        _assistant["text"] += event.get("text") or ""
    elif kind == "tool":
        _assistant = None
        _add({"role": "step", "name": event.get("name"), "toolUseId": event.get("toolUseId"),
              "side": event.get("side"), "status": "running", "label": _step_label(event.get("name"))})
    elif kind == "tool_result":
        step = _step(event.get("toolUseId"))
        if step is not None:
            step["status"] = "error" if event.get("isError") else "done"
            step["detail"] = (event.get("content") or "")[:2000]
        if event.get("imageId"):
            _picture_from_server(event["imageId"], event.get("imageUrl"))
    elif kind == "client_tool":
        _pending.append(event)
    elif kind == "memory":
        memory_event = event.get("memory") or {}
        _add({"role": "memory", "text": memory_event.get("text", ""), "action": memory_event.get("action")})
    elif kind == "brief":
        memory["brief"] = event.get("brief")
    elif kind == "question":
        question = event.get("question") or {}
        _add({"role": "question", "text": question.get("question", ""), "options": question.get("options") or [],
              "answered": None})
    elif kind == "done":
        _awaiting = list(event.get("awaiting") or [])
        conversation["charged"] += int(event.get("charged") or 0)
        if event.get("stopped"):
            _pending.clear()
    elif kind == "error":
        _add({"role": "error", "text": event.get("message") or "The agent could not finish.",
              "code": event.get("code")})
        _pending.clear()


def _stream_ended(token):
    if token is not _token:
        return  # a stream that was stopped or replaced
    if conversation["halted"]:
        conversation["phase"] = IDLE
        return
    waiting = [call for call in _pending if call.get("toolUseId") in _awaiting]
    _pending[:] = waiting
    if waiting:
        conversation["phase"] = TOOLS
        _run_next()
    else:
        _pending.clear()
        conversation["phase"] = IDLE
        if auth.signed_in():
            auth.refresh()  # the balance moved
        _drain()


def _stream_failed(token, error):
    if token is not _token:
        return
    conversation["phase"] = IDLE
    _pending.clear()
    if conversation["halted"]:
        return
    message = getattr(error, "message", None) or str(error)
    code = getattr(error, "code", None)
    _add({"role": "error", "text": message, "code": code})


# ─── Blender's tools ─────────────────────────────────────────────────────────────────────────────


def _step(tool_use_id):
    for item in reversed(conversation["items"]):
        if item.get("role") == "step" and item.get("toolUseId") == tool_use_id:
            return item
    return None


def _step_label(name):
    return (name or "tool").replace("_", " ").capitalize()


def _keep_copies():
    return bool(prefs.get("agent_keep_copies", True))


def _needs_card(call):
    """destructive asks unless the user chose full autonomy; anything the server's review flagged asks,
    whatever the autonomy. generate's card is the tool's own; read and write run straight away."""
    if (call.get("review") or {}).get("confirm"):
        return True
    return call.get("effect") == "destructive" and _props().approval != "auto"


def _run_next():
    if conversation["halted"]:
        return
    if not _pending:
        _send_results()
        return
    call = _pending[0]
    step = _step(call.get("toolUseId"))
    if step is not None:
        step["status"] = "running"
        step["input"] = call.get("input")
    spec = toolbox.spec(call.get("name"))
    risky = bool(spec and spec.risky)
    turn = changes.current()
    first_risky = risky and turn is not None and not turn.get("copy")

    if _needs_card(call):
        ticked, why = changes.copy_offer() if first_risky else (False, None)
        _open_card("approve", {
            "title": _approval_title(call),
            "summary": _approval_summary(call),
            "reasons": (call.get("review") or {}).get("reasons") or [],
            "copy": first_risky, "copy_ticked": ticked and _keep_copies(), "copy_note": why,
            "button": "Delete" if call.get("name") in ("delete", "purge_unused") else "Allow",
        }, lambda answer: _approved(call, answer), call)
        return
    note = None
    if first_risky and _keep_copies() and changes.copy_offer()[0]:
        note = changes.keep_copy(_step_label(call.get("name")).lower())
    _execute(call, note)


def _approval_title(call):
    name = call.get("name")
    args = call.get("input") or {}
    if name == "run_python":
        return f"Run Python: {args.get('purpose') or 'a script'}"
    if name == "delete":
        return "Delete " + ", ".join(map(str, (args.get("targets") or [])[:6]))
    if name == "open_file":
        return {"open": f"Open {args.get('path', 'another file')}", "new": "Start a new file",
                "revert": "Revert to the saved file"}.get(args.get("action"), "Open a file")
    return _step_label(name)


def _approval_summary(call):
    args = call.get("input") or {}
    if call.get("name") == "run_python":
        return args.get("code") or ""
    try:
        return json.dumps(args, indent=1)[:1500]
    except (TypeError, ValueError):
        return str(args)[:1500]


def _approved(call, answer):
    answer = answer or {}
    if not answer.get("allow"):
        _finish_call(call, toolbox.Result("The user declined.", error=True), declined=True)
        return
    note = changes.keep_copy(_step_label(call.get("name")).lower()) if answer.get("copy") else None
    _execute(call, note)


def _execute(call, note):
    ctx = toolbox.Context(call, lambda result: _finish_call(call, result, note=note), card=_tool_card,
                          session={"id": conversation["session_id"], "project": project_id()})
    toolbox.run(call, ctx)


def _finish_call(call, result, note=None, declined=False):
    if conversation["halted"] or not _pending or _pending[0] is not call:
        return  # stopped, or superseded by a new message
    if note and not result.error:
        result.text = f"{result.text}\n{note}"
    _results.append(result.payload(call.get("toolUseId")))
    step = _step(call.get("toolUseId"))
    if step is not None:
        step["status"] = "declined" if declined else ("error" if result.error else "done")
        step["detail"] = result.text[:2000]
        if result.label:
            step["label"] = result.label
    for data, media in result.images[:4]:
        _picture_from_bytes(data, media)
    _pending.pop(0)
    tasks.post(_run_next)  # the next call after this one's results are drawn


def _send_results():
    global _results
    if not _results:
        conversation["phase"] = IDLE
        return
    body, key = _body()
    body["toolResults"] = _results
    _results = []
    _stream(body, key)


def _abandon(reason):
    """Drops the calls waiting here (the server closes them when the next request arrives)."""
    global card, _results
    for call in _pending:
        step = _step(call.get("toolUseId"))
        if step is not None and step["status"] == "running":
            step["status"] = "stopped"
            step["detail"] = reason
    _pending.clear()
    _results = []
    if card is not None:
        card["item"]["status"] = "superseded"
        card = None
    conversation["phase"] = IDLE


# ─── Cards ───────────────────────────────────────────────────────────────────────────────────────


def _open_card(kind, payload, answer, call=None):
    global card
    item = _add({"role": "card", "kind": kind, "payload": payload, "status": "open"})
    card = {"kind": kind, "payload": payload, "answer": answer, "call": call, "item": item}
    conversation["phase"] = WAITING


def _tool_card(kind, payload, on_answer):
    """A card a tool asks for (credits for a generation, ask_user). Full autonomy approves credit cards — except
    a 3D model generation (payload "always_ask"), which the user always answers: paying for a model from their
    picture or description is their call, whatever the autonomy."""
    if kind == "generate" and _props().approval == "auto" and not payload.get("always_ask"):
        credits = payload.get("credits")
        _add({"role": "note", "text": f"Approved automatically · {payload.get('title', '')}"
                                      + (f" · {credits} credits" if credits else "")})
        on_answer(True)
        return
    _open_card(kind, payload, on_answer)


def answer_card(answer):
    """The user acted on the open card: answer is what its kind expects (see the panel)."""
    global card
    if card is None:
        return
    current, card = card, None
    current["item"]["status"] = "answered"
    current["item"]["answer"] = answer
    conversation["phase"] = TOOLS
    current["answer"](answer)


@toolbox.tool("ask_user")
def _ask_user(args, ctx):
    questions = args.get("questions")
    if not isinstance(questions, list) or not questions:
        raise toolbox.ToolError("ask_user needs 1–4 questions.")
    for index, question in enumerate(questions[:4]):
        question.setdefault("id", f"q{index + 1}")

    def answered(answers):
        if not answers or answers.get("skipped"):
            ctx.finish("The user closed the card without answering — carry on with your best judgment and "
                       "say what you assumed.")
            return
        lines = [f'The user answered (card "{args.get("title") or "Question"}"):']
        for question in questions[:4]:
            value = answers.get(question["id"])
            text = question.get("question", "")[:120]
            if value in (None, "", []):
                lines.append(f'- {question["id"]} — "{text}": left empty')
            elif question.get("kind") == "object":
                picked = value if isinstance(value, list) else [value]
                lines.append(f'- {question["id"]} — "{text}": ' + "; ".join(
                    f'{p} — target it as "{p}"' for p in picked))
            elif question.get("kind") == "choice":
                picked = value if isinstance(value, list) else [value]
                lines.append(f'- {question["id"]} — "{text}": chose ' + ", ".join(f'"{p}"' for p in picked))
            else:
                lines.append(f'- {question["id"]} — "{text}": wrote: "{value}"')
        ctx.finish("\n".join(lines))

    ctx.card("ask_user", {"title": args.get("title") or "A question", "questions": questions[:4]}, answered)
    return toolbox.LATER


# ─── Stop, events, files ─────────────────────────────────────────────────────────────────────────


def stop():
    """The Stop button: everything ends here, now, and nothing resumes until a new message."""
    global card
    conversation["halted"] = True
    if _token is not None:
        _token.cancel()
    if card is not None:
        card["item"]["status"] = "stopped"
        card = None
    for item in conversation["items"]:
        if item.get("role") == "step" and item.get("status") == "running":
            item["status"] = "stopped"
    _pending.clear()
    _results.clear()
    _queue.clear()
    conversation["phase"] = IDLE
    session_id = conversation["session_id"]
    if session_id:
        tasks.run(lambda: client.stop(session_id), name="zetta3d-agent-stop")
    _add({"role": "note", "text": "Stopped."})


def _on_app_event(kind, payload):
    _queue.append({"kind": kind, **payload})
    _drain()


def _drain():
    """Sends one queued app event once the conversation is idle (and not stopped)."""
    if conversation["phase"] != IDLE or conversation["halted"] or not _queue or not auth.signed_in():
        return
    if not conversation["session_id"]:
        _queue.clear()
        return
    event = _queue.pop(0)
    label = ("A generation finished" if event["kind"] == "job_finished" else "A pipeline run finished")
    _add({"role": "event", "text": label})
    body, key = _body()
    body["event"] = event
    _stream(body, key)


def new_chat():
    """+ New chat: a fresh conversation in this file."""
    if busy():
        stop()
    conversation.update(session_id=None, title=None, items=[], route=None, halted=False)


def on_file_loaded():
    """Another file opened, or a new one: the conversation belonged to the old file."""
    if busy():
        stop()
    new_chat()
    history["sessions"] = []
    memory.update(memories=[], brief=None)


# ─── History, memory, options ────────────────────────────────────────────────────────────────────


def load_history():
    if history["loading"] or not auth.signed_in():
        return
    history.update(loading=True, error=None)
    pid = project_id()

    def done(sessions):
        history.update(sessions=sessions, loading=False)

    def fail(error):
        history.update(loading=False, error=getattr(error, "message", str(error)))

    tasks.run(lambda: client.sessions(pid), done, fail, name="zetta3d-agent-history")


def open_session(session_id):
    if busy():
        stop()

    def done(answer):
        session = answer.get("session") or {}
        conversation.update(session_id=session.get("id"), title=session.get("title"), items=[], halted=False)
        for row in answer.get("transcript") or []:
            role = row.get("role")
            if role == "tool":
                _add({"role": "step", "name": row.get("name"), "toolUseId": row.get("toolUseId"),
                      "status": "error" if row.get("isError") else "done", "label": _step_label(row.get("name")),
                      "detail": row.get("summary", "")})
            elif role in ("user", "assistant", "event"):
                _add({"role": role, "text": row.get("text", "")})
        awaiting = answer.get("awaiting") or []
        if awaiting:
            # The server still waits on Blender for these: run them now, as the turn would have.
            changes.begin_turn(conversation["title"] or "Resumed")
            global _awaiting
            _awaiting = [call.get("toolUseId") for call in awaiting]
            _pending[:] = [dict(call, type="client_tool") for call in awaiting]
            for call in awaiting:
                _add({"role": "step", "name": call.get("name"), "toolUseId": call.get("toolUseId"),
                      "status": "running", "label": _step_label(call.get("name"))})
            conversation["phase"] = TOOLS
            _run_next()

    def fail(error):
        _add({"role": "error", "text": getattr(error, "message", str(error))})

    tasks.run(lambda: client.session(session_id), done, fail, name="zetta3d-agent-open")


def delete_session(session_id):
    def done(_):
        history["sessions"] = [s for s in history["sessions"] if s.get("id") != session_id]
        if conversation["session_id"] == session_id:
            new_chat()

    tasks.run(lambda: client.delete_session(session_id), done, name="zetta3d-agent-delete-session")


def load_memory():
    if memory["loading"] or not auth.signed_in():
        return
    memory.update(loading=True, error=None)
    pid = project_id()

    def done(answer):
        memory.update(memories=answer.get("memories") or [], brief=answer.get("brief"), loading=False)

    def fail(error):
        memory.update(loading=False, error=getattr(error, "message", str(error)))

    tasks.run(lambda: client.memories(pid), done, fail, name="zetta3d-agent-memory")


def forget_memory(memory_id):
    def done(_):
        memory["memories"] = [m for m in memory["memories"] if m.get("id") != memory_id]

    tasks.run(lambda: client.delete_memory(memory_id), done, name="zetta3d-agent-forget")


def remember(text, scope):
    pid = project_id()

    def done(answer):
        if isinstance(answer, dict) and answer.get("memory"):
            memory["memories"].append(answer["memory"])

    def fail(error):
        memory["error"] = getattr(error, "message", str(error))

    tasks.run(lambda: client.add_memory(text, scope, "preference" if scope == "global" else "project_fact", pid),
              done, fail, name="zetta3d-agent-remember")


def save_brief(text):
    pid = project_id()

    def done(answer):
        memory["brief"] = (answer or {}).get("brief", text)

    tasks.run(lambda: client.put_brief(pid, text), done, name="zetta3d-agent-brief")


def load_options():
    """The tiers, thinking levels and own-key providers the menus offer (the server's list is the truth)."""
    if options["loading"] or not auth.signed_in():
        return
    options.update(loading=True, error=None)

    def done(answer):
        answer = answer or {}
        options.update(tiers=answer.get("tiers") or [], thinking=answer.get("thinking") or [],
                       providers=answer.get("providers") or [], loaded=True, loading=False)

    def fail(error):
        status = getattr(error, "status", 0)
        options.update(loading=False, loaded=True,
                       error="The Zetta Agent for Blender is not available on your account yet." if status == 404
                       else getattr(error, "message", str(error)))

    tasks.run(client.options, done, fail, name="zetta3d-agent-options")


def available():
    """False when the server said the agent is not open (BLENDER_AGENT_ENABLED off): the panel says so."""
    return not (options["loaded"] and options["error"] and not options["tiers"])


# ─── Pictures in the chat ────────────────────────────────────────────────────────────────────────


def _picture_from_bytes(data, media):
    folder = os.path.join(tempfile.gettempdir(), "zetta3d-agent-pictures")
    os.makedirs(folder, exist_ok=True)
    ext = ".png" if "png" in media else ".jpg"
    path = os.path.join(folder, uuid.uuid4().hex[:12] + ext)
    with open(path, "wb") as handle:
        handle.write(data)
    _add({"role": "picture", "path": path})


def _picture_from_server(image_id, url):
    def work():
        link = url or (client.image(image_id) or {}).get("url")
        return http.fetch_bytes(link, timeout=60) if link else None

    def done(data):
        if data:
            _picture_from_bytes(data, "image/png")
            conversation["items"][-1]["image_id"] = image_id

    tasks.run(work, done, name="zetta3d-agent-picture")


# ─── Attachments ─────────────────────────────────────────────────────────────────────────────────


def attachment_from_file(path):
    """Sorts a file the user attached: (kind, entry) — "image" / "document" for the message, "text" to inline,
    "file" copied next to the .blend and mentioned (BlenderAgentProtocol.md › Mentions)."""
    import base64

    name = os.path.basename(path)
    ext = os.path.splitext(name)[1].lower()
    with open(path, "rb") as handle:
        data = handle.read()
    if ext in (".png", ".jpg", ".jpeg", ".webp"):
        if len(data) > 3_000_000:
            raise ValueError(f"{name} is over 3 MB — attach a smaller picture.")
        stored = attachments.store(name, data)
        media = "image/png" if ext == ".png" else "image/webp" if ext == ".webp" else "image/jpeg"
        return "image", {"data": base64.b64encode(data).decode("ascii"), "mediaType": media,
                         "name": f"attached:{stored}"}
    if ext == ".pdf":
        if len(data) > 6_000_000:
            raise ValueError(f"{name} is over 6 MB.")
        return "document", {"data": base64.b64encode(data).decode("ascii"), "name": name}
    if len(data) <= 200_000:
        try:
            return "text", {"name": name, "text": data.decode("utf-8")}
        except UnicodeDecodeError:
            pass
    folder = bpy.path.abspath("//zetta3d/attachments") if bpy.data.filepath else None
    if folder is None:
        raise ValueError("Save the file first: other attachments are copied next to it.")
    os.makedirs(folder, exist_ok=True)
    target = os.path.join(folder, name)
    with open(target, "wb") as handle:
        handle.write(data)
    return "file", {"name": name, "target": bpy.path.relpath(target)}


# ─── Registration ────────────────────────────────────────────────────────────────────────────────


@bpy.app.handlers.persistent
def _load_post(_dummy):
    on_file_loaded()


def register():
    events.listen(_on_app_event)
    if _load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_load_post)
    attachments.prune()


def unregister():
    events.forget(_on_app_event)
    if _load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_load_post)
    if _token is not None:
        _token.cancel()
