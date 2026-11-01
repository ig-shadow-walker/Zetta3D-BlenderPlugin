"""Pictures held by the Studio's forms (a Generate picture, the multi-view slots, Image Studio inputs…).

A picture is bytes plus a content type and a name, kept in memory (never in the .blend); each is written to a
temporary file once so the panel can show it as an icon. Every picture going to a 3D engine is checked the way
the web and Unity check it, from its header, without decoding.
"""

import os
import struct
import tempfile
import uuid

import bpy
import bpy.utils.previews

MAX_BYTES = 8 * 1024 * 1024
MIN_SIDE = 128
MAX_SIDE = 5000
ACCEPTED = ("image/png", "image/jpeg", "image/webp")
EXTENSIONS = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}

_previews = None
_folder = None


class Picture:
    """One picture: bytes, contentType, name; optional library post id and where it came from."""

    def __init__(self, data, content_type="image/png", name="picture", post_id=None, drawn_for=None):
        self.data = data
        self.content_type = content_type or sniff(data)
        self.name = name or "picture"
        self.post_id = post_id  # a 2D library row: sent by id, never re-uploaded
        self.drawn_for = drawn_for  # the prompt it was drawn from (Generate's text-to-3D)
        self.key = uuid.uuid4().hex
        self._icon = None

    @property
    def extension(self):
        return extension_for(self.content_type)

    def icon(self):
        """An icon id for the panel (written to a temporary file on first use)."""
        if self._icon is None:
            path = os.path.join(_temp_folder(), self.key + self.extension)
            with open(path, "wb") as handle:
                handle.write(self.data)
            self._icon = _collection().load(self.key, path, "IMAGE").icon_id
        return self._icon

    def size(self):
        return image_size(self.data, self.content_type)

    def problem(self):
        return check(self.data, self.content_type)

    def inline(self):
        from ..api import zetta

        return zetta.inline_image(self.data, self.content_type)

    def renamed(self, data, content_type):
        base = os.path.splitext(self.name)[0] or "picture"
        return Picture(data, content_type, base + extension_for(content_type), drawn_for=self.drawn_for)


def _collection():
    global _previews
    if _previews is None:
        _previews = bpy.utils.previews.new()
    return _previews


def _temp_folder():
    global _folder
    if _folder is None:
        _folder = tempfile.mkdtemp(prefix="zetta3d-pictures-")
    return _folder


def icon_for_file(key, path):
    """An icon id for an image file (library thumbnails)."""
    collection = _collection()
    if key in collection:
        return collection[key].icon_id
    return collection.load(key, path, "IMAGE").icon_id


def has_icon(key):
    return _previews is not None and key in _previews


def unregister():
    global _previews
    if _previews is not None:
        bpy.utils.previews.remove(_previews)
        _previews = None


# ─── Types and checks ─────────────────────────────────────────────────────────────────────────────


def sniff(data):
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def extension_for(content_type):
    return {"image/jpeg": ".jpg", "image/webp": ".webp"}.get(content_type, ".png")


def content_type_for(path):
    return EXTENSIONS.get(os.path.splitext(path)[1].lower())


def image_size(data, content_type=None):
    """(width, height) from the header, or None. PNG IHDR, JPEG SOFn, WebP VP8/VP8L/VP8X."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return struct.unpack(">II", data[16:24])
        if data[:2] == b"\xff\xd8":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                length = struct.unpack(">H", data[i + 2 : i + 4])[0]
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    height, width = struct.unpack(">HH", data[i + 5 : i + 9])
                    return width, height
                i += 2 + length
            return None
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            chunk = data[12:16]
            if chunk == b"VP8 ":
                width, height = struct.unpack("<HH", data[26:30])
                return width & 0x3FFF, height & 0x3FFF
            if chunk == b"VP8L":
                bits = struct.unpack("<I", data[21:25])[0]
                return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
            if chunk == b"VP8X":
                width = int.from_bytes(data[24:27], "little") + 1
                height = int.from_bytes(data[27:30], "little") + 1
                return width, height
    except (struct.error, IndexError):
        return None
    return None


def check(data, content_type):
    """The reason a picture can't go to a 3D engine, or None."""
    if not data:
        return "That image is empty."
    if content_type not in ACCEPTED:
        return "Use a PNG, JPEG or WebP image."
    if len(data) > MAX_BYTES:
        return f"That image is {len(data) / (1024 * 1024):.1f} MB — the limit is 8 MB."
    size = image_size(data, content_type)
    if size is None:
        return "That image could not be read."
    width, height = size
    if width < MIN_SIDE or height < MIN_SIDE:
        return f"That image is {width}×{height} px — each side needs at least {MIN_SIDE} px."
    if width > MAX_SIDE or height > MAX_SIDE:
        return f"That image is {width}×{height} px — each side can be at most 5,000 px."
    return None


