"""The Zetta Agent's chat, drawn over the 3D View sidebar's Zetta Agent tab.

WHY IT IS DRAWN, NOT LAID OUT WITH BLENDER'S WIDGETS: a sidebar scrolls as one page, so nothing in a panel can
stay put, and a text field cannot react while you type. Drawn, the tabs stay at the top and the message box at
the bottom while the conversation scrolls between them, and "@" opens a list of names as you type it.

HOW: `draw_region` paints one frame (a draw callback on the sidebar) and records where every button is. Clicks,
scrolling and typing come in through `handle`, from a modal operator that runs only while the mouse is over the
chat or the message box has focus (a modal that runs all the time would hold up Blender's auto-save). The
sidebar's key map starts it on the first mouse move, click or scroll over the chat (host.ZETTA_OT_agent_wake).

The conversation and its actions are the classic panel's (`ui.agent_panel`): the same state, the same calls.
"""

import sys
import time

import bpy

from ... import auth
from ...agent import changes, runtime
from ...core import tasks
from .. import agent_panel as classic
from ..ops import ACTIONS
from . import mentions, paint, textflow
from .editor import Editor
from .theme import Palette, Sizes, fade, mix

CATEGORY = classic.CATEGORY
ui = classic.ui
MAC = sys.platform == "darwin"
FIELD_LINES = 8  # the message box grows to this many lines, then scrolls
STEP_DETAIL_LINES = 24
CODE_LINES = 12
EXAMPLES = ("Bevel every hard edge on the selected object",
            "Light this scene for a product shot",
            "How do I UV unwrap?")

box = Editor()  # the message box
state = {
    "focus": False,  # the message box has the keyboard
    "hover": None,  # key of the button under the mouse
    "press": None,  # key of the button pressed, until release
    "drag": False,  # selecting text in the message box
    "pick": 0,  # highlighted row of the @ list
    "dismissed": None,  # where the @ was when the list was closed with Esc
    "picked": {},  # name → target, for names picked from the @ list
    "scroll": {},  # region pointer → {"top", "follow"}
    "field_first": 0,  # first message-box line shown when it is taller than FIELD_LINES
    "running": set(),  # windows whose event modal is running
    "starting": set(),
}
frames = {}  # region pointer → Frame, as last drawn
_wraps = {}


# ─── Frames and hits ─────────────────────────────────────────────────────────────────────────────


class Hit:
    __slots__ = ("x", "y", "w", "h", "key", "fn", "kind")

    def __init__(self, x, y, w, h, key, fn, kind):
        self.x, self.y, self.w, self.h, self.key, self.fn, self.kind = x, y, w, h, key, fn, kind

    def contains(self, x, y):
        return self.x <= x < self.x + self.w and self.y <= y < self.y + self.h


class Frame:
    """One drawn sidebar: where it is in its window, and its buttons."""

    def __init__(self, window, region, left, width):
        self.window = window
        self.rx, self.ry, self.rw, self.rh = region.x, region.y, region.width, region.height
        self.left, self.width = left, width  # the chat's part of the region (not the tab strip)
        self.hits = []
        self.conv = (0, 0, 0, 0)
        self.field = None  # {"x", "y", "w", "h", "lines", "first", "tx", "ty", "line"}
        self.popup = []  # the @ list's rows: [(target, name, label)]
        self.drawn = time.time()

    def inside(self, wx, wy):
        """Over the chat — not the strip along the sidebar's edge, where Blender resizes it."""
        x, y = wx - self.rx, wy - self.ry
        edge = round(6 * Sizes().scale)
        # The resize edge is the sidebar's inner side: the left one, unless the tabs are on the left.
        if self.left > 0:
            return self.left <= x < self.left + self.width - edge and 0 <= y < self.rh
        return edge <= x < self.width and 0 <= y < self.rh

    def local(self, wx, wy):
        return wx - self.rx, wy - self.ry

    def hit_at(self, x, y):
        for hit in reversed(self.hits):
            if hit.contains(x, y):
                return hit
        return None


def _scroll(region_key):
    """Scroll state of one list in one sidebar: keyed by the region and the view (chat, history, memory)."""
    return state["scroll"].setdefault(region_key, {"top": 0.0, "follow": True})


def scroll_key(region_key, context=None):
    try:
        return f"{region_key}:{classic.state(context).view}"
    except AttributeError:
        return f"{region_key}:chat"


def follow_all():
    for scroll in state["scroll"].values():
        scroll["follow"] = True


# ─── The canvas one frame is painted on ──────────────────────────────────────────────────────────


class Canvas:
    def __init__(self, frame, clip_box=None):
        self.f = frame
        self.P = Palette()
        self.Z = Sizes()
        self.clip_box = clip_box  # hits outside it (scrolled away) are not recorded

    def hit(self, x, y, w, h, key, fn, kind="button"):
        if self.clip_box is not None:
            cx, cy, cw, ch = self.clip_box
            x0, y0 = max(x, cx), max(y, cy)
            x1, y1 = min(x + w, cx + cw), min(y + h, cy + ch)
            if x1 <= x0 or y1 <= y0:
                return
            x, y, w, h = x0, y0, x1 - x0, y1 - y0
        self.f.hits.append(Hit(x, y, w, h, key, fn, kind))

    def hovered(self, key):
        return state["hover"] == key

    # Text measured and drawn with its box's top edge at `top` (y grows upwards).

    def baseline(self, top, line, size):
        return top - (line + size * 0.68) / 2

    def text(self, x, top, s, color=None, size=None, line=None, font=paint.UI_FONT):
        Z = self.Z
        size = size or Z.font
        line = line or Z.line
        paint.text(x, self.baseline(top, line, size), s, size, color or self.P.text, font)

    def button(self, x, y, w, h, label, key, fn, style="normal", selected=False, size=None, align="center",
               radius=None, suffix=None):
        P, Z = self.P, self.Z
        hover = self.hovered(key)
        if style == "accent" or selected:
            fill, ink = (P.accent_hover if hover else P.accent), P.accent_text
        elif style == "danger":
            fill, ink = (mix(P.danger, (1, 1, 1, 1), 0.1) if hover else P.danger), (1, 1, 1, 1)
        elif style == "ghost":
            fill, ink = (P.hover if hover else None), P.text
        elif style == "link":
            fill, ink = None, (P.text if hover else P.muted)
        else:
            fill, ink = (P.button_hover if hover else P.button), P.button_text
        if fill is not None:
            paint.rect(x, y, w, h, fill, Z.radius if radius is None else radius)
        size = size or Z.font
        margin = Z.gap if w > 4 * Z.gap else Z(2)
        room = w - 2 * margin
        if suffix:
            suffix_w = paint.measure(suffix, size)
            room -= suffix_w + Z(4)
            paint.text(x + w - margin - suffix_w, y + (h - size * 0.68) / 2, suffix, size, ink)
        label = paint.fit(label, room, size)
        width = paint.measure(label, size)
        tx = x + (room + 2 * margin - width) / 2 if align == "center" else x + margin
        paint.text(tx, y + (h - size * 0.68) / 2, label, size, ink)
        self.hit(x, y, w, h, key, fn)
        return width


def _attachment_file(name):
    """The stored file of an attached picture ("attached:<name>"), or None."""
    from ...agent import attachments

    if not isinstance(name, str) or not name.startswith("attached:"):
        return None
    return attachments.path(name[len("attached:"):])


def _tile_rows(entries, width, tile_h, gap):
    """Pictures laid out in rows of tiles tile_h tall: [[(entry, w)]]. entries: [(…, (texture, iw, ih))]."""
    rows, row, used = [], [], 0
    for entry in entries:
        _texture, iw, ih = entry[-1]
        w = tile_h * iw / ih if ih else tile_h
        w = max(tile_h * 0.6, min(w, tile_h * 1.8, width))
        if row and used + w > width:
            rows.append(row)
            row, used = [], 0
        row.append((entry, w))
        used += w + gap
    if row:
        rows.append(row)
    return rows


def _wrapped(s, width, size, font=paint.UI_FONT):
    key = (s, int(width), size, font)
    lines = _wraps.get(key)
    if lines is None:
        if len(_wraps) > 3000:
            _wraps.clear()
        ranges = textflow.wrap(s, width, lambda part: paint.measure(part, size, font))
        lines = [s[a:b].rstrip(" ") for a, b in ranges]
        _wraps[key] = lines
    return lines


# ─── Actions ─────────────────────────────────────────────────────────────────────────────────────


def act(name, arg="", arg2="", confirm=None):
    """A button's action: one of the classic panel's, asking first when `confirm` says so."""
    def run(context):
        if confirm:
            bpy.ops.zetta.do("INVOKE_DEFAULT", action=name, arg=str(arg), arg2=str(arg2), confirm=confirm)
        else:
            ACTIONS[name](context, str(arg), str(arg2))
        tasks.redraw()
    return run


def call(op, **props):
    def run(_context):
        module, _, name = op.partition(".")
        getattr(getattr(bpy.ops, module), name)("INVOKE_DEFAULT", **props)
    return run


def toggle(key):
    def run(_context):
        ui["open"].symmetric_difference_update({key})
    return run


def _set(attr, value):
    def run(context):
        setattr(classic.state(context), attr, value)
    return run


