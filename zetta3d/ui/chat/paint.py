"""Drawing primitives for the agent's chat: rectangles, rounded boxes, text and pictures, in region pixels.

Everything here draws with Blender's `gpu` and `blf` modules (Blender 4.2 and later), from a draw callback or
into an off-screen buffer for the tests. Coordinates have their origin at the bottom left, like Blender's.
"""

import os

import blf
import bpy
import gpu
from gpu_extras.batch import batch_for_shader

UI_FONT = 0
_mono = None
_shaders = {}
_textures = {}  # path → (image name, GPUTexture)


def _shader(name):
    if name not in _shaders:
        _shaders[name] = gpu.shader.from_builtin(name)
    return _shaders[name]


def begin():
    gpu.state.blend_set("ALPHA")


def end():
    gpu.state.blend_set("NONE")


# ─── Shapes ──────────────────────────────────────────────────────────────────────────────────────


def _corners(x, y, w, h, r, steps=4):
    """The outline of a rounded box, counter-clockwise from the bottom left."""
    import math

    r = max(0.0, min(r, w / 2, h / 2))
    if r < 1:
        return [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    points = []
    for cx, cy, start in ((x + w - r, y + r, -90), (x + w - r, y + h - r, 0), (x + r, y + h - r, 90),
                          (x + r, y + r, 180)):
        for i in range(steps + 1):
            angle = math.radians(start + 90 * i / steps)
            points.append((cx + r * math.cos(angle), cy + r * math.sin(angle)))
    return points


def rect(x, y, w, h, color, radius=0.0):
    gpu.state.blend_set("ALPHA")  # Blender's text drawing can leave blending off
    if w <= 0 or h <= 0 or color is None or color[3] <= 0:
        return
    points = _corners(x, y, w, h, radius)
    cx, cy = x + w / 2, y + h / 2
    tris = []
    for i in range(len(points)):
        tris.extend(((cx, cy), points[i], points[(i + 1) % len(points)]))
    shader = _shader("UNIFORM_COLOR")
    batch = batch_for_shader(shader, "TRIS", {"pos": tris})
    shader.uniform_float("color", color)
    batch.draw(shader)


def outline(x, y, w, h, color, radius=0.0, width=1.0):
    gpu.state.blend_set("ALPHA")  # Blender's text drawing can leave blending off
    if w <= 0 or h <= 0 or color is None or color[3] <= 0:
        return
    if width > 1.0:
        # A thick border is the box minus a smaller box: drawn as two fills, it stays crisp at every scale.
        inner = width
        points = _corners(x, y, w, h, radius)
        hole = _corners(x + inner, y + inner, w - 2 * inner, h - 2 * inner, max(0.0, radius - inner))
        if len(points) == len(hole):
            tris = []
            n = len(points)
            for i in range(n):
                a, b = points[i], points[(i + 1) % n]
                c, d = hole[i], hole[(i + 1) % n]
                tris.extend((a, b, d, a, d, c))
            shader = _shader("UNIFORM_COLOR")
            batch = batch_for_shader(shader, "TRIS", {"pos": tris})
            shader.uniform_float("color", color)
            batch.draw(shader)
            return
    points = _corners(x + 0.5, y + 0.5, w - 1, h - 1, radius)
    shader = _shader("UNIFORM_COLOR")
    batch = batch_for_shader(shader, "LINE_LOOP", {"pos": points})
    shader.uniform_float("color", color)
    batch.draw(shader)


def line(x1, y1, x2, y2, color):
    gpu.state.blend_set("ALPHA")  # Blender's text drawing can leave blending off
    shader = _shader("UNIFORM_COLOR")
    batch = batch_for_shader(shader, "LINES", {"pos": [(x1, y1), (x2, y2)]})
    shader.uniform_float("color", color)
    batch.draw(shader)


# ─── Text ────────────────────────────────────────────────────────────────────────────────────────


def mono():
    """A monospaced font for code, from Blender's own fonts; the UI font when there is none."""
    global _mono
    if _mono is None:
        _mono = UI_FONT
        try:
            folder = bpy.utils.system_resource("DATAFILES", path="fonts")
            names = sorted(os.listdir(folder)) if folder and os.path.isdir(folder) else []
            for name in names:
                if "mono" in name.lower() and name.lower().endswith((".ttf", ".otf", ".woff2", ".woff")):
                    loaded = blf.load(os.path.join(folder, name))
                    if loaded != -1:
                        _mono = loaded
                        break
        except (OSError, RuntimeError, ValueError):
            _mono = UI_FONT
    return _mono


def measure(s, size, font=UI_FONT):
    if not s:
        return 0.0
    blf.size(font, size)
    return blf.dimensions(font, s)[0]


def text(x, y, s, size, color, font=UI_FONT):
    """Draws one line with its baseline at y."""
    if not s:
        return
    blf.size(font, size)
    blf.color(font, *color)
    blf.position(font, round(x), round(y), 0)
    blf.draw(font, s)


def fit(s, width, size, font=UI_FONT):
    """s, shortened with an ellipsis to fit width."""
    if measure(s, size, font) <= width:
        return s
    lo, hi = 0, len(s)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if measure(s[:mid] + "…", size, font) <= width:
            lo = mid
        else:
            hi = mid - 1
    return s[:lo].rstrip() + "…" if lo else "…"


# ─── Clipping ────────────────────────────────────────────────────────────────────────────────────


class clip:
    """with clip(x, y, w, h): drawing outside the box is cut off. Region coordinates; the viewport (the region's
    place in the window) gives the offset to the window coordinates the scissor box is in."""

    def __init__(self, x, y, w, h):
        self.box = (x, y, w, h)

    def __enter__(self):
        self.saved = gpu.state.scissor_get()
        vx, vy, vw, vh = gpu.state.viewport_get()
        x, y, w, h = self.box
        x0, y0 = max(vx, vx + int(x)), max(vy, vy + int(y))
        x1, y1 = min(vx + vw, vx + int(x + w)), min(vy + vh, vy + int(y + h))
        gpu.state.scissor_test_set(True)
        gpu.state.scissor_set(x0, y0, max(0, x1 - x0), max(0, y1 - y0))
        return self

    def __exit__(self, *_):
        gpu.state.scissor_set(*self.saved)


# ─── Pictures ────────────────────────────────────────────────────────────────────────────────────


def picture(path):
    """(texture, width, height) for an image file, loaded once. The image is a hidden data-block (its name starts
    with a dot) with no users, so it is never saved into the .blend."""
    found = _textures.get(path)
    if found is not None:
        name, texture = found
        image = bpy.data.images.get(name)
        if image is not None:
            return texture, image.size[0], image.size[1]
    if not path or not os.path.isfile(path):
        return None
    try:
        image = bpy.data.images.load(path, check_existing=False)
        image.name = ".zetta-agent-" + os.path.basename(path)[:40]
        texture = gpu.texture.from_image(image)
    except (RuntimeError, ValueError):
        return None
    _textures[path] = (image.name, texture)
    return texture, image.size[0], image.size[1]


def _image_shader():
    """Blender hands an image to the GPU in linear colour; drawn as it is, every picture looks too dark. This
    shader turns it back into display colours (Blender 4.0+); plain IMAGE where it is missing."""
    if "image" not in _shaders:
        try:
            _shaders["image"] = gpu.shader.from_builtin("IMAGE_SCENE_LINEAR_TO_REC709_SRGB")
        except (ValueError, TypeError, SystemError):
            _shaders["image"] = gpu.shader.from_builtin("IMAGE")
    return _shaders["image"]


def image(texture, x, y, w, h):
    gpu.state.blend_set("ALPHA")  # Blender's text drawing can leave blending off
    shader = _image_shader()
    batch = batch_for_shader(shader, "TRI_FAN", {"pos": [(x, y), (x + w, y), (x + w, y + h), (x, y + h)],
                                                 "texCoord": [(0, 0), (1, 0), (1, 1), (0, 1)]})
    shader.uniform_sampler("image", texture)
    batch.draw(shader)


def forget_pictures():
    for name, _texture in _textures.values():
        image = bpy.data.images.get(name)
        if image is not None and image.users == 0:
            bpy.data.images.remove(image)
    _textures.clear()
