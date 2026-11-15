"""The agent's drawn chat (ui/chat): the message box's editing, wrapping, the @ list, key handling, and the chat
painted off-screen in each of its states.

    tests/run.sh tests/agent_chat.py
    ZETTA_RENDER_OUT=/some/folder tests/run.sh tests/agent_chat.py   # also saves the renders as PNGs

Rendering needs Blender's GPU module in background mode (gpu.init, Blender 5.0 and later); on older versions
those checks are skipped and say so.
"""

import os
import sys
from types import SimpleNamespace

import bpy

sys.path.insert(0, os.path.dirname(__file__))
from agent_common import fresh, module, run_checks  # noqa: E402

editor = module("ui.chat.editor")
textflow = module("ui.chat.textflow")
mentions = module("ui.chat.mentions")
view = module("ui.chat.view")
classic = module("ui.agent_panel")
runtime = module("agent.runtime")
changes = module("agent.changes")
auth = module("auth")
tasks = module("core.tasks")

OUT = os.environ.get("ZETTA_RENDER_OUT")


# ─── The message box ─────────────────────────────────────────────────────────────────────────────


def test_typing_and_word_moves():
    e = editor.Editor()
    for ch in "bevel the chair":
        e.insert(ch)
    assert e.text == "bevel the chair" and e.caret == 15
    e.left(word=True)
    assert e.caret == 10, e.caret
    e.left(word=True, select=True)
    assert e.selected() == "the ", repr(e.selected())
    e.insert("every ")
    assert e.text == "bevel every chair", e.text
    e.backspace(word=True)
    assert e.text == "bevel chair", e.text
    e.end()
    e.backspace()
    assert e.text == "bevel chai"


def test_undo_groups_typing():
    e = editor.Editor()
    for ch in "hello":
        e.insert(ch)
    e.insert(" ")
    for ch in "world":
        e.insert(ch)
    e.undo()
    assert e.text == "hello ", repr(e.text)
    e.undo()
    assert e.text == "hello", repr(e.text)
    e.redo()
    assert e.text == "hello ", repr(e.text)


def test_mention_query():
    e = editor.Editor("make @Cu")
    assert e.mention_query() == (5, "Cu")
    e = editor.Editor("@")
    assert e.mention_query() == (0, "")
    e = editor.Editor("mail me@home")
    assert e.mention_query() is None, "an @ inside a word is not a tag"
    e = editor.Editor("@Cube is ")
    assert e.mention_query() is None, "the tag ended at the space"
    e = editor.Editor("(@Ch")
    assert e.mention_query() == (1, "Ch")


def test_wrap():
    measure = len  # one unit per character
    assert textflow.wrap("", 10, measure) == [(0, 0)]
    text = "aaa bbb ccc"
    lines = textflow.wrap(text, 6, measure)
    assert [text[a:b] for a, b in lines] == ["aaa ", "bbb ", "ccc"], lines
    text = "abcdefghij"
    assert [text[a:b] for a, b in textflow.wrap(text, 4, measure)] == ["abcd", "efgh", "ij"]
    text = "one\n\ntwo"
    assert [text[a:b] for a, b in textflow.wrap(text, 10, measure)] == ["one", "", "two"]
    lines = textflow.wrap("aaa bbb", 4, measure)
    assert textflow.line_of(lines, 4) == 1, "at a wrap point the caret is on the next line"
    assert textflow.index_at("aaa bbb", lines[1], 2, measure) == 6


# ─── The @ list ──────────────────────────────────────────────────────────────────────────────────


def _scene():
    fresh()
    for name in ("Chair", "Chair Leg", "Table", "Lamp"):
        obj = bpy.data.objects.new(name, None)
        bpy.context.scene.collection.objects.link(obj)
    bpy.data.materials.new("Oak")
    bpy.data.materials.new(".hidden")


def test_find_and_resolve():
    _scene()
    names = [name for _t, name, _l in mentions.find("ch")]
    assert names[:2] == ["Chair", "Chair Leg"], names
    assert ("material:Oak", "Oak", "Material") in mentions.find("oa")
    assert not any(name == ".hidden" for _t, name, _l in mentions.find(""))
    assert [t for t, _n, _l in mentions.find("leg")] == ["Chair Leg"], "a word inside the name matches"
    assert mentions.resolve_typed("put @table under @oak, and @nothing") == ["Table", "material:Oak"]