def _view(value):
    def run(context):
        ACTIONS["agent_view"](context, value, "")
        state["focus"] = state["focus"] and value == "chat"
    return run


def _example(text):
    def run(context):
        box.set_text(text)
        classic.state(context).input = text
        state["focus"] = True
    return run


def send(context):
    if runtime.busy() and runtime.phase() != runtime.WAITING:
        return
    text = box.text
    if not text.strip() and not any(ui[k] for k in ("images", "documents", "texts", "files")):
        return
    targets = [target for name, target in state["picked"].items() if f"@{name}" in text]
    targets += mentions.resolve_typed(text)
    for target in targets:
        if target not in ui["mentions"]:
            ui["mentions"].append(target)
    props = classic.state(context)
    props.input = text
    classic._send(context)
    if not props.input:
        box.clear()
        state["picked"].clear()
        state["field_first"] = 0
        follow_all()
    tasks.redraw()


def _send(context):
    send(context)


def _stop(_context):
    classic._stop()


def _unmention(kind, index):
    def run(_context):
        try:
            ui[kind].pop(index)
        except (KeyError, IndexError):
            pass
    return run


def _dismiss_notice(_context):
    ui["notice"] = None


def _start_mention(_context):
    """The @ button: types an @ where the caret is, which opens the list."""
    state["focus"] = True
    before = box.text[box.caret - 1:box.caret] if box.caret else ""
    box.insert("@" if not before or before.isspace() else " @")
    state["dismissed"] = None


def _pick(target, name):
    def run(context):
        found = box.mention_query()
        if found is None:
            return
        start, _query = found
        word = name if " " not in name else name.replace(" ", " ")  # one word, so the tag stays whole
        box.replace(start, box.caret, f"@{word} ")
        state["picked"][word] = target
        state["pick"] = 0
        classic.state(context).input = box.text
    return run


# ─── Drawing: the whole frame ────────────────────────────────────────────────────────────────────


def tab_active(region):
    category = getattr(region, "active_panel_category", None)
    if category is not None:
        return category == CATEGORY
    return hosted.get(region.as_pointer(), False)


hosted = {}  # region pointer → True when the panel underneath was laid out (Blender without active category)


def draw_callback():
    context = bpy.context
    region = context.region
    if region is None or not tab_active(region) or classic.classic_panel():
        return
    try:
        draw_region(context, region)
    except Exception:  # noqa: BLE001 — a drawing bug must not break Blender's own drawing
        import traceback

        traceback.print_exc()


def canvas_box(region):
    """(left, width) of the chat inside the region: the tab strip keeps its own strip."""
    Z = Sizes()
    tabs = Z.widget_unit
    if getattr(region, "alignment", "RIGHT") == "LEFT":
        return tabs, max(1, region.width - tabs)
    return 0, max(1, region.width - tabs)


def draw_region(context, region, window_key=None):
    left, width = canvas_box(region)
    window = window_key if window_key is not None else (context.window.as_pointer() if context.window else 0)
    frame = Frame(window, region, left, width)
    frames[region.as_pointer()] = frame
    _paint(context, frame, region.as_pointer(), region.height)
    # The modal starts when the mouse comes over the chat (host.ZETTA_OT_agent_wake); a draw only restarts it for a
    # message box that still has the keyboard (after a file was opened, say).
    if window_key is None and state["focus"] and context.window is not None and context.area is not None:
        ensure_events(context.window, context.area, region)


def _paint(context, frame, region_key, height):
    c = Canvas(frame)
    P = c.P
    paint.begin()
    try:
        x0, w = frame.left, frame.width
        paint.rect(x0, 0, w, height, P.bg)
        blocked = _blocked()
        top = height
        top = _draw_tabs(c, context, x0, top, w)
        if blocked:
            _draw_message(c, x0, 0, w, top, blocked)
            return
        props = classic.state(context)
        if not state["focus"] and props.input != box.text:
            box.set_text(props.input)  # sent or written elsewhere (Write more…, the classic panel)
        scroll = scroll_key(region_key, context)
        if props.view == "chat":
            composer_top = _draw_composer(c, context, x0, w)
            _draw_conversation(c, context, scroll, x0, composer_top, w, top - composer_top)
            _draw_composer(c, context, x0, w, paint_it=True)
            _draw_popup(c, context)
        elif props.view == "history":
            _draw_list(c, context, scroll, x0, 0, w, top, _history_elements(c, w))
        else:
            _draw_list(c, context, scroll, x0, 0, w, top, _memory_elements(c, w))
    finally:
        paint.end()


def _blocked():
    if not bpy.app.online_access:
        return "Online access is turned off in Blender (Preferences › System › Network). The Zetta Agent needs it."
    if not auth.signed_in():
        return "Sign in to Zetta 3D in the Zetta 3D tab to use the agent."
    if not runtime.options["loaded"]:
        runtime.load_options()
        return "Loading…" if runtime.options.get("loading") or not runtime.options.get("error") else \
            runtime.options["error"]
    if not runtime.available():
        return runtime.options["error"] or "The Zetta Agent is not available on this account yet."
    return None


def _draw_message(c, x, y, w, top, message):
    P, Z = c.P, c.Z
    cy = top - Z.pad * 3
    for line in _wrapped(message, w - 4 * Z.pad, Z.font):
        c.text(x + 2 * Z.pad, cy, line, P.muted)
        cy -= Z.line


def _draw_tabs(c, context, x, top, w):
    """Chat · History · Memory — always at the top."""
    P, Z = c.P, c.Z
    h = Z.button
    y = top - Z.pad - h
    current = classic.state(context).view
    labels = (("chat", "Chat"), ("history", "History"), ("memory", "Memory"))
    inner = w - 2 * Z.pad
    paint.rect(x + Z.pad, y, inner, h, P.field, Z.radius)
    each = inner / len(labels)
    for i, (key, label) in enumerate(labels):
        bx = x + Z.pad + i * each
        c.button(bx + 1, y + 1, each - 2, h - 2, label, f"tab:{key}", _view(key), style="ghost",
                 selected=current == key, radius=Z.radius - 1)
    bottom = y - Z.pad
    paint.line(x, bottom + 0.5, x + w, bottom + 0.5, P.card_line)
    return bottom


# ─── Scrolling lists ─────────────────────────────────────────────────────────────────────────────


class Element:
    """One block of a scrolling list: its height at the list's width, and how to draw it at a given top."""

    __slots__ = ("height", "draw", "key")

    def __init__(self, height, draw, key=None):
        self.height, self.draw, self.key = height, draw, key


def _draw_list(c, context, region_key, x, y, w, top, elements, bottom_gap=None):
    """Draws elements in the box (x, y)–(x+w, top), scrolled; returns nothing. Scroll state is per region."""
    P, Z = c.P, c.Z
    gap = Z.gap if bottom_gap is None else bottom_gap
    view_h = top - y
    content = sum(e.height for e in elements) + gap * (len(elements) + 1)
    scroll = _scroll(region_key)
    max_top = max(0.0, content - view_h)
    if scroll["follow"]:
        scroll["top"] = max_top
    scroll["top"] = min(max(0.0, scroll["top"]), max_top)
    if scroll["top"] >= max_top - 1:
        scroll["follow"] = True
    scroll["max"] = max_top
    c.f.conv = (x, y, w, view_h)
    offset = scroll["top"]
    c.clip_box = (x, y, w, view_h)
    with paint.clip(x, y, w, view_h):
        cursor = top + offset - gap
        for element in elements:
            if cursor - element.height < top and cursor > y:
                element.draw(c, x + Z.pad, cursor, w - 2 * Z.pad)
            cursor -= element.height + gap
    c.clip_box = None
    if max_top > 0:  # a thin scrollbar
        bar_h = max(Z(24), view_h * view_h / content)
        frac = offset / max_top if max_top else 1
        bar_y = top - bar_h - (view_h - bar_h) * frac
        paint.rect(x + w - Z.scrollbar - 2, bar_y, Z.scrollbar, bar_h, fade(P.text, 0.25), Z.scrollbar / 2)


def scroll_by(frame, region_key, dy):
    scroll = _scroll(region_key)
    scroll["top"] = min(max(0.0, scroll["top"] - dy), scroll.get("max", 0.0))
    scroll["follow"] = scroll["top"] >= scroll.get("max", 0.0) - 1


# ─── The conversation ────────────────────────────────────────────────────────────────────────────


def _draw_conversation(c, context, region_key, x, y, w, h):
    elements = _conversation_elements(c, context, w - 2 * c.Z.pad)
    _draw_list(c, context, region_key, x, y, w, y + h, elements)


def _conversation_elements(c, context, width):
    rows = runtime.items()
    elements = []
    if not rows:
        elements.extend(_welcome(c, width))
        return elements
    hidden = len(rows) - ui["shown"]
    if hidden > 0:
        elements.append(_link_element(c, f"Show earlier ({hidden})", "more", act("agent_more")))
        rows = rows[hidden:]
    i = 0
    while i < len(rows):
        item = rows[i]
        if item.get("role") == "step":
            group = []
            while i < len(rows) and rows[i].get("role") == "step":
                group.append(rows[i])
                i += 1
            elements.append(_steps_element(c, group, width))
            continue
        element = _item_element(c, item, width)
        if element is not None:
            elements.append(element)
        i += 1
    changed = _changes_element(c, width)
    if changed is not None:
        elements.append(changed)
    if runtime.phase() == runtime.STREAMING:
        route = runtime.conversation.get("route") or {}
        parts = [p for p in (route.get("label"), route.get("thinking") and f"{route['thinking']} thinking") if p]
        dots = "." * (1 + int(time.time() * 2) % 3)
        elements.append(_note_element(c, "Thinking" + (" · " + " · ".join(parts) if parts else "") + dots, width,
                                      glyph="⋯"))
    return elements


