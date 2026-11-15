"""The Zetta Agent's chat: the 3D View sidebar's Zetta Agent tab (BlenderAgent.md §2.1, decision D1).

DRAWN WITH BLENDER'S OWN UI. Labels, boxes and buttons — no custom drawing — so it looks and behaves like
Blender at every scale and theme. What that costs is planned for: text is wrapped by measuring it (panels.wrap)
and every message has Copy (labels can't be selected); a long message is written in a Text Editor window
(Write more…); pictures are preview icons.

The conversation itself is `agent.runtime`; this module draws it and turns clicks into its calls.
"""

import base64
import os

import bpy
from bpy.props import BoolProperty, EnumProperty, StringProperty

from .. import auth, providers
from ..agent import changes, runtime
from ..agent import tools as toolbox
from ..core import keychain, prefs, tasks
from ..studio import pictures
from .ops import action
from .panels import btn, wrap

CATEGORY = "Zetta Agent"
SHOWN = 30  # transcript rows drawn; Show earlier adds more
MESSAGE_TEXT = "Zetta Agent message"

ui = {"shown": SHOWN, "open": set(), "mentions": [], "images": [], "documents": [], "texts": [], "files": [],
      "notice": None, "answers": {}, "copy": None}

# ─── Properties ──────────────────────────────────────────────────────────────────────────────────

_kept = {}  # enum items must stay referenced while Blender shows them


def _items(key, items):
    _kept[key] = items
    return items


def _tier_items(_self, _context):
    found = [(t["key"], t.get("label") or t["key"], t.get("blurb") or "") for t in runtime.options["tiers"]
             if t.get("available", True)]
    return _items("tier", found or [("auto", "Auto", "Picks the model and the thinking for each message.")])


def _thinking_items(_self, _context):
    own = runtime.own_model()
    levels = []
    if own:
        for provider in runtime.options["providers"]:
            if provider.get("key") == own[0]:
                for model in provider.get("models") or []:
                    if model.get("key") == own[1]:
                        levels = [(k, k.capitalize(), "") for k in model.get("thinking") or []]
        levels = [("auto", "Auto", "Lets the agent decide how hard to think.")] + levels
    else:
        levels = [(t["key"], t.get("label") or t["key"], "") for t in runtime.options["thinking"]
                  if t.get("available", True)]
    return _items("thinking", levels or [("auto", "Auto", "")])


def _provider_items(_self, _context):
    found = [("zetta", "Zetta Account", "Run the agent on your Zetta 3D account")]
    for provider in runtime.options["providers"] or providers.providers():
        if keychain.has(provider.get("key", "")):
            found.append((provider["key"], provider.get("label") or provider["key"], "Your own API key"))
    return _items("provider", found)


def _model_items(self, _context):
    found = []
    for provider in runtime.options["providers"] or providers.providers():
        if provider.get("key") == self.provider:
            found = [(m["key"], m.get("label") or m["key"], m.get("blurb") or "") for m in provider.get("models") or []]
    return _items("model", found or [("none", "No models", "")])


class ZETTA_PG_agent(bpy.types.PropertyGroup):
    input: StringProperty(name="Message", description="What you want the agent to do — or ask it something",
                          maxlen=16000)
    mode: EnumProperty(name="Mode", items=[
        ("agent", "Agent", "Works in your file: models, shades, lights, animates, scripts"),
        ("ask", "Ask", "Explains and shows you where things are — changes nothing"),
    ], default="agent")
    approval: EnumProperty(name="Autonomy", items=[
        ("confirm_destructive", "Ask first", "Asks before deleting, running Python, opening files or spending credits"),
        ("auto", "Full autonomy", "Never stops to ask: every call runs, credit cards included"),
    ], default="confirm_destructive")
    tier: EnumProperty(name="Model", items=_tier_items)
    thinking: EnumProperty(name="Thinking", items=_thinking_items)
    provider: EnumProperty(name="Runs on", items=_provider_items)
    model: EnumProperty(name="Model", items=_model_items)
    view: EnumProperty(name="View", items=[("chat", "Chat", ""), ("history", "History", ""),
                                           ("memory", "Memory", "")], default="chat")