def _event(kind, unicode="", value="PRESS", shift=False, ctrl=False, alt=False, oskey=False):
    return SimpleNamespace(type=kind, value=value, unicode=unicode, shift=shift, ctrl=ctrl, alt=alt, oskey=oskey)


def _type(text):
    for ch in text:
        kind = {"@": "TWO", " ": "SPACE"}.get(ch, ch.upper())
        assert view._key(bpy.context, _event(kind, ch)) == "consume"


def test_at_list_reacts_while_typing():
    _scene()
    view.box.clear()
    view.state.update(focus=True, dismissed=None, pick=0)
    view.state["picked"].clear()
    _type("move @ch")
    rows = view.popup_rows()
    assert [name for _t, name, _l in rows][:2] == ["Chair", "Chair Leg"], rows
    view._key(bpy.context, _event("DOWN_ARROW"))
    view._key(bpy.context, _event("RET"))
    assert view.box.text == "move @Chair Leg ", repr(view.box.text)
    assert view.state["picked"] == {"Chair Leg": "Chair Leg"}
    assert view.popup_rows() == [], "the list closes once a name is picked"
    _type("next to @")
    assert view.popup_rows(), "a bare @ lists everything"
    view._key(bpy.context, _event("ESC"))
    assert view.popup_rows() == [] and view.state["focus"], "Esc closes the list, not the box"
    view._key(bpy.context, _event("ESC"))
    assert not view.state["focus"]


def test_enter_sends_and_shift_enter_breaks():
    _scene()
    sent = []
    original = classic._send
    classic._send = lambda context, *_: (sent.append((classic.state(context).input, list(classic.ui["mentions"]))),
                                         setattr(classic.state(context), "input", ""))
    try:
        view.box.clear()
        classic.ui["mentions"].clear()
        view.state.update(focus=True, dismissed=None, pick=0)
        view.state["picked"].clear()
        _type("light @table")
        view._key(bpy.context, _event("ESC"))  # close the list, keep typing
        view._key(bpy.context, _event("RET", shift=True))
        _type("softly")
        assert view.box.text == "light @table\nsoftly", repr(view.box.text)
        view._key(bpy.context, _event("RET"))
        assert sent == [("light @table\nsoftly", ["Table"])], sent
        assert view.box.text == "", "the box empties once the message is sent"
    finally:
        classic._send = original
        classic.ui["mentions"].clear()


def test_clipboard_keys():
    view.box.clear()
    view.state.update(focus=True)
    _type("abc")
    cmd = {"oskey": True} if view.MAC else {"ctrl": True}
    assert view._key(bpy.context, _event("S", **cmd)) == "pass", "Blender's own shortcuts still work"
    bpy.context.window_manager.clipboard = "probe"
    if bpy.context.window_manager.clipboard != "probe":
        print("  (no clipboard in background Blender: copy and paste not checked)")
        return
    view._key(bpy.context, _event("A", **cmd))
    view._key(bpy.context, _event("C", **cmd))
    assert bpy.context.window_manager.clipboard == "abc"
    view._key(bpy.context, _event("RIGHT_ARROW"))
    view._key(bpy.context, _event("V", **cmd))
    assert view.box.text == "abcabc"


# ─── Painted off-screen ──────────────────────────────────────────────────────────────────────────


class Region:
    def __init__(self, width, height):
        self.x, self.y, self.width, self.height = 0, 0, width, height
        self.alignment = "RIGHT"

    def as_pointer(self):
        return 1000 + self.width


def _signed_in():
    auth.user = {"id": 1, "email": "demo@example.com", "displayName": "Demo",
                 "credits": {"threeD": {"monthly": 2450, "purchased": 0, "bonusMonthly": 0, "bonus": 0}}}
    auth.current = auth.SIGNED_IN
    runtime.options.update(loaded=True, loading=False, error=None, providers=[],
                           tiers=[{"key": "auto", "label": "Auto", "available": True},
                                  {"key": "capable", "label": "Capable", "available": True}],
                           thinking=[{"key": "auto", "label": "Auto", "available": True}])


def _gpu():
    import gpu

    if not hasattr(gpu, "init"):
        return None
    try:
        gpu.init()
    except Exception:  # noqa: BLE001
        pass
    return gpu