def _welcome(c, width):
    P, Z = c.P, c.Z
    intro = ("Ask for anything in this file. The agent works with Blender's own tools and shows you what it "
             "changed, so you can keep it or undo it.")
    lines = _wrapped(intro, width, Z.font)

    def draw_intro(c, x, top, w):
        c.text(x, top, "Zetta Agent", P.text, size=Z.title, line=Z.line)
        cy = top - Z.line - Z(4)
        for line in lines:
            c.text(x, cy, line, P.muted)
            cy -= Z.line
        cy -= Z.gap
        c.text(x, cy, "Try:", P.faint, size=Z.small, line=Z.small_line)

    elements = [Element(Z.line + Z(4) + len(lines) * Z.line + Z.gap + Z.small_line, draw_intro)]
    for example in EXAMPLES:
        def draw_example(c, x, top, w, example=example):
            c.button(x, top - Z.button, w, Z.button, example, f"example:{example}", _example(example),
                     style="normal", align="left", size=Z.small)
        elements.append(Element(Z.button, draw_example))
    return elements


def _link_element(c, label, key, fn):
    Z = c.Z

    def draw(c, x, top, w):
        c.button(x, top - Z.button, w, Z.button, label, key, fn, style="link")
    return Element(Z.button, draw)


def _note_element(c, text, width, glyph="ⓘ", color=None):
    P, Z = c.P, c.Z
    indent = Z(16)
    lines = _wrapped(text, width - indent, Z.small)

    def draw(c, x, top, w):
        c.text(x, top, glyph, color or P.faint, size=Z.small, line=Z.small_line)
        for n, line in enumerate(lines):
            c.text(x + indent, top - n * Z.small_line, line, color or P.muted, size=Z.small, line=Z.small_line)
    return Element(len(lines) * Z.small_line, draw)


def _item_element(c, item, width):
    role = item.get("role")
    if role == "user":
        return _user_element(c, item, width)
    if role == "assistant":
        return _assistant_element(c, item, width)
    if role == "card":
        return _card_element(c, item, width)
    if role == "question":
        return _question_element(c, item, width)
    if role == "picture":
        return _picture_element(c, item, width)
    if role == "error":
        return _error_element(c, item, width)
    if role == "memory":
        verb = "Remembered" if item.get("action") == "saved" else "Forgot"
        return _note_element(c, f"{verb}: {item.get('text', '')}", width, glyph="★")
    if item.get("text"):
        return _note_element(c, item["text"], width)
    return None


def _user_element(c, item, width):
    P, Z = c.P, c.Z
    indent = Z(28)
    inner = width - indent - 2 * Z.pad
    lines = _wrapped(item.get("text") or "", inner, Z.font)
    pictures, others = [], []
    for name in item.get("attachments") or []:
        path = _attachment_file(name)
        found = paint.picture(path) if path else None
        if found is not None:
            pictures.append((path, found))
        else:
            others.append(str(name).replace("attached:", ""))
    tags = [mentions.label(t) for t in (item.get("mentions") or [])] + others
    tag_lines = _wrapped("@ " + ", ".join(tags), inner, Z.small) if tags else []
    tile_h, tile_gap = Z(84), Z(4)
    tiles = _tile_rows(pictures, inner, tile_h, tile_gap)
    tiles_h = len(tiles) * (tile_h + tile_gap)
    height = Z.pad * 2 + (len(lines) * Z.line if item.get("text") else 0) + tiles_h + len(tag_lines) * Z.small_line
    key = f"copy:{item['id']}"

    def draw(c, x, top, w):
        bx, bw = x + indent, w - indent
        paint.rect(bx, top - height, bw, height, P.user, Z.radius * 2)
        cy = top - Z.pad
        if item.get("text"):
            for line in lines:
                c.text(bx + Z.pad, cy, line, P.text)
                cy -= Z.line
        c.hit(bx, top - height, bw, height, key, act("agent_copy", item["id"]), kind="copy")
        for row in tiles:
            cx = bx + Z.pad
            for (path, (texture, _iw, _ih)), w in row:
                paint.image(texture, cx, cy - tile_h, w, tile_h)
                paint.outline(cx, cy - tile_h, w, tile_h, P.card_line)
                c.hit(cx, cy - tile_h, w, tile_h, f"picture:{item['id']}:{path}", act("agent_picture", path))
                cx += w + tile_gap
            cy -= tile_h + tile_gap
        for line in tag_lines:
            c.text(bx + Z.pad, cy, line, P.muted, size=Z.small, line=Z.small_line)
            cy -= Z.small_line
        if c.hovered(key):
            label = "Copy"
            lw = paint.measure(label, Z.small)
            c.text(bx + bw - lw - Z.pad, top - Z(2), label, P.muted, size=Z.small, line=Z.small_line)
    return Element(height, draw)


def _markdown_blocks(text, width, Z):
    """[(kind, lines)] for a reply: paragraphs, headings, bullets and code, each already wrapped."""
    blocks = []
    code = None
    for raw in (text or "").split("\n"):
        line = raw.rstrip()
        if line.lstrip().startswith("```"):
            if code is None:
                code = []
            else:
                blocks.append(("code", code))
                code = None
            continue
        if code is not None:
            code.append(line)
            continue
        plain = line.replace("**", "").replace("__", "").replace("`", "")
        if not plain.strip():
            if blocks and blocks[-1][0] != "gap":
                blocks.append(("gap", []))
        elif plain.lstrip().startswith("#"):
            blocks.append(("heading", _wrapped(plain.lstrip("# ").strip(), width, Z.title)))
        elif plain.lstrip()[:2] in ("- ", "* ", "• ") or (plain.lstrip()[:1].isdigit() and ". " in plain[:5]):
            body = plain.lstrip()
            mark, rest = ("•", body[2:]) if body[:2] in ("- ", "* ", "• ") else body.split(" ", 1)
            blocks.append(("bullet", [mark] + _wrapped(rest.strip(), width - Z(14), Z.font)))
        else:
            blocks.append(("text", _wrapped(plain.strip(), width, Z.font)))
    if code is not None:
        blocks.append(("code", code))
    while blocks and blocks[-1][0] == "gap":
        blocks.pop()
    return blocks


def _blocks_height(blocks, Z):
    h = 0
    for kind, lines in blocks:
        if kind == "gap":
            h += Z.gap
        elif kind == "heading":
            h += len(lines) * Z.line + Z(2)
        elif kind == "bullet":
            h += (len(lines) - 1) * Z.line
        elif kind == "code":
            h += min(len(lines), 40) * Z.small_line + 2 * Z.gap
        else:
            h += len(lines) * Z.line
    return h


def _draw_blocks(c, blocks, x, top, w):
    P, Z = c.P, c.Z
    cy = top
    mono = paint.mono()
    for kind, lines in blocks:
        if kind == "gap":
            cy -= Z.gap
        elif kind == "heading":
            for line in lines:
                c.text(x, cy, line, P.text, size=Z.title)
                cy -= Z.line
            cy -= Z(2)
        elif kind == "bullet":
            c.text(x + Z(2), cy, lines[0], P.muted)
            for line in lines[1:]:
                c.text(x + Z(14), cy, line, P.text)
                cy -= Z.line
        elif kind == "code":
            shown = lines[:40]
            h = len(shown) * Z.small_line + 2 * Z.gap
            paint.rect(x, cy - h, w, h, P.code, Z.radius)
            ly = cy - Z.gap
            for line in shown:
                c.text(x + Z.gap, ly, paint.fit(line, w - 2 * Z.gap, Z.small, mono), P.text, size=Z.small,
                       line=Z.small_line, font=mono)
                ly -= Z.small_line
            cy -= h
        else:
            for line in lines:
                c.text(x, cy, line, P.text)
                cy -= Z.line
    return cy


def _assistant_element(c, item, width):
    P, Z = c.P, c.Z
    blocks = _markdown_blocks(item.get("text") or "", width, Z)
    body = _blocks_height(blocks, Z)
    height = Z.small_line + Z(2) + body + Z.small_line + Z(2)
    key = f"copy:{item['id']}"

    def draw(c, x, top, w):
        c.text(x, top, "Zetta Agent", P.faint, size=Z.small, line=Z.small_line)
        _draw_blocks(c, blocks, x, top - Z.small_line - Z(2), w)
        c.button(x - Z.gap, top - height, Z(52), Z.small_line + Z(2), "Copy", key, act("agent_copy", item["id"]),
                 style="link", size=Z.small, align="left")
    return Element(height, draw)


STEP_GLYPH = {"running": "⋯", "done": "✓", "error": "⚠", "declined": "×", "stopped": "■"}


