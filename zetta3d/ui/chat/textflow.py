"""Breaking text into lines that fit a width, given a function that measures a string.

Pure Python (the measuring function is passed in), so the tests run it without a window.
"""


def wrap(text, width, measure):
    """[(start, end)] index ranges of `text`, one per drawn line, breaking at spaces where it can and inside a
    word only when the word alone is wider than the line. A newline always starts a new line; the ranges never
    include it. An empty text is one empty line."""
    lines = []
    width = max(1.0, width)
    start = 0
    n = len(text)
    while True:
        newline = text.find("\n", start)
        stop = n if newline == -1 else newline
        _wrap_paragraph(text, start, stop, width, measure, lines)
        if newline == -1:
            break
        start = newline + 1
    return lines


def _wrap_paragraph(text, start, stop, width, measure, lines):
    if start == stop:
        lines.append((start, stop))
        return
    line_start = start
    i = start
    last_break = None  # index just after the last space that fits
    while i < stop:
        # The next word (with the spaces after it).
        j = i
        while j < stop and text[j] != " ":
            j += 1
        while j < stop and text[j] == " ":
            j += 1
        if measure(text[line_start:j].rstrip(" ")) <= width:
            last_break = j
            i = j
            continue
        if last_break is not None and last_break > line_start:
            lines.append((line_start, last_break))
            line_start = last_break
            last_break = None
            i = line_start
            continue
        # One word wider than the line: break it where it stops fitting.
        k = line_start + 1
        while k < j and measure(text[line_start:k + 1]) <= width:
            k += 1
        lines.append((line_start, k))
        line_start = k
        last_break = None
        i = k
    lines.append((line_start, stop))


def line_of(lines, index):
    """Which line the caret at `index` is on: at a wrap point it belongs to the next line."""
    for number, (start, end) in enumerate(lines):
        if index < start:
            return max(0, number - 1)
        if start <= index <= end:
            if index == end and number + 1 < len(lines) and lines[number + 1][0] == end:
                return number + 1
            return number
    return len(lines) - 1


def index_at(text, line, x, measure):
    """The caret index nearest to x on a line (start, end)."""
    start, end = line
    best, best_gap = start, abs(x)
    for i in range(start + 1, end + 1):
        gap = abs(measure(text[start:i]) - x)
        if gap < best_gap:
            best, best_gap = i, gap
        elif gap > best_gap:
            break
    return best