def state(context=None):
    return (context or bpy.context).window_manager.zetta_agent


# ─── Actions (zetta.do) ──────────────────────────────────────────────────────────────────────────


@action("agent_send")
def _send(context, *_):
    props = state(context)
    text = props.input
    for block in ui["texts"]:
        text += f'\n\n<attached_file name="{block["name"]}">\n{block["text"]}\n</attached_file>'
    for file in ui["files"]:
        ui["mentions"].append(file["target"])
    pictures_named = [i["name"] for i in ui["images"]]
    if pictures_named:
        text += "\n\n" + " ".join(f"[{name}]" for name in pictures_named)
    problem = runtime.send(text, ui["images"], ui["documents"], list(ui["mentions"]), shown=props.input)
    if problem:
        ui["notice"] = problem
        return
    props.input = ""
    for key in ("mentions", "images", "documents", "texts", "files"):
        ui[key] = []
    ui["notice"] = None
    ui["shown"] = SHOWN


@action("agent_stop")
def _stop(*_):
    runtime.stop()


@action("agent_new")
def _new(context, *_):
    runtime.new_chat()
    state(context).view = "chat"


@action("agent_view")
def _view(context, view, *_):
    state(context).view = view
    if view == "history":
        runtime.load_history()
    elif view == "memory":
        runtime.load_memory()


@action("agent_open_session")
def _open_session(context, session_id, *_):
    state(context).view = "chat"
    runtime.open_session(int(session_id))


@action("agent_delete_session")
def _delete_session(_context, session_id, *_):
    runtime.delete_session(int(session_id))


@action("agent_forget")
def _forget(_context, memory_id, *_):
    runtime.forget_memory(int(memory_id))


@action("agent_more")
def _more(*_):
    ui["shown"] += SHOWN


@action("agent_toggle")
def _toggle(_context, item_id, *_):
    ui["open"].symmetric_difference_update({item_id})


@action("agent_copy")
def _copy(context, item_id, *_):
    for item in runtime.items():
        if item.get("id") == item_id:
            context.window_manager.clipboard = item.get("text") or item.get("detail") or ""
            ui["notice"] = "Copied."


@action("agent_unmention")
def _unmention(_context, kind, index):
    try:
        ui[kind].pop(int(index))
    except (KeyError, IndexError, ValueError):
        pass


@action("agent_answer")
def _answer(_context, item_id, label):
    for item in runtime.items():
        if item.get("id") == item_id and item.get("role") == "question" and not item.get("answered"):
            runtime.answer_question(item, label)


@action("agent_card")
def _card(_context, verdict, *_):
    card = runtime.card
    if card is None:
        return
    if card["kind"] == "approve":
        runtime.answer_card({"allow": verdict == "allow", "copy": bool(ui.get("copy"))})
    elif card["kind"] == "generate":
        runtime.answer_card(verdict == "allow")
    elif card["kind"] == "ask_user":
        runtime.answer_card({"skipped": True} if verdict == "skip" else dict(ui["answers"]))
    ui["answers"], ui["copy"] = {}, None


@action("agent_card_copy")
def _card_copy(*_):
    ui["copy"] = not ui.get("copy")


@action("agent_choose")
def _choose(_context, question_id, label):
    card = runtime.card
    question = next((q for q in (card or {}).get("payload", {}).get("questions", []) if q["id"] == question_id), None)
    if question is None:
        return
    if question.get("multiple"):
        picked = set(ui["answers"].get(question_id) or [])
        picked.symmetric_difference_update({label})
        ui["answers"][question_id] = sorted(picked)
    else:
        ui["answers"][question_id] = label


@action("agent_use_selection")
def _use_selection(context, question_id, *_):
    picked = [obj.name for obj in context.selected_objects]
    if picked:
        ui["answers"][question_id] = picked