def _steps_element(c, steps, width):
    P, Z = c.P, c.Z
    key = "steps:" + steps[0].get("id", "")
    opened = key in ui["open"]
    running = [s for s in steps if s.get("status") == "running"]
    failed = [s for s in steps if s.get("status") == "error"]
    if running:
        summary = "Working · " + (running[-1].get("label") or running[-1].get("name") or "a step")
        glyph, colour = "⋯", P.muted
    else:
        summary = f"{len(steps)} step{'s' if len(steps) != 1 else ''}" + (f" · {len(failed)} failed" if failed
                                                                         else "")
        glyph, colour = ("⚠", P.warning) if failed else ("✓", P.success)
    rows = [("head", None, None)]
    if failed and not opened:  # why it failed, without having to open it
        reason = str(failed[-1].get("detail") or "").strip().split("\n")[0]
        if reason:
            rows.append(("reason", failed[-1], _wrapped(reason, width - Z(30), Z.small)[:3]))
    if opened:
        for step in steps:
            rows.append(("step", step, None))
            if step.get("id") in ui["open"]:
                detail = []
                if step.get("input"):
                    detail.extend(_wrapped(str(step["input"])[:600], width - Z(30), Z.small))
                if step.get("detail"):
                    for line in str(step["detail"]).split("\n")[:STEP_DETAIL_LINES]:
                        detail.extend(_wrapped(line, width - Z(30), Z.small))
                rows.append(("detail", step, detail[:60]))
    height = 0
    for kind, _step, detail in rows:
        if kind == "detail":
            height += len(detail) * Z.small_line + Z.gap
        elif kind == "reason":
            height += len(detail) * Z.small_line + Z(2)
        else:
            height += Z.button

    def draw(c, x, top, w):
        cy = top
        for kind, step, detail in rows:
            if kind == "head":
                arrow = "▾" if opened else "▸"
                c.button(x - Z(4), cy - Z.button, w + Z(4), Z.button, "", key, toggle(key), style="ghost")
                c.text(x, cy, arrow, P.muted, line=Z.button)
                c.text(x + Z(14), cy, glyph, colour, line=Z.button)
                c.text(x + Z(30), cy, paint.fit(summary, w - Z(34), Z.font), P.muted, line=Z.button)
                cy -= Z.button
            elif kind == "step":
                status = step.get("status")
                label = step.get("label") or step.get("name") or "Step"
                skey = step.get("id", "")
                c.button(x + Z(10), cy - Z.button, w - Z(10), Z.button, "", f"step:{skey}", toggle(skey),
                         style="ghost")
                tone = P.success if status == "done" else P.warning if status == "error" else P.muted
                c.text(x + Z(16), cy, STEP_GLYPH.get(status, "•"), tone, line=Z.button)
                c.text(x + Z(32), cy, paint.fit(label, w - Z(36), Z.font), P.text, line=Z.button)
                cy -= Z.button
            elif kind == "reason":
                for line in detail:
                    c.text(x + Z(30), cy, line, P.warning, size=Z.small, line=Z.small_line)
                    cy -= Z.small_line
                cy -= Z(2)
            else:
                h = len(detail) * Z.small_line + Z.gap
                paint.rect(x + Z(28), cy - h, w - Z(28), h, P.code, Z.radius)
                ly = cy - Z.gap / 2
                for line in detail:
                    c.text(x + Z(34), ly, line, P.muted, size=Z.small, line=Z.small_line)
                    ly -= Z.small_line
                cy -= h
    return Element(height, draw)


def _question_element(c, item, width):
    P, Z = c.P, c.Z
    lines = _wrapped(item.get("text") or "", width, Z.font)
    answered = item.get("answered")
    options = [] if answered else (item.get("options") or [])
    height = len(lines) * Z.line + Z.gap + (Z.line if answered else len(options) * (Z.button + Z(3)))

    def draw(c, x, top, w):
        cy = top
        for line in lines:
            c.text(x, cy, line, P.text)
            cy -= Z.line
        cy -= Z.gap
        if answered:
            c.text(x, cy, f"✓ You chose: {answered}", P.muted)
            return
        for option in options:
            label = option.get("label", "?")
            c.button(x, cy - Z.button, w, Z.button, label, f"answer:{item['id']}:{label}",
                     act("agent_answer", item["id"], label), align="left")
            cy -= Z.button + Z(3)
    return Element(height, draw)


def _picture_element(c, item, width):
    P, Z = c.P, c.Z
    found = paint.picture(item.get("path") or "")
    if found is None:
        return None
    texture, iw, ih = found
    w = min(width, Z(240))
    h = w * ih / iw if iw else w * 0.6
    if h > Z(200):
        h = Z(200)
        w = h * iw / ih if ih else w
    key = f"picture:{item['id']}"

    def draw(c, x, top, width_):
        paint.image(texture, x, top - h, w, h)
        paint.outline(x, top - h, w, h, P.card_line)
        c.hit(x, top - h, w, h, key, act("agent_picture", item.get("path", "")))
        if c.hovered(key):
            label = "Open in Image Editor"
            lw = paint.measure(label, Z.small) + 2 * Z.gap
            paint.rect(x + Z.gap, top - h + Z.gap, lw, Z.small_line + Z(4), fade(P.bg, 0.85), Z.radius)
            c.text(x + 2 * Z.gap, top - h + Z.gap + Z.small_line + Z(4), label, P.text, size=Z.small,
                   line=Z.small_line + Z(4))
    return Element(h, draw)


def _error_element(c, item, width):
    P, Z = c.P, c.Z
    lines = _wrapped(item.get("text") or "", width - 2 * Z.pad - Z(16), Z.font)
    settings = item.get("code") in classic.KEY_CODES
    height = 2 * Z.pad + len(lines) * Z.line + ((Z.button + Z.gap) if settings else 0)

    def draw(c, x, top, w):
        paint.rect(x, top - height, w, height, P.danger_bg, Z.radius * 2)
        c.text(x + Z.pad, top - Z.pad, "⚠", P.danger)
        cy = top - Z.pad
        for line in lines:
            c.text(x + Z.pad + Z(16), cy, line, P.text)
            cy -= Z.line
        if settings:
            c.button(x + Z.pad, cy - Z.gap - Z.button, Z(120), Z.button, "Open settings", f"settings:{item['id']}",
                     act("agent_settings"))
    return Element(height, draw)


# ─── Cards: what waits on the user ───────────────────────────────────────────────────────────────


def _card_open(item):
    return runtime.card is not None and runtime.card.get("item") is item