def render(name, width=320, height=640):
    """Paints the chat as the sidebar would at this size; returns the frame (its buttons) or None without a
    GPU."""
    gpu = _gpu()
    if gpu is None:
        print(f"  (no GPU in background on Blender {bpy.app.version_string}: {name} not rendered)")
        return None
    from mathutils import Matrix

    region = Region(width, height)
    left, w = view.canvas_box(region)
    frame = view.Frame(0, region, left, w)
    offscreen = gpu.types.GPUOffScreen(width, height)
    with offscreen.bind():
        fb = gpu.state.active_framebuffer_get()
        fb.clear(color=(0.1, 0.1, 0.1, 1.0))
        gpu.state.viewport_set(0, 0, width, height)
        gpu.state.scissor_set(0, 0, width, height)
        with gpu.matrix.push_pop():
            gpu.matrix.load_matrix(Matrix.Identity(4))
            gpu.matrix.load_projection_matrix(Matrix.Identity(4))
            gpu.matrix.translate((-1.0, -1.0))
            gpu.matrix.scale((2.0 / width, 2.0 / height))
            view._paint(bpy.context, frame, region.as_pointer(), height)
        pixels = fb.read_color(0, 0, width, height, 4, 0, "FLOAT")
    offscreen.free()
    frame.pixels = pixels.to_list()  # [row][column][rgba]
    if OUT:
        os.makedirs(OUT, exist_ok=True)
        pixels.dimensions = width * height * 4
        image = bpy.data.images.new(f"render-{name}", width, height, alpha=True)
        image.pixels.foreach_set(pixels)
        image.filepath_raw = os.path.join(OUT, f"{name}.png")
        image.file_format = "PNG"
        image.save()
        bpy.data.images.remove(image)
    return frame


def _keys(frame):
    return {hit.key for hit in frame.hits} if frame else set()


def _reset_chat():
    runtime.conversation["items"] = []
    runtime.conversation["phase"] = runtime.IDLE
    runtime.card = None
    changes.turns.clear()
    view.box.clear()
    view.state.update(focus=False, hover=None, dismissed=None, pick=0, field_first=0)
    view.state["picked"].clear()
    view.state["scroll"].clear()
    for key in ("mentions", "images", "documents", "texts", "files"):
        classic.ui[key] = []
    classic.ui["notice"] = None
    classic.ui["open"] = set()
    classic.state().view = "chat"
    classic.state().input = ""


def test_render_signed_out():
    auth.user, auth.current = None, auth.SIGNED_OUT
    _reset_chat()
    frame = render("signed_out")
    if frame:
        assert {"tab:chat", "tab:history", "tab:memory"} <= _keys(frame)
        assert "send" not in _keys(frame), "nothing to send while signed out"


def test_render_welcome():
    _scene()
    _signed_in()
    _reset_chat()
    frame = render("welcome")
    if frame:
        keys = _keys(frame)
        assert {"tab:chat", "send", "plus", "at", "mode:agent", "mode:ask", "settings", "field"} <= keys, keys
        assert any(k.startswith("example:") for k in keys)
        field = frame.field
        tabs = next(h for h in frame.hits if h.key == "tab:chat")
        assert field["y"] < 120 and tabs.y > 560, "the message box sits at the bottom, the tabs at the top"


def _conversation(card=False):
    _scene()
    _signed_in()
    _reset_chat()
    rows = [
        {"role": "user", "text": "Light the cube like brushed metal", "mentions": ["Chair"]},
        {"role": "step", "name": "inspect", "status": "done", "label": "Inspected Chair"},
        {"role": "step", "name": "add_light", "status": "done", "label": "Added Key Light"},
        {"role": "step", "name": "add_light", "status": "done", "label": "Added Rim Light"},
        {"role": "assistant", "text": "I added a three-point light setup and brightened the world.\n\n"
                                      "- **Key Light**: area, front right, 800 W\n- Fill Light: soft blue, 300 W\n"
                                      "- Rim Light: warm, behind, 500 W\n\nSay if you want a floor under it."},
    ]
    for row in rows:
        runtime._add(row)
    turn = changes.begin_turn("lights")
    for label in ("Added Key Light", "Added Rim Light", "Edited nodes of world"):
        changes.scene_step(label)
    if card:
        runtime._add({"role": "user", "text": "Scatter 40 rocks around it"})
        runtime._add({"role": "step", "name": "run_python", "status": "running", "label": "Run python"})
        runtime._open_card("approve", {
            "title": "Run Python: scatter 40 rocks around the chair",
            "summary": "import random\nrocks = bpy.data.collections.new(\"Rocks\")\nfor i in range(40):\n"
                       "    angle = random.uniform(0, 6.283)\n    ...",
            "reasons": [], "copy": True, "copy_ticked": True, "copy_note": None, "button": "Allow"},
            lambda _answer: None)
        runtime.conversation["phase"] = runtime.WAITING
    return turn


