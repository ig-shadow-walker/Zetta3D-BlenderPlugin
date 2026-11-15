"""The message box's text: caret, selection, word moves, clipboard and undo. Pure Python — the chat view turns
key presses into these calls, and the tests drive it directly."""

import re

_WORD = re.compile(r"\w", re.UNICODE)
MAX_UNDO = 100
MENTION_LIMIT = 48


class Editor:
    def __init__(self, text=""):
        self.text = text
        self.caret = len(text)
        self.anchor = None  # the other end of the selection, or None
        self._undo = []
        self._redo = []
        self._typing = False  # consecutive typing is one undo step

    # ─── Reading ─────────────────────────────────────────────────────────────────────────────────

    def selection(self):
        """(start, end), or None when nothing is selected."""
        if self.anchor is None or self.anchor == self.caret:
            return None
        return min(self.anchor, self.caret), max(self.anchor, self.caret)

    def selected(self):
        span = self.selection()
        return self.text[span[0]:span[1]] if span else ""

    def mention_query(self):
        """(start, query) while the caret is right after "@something" — the start of a word, no spaces — else
        None. The @ list shows the names that match the query."""
        if self.selection():
            return None
        i = self.caret - 1
        while i >= 0 and self.caret - i <= MENTION_LIMIT:
            ch = self.text[i]
            if ch == "@":
                if i == 0 or self.text[i - 1].isspace() or self.text[i - 1] in "([{\"'":
                    return i, self.text[i + 1:self.caret]
                return None
            if ch.isspace():
                return None
            i -= 1
        return None

    # ─── Editing ─────────────────────────────────────────────────────────────────────────────────

    def _remember(self, typing=False):
        if typing and self._typing:
            return
        self._undo.append((self.text, self.caret))
        del self._undo[:-MAX_UNDO]
        self._redo.clear()
        self._typing = typing

    def set_text(self, text):
        if text == self.text:
            return
        self._remember()
        self.text = text
        self.caret = len(text)
        self.anchor = None

    def clear(self):
        self.text, self.caret, self.anchor = "", 0, None
        self._undo.clear()
        self._redo.clear()
        self._typing = False

    def replace(self, start, end, s, typing=False):
        self._remember(typing)
        self.text = self.text[:start] + s + self.text[end:]
        self.caret = start + len(s)
        self.anchor = None

    def insert(self, s):
        span = self.selection()
        if span:
            self.replace(span[0], span[1], s)
        else:
            self.replace(self.caret, self.caret, s, typing=len(s) == 1 and not s.isspace())

    def backspace(self, word=False):
        span = self.selection()
        if span:
            self.replace(span[0], span[1], "")
        elif self.caret > 0:
            start = self._word_left(self.caret) if word else self.caret - 1
            self.replace(start, self.caret, "")

    def delete(self, word=False):
        span = self.selection()
        if span:
            self.replace(span[0], span[1], "")
        elif self.caret < len(self.text):
            end = self._word_right(self.caret) if word else self.caret + 1
            self.replace(self.caret, end, "")

    def undo(self):
        if self._undo:
            self._redo.append((self.text, self.caret))
            self.text, self.caret = self._undo.pop()
            self.anchor = None
            self._typing = False

    def redo(self):
        if self._redo:
            self._undo.append((self.text, self.caret))
            self.text, self.caret = self._redo.pop()
            self.anchor = None
            self._typing = False

    def cut(self):
        s = self.selected()
        if s:
            self.backspace()
        return s

    # ─── Moving ──────────────────────────────────────────────────────────────────────────────────

    def move_to(self, index, select=False):
        index = max(0, min(len(self.text), index))
        if select:
            if self.anchor is None:
                self.anchor = self.caret
        else:
            self.anchor = None
        self.caret = index
        self._typing = False

    def left(self, word=False, select=False):
        span = self.selection()
        if span and not select:
            self.move_to(span[0])
        else:
            self.move_to(self._word_left(self.caret) if word else self.caret - 1, select)

    def right(self, word=False, select=False):
        span = self.selection()
        if span and not select:
            self.move_to(span[1])
        else:
            self.move_to(self._word_right(self.caret) if word else self.caret + 1, select)

    def home(self, select=False):
        self.move_to(self.text.rfind("\n", 0, self.caret) + 1, select)

    def end(self, select=False):
        stop = self.text.find("\n", self.caret)
        self.move_to(len(self.text) if stop == -1 else stop, select)

    def select_all(self):
        self.anchor, self.caret = 0, len(self.text)

    def select_word(self, index):
        start, end = index, index
        while start > 0 and _WORD.match(self.text[start - 1]):
            start -= 1
        while end < len(self.text) and _WORD.match(self.text[end]):
            end += 1
        self.anchor, self.caret = start, end

    def _word_left(self, i):
        while i > 0 and not _WORD.match(self.text[i - 1]):
            i -= 1
        while i > 0 and _WORD.match(self.text[i - 1]):
            i -= 1
        return i

    def _word_right(self, i):
        n = len(self.text)
        while i < n and not _WORD.match(self.text[i]):
            i += 1
        while i < n and _WORD.match(self.text[i]):
            i += 1
        return i