def _card_element(c, item, width):
    P, Z = c.P, c.Z
    payload = item.get("payload") or {}
    if not _card_open(item):
        status = item.get("status") or ""
        text = f"{payload.get('title', 'Card')}" + (f" — {status}" if status else "")
        return _note_element(c, text, width, glyph="✓" if status in ("allowed", "answered", "done") else "•")
    kind = item.get("kind")
    inner = width - 2 * Z.pad
    parts = []  # (height, draw(c, x, top, w))

    def add_text(s, colour=None, size=None, glyph=None):
        size = size or Z.font
        line_h = Z.line if size == Z.font else Z.small_line if size == Z.small else Z.line
        indent = Z(16) if glyph else 0
        lines = _wrapped(s, inner - indent, size)

        def draw(c, x, top, w):
            if glyph:
                c.text(x, top, glyph, colour or P.muted, size=size, line=line_h)
            for n, line in enumerate(lines):
                c.text(x + indent, top - n * line_h, line, colour or P.text, size=size, line=line_h)
        parts.append((len(lines) * line_h, draw))

    def add_buttons(buttons):
        def draw(c, x, top, w):
            each = (w - Z(4) * (len(buttons) - 1)) / len(buttons)
            for n, (label, key, fn, style) in enumerate(buttons):
                c.button(x + n * (each + Z(4)), top - Z.button, each, Z.button, label, key, fn, style=style)
        parts.append((Z.button, draw))

    def add_row(label, key, fn, glyph=None, selected=False):
        def draw(c, x, top, w):
            c.button(x - Z(4), top - Z.button, w + Z(4), Z.button, "", key, fn, style="ghost")
            if glyph:
                c.text(x, top, glyph, P.accent if selected else P.muted, line=Z.button)
            c.text(x + (Z(18) if glyph else 0), top, paint.fit(label, w - Z(22), Z.font), P.text, line=Z.button)
        parts.append((Z.button, draw))

    title = payload.get("title") or "Needs your OK"
    danger = kind == "approve" and payload.get("button") == "Delete"
    add_text(title, P.text, Z.title)
    if kind == "approve":
        for reason in payload.get("reasons") or []:
            add_text(reason, P.muted, Z.small, glyph="ⓘ")
        summary = (payload.get("summary") or "").split("\n")
        code_key = f"code:{item['id']}"
        shown = summary if code_key in ui["open"] else summary[:CODE_LINES]
        mono = paint.mono()

        def draw_code(c, x, top, w, shown=shown):
            h = len(shown) * Z.small_line + 2 * Z.gap
            paint.rect(x, top - h, w, h, P.code, Z.radius)
            ly = top - Z.gap
            for line in shown:
                c.text(x + Z.gap, ly, paint.fit(line, w - 2 * Z.gap, Z.small, mono), P.text, size=Z.small,
                       line=Z.small_line, font=mono)
                ly -= Z.small_line
        if any(line.strip() for line in summary):
            parts.append((len(shown) * Z.small_line + 2 * Z.gap, draw_code))
            if len(summary) > CODE_LINES:
                more = "Show less" if code_key in ui["open"] else f"Show all {len(summary)} lines"
                parts.append((Z.button, lambda c, x, top, w: c.button(x - Z(4), top - Z.button, Z(140), Z.button,
                                                                      more, code_key, toggle(code_key),
                                                                      style="link", align="left")))
        if payload.get("copy"):
            if ui.get("copy") is None:
                ui["copy"] = bool(payload.get("copy_ticked"))
            add_row("Keep a copy of the file first", "card:copy", act("agent_card_copy"),
                    glyph="☑" if ui["copy"] else "☐", selected=ui["copy"])
            if payload.get("copy_note"):
                add_text(payload["copy_note"], P.muted, Z.small)
        add_buttons([(payload.get("button") or "Allow", "card:allow", act("agent_card", "allow"),
                      "danger" if danger else "accent"),
                     ("Decline", "card:decline", act("agent_card", "decline"), "normal")])
    elif kind == "generate":
        for line in payload.get("lines") or []:
            add_text(line, P.muted)
        if payload.get("credits") is not None:
            add_text(f"{payload['credits']:,} credits · you have {auth.credits():,}", P.text, glyph="●")
        if payload.get("alternative") == "blender":
            add_buttons([(payload.get("button") or "Generate", "card:allow", act("agent_card", "allow"), "accent")])
            add_buttons([("Build in Blender", "card:blender", act("agent_card", "blender"), "normal"),
                         ("Not now", "card:decline", act("agent_card", "decline"), "normal")])
            add_text("Build in Blender spends no credits: the agent models it with Blender's own tools.", P.muted,
                     Z.small)
        else:
            add_buttons([(payload.get("button") or "Generate", "card:allow", act("agent_card", "allow"), "accent"),
                         ("Not now", "card:decline", act("agent_card", "decline"), "normal")])
    elif kind == "ask_user":
        for question in payload.get("questions") or []:
            qid = question["id"]
            add_text(question.get("question") or "", P.text)
            answer = ui["answers"].get(qid)
            if question.get("kind") == "choice":
                for option in question.get("options") or []:
                    label = option.get("label", "?")
                    chosen = label in answer if isinstance(answer, list) else answer == label
                    glyph = ("☑" if chosen else "☐") if question.get("multiple") else ("●" if chosen else "○")
                    add_row(label, f"choose:{qid}:{label}", act("agent_choose", qid, label), glyph=glyph,
                            selected=chosen)
                if question.get("allowOther", True):
                    add_row("Something else…", f"other:{qid}", call("zetta.agent_card_text", question=qid),
                            glyph="✎")
            elif question.get("kind") == "object":
                picked = ", ".join(answer) if isinstance(answer, list) else (answer or "")
                add_buttons([(picked or "Pick…", f"pick:{qid}", call("zetta.agent_mention", question=qid),
                              "normal"),
                             ("Use selection", f"usesel:{qid}", act("agent_use_selection", qid), "normal")])
                if question.get("allowOther", True):
                    add_row("Describe it instead", f"other:{qid}", call("zetta.agent_card_text", question=qid),
                            glyph="✎")
            else:
                add_row(str(answer) if answer else (question.get("placeholder") or "Write your answer…"),
                        f"other:{qid}", call("zetta.agent_card_text", question=qid), glyph="✎")
        ready = all(ui["answers"].get(q["id"]) not in (None, "", []) or q.get("optional")
                    for q in payload.get("questions") or [])
        add_buttons([("OK", "card:ok", act("agent_card", "ok") if ready else (lambda _c: None),
                      "accent" if ready else "normal"),
                     ("Skip", "card:skip", act("agent_card", "skip"), "normal")])
    height = 2 * Z.pad + sum(h for h, _d in parts) + Z.gap * (len(parts) - 1)

    def draw(c, x, top, w):
        paint.rect(x, top - height, w, height, P.card, Z.radius * 2)
        paint.outline(x, top - height, w, height, P.danger if danger else P.accent, Z.radius * 2,
                      width=max(1, Z.pixel))
        cy = top - Z.pad
        for h, part in parts:
            part(c, x + Z.pad, cy, w - 2 * Z.pad)
            cy -= h + Z.gap
    return Element(height, draw)


def _changes_element(c, width):
    P, Z = c.P, c.Z
    turn = changes.current()
    if turn is None or runtime.busy() or not turn["lines"]:
        return None
    open_lines = [line for line in turn["lines"] if line["state"] == "done"]
    if not open_lines:
        return None
    key = f"changes:{turn['id']}"
    opened = key in ui["open"]
    listed = open_lines[:12] if opened else []
    has_copy = bool(turn.get("copy"))
    height = 2 * Z.pad + Z.button + len(listed) * Z.small_line + (Z.gap + Z.button if has_copy else 0)
    if listed:
        height += Z.gap
    glyphs = {"scene": "●", "file": "▪", "copy": "▣", "note": "ⓘ"}

    def draw(c, x, top, w):
        paint.rect(x, top - height, w, height, P.card, Z.radius * 2)
        cy = top - Z.pad
        bw = Z(58)
        label = f"{len(open_lines)} change{'s' if len(open_lines) != 1 else ''}"
        head_w = w - 2 * Z.pad - 2 * bw - Z(4)
        c.button(x + Z(4), cy - Z.button, head_w, Z.button, "", key, toggle(key), style="ghost")
        c.text(x + Z.pad, cy, "▾" if opened else "▸", P.muted, line=Z.button)
        c.text(x + Z.pad + Z(14), cy, paint.fit(label, head_w - Z(20), Z.font), P.text, line=Z.button)
        bx = x + w - Z.pad - 2 * bw - Z(4)
        c.button(bx, cy - Z.button, bw, Z.button, "Keep", f"keep:{turn['id']}", act("agent_keep_turn", turn["id"]))
        c.button(bx + bw + Z(4), cy - Z.button, bw, Z.button, "Undo", f"undo:{turn['id']}",
                 act("agent_undo_turn", turn["id"], confirm="Undo everything the last message changed?"))
        cy -= Z.button
        if listed:
            cy -= Z.gap
            for line in listed:
                c.text(x + Z.pad + Z(14), cy, glyphs.get(line["kind"], "•"), P.faint, size=Z.small,
                       line=Z.small_line)
                c.text(x + Z.pad + Z(28), cy, paint.fit(line.get("label", ""), w - 2 * Z.pad - Z(30), Z.small),
                       P.muted, size=Z.small, line=Z.small_line)
                cy -= Z.small_line
        if has_copy:
            cy -= Z.gap
            c.button(x + Z.pad, cy - Z.button, w - 2 * Z.pad, Z.button, "Restore the file as it was",
                     f"restore:{turn['id']}",
                     act("agent_restore_copy", turn["id"],
                         confirm="Open the copy kept before this message in place of the current file? Changes "
                                 "since are lost."))
    return Element(height, draw)


# ─── History and memory ──────────────────────────────────────────────────────────────────────────


def _history_elements(c, width):
    P, Z = c.P, c.Z
    elements = []

    def new_chat(c, x, top, w):
        c.button(x, top - Z.button, w, Z.button, "+  New chat", "new", act("agent_new"), style="accent")
    elements.append(Element(Z.button, new_chat))
    history = runtime.history
    if history["loading"]:
        elements.append(_note_element(c, "Loading…", width, glyph="⋯"))
    if history["error"]:
        elements.append(_note_element(c, history["error"], width, glyph="⚠", color=P.danger))
    sessions = history["sessions"]
    if not sessions and not history["loading"]:
        elements.append(_note_element(c, "No conversations in this file yet.", width))
    for session in sessions:
        sid = session.get("id")
        title = session.get("title") or "Untitled"
        waiting = bool(session.get("awaiting"))

        def row(c, x, top, w, sid=sid, title=title, waiting=waiting):
            key = f"session:{sid}"
            c.button(x - Z(4), top - Z.button, w - Z(20), Z.button, "", key, act("agent_open_session", sid),
                     style="ghost")
            c.text(x, top, "⋯" if waiting else "▸", P.muted, line=Z.button)
            c.text(x + Z(14), top, paint.fit(title, w - Z(44), Z.font), P.text, line=Z.button)
            c.button(x + w - Z(22), top - Z.button, Z(22), Z.button, "×", f"delete:{sid}",
                     act("agent_delete_session", sid, confirm="Delete this conversation?"), style="link")
        elements.append(Element(Z.button, row))
    return elements