def test_render_conversation():
    _conversation()
    frame = render("conversation")
    if frame:
        keys = _keys(frame)
        assert any(k.startswith("steps:") for k in keys), "the steps fold into one line"
        assert any(k.startswith("undo:") for k in keys) and any(k.startswith("keep:") for k in keys)
    # Opening the steps lists each one.
    key = next(k for k in _keys(frame) if k.startswith("steps:")) if frame else None
    if key:
        classic.ui["open"].add(key)
        frame = render("conversation_steps_open")
        assert sum(1 for k in _keys(frame) if k.startswith("step:")) == 3


def test_hover_is_a_faint_highlight():
    """Hovering a row lights it up a little — not a white bar over its text (Blender's text drawing can leave
    blending off, so every shape sets it)."""
    _conversation()
    frame = render("before_hover")
    if frame is None:
        return
    hit = next(h for h in frame.hits if h.key.startswith("changes:"))
    view.state["hover"] = hit.key
    try:
        frame = render("changes_hover")
    finally:
        view.state["hover"] = None
    x, y = int(hit.x + hit.w * 0.8), int(hit.y + hit.h / 2)  # inside the row, right of its label
    r, g, b, _a = frame.pixels[y][x]
    assert max(r, g, b) < 0.45, f"the hovered row is drawn too bright: {(r, g, b)}"


def _picture_file(name, width=96, height=64):
    """A small PNG stored as an attachment; returns its "attached:" name."""
    import tempfile

    attachments = module("agent.attachments")
    image = bpy.data.images.new("probe", width, height)
    image.pixels.foreach_set([0.8, 0.4, 0.1, 1.0] * (width * height))
    path = os.path.join(tempfile.mkdtemp(), name)
    image.filepath_raw, image.file_format = path, "PNG"
    image.save()
    bpy.data.images.remove(image)
    with open(path, "rb") as handle:
        return "attached:" + attachments.store(name, handle.read())


def test_attached_pictures_show_as_pictures():
    _conversation()
    name = _picture_file("ref.png")
    classic.ui["images"] = [{"name": name, "data": "", "mediaType": "image/png"}]
    runtime._add({"role": "user", "text": "Build this", "attachments": [name]})
    frame = render("attached_pictures")
    if frame is None:
        return
    keys = _keys(frame)
    assert {"thumb:0", "chip:images:0"} <= keys, "the picture in the message box is a thumbnail with ×"
    assert not any(k.startswith("chip:images") and k != "chip:images:0" for k in keys)
    assert any(k.startswith("picture:") for k in keys), "the sent message shows its picture"
    thumb = next(h for h in frame.hits if h.key == "thumb:0")
    r, g, b, _a = frame.pixels[int(thumb.y + thumb.h / 2)][int(thumb.x + thumb.w / 2)]
    assert abs(r - 0.8) < 0.06 and abs(g - 0.4) < 0.06, f"pictures keep their colours: {(r, g, b)}"


def test_failed_step_says_why():
    _conversation()
    runtime._add({"role": "step", "name": "edit_image", "status": "error", "label": "Edit image",
                  "detail": "The image service refused the picture: it is smaller than 128 px."})
    render("failed_step")  # the reason is drawn under "1 step · 1 failed" without opening it


def test_generate_card_offers_build_in_blender():
    zc = module("agent.zetta_common")
    tools = module("agent.tools")
    _conversation()
    answers = []
    runtime._open_card("generate", {"title": "Generate 3D: Jester trousers", "lines": ["Zetta N2.5"], "credits": 50,
                                    "button": "Generate", "alternative": "blender"}, answers.append)
    frame = render("generate_card")
    if frame:
        assert {"card:allow", "card:blender", "card:decline"} <= _keys(frame)
    classic._card(bpy.context, "blender", "")
    assert answers == ["blender"], answers
    # The tool side: choosing it ends the call with the instruction to model it in Blender.
    finished = []
    ctx = tools.Context({"toolUseId": "t", "name": "generate_3d_model", "input": {}, "effect": "write"},
                        finished.append)
    ctx.card = lambda kind, payload, on_answer: (finished.append(payload), on_answer("blender"))
    zc.confirm(ctx, "Generate 3D: x", ["line"], 50, "Generate", lambda: None, alternative=True)
    assert finished[0]["alternative"] == "blender"
    assert not finished[1].error and "Build in Blender" in finished[1].text, finished[1].text