@action("agent_restore_copy")
def _restore_copy(_context, turn_id, *_):
    for turn in changes.turns:
        if turn["id"] == turn_id:
            problem = changes.restore_copy(turn)
            ui["notice"] = problem or "The file is back as it was before that message."


@action("agent_undo_turn")
def _undo_turn(_context, turn_id, *_):
    for turn in changes.turns:
        if turn["id"] == turn_id:
            problems = changes.undo_turn(turn)
            ui["notice"] = " ".join(problems) if problems else "Undone."


@action("agent_keep_turn")
def _keep_turn(_context, turn_id, *_):
    for turn in changes.turns:
        if turn["id"] == turn_id:
            changes.keep(turn)


@action("agent_settings")
def _settings(*_):
    bpy.ops.screen.userpref_show()
    bpy.context.preferences.active_section = "ADDONS"


@action("agent_picture")
def _picture(_context, path, *_):
    if path and os.path.isfile(path):
        image = bpy.data.images.load(path, check_existing=True)
        for area in bpy.context.screen.areas:
            if area.type == "IMAGE_EDITOR":
                area.spaces.active.image = image
                return
        ui["notice"] = f"Loaded as the image {image.name} — open it in an Image Editor."


@action("agent_attach_view")
def _attach_view(*_):
    """+ › Picture of the viewport: the agent's own capture tool, kept as an attachment."""
    def done(result):
        if result.error or not result.images:
            ui["notice"] = result.text if result.error else "No picture came back."
            return
        from ..agent import attachments

        data, media = result.images[0]
        name = attachments.store("viewport.jpg", data)
        ui["images"].append({"data": base64.b64encode(data).decode("ascii"), "mediaType": media,
                             "name": f"attached:{name}"})
        tasks.redraw()

    call = {"toolUseId": "composer", "name": "capture_view", "input": {"view": "viewport", "angles": "current"},
            "effect": "read"}
    toolbox.run(call, toolbox.Context(call, done))


# ─── Operators ───────────────────────────────────────────────────────────────────────────────────


def _mention_items(_self, _context):
    found = [(obj.name, obj.name, obj.type.capitalize(), "OBJECT_DATA", i) for i, obj in enumerate(bpy.data.objects)]
    n = len(found)
    for kind, data, icon in (("material", bpy.data.materials, "MATERIAL"),
                             ("collection", bpy.data.collections, "OUTLINER_COLLECTION"),
                             ("node_group", bpy.data.node_groups, "NODETREE"),
                             ("image", bpy.data.images, "IMAGE_DATA"),
                             ("text", bpy.data.texts, "TEXT"),
                             ("action", bpy.data.actions, "ACTION"),
                             ("world", bpy.data.worlds, "WORLD")):
        for block in data:
            found.append((f"{kind}:{block.name}", f"{block.name}", kind.replace("_", " "), icon, n))
            n += 1
    return _items("mention", found or [("none", "Nothing to tag", "", "NONE", 0)])


class ZETTA_OT_agent_mention(bpy.types.Operator):
    """Tag an object or data-block for the agent (@)"""

    bl_idname = "zetta.agent_mention"
    bl_label = "Tag for the Zetta Agent"
    bl_property = "target"
    bl_options = {"INTERNAL"}

    target: EnumProperty(items=_mention_items)
    question: StringProperty(options={"SKIP_SAVE"})

    def invoke(self, context, _event):
        context.window_manager.invoke_search_popup(self)
        return {"RUNNING_MODAL"}

    def execute(self, _context):
        if self.target and self.target != "none":
            if self.question:
                ui["answers"][self.question] = self.target
            elif self.target not in ui["mentions"]:
                ui["mentions"].append(self.target)
        tasks.redraw()
        return {"FINISHED"}