def _memory_elements(c, width):
    P, Z = c.P, c.Z
    memory = runtime.memory
    elements = []
    if memory["loading"]:
        elements.append(_note_element(c, "Loading…", width, glyph="⋯"))
    if memory["error"]:
        elements.append(_note_element(c, memory["error"], width, glyph="⚠", color=P.danger))
    brief = memory["brief"] or "None yet — the agent writes one once it understands the file."
    lines = _wrapped(brief, width - 2 * Z.pad, Z.font)

    def draw_brief(c, x, top, w):
        h = 2 * Z.pad + Z.small_line + Z(2) + len(lines) * Z.line
        paint.rect(x, top - h, w, h, P.card, Z.radius * 2)
        c.text(x + Z.pad, top - Z.pad, "BRIEF FOR THIS FILE", P.faint, size=Z.small, line=Z.small_line)
        cy = top - Z.pad - Z.small_line - Z(2)
        for line in lines:
            c.text(x + Z.pad, cy, line, P.text if memory["brief"] else P.muted)
            cy -= Z.line
    elements.append(Element(2 * Z.pad + Z.small_line + Z(2) + len(lines) * Z.line, draw_brief))
    for scope, title in (("global", "EVERYWHERE"), ("project", "THIS FILE")):
        def head(c, x, top, w, title=title):
            c.text(x, top, title, P.faint, size=Z.small, line=Z.small_line)
        elements.append(Element(Z.small_line, head))
        rows = [m for m in memory["memories"] if m.get("scope") == scope]
        if not rows:
            elements.append(_note_element(c, "Nothing yet.", width))
        for entry in rows:
            entry_lines = _wrapped(entry.get("text", ""), width - Z(40), Z.font)

            def row(c, x, top, w, entry=entry, entry_lines=entry_lines):
                c.text(x, top, "•", P.muted)
                for n, line in enumerate(entry_lines):
                    c.text(x + Z(12), top - n * Z.line, line, P.text)
                c.button(x + w - Z(22), top - Z.button, Z(22), Z.button, "×", f"forget:{entry.get('id')}",
                         act("agent_forget", entry.get("id"), confirm="Forget this?"), style="link")
            elements.append(Element(max(Z.button, len(entry_lines) * Z.line), row))
    return elements


# ─── The composer: always at the bottom ──────────────────────────────────────────────────────────


def _settings_label(context):
    props = classic.state(context)
    parts = []
    own = runtime.own_model()
    try:
        if own:
            for key, label, *_rest in classic._model_items(props, context):
                if key == props.model:
                    parts.append(label)
        else:
            for key, label, *_rest in classic._tier_items(props, context):
                if key == props.tier:
                    parts.append(label)
        for key, label, *_rest in classic._thinking_items(props, context):
            if key == props.thinking and key != "auto":
                parts.append(label)
    except (AttributeError, TypeError, ValueError):
        pass
    if props.approval != "confirm_destructive":
        parts.append("Full autonomy")
    return " · ".join(p for p in parts if p) or "Settings"


def _thumbnails():
    """[(index, path, (texture, w, h))] for the pictures attached to the message being written."""
    found = []
    for index, entry in enumerate(ui["images"]):
        path = _attachment_file(entry.get("name"))
        picture = paint.picture(path) if path else None
        if picture is not None:
            found.append((index, path, picture))
    return found


def _chips():
    shown = {index for index, _path, _picture in _thumbnails()}
    chips = [("mentions", i, "@" + mentions.label(m)) for i, m in enumerate(ui["mentions"])]
    chips += [("images", i, e.get("name", "picture").replace("attached:", "")) for i, e in enumerate(ui["images"])
              if i not in shown]
    for kind in ("documents", "texts", "files"):
        for i, entry in enumerate(ui[kind]):
            name = entry.get("name", "file")
            chips.append((kind, i, name.replace("attached:", "")))
    return chips


def _field_layout(c, width):
    """(lines, visible line ranges, height) of the message box's text at this width."""
    Z = c.Z
    tw = width - 2 * Z.pad
    lines = textflow.wrap(box.text, tw, lambda s: paint.measure(s, Z.font))
    shown = min(len(lines), FIELD_LINES)
    return lines, max(1, shown) * Z.line + 2 * Z(5)


def _draw_composer(c, context, x, w, paint_it=False):
    """Lays out (and with paint_it, draws) the composer from the bottom up; returns its top edge."""
    P, Z = c.P, c.Z
    props = classic.state(context)
    inner_x, inner_w = x + Z.pad, w - 2 * Z.pad
    y = Z.pad
    # The bottom row: +, @, Agent | Ask, settings, Send (two rows when narrow).
    row_y = y
    y += Z.button + Z.gap
    if inner_w < 2 * Z.button + Z(3) + Z(6) + 2 * Z(44) + Z(6) + Z(70) + Z(6) + Z(60):
        y += Z.button + Z(4)
    lines, field_h = _field_layout(c, inner_w)
    field_y = y
    y += field_h
    thumbs = _thumbnails()
    thumb_h, thumb_gap = Z(52), Z(4)
    thumb_rows = _tile_rows(thumbs, inner_w, thumb_h, thumb_gap)
    if thumb_rows:
        y += Z(4)
        thumbs_y = y
        y += len(thumb_rows) * (thumb_h + thumb_gap) - thumb_gap
    chips = _chips()
    chip_rows = []
    if chips:
        y += Z(4)
        row, used = [], 0
        for chip in chips:
            cw = min(inner_w, paint.measure(chip[2], Z.small) + Z(28))
            if row and used + cw > inner_w:
                chip_rows.append(row)
                row, used = [], 0
            row.append((chip, cw))
            used += cw + Z(4)
        chip_rows.append(row)
        chips_y = y
        y += len(chip_rows) * (Z.small_line + Z(6)) + Z(2)
    waiting = runtime.card is not None and runtime.phase() == runtime.WAITING
    if waiting:
        banner_y = y + Z.gap
        y = banner_y + Z.button
    notice = ui.get("notice")
    notice_lines = _wrapped(notice, inner_w - Z(20), Z.small) if notice else []
    if notice_lines:
        notice_y = y + Z.gap
        y = notice_y + len(notice_lines) * Z.small_line
    top = y + Z.pad
    if not paint_it:
        return top

    paint.rect(x, 0, w, top, P.bg)
    paint.line(x, top - 0.5, x + w, top - 0.5, P.card_line)
    # Notice
    if notice_lines:
        ny = notice_y + len(notice_lines) * Z.small_line
        c.text(inner_x, ny, "ⓘ", P.muted, size=Z.small, line=Z.small_line)
        for n, line in enumerate(notice_lines):
            c.text(inner_x + Z(14), ny - n * Z.small_line, line, P.muted, size=Z.small, line=Z.small_line)
        c.button(inner_x + inner_w - Z(18), ny - Z.small_line, Z(18), Z.small_line, "×", "notice:x",
                 _dismiss_notice, style="link", size=Z.small)
    # Waiting card banner
    if waiting:
        kind = runtime.card.get("kind")
        text = {"approve": "The agent is waiting for your OK", "generate": "Generate? The agent is waiting",
                "ask_user": "The agent asked you something"}.get(kind, "The agent is waiting for you")
        paint.rect(inner_x, banner_y, inner_w, Z.button, mix(P.bg, P.accent, 0.25), Z.radius)
        c.text(inner_x + Z.gap, banner_y + Z.button, "↑", P.accent_text if P.dark else P.text, line=Z.button)
        c.text(inner_x + Z.gap + Z(14), banner_y + Z.button, paint.fit(text, inner_w - Z(30), Z.small), P.text,
               size=Z.small, line=Z.button)

        def show_card(_context):
            follow_all()
        c.hit(inner_x, banner_y, inner_w, Z.button, "banner", show_card)
    # Chips
    if chips:
        cy = chips_y + len(chip_rows) * (Z.small_line + Z(6))
        for row in chip_rows:
            cx = inner_x
            for (kind, index, label), cw in row:
                h = Z.small_line + Z(4)
                paint.rect(cx, cy - h, cw, h, P.button, h / 2)
                c.text(cx + Z(8), cy, paint.fit(label, cw - Z(26), Z.small), P.button_text, size=Z.small, line=h)
                c.button(cx + cw - Z(18), cy - h, Z(16), h, "×", f"chip:{kind}:{index}", _unmention(kind, index),
                         style="link", size=Z.small)
                cx += cw + Z(4)
            cy -= Z.small_line + Z(6)
    # Attached pictures
    if thumb_rows:
        ty = thumbs_y + len(thumb_rows) * (thumb_h + thumb_gap) - thumb_gap
        for row in thumb_rows:
            tx = inner_x
            for (index, path, (texture, _iw, _ih)), tw in row:
                paint.image(texture, tx, ty - thumb_h, tw, thumb_h)
                paint.outline(tx, ty - thumb_h, tw, thumb_h, P.card_line, Z.radius)
                c.hit(tx, ty - thumb_h, tw, thumb_h, f"thumb:{index}", act("agent_picture", path))
                r = Z(9)
                cx, cy = tx + tw - r - Z(3), ty - r - Z(3)
                paint.rect(cx - r, cy - r, 2 * r, 2 * r, fade(P.bg, 0.85), r)
                c.button(cx - r, cy - r, 2 * r, 2 * r, "×", f"chip:images:{index}", _unmention("images", index),
                         style="link", size=Z.small)
                tx += tw + thumb_gap
            ty -= thumb_h + thumb_gap
    # The message box
    _draw_field(c, context, inner_x, field_y, inner_w, field_h, lines)
    # Bottom rows: + and @ on the left, Send on the right; Agent | Ask and the settings between them, or on a row
    # of their own above when the sidebar is narrow.
    h = Z.button
    send_w = Z(60)
    seg = Z(44)
    two_rows = inner_w < 2 * h + Z(3) + Z(6) + 2 * seg + Z(6) + Z(70) + Z(6) + send_w
    bx = inner_x
    c.button(bx, row_y, h, h, "+", "plus", call("wm.call_menu", name="ZETTA_MT_agent_add"))
    bx += h + Z(3)
    c.button(bx, row_y, h, h, "@", "at", _start_mention)
    bx += h + Z(6)
    send_x = inner_x + inner_w - send_w
    mode_y = row_y
    if two_rows:
        mode_y = row_y + h + Z(4)
        bx = inner_x
    paint.rect(bx, mode_y, 2 * seg, h, P.field, Z.radius)
    c.button(bx + 1, mode_y + 1, seg - 1, h - 2, "Agent", "mode:agent", _set("mode", "agent"), style="ghost",
             selected=props.mode == "agent", size=Z.small)
    c.button(bx + seg, mode_y + 1, seg - 1, h - 2, "Ask", "mode:ask", _set("mode", "ask"), style="ghost",
             selected=props.mode == "ask", size=Z.small)
    bx += 2 * seg + Z(6)
    settings_end = inner_x + inner_w if two_rows else send_x - Z(6)
    if settings_end - bx > Z(36):
        c.button(bx, mode_y, settings_end - bx, h, _settings_label(context), "settings",
                 call("wm.call_panel", name="ZETTA_PT_agent_settings", keep_open=True), style="ghost",
                 size=Z.small, align="left", suffix="▾")
    busy = runtime.busy() and runtime.phase() != runtime.WAITING
    if busy:
        c.button(send_x, row_y, send_w, h, "Stop", "stop", _stop, style="normal", size=Z.small, suffix="■")
    else:
        ready = bool(box.text.strip()) or any(ui[k] for k in ("images", "documents", "texts", "files"))
        c.button(send_x, row_y, send_w, h, "Send", "send", _send, style="accent" if ready else "normal",
                 size=Z.small, suffix="➤")
    return top