def test_full_autonomy_still_asks_before_generating_a_3d_model():
    """Full autonomy approves credit cards by itself — but never a 3D model generation."""
    zc = module("agent.zetta_common")
    tools = module("agent.tools")
    _conversation()
    props = classic.state()
    old = props.approval
    props.approval = "auto"
    try:
        for alternative, expect_card in ((False, False), (True, True)):
            runtime.card = None
            ran = []
            ctx = tools.Context({"toolUseId": "t", "name": "x", "input": {}, "effect": "write"}, lambda _r: None)
            ctx.card = lambda kind, payload, on_answer: runtime._tool_card(kind, payload, on_answer)
            zc.confirm(ctx, "Generate 3D: x", ["line"], 50, "Generate", lambda: ran.append(1),
                       alternative=alternative)
            assert (runtime.card is not None) == expect_card, (alternative, runtime.card)
    finally:
        props.approval = old
        runtime.card = None
        runtime.conversation["phase"] = runtime.IDLE


def test_render_card_waiting():
    _conversation(card=True)
    frame = render("card")
    if frame:
        keys = _keys(frame)
        assert {"card:allow", "card:decline", "card:copy", "banner"} <= keys, keys


def test_render_typing_with_at_list():
    _conversation()
    view.state["focus"] = True
    view.box.set_text("put a soft light on @ch")
    frame = render("typing_at")
    if frame:
        keys = _keys(frame)
        assert "popup:0" in keys and "popup:1" in keys, keys
        popup = next(h for h in frame.hits if h.key == "popup")
        assert popup.y > frame.field["y"], "the list opens above the message box"
    view.state["focus"] = False
    frame = render("typing_at_unfocused")
    if frame:
        assert "popup" not in _keys(frame), "no list while the box doesn't have the keyboard"


def test_render_long_message_and_chips():
    _conversation()
    view.state["focus"] = True
    classic.ui["mentions"] = ["Chair", "material:Oak"]
    view.box.set_text(" ".join(["Make every leg of the chair a little thinner and taper it toward the floor."] * 6))
    frame = render("long_message")
    if frame:
        assert frame.field["h"] <= view.FIELD_LINES * view.Sizes().line + view.Sizes()(12)
        assert {"chip:mentions:0", "chip:mentions:1"} <= _keys(frame)


def test_render_history_and_memory():
    _signed_in()
    _reset_chat()
    runtime.history.update(loading=False, error=None, sessions=[
        {"id": 1, "title": "Bronze statue"}, {"id": 2, "title": "Product lighting", "awaiting": True}])
    classic.state().view = "history"
    frame = render("history")
    if frame:
        assert {"new", "session:1", "session:2", "delete:1"} <= _keys(frame)
        assert "send" not in _keys(frame), "the composer is the chat's"
    runtime.memory.update(loading=False, error=None, brief="A low-poly diorama for a mobile game.",
                          memories=[{"id": 5, "scope": "global", "text": "Prefers metric units"},
                                    {"id": 6, "scope": "project", "text": "The hero object is Chair"}])
    classic.state().view = "memory"
    frame = render("memory")
    if frame:
        assert {"forget:5", "forget:6"} <= _keys(frame)
    classic.state().view = "chat"


def test_render_narrow_and_scaled():
    _conversation(card=True)
    render("narrow", width=220, height=560)
    prefs = bpy.context.preferences
    old = prefs.view.ui_scale
    try:
        prefs.view.ui_scale = 1.5
        if prefs.system.ui_scale < 1.4:
            print("  (background Blender keeps its interface scale: the scaled render is at 1.0)")
        frame = render("scaled", width=420, height=760)
        if frame and prefs.system.ui_scale >= 1.4:
            send = next(h for h in frame.hits if h.key == "send")
            assert send.h >= 30, f"buttons grow with the interface scale ({send.h})"
    finally:
        prefs.view.ui_scale = old


def test_scrolling_keeps_up_with_new_messages():
    _conversation()
    frame = render("scroll_a", height=300)
    if frame is None:
        return
    key = view.scroll_key(Region(320, 300).as_pointer())
    scroll = view.state["scroll"][key]
    assert scroll["follow"] and scroll["max"] > 0, scroll
    view.scroll_by(frame, key, view.Sizes().line * 3)
    assert not scroll["follow"], "scrolling up stops following"
    runtime._add({"role": "assistant", "text": "Another reply."})
    render("scroll_b", height=300)
    assert not scroll["follow"], "a new message does not pull the reader back down"
    scroll["follow"] = True
    view.follow_all()


run_checks(globals())