class ZETTA_OT_agent_add_selection(bpy.types.Operator):
    """Tag what is selected for the Zetta Agent"""

    bl_idname = "zetta.agent_add_selection"
    bl_label = "Add to Zetta Agent"

    def execute(self, context):
        added = 0
        for obj in context.selected_objects:
            if obj.name not in ui["mentions"]:
                ui["mentions"].append(obj.name)
                added += 1
        material = getattr(context, "material", None)
        if material is not None and f"material:{material.name}" not in ui["mentions"]:
            ui["mentions"].append(f"material:{material.name}")
            added += 1
        self.report({"INFO"}, f"Tagged {added} for the Zetta Agent" if added else "Nothing new to tag")
        tasks.redraw()
        return {"FINISHED"}


class ZETTA_OT_agent_attach(bpy.types.Operator):
    """Attach a file: pictures and PDFs go to the agent, text is read in, anything else is copied next to the
    .blend and tagged"""

    bl_idname = "zetta.agent_attach"
    bl_label = "Attach a file"
    bl_options = {"INTERNAL"}

    filepath: StringProperty(subtype="FILE_PATH", options={"SKIP_SAVE"})

    def invoke(self, context, _event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, _context):
        try:
            kind, entry = runtime.attachment_from_file(self.filepath)
        except (OSError, ValueError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        ui[{"image": "images", "document": "documents", "text": "texts", "file": "files"}[kind]].append(entry)
        tasks.redraw()
        return {"FINISHED"}


class ZETTA_OT_agent_card_text(bpy.types.Operator):
    """Answer in your own words"""

    bl_idname = "zetta.agent_card_text"
    bl_label = "Your answer"
    bl_options = {"INTERNAL"}

    question: StringProperty(options={"SKIP_SAVE"})
    text: StringProperty(name="", options={"SKIP_SAVE"})

    def invoke(self, context, _event):
        self.text = str(ui["answers"].get(self.question) or "")
        return context.window_manager.invoke_props_dialog(self, width=420)

    def draw(self, _context):
        self.layout.prop(self, "text", text="")

    def execute(self, _context):
        ui["answers"][self.question] = self.text.strip()
        tasks.redraw()
        return {"FINISHED"}


class ZETTA_OT_agent_write_more(bpy.types.Operator):
    """Write a long message in a Text Editor window, then press Send to Zetta Agent in its header"""

    bl_idname = "zetta.agent_write_more"
    bl_label = "Write more…"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        text = bpy.data.texts.get(MESSAGE_TEXT) or bpy.data.texts.new(MESSAGE_TEXT)
        if not text.as_string().strip() and state(context).input:
            text.from_string(state(context).input)
        bpy.ops.wm.window_new()
        window = context.window_manager.windows[-1]
        area = window.screen.areas[0]
        area.type = "TEXT_EDITOR"
        area.spaces.active.text = text
        return {"FINISHED"}


class ZETTA_OT_agent_send_text(bpy.types.Operator):
    """Send this text to the Zetta Agent"""

    bl_idname = "zetta.agent_send_text"
    bl_label = "Send to Zetta Agent"

    def execute(self, context):
        text = getattr(context.space_data, "text", None)
        if text is None or not text.as_string().strip():
            return {"CANCELLED"}
        state(context).input = text.as_string()
        _send(context)
        if ui["notice"]:
            self.report({"WARNING"}, ui["notice"])
            return {"CANCELLED"}
        text.clear()
        return {"FINISHED"}


def _text_header(self, context):
    text = getattr(context.space_data, "text", None)
    if text is not None and text.name == MESSAGE_TEXT:
        self.layout.operator("zetta.agent_send_text", icon="PLAY")


def _context_menu(self, _context):
    self.layout.operator("zetta.agent_add_selection", icon_value=pictures.mark())


# ─── Drawing ─────────────────────────────────────────────────────────────────────────────────────

STATUS_ICON = {"running": "TIME", "done": "CHECKMARK", "error": "ERROR", "declined": "CANCEL",
               "stopped": "PAUSE"}
KEY_CODES = {"provider_key_invalid", "provider_quota", "provider_model_unavailable", "provider_key_missing"}


def _markdown(layout, text):
    """Headings, bullets and code blocks drawn as their own rows; the rest wrapped."""
    col = layout.column(align=True)
    code = None
    for raw in (text or "").split("\n"):
        line = raw.rstrip()
        if line.startswith("```"):
            if code is None:
                code = col.box().column(align=True)
            else:
                code = None
            continue
        if code is not None:
            code.label(text=line or " ")
            continue
        if not line.strip():
            col.separator(factor=0.4)
        elif line.startswith("#"):
            col.label(text=line.lstrip("# ").replace("**", ""), icon="DOT")
        elif line.lstrip().startswith(("- ", "* ")):
            wrap(col, "• " + line.lstrip()[2:].replace("**", "").replace("`", ""))
        else:
            wrap(col, line.replace("**", "").replace("`", ""))


def _draw_item(layout, item):
    role = item.get("role")
    if role == "user":
        box = layout.box()
        row = box.row()
        row.label(text="You", icon="USER")
        btn(row, "agent_copy", "", "COPYDOWN", arg=item["id"], hint="Copy", emboss=False)
        wrap(box, item.get("text") or "")
        tags = list(item.get("mentions") or []) + list(item.get("attachments") or [])
        if tags:
            box.label(text="· " + ", ".join(tags[:6]), icon="LINKED")
    elif role == "assistant":
        col = layout.column()
        row = col.row()
        row.label(text="Zetta Agent", icon_value=pictures.mark())
        btn(row, "agent_copy", "", "COPYDOWN", arg=item["id"], hint="Copy", emboss=False)
        _markdown(col, item.get("text"))
    elif role == "step":
        row = layout.row(align=True)
        opened = item["id"] in ui["open"]
        btn(row, "agent_toggle", "", "DOWNARROW_HLT" if opened else "RIGHTARROW", arg=item["id"], emboss=False)
        row.label(text=item.get("label") or item.get("name") or "Step",
                  icon=STATUS_ICON.get(item.get("status"), "DOT"))
        if opened:
            box = layout.box()
            if item.get("input"):
                wrap(box, str(item["input"])[:600])
            if item.get("detail"):
                for line in item["detail"].split("\n")[:24]:
                    wrap(box, line)
                btn(box, "agent_copy", "Copy", "COPYDOWN", arg=item["id"])
    elif role == "card":
        _draw_card(layout, item)
    elif role == "question":
        box = layout.box()
        wrap(box, item.get("text") or "", "QUESTION")
        if item.get("answered"):
            box.label(text=f"You chose: {item['answered']}", icon="CHECKMARK")
        else:
            for option in item.get("options") or []:
                btn(box, "agent_answer", option.get("label", "?"), arg=item["id"], arg2=option.get("label", ""),
                    hint=option.get("blurb") or "")
    elif role == "picture":
        key = f"agent-{item['id']}"
        icon = pictures.icon_for_file(key, item["path"]) if os.path.isfile(item.get("path", "")) else 0
        if icon:
            layout.template_icon(icon_value=icon, scale=7)
        btn(layout, "agent_picture", "Open in Image Editor", "IMAGE_DATA", arg=item.get("path", ""))
    elif role == "error":
        box = layout.box()
        wrap(box, item.get("text") or "", "ERROR", alert=True)
        if item.get("code") in KEY_CODES:
            btn(box, "agent_settings", "Open settings", "PREFERENCES")
    elif role == "memory":
        layout.label(text=("Remembered: " if item.get("action") == "saved" else "Forgot: ") + item.get("text", "")[:80],
                     icon="BOOKMARKS")
    else:  # note, event
        wrap(layout, item.get("text") or "", "INFO")


def _draw_card(layout, item):
    payload = item.get("payload") or {}
    box = layout.box()
    is_open = runtime.card is not None and runtime.card.get("item") is item
    kind = item.get("kind")
    if not is_open:
        box.label(text=f"{payload.get('title', 'Card')} — {item.get('status', '')}", icon="CHECKBOX_HLT")
        return
    box.alert = kind == "approve" and payload.get("button") == "Delete"
    wrap(box, payload.get("title") or "Needs your OK", "QUESTION")
    if kind == "approve":
        for reason in payload.get("reasons") or []:
            wrap(box, reason, "INFO")
        summary = (payload.get("summary") or "").split("\n")
        code = box.box().column(align=True)
        for line in summary[:30]:
            code.label(text=line[:160] or " ")
        if len(summary) > 30:
            code.label(text=f"… {len(summary) - 30} more lines")
        if payload.get("copy"):
            if ui.get("copy") is None:
                ui["copy"] = bool(payload.get("copy_ticked"))
            btn(box, "agent_card_copy", "Keep a copy of the file first",
                "CHECKBOX_HLT" if ui["copy"] else "CHECKBOX_DEHLT", emboss=False)
            if payload.get("copy_note"):
                wrap(box, payload["copy_note"])
        row = box.row(align=True)
        btn(row, "agent_card", payload.get("button") or "Allow", "CHECKMARK", arg="allow")
        btn(row, "agent_card", "Decline", "CANCEL", arg="decline")
    elif kind == "generate":
        for line in payload.get("lines") or []:
            wrap(box, line)
        if payload.get("credits") is not None:
            box.label(text=f"{payload['credits']:,} credits · you have {auth.credits():,}", icon="FUND")
        row = box.row(align=True)
        row.scale_y = 1.2
        btn(row, "agent_card", payload.get("button") or "Generate", "PLAY", arg="allow")
        btn(row, "agent_card", "Not now", "CANCEL", arg="decline")
    elif kind == "ask_user":
        for question in payload.get("questions") or []:
            qid = question["id"]
            col = box.column(align=True)
            wrap(col, question.get("question") or "", "QUESTION")
            answer = ui["answers"].get(qid)
            if question.get("kind") == "choice":
                for option in question.get("options") or []:
                    label = option.get("label", "?")
                    chosen = label in answer if isinstance(answer, list) else answer == label
                    btn(col, "agent_choose", label, "RADIOBUT_ON" if chosen else "RADIOBUT_OFF", arg=qid, arg2=label,
                        hint=option.get("description") or "")
                if question.get("allowOther", True):
                    op = col.operator("zetta.agent_card_text", text="Something else…", icon="GREASEPENCIL")
                    op.question = qid
            elif question.get("kind") == "object":
                row = col.row(align=True)
                op = row.operator("zetta.agent_mention", text=str(answer) if answer else "Pick…", icon="EYEDROPPER")
                op.question = qid
                btn(row, "agent_use_selection", "Use selection", "RESTRICT_SELECT_OFF", arg=qid)
                if question.get("allowOther", True):
                    op = col.operator("zetta.agent_card_text", text="Describe it instead", icon="GREASEPENCIL")
                    op.question = qid
            else:
                op = col.operator("zetta.agent_card_text", text=str(answer) if answer else
                                  (question.get("placeholder") or "Write your answer…"), icon="GREASEPENCIL")
                op.question = qid
        row = box.row(align=True)
        ready = all(ui["answers"].get(q["id"]) not in (None, "", []) or q.get("optional")
                    for q in payload.get("questions") or [])
        sub = row.row(align=True)
        sub.enabled = ready
        btn(sub, "agent_card", "OK", "CHECKMARK", arg="ok")
        btn(row, "agent_card", "Skip", "FORWARD", arg="skip")


def _draw_changes(layout):
    turn = changes.current()
    if turn is None or runtime.busy() or not turn["lines"]:
        return
    open_lines = [line for line in turn["lines"] if line["state"] == "done"]
    if not open_lines:
        return
    box = layout.box()
    box.label(text=f"Changed by the last message ({len(open_lines)})", icon="FILE_REFRESH")
    for line in open_lines[:8]:
        icon = {"scene": "SCENE_DATA", "file": "FILE", "copy": "FILE_BACKUP", "note": "INFO"}.get(line["kind"], "DOT")
        box.label(text=line.get("label", "")[:70], icon=icon)
    row = box.row(align=True)
    btn(row, "agent_keep_turn", "Keep all", "CHECKMARK", arg=turn["id"])
    btn(row, "agent_undo_turn", "Undo all", "LOOP_BACK", arg=turn["id"],
        confirm="Undo everything the last message changed?")
    if turn.get("copy"):
        btn(box, "agent_restore_copy", "Restore the file as it was", "FILE_BACKUP", arg=turn["id"],
            confirm="Open the copy kept before this message in place of the current file? Changes since are lost.")


def _draw_toolbar(layout, props):
    row = layout.row(align=True)
    row.prop(props, "view", expand=True)
    btn(row, "agent_new", "", "ADD", hint="New chat")
    row = layout.row(align=True)
    if prefs.get("agent_account", providers.ZETTA) == providers.OWN:
        row.prop(props, "provider", text="")
        if props.provider and props.provider != "zetta":
            row.prop(props, "model", text="")
        else:
            row.prop(props, "tier", text="")
    else:
        row.prop(props, "tier", text="")
    row.prop(props, "thinking", text="")
    row = layout.row(align=True)
    row.prop(props, "mode", expand=True)
    row.prop(props, "approval", text="")


def _draw_composer(layout, props):
    box = layout.box()
    chips = [("mentions", m) for m in ui["mentions"]] + [("images", i["name"]) for i in ui["images"]] + \
            [("documents", d["name"]) for d in ui["documents"]] + [("texts", t["name"]) for t in ui["texts"]] + \
            [("files", f["name"]) for f in ui["files"]]
    if chips:
        flow = box.column(align=True)
        counters = {}
        for kind, label in chips:
            index = counters.get(kind, 0)
            counters[kind] = index + 1
            row = flow.row(align=True)
            row.label(text=label[:48], icon="LINKED" if kind == "mentions" else "FILE")
            btn(row, "agent_unmention", "", "X", arg=kind, arg2=index, emboss=False)
    box.prop(props, "input", text="")
    row = box.row(align=True)
    row.operator("zetta.agent_mention", text="", icon="OUTLINER_OB_EMPTY").question = ""
    row.operator("zetta.agent_add_selection", text="", icon="RESTRICT_SELECT_OFF")
    btn(row, "agent_attach_view", "", "SCENE", hint="Attach a picture of the viewport")
    row.operator("zetta.agent_attach", text="", icon="FILE_FOLDER")
    row.operator("zetta.agent_write_more", text="", icon="TEXT")
    sub = row.row(align=True)
    sub.scale_x = 2.0
    if runtime.busy() and runtime.phase() != runtime.WAITING:
        btn(sub, "agent_stop", "Stop", "SNAP_FACE")
    else:
        btn(sub, "agent_send", "Send", "PLAY")
    if ui["notice"]:
        wrap(box, ui["notice"], "INFO")


def _draw_history(layout):
    if runtime.history["loading"]:
        layout.label(text="Loading…", icon="TIME")
    if runtime.history["error"]:
        wrap(layout, runtime.history["error"], "ERROR", alert=True)
    sessions = runtime.history["sessions"]
    if not sessions and not runtime.history["loading"]:
        layout.label(text="No conversations in this file yet.")
    for session in sessions:
        row = layout.row(align=True)
        btn(row, "agent_open_session", (session.get("title") or "Untitled")[:44],
            "TIME" if session.get("awaiting") else "TEXT", arg=session.get("id"))
        btn(row, "agent_delete_session", "", "TRASH", arg=session.get("id"), confirm="Delete this conversation?",
            emboss=False)


def _draw_memory(layout):
    memory = runtime.memory
    if memory["loading"]:
        layout.label(text="Loading…", icon="TIME")
    if memory["error"]:
        wrap(layout, memory["error"], "ERROR", alert=True)
    box = layout.box()
    box.label(text="Brief for this file", icon="TEXT")
    wrap(box, memory["brief"] or "None yet — the agent writes one once it understands the file.")
    for scope, title in (("global", "Everywhere"), ("project", "This file")):
        box = layout.box()
        box.label(text=title, icon="BOOKMARKS")
        rows = [m for m in memory["memories"] if m.get("scope") == scope]
        if not rows:
            box.label(text="Nothing yet.")
        for entry in rows:
            row = box.row(align=True)
            row.label(text=entry.get("text", "")[:70])
            btn(row, "agent_forget", "", "X", arg=entry.get("id"), confirm="Forget this?", emboss=False)


class ZETTA_PT_agent(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = CATEGORY
    bl_label = "Zetta Agent"
    bl_idname = "ZETTA_PT_agent"

    def draw_header(self, _context):
        self.layout.label(icon_value=pictures.mark())

    def draw(self, context):
        layout = self.layout
        if not bpy.app.online_access:
            wrap(layout, "Online access is turned off in Blender. The Zetta Agent needs it.", "ERROR", alert=True)
            return
        if not auth.signed_in():
            wrap(layout, "Sign in to Zetta 3D in the Zetta 3D tab to use the agent.", "LOCKED")
            return
        if not runtime.options["loaded"]:
            runtime.load_options()
        if not runtime.available():
            wrap(layout, runtime.options["error"], "INFO")
            return
        props = state(context)
        _draw_toolbar(layout, props)
        if props.view == "history":
            _draw_history(layout)
            return
        if props.view == "memory":
            _draw_memory(layout)
            return
        rows = runtime.items()
        if not rows:
            wrap(layout, "Ask for anything in this file — \"bevel every hard edge on the chair\", \"light this "
                         "for a product shot\", \"how do I UV unwrap?\". The agent works with Blender's own tools "
                         "and shows you what it did.", "INFO")
        if len(rows) > ui["shown"]:
            btn(layout, "agent_more", f"Show earlier ({len(rows) - ui['shown']})", "TRIA_UP")
        for item in rows[-ui["shown"]:]:
            _draw_item(layout, item)
        if runtime.phase() == runtime.STREAMING:
            route = runtime.conversation.get("route") or {}
            label = " · ".join(p for p in (route.get("label"), route.get("thinking") and
                                           f"{route['thinking']} thinking") if p)
            layout.label(text=f"Thinking{' · ' + label if label else ''}…", icon="TIME")
        _draw_changes(layout)
        _draw_composer(layout, props)


CLASSES = (ZETTA_PG_agent, ZETTA_OT_agent_mention, ZETTA_OT_agent_add_selection, ZETTA_OT_agent_attach,
           ZETTA_OT_agent_card_text, ZETTA_OT_agent_write_more, ZETTA_OT_agent_send_text, ZETTA_PT_agent)

_keymaps = []


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.WindowManager.zetta_agent = bpy.props.PointerProperty(type=ZETTA_PG_agent)
    bpy.types.TEXT_HT_header.append(_text_header)
    bpy.types.VIEW3D_MT_object_context_menu.append(_context_menu)
    bpy.types.OUTLINER_MT_object.append(_context_menu)
    keyconfig = bpy.context.window_manager.keyconfigs.addon
    if keyconfig is not None:
        keymap = keyconfig.keymaps.new(name="3D View", space_type="VIEW_3D")
        item = keymap.keymap_items.new("zetta.agent_add_selection", "M", "PRESS", shift=True, oskey=True)
        _keymaps.append((keymap, item))
        item = keymap.keymap_items.new("zetta.agent_add_selection", "M", "PRESS", shift=True, ctrl=True)
        _keymaps.append((keymap, item))


def unregister():
    for keymap, item in _keymaps:
        try:
            keymap.keymap_items.remove(item)
        except (ReferenceError, RuntimeError):
            pass
    _keymaps.clear()
    bpy.types.OUTLINER_MT_object.remove(_context_menu)
    bpy.types.VIEW3D_MT_object_context_menu.remove(_context_menu)
    bpy.types.TEXT_HT_header.remove(_text_header)
    del bpy.types.WindowManager.zetta_agent
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