def _mention_spans(text):
    spans = []
    for name in state["picked"]:
        token = "@" + name
        start = text.find(token)
        while start != -1:
            spans.append((start, start + len(token)))
            start = text.find(token, start + 1)
    return spans


def _draw_field(c, context, x, y, w, h, lines):
    P, Z = c.P, c.Z
    focus = state["focus"]
    paint.rect(x, y, w, h, P.field, Z.radius + Z(1))
    paint.outline(x, y, w, h, P.accent if focus else P.field_line, Z.radius + Z(1),
                  width=max(1, Z.pixel) * (2 if focus else 1))
    tx, ty_top = x + Z.pad, y + h - Z(5)
    caret_line = textflow.line_of(lines, box.caret)
    first = state["field_first"]
    if caret_line < first:
        first = caret_line
    elif caret_line >= first + FIELD_LINES:
        first = caret_line - FIELD_LINES + 1
    first = max(0, min(first, max(0, len(lines) - FIELD_LINES)))
    state["field_first"] = first
    c.f.field = {"x": x, "y": y, "w": w, "h": h, "lines": lines, "first": first, "tx": tx, "top": ty_top}
    c.hit(x, y, w, h, "field", None, kind="field")
    if not box.text:
        hint = "Ask Zetta, or type @ to tag an object…"
        c.text(tx, ty_top, paint.fit(hint, w - 2 * Z.pad, Z.font), P.muted)
    span = box.selection()
    spans = _mention_spans(box.text)
    measure = lambda s: paint.measure(s, Z.font)  # noqa: E731
    with paint.clip(x + 1, y + 1, w - 2, h - 2):
        for n in range(first, min(len(lines), first + FIELD_LINES)):
            a, b = lines[n]
            top = ty_top - (n - first) * Z.line
            base_y = top - Z.line
            for s0, s1 in spans:  # picked tags read as tags
                lo, hi = max(a, s0), min(b, s1)
                if lo < hi:
                    sx = tx + measure(box.text[a:lo])
                    paint.rect(sx - Z(1), base_y + Z(2), measure(box.text[lo:hi]) + Z(2), Z.line - Z(4),
                               fade(P.accent, 0.35), Z(3))
            if span:
                lo, hi = max(a, span[0]), min(b, span[1])
                if lo < hi or (span[0] <= a and span[1] > b):
                    sx = tx + measure(box.text[a:lo])
                    sw = measure(box.text[lo:hi]) + (Z(4) if span[1] > b else 0)
                    paint.rect(sx, base_y + Z(1), max(sw, Z(2)), Z.line - Z(2), P.selection)
            c.text(tx, top, box.text[a:b], P.field_text)
        if focus and caret_line >= first and caret_line < first + FIELD_LINES:
            a, _b = lines[caret_line]
            cx = tx + measure(box.text[a:box.caret])
            top = ty_top - (caret_line - first) * Z.line
            paint.rect(cx, top - Z.line + Z(3), max(1, Z.pixel), Z.line - Z(6), P.field_text)
    if len(lines) > FIELD_LINES:  # a thin bar says there is more above or below
        track = h - 2 * Z(5)
        bar = max(Z(12), track * FIELD_LINES / len(lines))
        frac = first / (len(lines) - FIELD_LINES)
        paint.rect(x + w - Z.scrollbar - Z(3), y + Z(5) + (track - bar) * (1 - frac), Z.scrollbar, bar,
                   fade(P.field_text, 0.3), Z.scrollbar / 2)


# ─── The @ list ──────────────────────────────────────────────────────────────────────────────────


def popup_rows():
    if not state["focus"]:
        return []
    found = box.mention_query()
    if found is None or state["dismissed"] == found[0]:
        return []
    return mentions.find(found[1])


def _draw_popup(c, context):
    P, Z = c.P, c.Z
    field = c.f.field
    rows = popup_rows()
    c.f.popup = rows
    if field is None or not state["focus"]:
        return
    found = box.mention_query()
    if found is None or state["dismissed"] == found[0]:
        return
    x, w = field["x"], field["w"]
    row_h = Z.button
    head = Z.small_line + Z(4)
    h = head + max(1, len(rows)) * row_h + Z(4)
    y = field["y"] + field["h"] + Z(4)
    state["pick"] = max(0, min(state["pick"], len(rows) - 1))
    paint.rect(x + Z(1), y - Z(1), w, h, fade((0, 0, 0, 1), 0.25), Z.radius + Z(1))  # a soft edge
    paint.rect(x, y, w, h, P.menu, Z.radius + Z(1))
    paint.outline(x, y, w, h, P.card_line, Z.radius + Z(1))
    query = found[1]
    c.text(x + Z.pad, y + h - Z(2), f"Tag {('“' + query + '”') if query else 'something in the file'}", P.faint,
           size=Z.small, line=head)
    c.hit(x, y, w, h, "popup", lambda _c: None, kind="popup")
    if not rows:
        c.text(x + Z.pad, y + h - head, "Nothing matches", P.muted, line=row_h)
        return
    cy = y + h - head
    for n, (target, name, label) in enumerate(rows):
        key = f"popup:{n}"
        selected = n == state["pick"]
        if selected or c.hovered(key):
            paint.rect(x + Z(3), cy - row_h, w - Z(6), row_h, P.accent if selected else P.hover, Z.radius)
        ink = P.accent_text if selected else P.text
        lw = paint.measure(label, Z.small)
        c.text(x + Z.pad, cy, paint.fit(name, w - 3 * Z.pad - lw, Z.font), ink, line=row_h)
        c.text(x + w - Z.pad - lw, cy, label, ink if selected else P.muted, size=Z.small, line=row_h)
        c.hit(x, cy - row_h, w, row_h, key, _pick(target, name))
        cy -= row_h


# ─── Input ───────────────────────────────────────────────────────────────────────────────────────


def frame_under(window, wx, wy):
    """(region pointer, Frame) of the chat under the mouse — only a sidebar that still shows the chat."""
    if window is None or classic.classic_panel():
        return None, None
    window_key = window.as_pointer()
    for key, frame in list(frames.items()):
        if frame.window != window_key or not frame.inside(wx, wy):
            continue
        region = _live_region(window, key)
        if region is None or not tab_active(region):
            frames.pop(key, None)
            continue
        return key, frame
    return None, None


def _chat_on_screen(window):
    if window is None or classic.classic_panel():
        return False
    window_key = window.as_pointer()
    for key, frame in list(frames.items()):
        if frame.window == window_key:
            region = _live_region(window, key)
            if region is not None and tab_active(region) and classic.state().view == "chat":
                return True
    return False


def _live_region(window, key):
    screen = window.screen
    if screen is None:
        return None
    for area in screen.areas:
        if area.type != "VIEW_3D":
            continue
        for region in area.regions:
            if region.as_pointer() == key and region.width > 1:
                return region
    return None


