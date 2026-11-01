"""A port of the web Studio's request builder (Zetta3D-Frontend src/lib/gen3d-request.ts deriveSpec /
engineFields / buildCreateRequest / buildMultiViewRequest).

Every cross-field rule here exists because the server refuses the combination after credits have moved —
checking it on this side costs nothing. Bodies carry only the fields that are set: the API rejects unknown
fields, and some known ones in the wrong combination.
"""

from dataclasses import dataclass, field

from . import catalog


@dataclass
class GenerateOptions:
    """The Generate / Smart 3D form."""

    prompt: str = ""
    image_url: str = ""  # an uploaded, engine-readable image URL
    image_from_prompt: bool = False  # the image was drawn from the prompt (text-to-3D): both are sent
    views: list = field(default_factory=list)  # [(viewType, url)] — multi-view, front first
    model: str = "n1"
    quality: str = "high-res"  # low-poly | high-res | ultra
    output: str = "textured"  # textured | geometry
    pbr: bool = True
    texture_quality: str = "standard"  # standard | detailed | extreme
    face_count: int = None  # None = the engine's default for the rung (no custom-count charge)
    polygon_type: str = "triangle"  # triangle | quadrilateral
    title: str = ""
    segment_parts: bool = False
    real_world_scale: bool = False
    delight: bool = False
    autofix_image: bool = False
    compress_geometry: bool = False
    texture_alignment: str = "geometry"  # geometry | original_image
    export_uv: bool = True
    control_mode: bool = False
    availability: str = None  # None = the server's plan-aware default, or public | private

    @property
    def multi_view(self):
        return len(self.views) > 0


@dataclass
class BuiltRequest:
    body: dict = None
    base: str = ""
    modifiers: list = field(default_factory=list)
    problems: list = field(default_factory=list)

    @property
    def ok(self):
        return not self.problems


def _may_choose_count(gates):
    return (gates or {}).get("allowsCustomFaceCount") is not False


def _custom_count(o, gates):
    return (
        _may_choose_count(gates)
        and o.face_count is not None
        and o.face_count != catalog.face_preset(o.model, o.quality, o.polygon_type == "quadrilateral")
    )


def spec(o, gates=None):
    """(base, modifiers) — the priced choices."""
    m = catalog.model(o.model)
    if o.quality == "low-poly" and m.low_poly:
        base = "lowpoly"
    elif o.output == "geometry":
        base = "geometry_only"
    else:
        base = "textured"

    modifiers = []
    if o.multi_view:
        modifiers.append("multi_view")
    if o.pbr and o.output == "textured":
        modifiers.append("pbr")
    if o.quality == "ultra" and m.detailed_geometry:
        modifiers.append("detailed_geometry")
    if o.segment_parts and o.output == "geometry" and o.quality != "low-poly" and m.parts:
        modifiers.append("generate_parts")
    if o.output == "textured":
        if o.texture_quality == "extreme" and m.extreme_texture:
            modifiers.append("extreme_texture")
        elif o.texture_quality == "detailed" and m.detailed_texture:
            modifiers.append("detailed_texture")
    if _custom_count(o, gates):
        modifiers.append("custom_face_count")
    # No `quad_mesh`: the web Studio never sends it; the request's polygonType selects quads.
    return base, modifiers


def build(o, gates=None):
    result = BuiltRequest()
    m = catalog.model(o.model)
    base, modifiers = spec(o, gates)
    result.base, result.modifiers = base, modifiers

    if o.face_count is not None and _may_choose_count(gates):
        low, high = catalog.face_range(o.model, o.quality, o.polygon_type == "quadrilateral")
        if o.face_count < low or o.face_count > high:
            result.problems.append(f"The polygon count must be between {low:,} and {high:,}.")

    body = {"spec": {"generation": {"base": base, "modifiers": list(modifiers)}}}

    prompt = (o.prompt or "").strip()
    image = (o.image_url or "").strip()
    if o.multi_view:
        body["multiViewImages"] = [{"viewType": view, "imageUrl": url} for view, url in o.views]
    else:
        has_image, has_prompt = bool(image), bool(prompt)
        if has_image and has_prompt and not o.image_from_prompt:
            result.problems.append("Use either a prompt or an image, not both.")
        if not has_image and not has_prompt:
            result.problems.append("Describe the model or attach an image.")
        if not has_image and has_prompt and not m.bare_prompt:
            result.problems.append(f"{m.label} generates from a picture — draw one from the prompt first.")
        if has_prompt and len(prompt) > 1024:
            result.problems.append("The prompt is longer than 1,024 characters.")
        if has_image and o.image_from_prompt and has_prompt:
            body["imageUrl"] = image
            body["prompt"] = prompt
            body["imageFromPrompt"] = True
        elif has_image:
            body["imageUrl"] = image
        else:
            body["prompt"] = prompt

    if o.face_count is not None and _may_choose_count(gates):
        body["faceCount"] = int(o.face_count)

    # Engine fields (engineFields in the web Studio).
    parts = "generate_parts" in modifiers
    quads_possible = (base == "lowpoly" or m.quad_any_base) and not parts
    if o.polygon_type == "quadrilateral" and quads_possible:
        body["polygonType"] = "quadrilateral"
    elif quads_possible:
        body["polygonType"] = "triangle"
    if base == "lowpoly" and o.output == "geometry" and o.model == "n25":
        body["generationType"] = "Geometry"
    if o.availability:
        body["availability"] = o.availability
    if m.image_tuning:
        if o.real_world_scale:
            body["autoSize"] = True
        if o.texture_alignment != "original_image":
            body["textureAlignment"] = o.texture_alignment
        if o.autofix_image:
            body["autofixImage"] = True
        if o.compress_geometry:
            body["compressGeometry"] = True
        if o.delight:
            body["delight"] = True
        # Inverted on purpose: the engine defaults UVs ON, so only "off" is sent.
        if not o.export_uv:
            body["exportUv"] = False

    title = (o.title or "").strip()
    if title:
        body["title"] = title[:255]
    if o.control_mode and not o.multi_view:
        body["advanceControl"] = True
    body["model"] = o.model

    result.body = body
    return result


def price(prices, built, model_id):
    """The credits the job will cost, from the price list (the server's quote is authoritative)."""
    if not prices:
        return None
    return price_total(prices, model_id, [built.base, *built.modifiers])


def price_of(prices, code, model_id=None):
    """An exact model row beats the null-model default."""
    for item in prices or ():
        if item.get("code") == code and item.get("model") == model_id:
            return int(item.get("credits") or 0)
    for item in prices or ():
        if item.get("code") == code and not item.get("model"):
            return int(item.get("credits") or 0)
    return None


def price_total(prices, model_id, codes):
    total = 0
    for code in codes:
        value = price_of(prices, code, model_id)
        if value is None:
            return None
        total += value
    return total


def post_process(code, source_post_id, options=None):
    """A post-process job on an existing generation: exactly one operation code."""
    body = {"spec": {"postProcess": [code]}, "sourcePostId": int(source_post_id)}
    for key, value in (options or {}).items():
        if value is not None:
            body[key] = value
    return body