# ─── Reading pictures from Blender ────────────────────────────────────────────────────────────────


def from_file(path):
    """A Picture from a file on disk; raises ValueError with the reason it can't be used."""
    path = bpy.path.abspath(path)
    content_type = content_type_for(path)
    if content_type is None:
        raise ValueError("Choose a PNG, JPEG or WebP image.")
    with open(path, "rb") as handle:
        data = handle.read()
    return Picture(data, content_type, os.path.basename(path))


def from_image(image):
    """A Picture from a Blender image datablock (its file when it is a PNG/JPEG/WebP, else a PNG of its pixels)."""
    from .. import importer

    data, content_type = importer.image_bytes(image)
    name = os.path.splitext(image.name)[0] + extension_for(content_type)
    return Picture(data, content_type, name)


def downscaled(picture, longest=1536, quality=90):
    """A copy no longer than `longest` px on its long side, as JPEG — used only when it comes out smaller (and never
    for WebP). Main thread only (it goes through Blender's image API)."""
    size = picture.size()
    if size is None or max(size) <= longest or picture.content_type == "image/webp":
        return picture
    folder = tempfile.mkdtemp(prefix="zetta3d-")
    source = os.path.join(folder, "source" + picture.extension)
    with open(source, "wb") as handle:
        handle.write(picture.data)
    image = bpy.data.images.load(source, check_existing=False)
    try:
        scale = longest / max(size)
        image.scale(max(1, round(size[0] * scale)), max(1, round(size[1] * scale)))
        target = os.path.join(folder, "scaled.jpg")
        scene = bpy.context.scene
        settings = scene.render.image_settings
        previous = (settings.file_format, settings.quality, settings.color_mode)
        settings.file_format, settings.quality, settings.color_mode = "JPEG", quality, "RGB"
        try:
            image.save_render(target, scene=scene)
        finally:
            settings.file_format, settings.quality, settings.color_mode = previous
    finally:
        bpy.data.images.remove(image)
    with open(target, "rb") as handle:
        data = handle.read()
    if len(data) >= len(picture.data):
        return picture
    copy = Picture(data, "image/jpeg", os.path.splitext(picture.name)[0] + ".jpg", picture.post_id, picture.drawn_for)
    return copy


# ─── Slots ────────────────────────────────────────────────────────────────────────────────────────

# Every picture a form holds, by slot name: "generate", "mv_front", "tex_single", "img_edit", "img_compose_3"…
slots = {}
errors = {}  # slot → the reason the last picture put there was refused


def get(slot):
    return slots.get(slot)


def put(slot, picture, for_3d=True):
    """Puts a picture in a slot. A picture that can't go to a 3D engine is refused (and the slot cleared)."""
    errors.pop(slot, None)
    if picture is not None and for_3d:
        problem = picture.problem()
        if problem:
            errors[slot] = problem
            slots.pop(slot, None)
            return problem
    if picture is None:
        slots.pop(slot, None)
    else:
        slots[slot] = picture
    return None


def clear(slot):
    slots.pop(slot, None)
    errors.pop(slot, None)


def clear_prefix(prefix):
    for key in [k for k in slots if k.startswith(prefix)]:
        slots.pop(key, None)
    for key in [k for k in errors if k.startswith(prefix)]:
        errors.pop(key, None)


def with_prefix(prefix):
    """The pictures in slots named prefix0, prefix1, … in order."""
    found = sorted(
        ((int(k[len(prefix):]), v) for k, v in slots.items() if k.startswith(prefix) and k[len(prefix):].isdigit()),
        key=lambda pair: pair[0],
    )
    return [picture for _, picture in found]


def set_list(prefix, pictures):
    clear_prefix(prefix)
    for index, picture in enumerate(pictures):
        slots[f"{prefix}{index}"] = picture