def _caret_from(frame, x, y):
    field = frame.field
    if field is None:
        return box.caret
    lines = field["lines"]
    row = field["first"] + int((field["top"] - y) // Sizes().line)
    row = max(0, min(len(lines) - 1, row))
    Z = Sizes()
    return textflow.index_at(box.text, lines[row], x - field["tx"], lambda s: paint.measure(s, Z.font))


def _sync(context):
    props = classic.state(context)
    if props.input != box.text:
        props.input = box.text


def handle(context, event):
    """One event. Returns "consume", "pass" or "finish" (the event goes on to Blender and the modal ends)."""
    wx, wy = event.mouse_x, event.mouse_y
    key, frame = frame_under(context.window, wx, wy)
    kind = event.type

    if kind in {"TIMER", "TIMER_REPORT"} or kind.startswith("TIMER"):
        if frame is None and not state["focus"] and not state["drag"]:
            _set_hover(None)
            return "finish"
        return "pass"

    if kind in {"MOUSEMOVE", "INBETWEEN_MOUSEMOVE"}:
        if state["drag"] and frame is not None:
            x, y = frame.local(wx, wy)
            box.move_to(_caret_from(frame, x, y), select=True)
            redraw_chat()
            return "consume"
        if frame is None:
            _set_hover(None)
            return "pass" if state["focus"] else "finish"
        x, y = frame.local(wx, wy)
        hit = frame.hit_at(x, y)
        _set_hover(hit.key if hit and hit.kind in {"button", "copy"} or (hit and hit.key.startswith("popup:"))
                   else None)
        return "pass"

    if kind == "LEFTMOUSE":
        if event.value == "RELEASE":
            pressed, state["press"] = state["press"], None
            if state["drag"]:
                state["drag"] = False
                return "consume"
            if frame is None:
                return "pass"
            x, y = frame.local(wx, wy)
            hit = frame.hit_at(x, y)
            if pressed is None and hit is not None:
                # The press went to the panel underneath, just before this modal started: the release is the
                # whole click.
                _press(context, frame, x, y, event)
                state["drag"] = False
                pressed, state["press"] = state["press"], None
            if hit is not None and pressed == hit.key and hit.fn is not None:
                hit.fn(context)
                _sync(context)
                redraw_chat()
            return "consume"
        if event.value in {"PRESS", "DOUBLE_CLICK"}:
            if frame is None:
                if state["focus"]:
                    state["focus"] = False
                    redraw_chat()
                return "finish"
            x, y = frame.local(wx, wy)
            return _press(context, frame, x, y, event)
        return "consume" if frame is not None else "pass"

    if kind in {"WHEELUPMOUSE", "WHEELDOWNMOUSE", "TRACKPADPAN"} and frame is not None:
        x, y = frame.local(wx, wy)
        if kind == "TRACKPADPAN":
            dy = -(event.mouse_y - event.mouse_prev_y)
        else:
            dy = Sizes().line * 3 * (1 if kind == "WHEELUPMOUSE" else -1)
        cx, cy, cw, ch = frame.conv
        if cy <= y <= cy + ch:
            scroll_by(frame, scroll_key(key, context), dy)
            redraw_chat()
        return "consume"

    if kind in {"RIGHTMOUSE", "MIDDLEMOUSE"} and frame is not None:
        return "consume"

    if state["focus"] and not _chat_on_screen(context.window):
        state["focus"] = False  # the sidebar was closed or switched: the keyboard is Blender's again
        return "finish"
    if state["focus"]:
        verdict = _key(context, event)
        if verdict != "pass":
            _sync(context)
            redraw_chat()
        return verdict
    return "pass"


def _set_hover(key):
    if state["hover"] != key:
        state["hover"] = key
        redraw_chat()


def redraw_chat():
    """Redraws the sidebars showing the chat — not the whole 3D View, which can be slow to draw."""
    wm = bpy.context.window_manager
    for window in wm.windows:
        screen = window.screen
        if screen is None:
            continue
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            for region in area.regions:
                if region.type == "UI" and region.as_pointer() in frames:
                    region.tag_redraw()


def _press(context, frame, x, y, event):
    hit = frame.hit_at(x, y)
    if hit is None:
        if state["focus"]:
            state["focus"] = False
            redraw_chat()
        return "consume"
    if hit.kind == "field":
        state["focus"] = True
        state["dismissed"] = None
        index = _caret_from(frame, x, y)
        if event.value == "DOUBLE_CLICK":
            box.select_word(index)
        else:
            box.move_to(index, select=event.shift)
            state["drag"] = True
        redraw_chat()
        return "consume"
    keeps_focus = hit.key in {"at", "plus", "send", "popup"} or hit.key.startswith(("popup:", "chip:", "mode:"))
    if not keeps_focus and state["focus"]:
        state["focus"] = False
    state["press"] = hit.key
    redraw_chat()
    return "consume"


def _key(context, event):
    if event.value == "RELEASE":
        return "consume" if event.type not in {"LEFT_CTRL", "RIGHT_CTRL", "OSKEY", "LEFT_ALT", "RIGHT_ALT",
                                               "LEFT_SHIFT", "RIGHT_SHIFT"} else "pass"
    if event.value not in {"PRESS", "CLICK", "DOUBLE_CLICK"}:
        return "pass"
    kind = event.type
    cmd = event.oskey if MAC else event.ctrl
    word = event.alt if MAC else event.ctrl
    shift = event.shift
    rows = popup_rows()

    if rows:
        if kind in {"UP_ARROW", "DOWN_ARROW"}:
            state["pick"] = (state["pick"] + (1 if kind == "DOWN_ARROW" else -1)) % len(rows)
            return "consume"
        if kind in {"RET", "NUMPAD_ENTER", "TAB"}:
            target, name, _label = rows[max(0, min(state["pick"], len(rows) - 1))]
            _pick(target, name)(context)
            return "consume"
        if kind == "ESC":
            found = box.mention_query()
            state["dismissed"] = found[0] if found else None
            return "consume"

    if kind in {"RET", "NUMPAD_ENTER"}:
        if shift:
            box.insert("\n")
        else:
            send(context)
        return "consume"
    if kind == "ESC":
        state["focus"] = False
        return "consume"
    if kind == "BACK_SPACE":
        if cmd and not box.selection():
            box.move_to(box.text.rfind("\n", 0, box.caret) + 1, select=True)
        box.backspace(word=word)
        _reset_pick()
        return "consume"
    if kind == "DEL":
        box.delete(word=word)
        return "consume"
    if kind == "LEFT_ARROW":
        box.home(shift) if cmd else box.left(word, shift)
        return "consume"
    if kind == "RIGHT_ARROW":
        box.end(shift) if cmd else box.right(word, shift)
        return "consume"
    if kind in {"UP_ARROW", "DOWN_ARROW"}:
        _vertical(-1 if kind == "UP_ARROW" else 1, shift)
        return "consume"
    if kind == "HOME":
        box.home(shift)
        return "consume"
    if kind == "END":
        box.end(shift)
        return "consume"
    if cmd and kind == "A":
        box.select_all()
        return "consume"
    if cmd and kind == "C":
        if box.selected():
            context.window_manager.clipboard = box.selected()
        return "consume"
    if cmd and kind == "X":
        cut = box.cut()
        if cut:
            context.window_manager.clipboard = cut
        return "consume"
    if cmd and kind == "V":
        pasted = (context.window_manager.clipboard or "").replace("\r\n", "\n").replace("\r", "\n")
        if pasted:
            box.insert(pasted)
        return "consume"
    if cmd and kind == "Z":
        box.redo() if shift else box.undo()
        return "consume"
    if event.ctrl and kind == "Y" and not MAC:
        box.redo()
        return "consume"
    if cmd or (event.ctrl and MAC):
        return "pass"  # Blender's own shortcuts (save, quit, …)
    if kind == "TAB":
        return "consume"
    ch = event.unicode
    if ch and ch.isprintable():
        box.insert(ch)
        _reset_pick()
        return "consume"
    return "consume"


def _reset_pick():
    state["pick"] = 0
    found = box.mention_query()
    if found is None or state["dismissed"] != found[0]:
        state["dismissed"] = None


def _vertical(direction, select):
    field = next((f.field for f in frames.values() if f.field is not None), None)
    if field is None:
        return
    lines = field["lines"]
    row = textflow.line_of(lines, box.caret)
    target = row + direction
    if target < 0:
        box.move_to(0, select)
        return
    if target >= len(lines):
        box.move_to(len(box.text), select)
        return
    Z = Sizes()
    measure = lambda s: paint.measure(s, Z.font)  # noqa: E731
    x = measure(box.text[lines[row][0]:box.caret])
    box.move_to(textflow.index_at(box.text, lines[target], x, measure), select)


# ─── The event modal's lifetime ──────────────────────────────────────────────────────────────────


def ensure_events(window, area, region):
    """Starts the event modal for this window, if it is not running: from a timer, since a draw callback can't
    run operators."""
    key = window.as_pointer()
    if key in state["running"] or key in state["starting"]:
        return
    state["starting"].add(key)
    pointers = (key, area.as_pointer(), region.as_pointer())
    bpy.app.timers.register(lambda: _start_events(*pointers), first_interval=0.0)


def _start_events(window_key, area_key, region_key):
    state["starting"].discard(window_key)
    if window_key in state["running"]:
        return None
    wm = bpy.context.window_manager
    for window in wm.windows:
        if window.as_pointer() != window_key or window.screen is None:
            continue
        for area in window.screen.areas:
            if area.as_pointer() != area_key:
                continue
            for region in area.regions:
                if region.as_pointer() == region_key:
                    try:
                        with bpy.context.temp_override(window=window, area=area, region=region):
                            bpy.ops.zetta.agent_events("INVOKE_DEFAULT")
                    except (RuntimeError, TypeError):
                        pass
                    return None
    return None


def forget_frames():
    """Frames of regions that have not drawn for a while are gone (an area closed, a tab switched)."""
    now = time.time()
    for key in [k for k, f in frames.items() if now - f.drawn > 30]:
        frames.pop(key, None)
