"""Pictures for the agent's 2D tools: reading an image source, fetching library pictures, and the pictures it made.

An image source is {imageId} (a picture in the user's 2D library) or {path}: a file relative to the .blend
("//textures/hero.png"), an Image data-block ("image:Hero" — packed and generated images too, encoded as PNG), or a
picture the user attached ("attached:<name>"). Blender data is read on the main thread (`resolve`); library pictures
are downloaded on a worker (`inline`, `library_bytes`).
"""

import os
import tempfile

import bpy

from ..api import zetta
from ..core import http
from ..studio import pictures
from . import targets
from .tools import ToolError

MAX_SIDE = 2048
_made = {}  # image id → (bytes, content type): pictures the agent's tools made this session, for save_image
_MADE_KEEP = 24


class Source:
    def __init__(self, label, image_id=None, picture=None):
        self.label = label
        self.id = image_id
        self.picture = picture  # pictures.Picture, read from Blender or disk


def _png_of(image):
    """PNG bytes of any Blender image — packed, generated, painted or a format the API does not read."""
    temp = os.path.join(tempfile.mkdtemp(prefix="zetta3d-"), "image.png")
    if image.file_format == "PNG":
        try:
            image.save(filepath=temp)  # writes the pixels as they are (painted too), keeps the image's own path
        except (RuntimeError, TypeError):
            pass
        if os.path.isfile(temp):
            with open(temp, "rb") as handle:
                data = handle.read()
            if data[:8] == b"\x89PNG\r\n\x1a\n":
                return data
    copy = image.copy()
    try:
        copy.filepath_raw = temp
        copy.file_format = "PNG"
        copy.save()
    finally:
        bpy.data.images.remove(copy)
    with open(temp, "rb") as handle:
        return handle.read()


def acceptable(picture, label):
    """A picture the 2D API takes: PNG, JPEG or WebP, 128–2,048 px a side, under 8 MB — larger ones scaled down."""
    problem = pictures.check(picture.data, picture.content_type)
    size = picture.size()
    if problem is None and size is not None and max(size) <= MAX_SIDE:
        return picture
    if picture.content_type in pictures.ACCEPTED and size is not None and min(size) >= pictures.MIN_SIDE:
        smaller = pictures.downscaled(picture, longest=MAX_SIDE)
        if pictures.check(smaller.data, smaller.content_type) is None:
            return smaller
    raise ToolError(f"{label}: {problem or 'that image could not be read.'}")


def _from_image(image, label):
    if image.size[0] == 0 and not image.packed_file and image.source == "FILE":
        path = bpy.path.abspath(image.filepath)
        if not os.path.isfile(path):
            raise ToolError(f"{label} has no pixels — its file {image.filepath} is missing.")
    content_type = None
    path = bpy.path.abspath(image.filepath) if image.filepath else ""
    if image.source == "FILE" and not image.packed_file and not image.is_dirty and os.path.isfile(path):
        content_type = pictures.content_type_for(path)
        if content_type is not None:
            with open(path, "rb") as handle:
                return pictures.Picture(handle.read(), content_type, os.path.basename(path))
    return pictures.Picture(_png_of(image), "image/png", os.path.splitext(image.name)[0] + ".png")


def _from_file(full, label):
    content_type = pictures.content_type_for(full)
    if content_type is not None:
        with open(full, "rb") as handle:
            return pictures.Picture(handle.read(), content_type, os.path.basename(full))
    # Anything else Blender reads (TGA, EXR, BMP, TIFF…): through an image, as PNG.
    try:
        image = bpy.data.images.load(full, check_existing=False)
    except RuntimeError:
        raise ToolError(f"{label} is not an image Blender can read.") from None
    try:
        return pictures.Picture(_png_of(image), "image/png", os.path.splitext(os.path.basename(full))[0] + ".png")
    finally:
        bpy.data.images.remove(image)


def resolve(source):
    """{imageId} | {path} → Source. Main thread."""
    if not isinstance(source, dict):
        raise ToolError("A picture is { imageId } or { path }.")
    image_id, path = source.get("imageId"), source.get("path")
    if image_id is not None and path:
        raise ToolError("Give a picture's imageId or its path, not both.")
    if image_id is not None:
        if isinstance(image_id, bool) or not isinstance(image_id, int) or image_id <= 0:
            raise ToolError("'imageId' must be a positive number from a picture tool or list_images.")
        return Source(f"image #{image_id}", image_id=image_id)
    if not isinstance(path, str) or not path.strip():
        raise ToolError("A picture needs an imageId (from the 2D library) or a path (\"//file.png\", "
                        "\"image:Name\" or \"attached:<name>\").")
    path = path.strip()
    if path.startswith("image:"):
        image = targets.datablock(path)
        return Source(path, picture=acceptable(_from_image(image, path), path))
    full = targets.file_path(path, must_exist=True)
    return Source(path, picture=acceptable(_from_file(full, path), path))


def library_bytes(image_id):
    """(bytes, content type) of a picture in the user's 2D library. Worker thread."""
    if image_id in _made:
        return _made[image_id]
    from . import client

    try:
        answer = client.image(image_id) or {}
    except http.ApiError as error:
        if error.status == 404:
            raise ToolError(f"Image #{image_id} was not found. Use an imageId a picture tool returned, or one from "
                            "list_images.") from None
        raise
    url = answer.get("url")
    if not url:
        raise ToolError(f"Image #{image_id} has no downloadable file.")
    data = http.fetch_bytes(url, timeout=60)
    if not data:
        raise ToolError(f"Image #{image_id} downloaded empty.")
    return data, answer.get("contentType") or pictures.sniff(data)


def library_url(image_id):
    """A signed URL for a library picture (worker thread) — checks it is the user's."""
    from . import client

    try:
        answer = client.image(image_id) or {}
    except http.ApiError as error:
        if error.status == 404:
            raise ToolError(f"Image #{image_id} was not found. Use an imageId a picture tool returned, or one from "
                            "list_images.") from None
        raise
    if not answer.get("url"):
        raise ToolError(f"Image #{image_id} has no downloadable file.")
    return answer["url"]


def inline(source):
    """The API's inline image for a source (a library picture is downloaded). Worker thread."""
    if source.picture is not None:
        return source.picture.inline()
    data, content_type = library_bytes(source.id)
    if pictures.check(data, content_type) is not None:
        raise ToolError(f"Image #{source.id}: {pictures.check(data, content_type)}")
    return zetta.inline_image(data, content_type)


def remember(image_id, data, content_type):
    _made[image_id] = (data, content_type)
    while len(_made) > _MADE_KEEP:
        _made.pop(next(iter(_made)))
