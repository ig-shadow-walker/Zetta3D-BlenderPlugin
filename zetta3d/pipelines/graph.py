"""3D Pipelines: the recipe graph — its steps, their settings, the requests they send, and the rules.

Pure Python (no bpy), a port of the Unity plugin's NodeCatalog / NodeSettings / GenerateForm / RequestBuilder /
GraphRules / GraphEditing, themselves ports of the web's pipeline code. Requests must match the web's byte for
byte: the server only reads each step's `request`, so it is rebuilt from `settings` before every save, every
"Describe it" edit and every new run.

A graph is the API's own JSON: {version: 1, nodes: [{id, kind, position, settings, request, label?}],
edges: [{id, source, sourceHandle, target, targetHandle}]}. Unknown keys and unknown kinds are kept untouched.
"""

import copy
import json
import math
import random
import string

from ..api import catalog, requests

# ─── Catalogue ────────────────────────────────────────────────────────────────────────────────────

PICTURE, VIEWS, MESH, SPLAT, FILE = "picture", "views", "mesh", "splat", "file"
PORT_LABELS = {PICTURE: "Picture", VIEWS: "Views", MESH: "Model", SPLAT: "Splat", FILE: "File"}
PART_PREFIX = "part:"
MAX_NODES = 200
MAX_BATCH = 20

# kind: (label, group, inputs {handle: (accepts, required)}, outputs {handle: type}, is_3d, engine_choice, offered,
#        blurb)
KINDS = {
    "seed_picture": ("Picture seed", "Seed", {}, {"out": PICTURE}, False, False, True,
                     "The picture the pipeline starts from — upload one, pick one, or make one."),
    "seed_model": ("Model seed", "Seed", {}, {"out": MESH}, False, False, True,
                   "A model from your library, or one you upload."),
    "seed_views": ("Multi-view seed", "Seed", {}, {"out": VIEWS}, False, False, True,
                   "Pictures of each side — front, back, left, right — for a multi-view generation."),
    "remove_background": ("Remove background", "Picture", {"in": ((PICTURE,), True)}, {"out": PICTURE}, False, False,
                          True, "Cuts the subject out of its background. Free."),
    "ai_edit": ("AI edit", "Picture", {"in": ((PICTURE,), True)}, {"out": PICTURE}, False, False, True,
                "Changes the picture by an instruction."),
    "draw_views": ("Draw views", "Picture", {"in": ((PICTURE,), True)}, {"out": VIEWS}, False, False, True,
                   "Draws the back and sides from one picture, for a multi-view generation."),
    "isolate_parts": ("Isolate parts", "Picture", {"in": ((PICTURE,), True)},
                      {"character": PICTURE, "each": PICTURE}, False, False, True,
                      "Splits a picture into its parts — the character, the weapon, the shield — each on its own."),
    "generate_3d": ("Generate 3D", "Make", {"in": ((PICTURE,), True)}, {"out": MESH}, True, True, True,
                    "A model from one picture. Zetta N1 or N2.5."),
    "generate_smart_3d": ("Generate Smart 3D", "Make", {"in": ((PICTURE,), True)}, {"out": MESH}, True, True, True,
                          "Clean topology from one picture. Zetta Np1 or Np1.5."),
    "generate_3d_multiview": ("Multi-view - Generate 3D", "Make", {"in": ((VIEWS,), True)}, {"out": MESH}, True, True,
                              True, "A model from pictures of each side — a Multi-view seed or Draw views. "
                                    "Zetta N1 or N2.5."),
    "generate_smart_3d_multiview": ("Multi-view - Smart 3D", "Make", {"in": ((VIEWS,), True)}, {"out": MESH}, True,
                                    True, True, "Clean topology from pictures of each side — a Multi-view seed or "
                                                "Draw views. Zetta Np1 or Np1.5."),
    "generate_splat": ("Generate Splat", "Make", {"in": ((PICTURE,), True)}, {"out": SPLAT}, True, True, False,
                       "A splat object or environment from a picture."),
    "retopology": ("Retopology", "Refine", {"in": ((MESH,), True)}, {"out": MESH}, True, True, True,
                   "A clean mesh at the polygon budget you set."),
    "segmentation": ("Segmentation", "Refine", {"in": ((MESH,), True)}, {"out": MESH}, True, False, True,
                     "Splits the model into parts — AI or Smart."),
    "smart_segmentation": ("Smart segmentation", "Refine", {"in": ((MESH,), True)}, {"out": MESH}, True, False, False,
                           "Splits the model into named parts."),
    "texture": ("AI texturing", "Refine", {"in": ((MESH,), True)}, {"out": MESH}, True, False, True,
                "New textures, from a reference picture or a description."),
    "texture_multiview": ("Multi-view - AI texturing", "Refine", {"in": ((MESH,), True), "views": ((VIEWS,), False)},
                          {"out": MESH}, True, False, True,
                          "New textures from pictures of each side — wired from a Multi-view seed or Draw views, "
                          "or given in each run."),
    "smart_uv": ("Smart UV", "Refine", {"in": ((MESH,), True)}, {"out": MESH}, True, False, True,
                 "A new UV layout, textures re-baked onto it. Up to 80,000 triangles."),
    "rigging": ("Auto rig", "Refine", {"in": ((MESH,), True)}, {"out": MESH}, True, False, True,
                "A skeleton, skinned, ready to animate."),
    "export": ("Export", "Output", {"in": ((MESH,), True)}, {"out": FILE}, False, False, False,
               "GLB, FBX, OBJ or STL. Free."),
}
GROUPS = ("Seed", "Picture", "Make", "Refine", "Output")
SEEDS = ("seed_picture", "seed_model", "seed_views")
GENERATE_KINDS = ("generate_3d", "generate_smart_3d", "generate_3d_multiview", "generate_smart_3d_multiview")
SMART_GENERATE_KINDS = ("generate_smart_3d", "generate_smart_3d_multiview")
MULTIVIEW_OF = {"generate_3d": "generate_3d_multiview", "generate_smart_3d": "generate_smart_3d_multiview"}
SINGLE_OF = {v: k for k, v in MULTIVIEW_OF.items()}


def known(kind):
    return kind in KINDS


def offered(kind):
    return known(kind) and KINDS[kind][6]


def label(kind):
    return KINDS[kind][0] if known(kind) else kind


def blurb(kind):
    return KINDS[kind][7] if known(kind) else ""


def group(kind):
    return KINDS[kind][1] if known(kind) else ""


def inputs(kind):
    return KINDS[kind][2] if known(kind) else {}


def is_seed(kind):
    return kind in SEEDS


def is_generate(kind):
    return kind in GENERATE_KINDS


def is_3d(kind):
    return known(kind) and KINDS[kind][4]


def has_engine_choice(kind):
    return known(kind) and KINDS[kind][5]


def outputs(node):
    """{handle: type} — Isolate parts adds one output per part it has learnt."""
    kind = node.get("kind")
    found = dict(KINDS[kind][3]) if known(kind) else {}
    if kind == "isolate_parts":
        for part in (node.get("settings") or {}).get("knownParts") or []:
            if isinstance(part, dict) and part.get("key"):
                found[PART_PREFIX + part["key"]] = PICTURE
    return found


def output_type(node, handle):
    if node.get("kind") == "isolate_parts" and str(handle).startswith(PART_PREFIX):
        return PICTURE
    return outputs(node).get(handle)


def output_label(node, handle):
    if handle == "out":
        return PORT_LABELS.get(output_type(node, handle), "Output")
    if handle == "character":
        return "Character"
    if handle == "each":
        return "Each part"
    if str(handle).startswith(PART_PREFIX):
        key = handle[len(PART_PREFIX):]
        for part in (node.get("settings") or {}).get("knownParts") or []:
            if part.get("key") == key:
                return f"Part: {part.get('label') or key}"
        return f"Part: {key}"
    return handle


def node_name(node):
    return (node.get("label") or "").strip() or label(node.get("kind"))


# ─── Settings: defaults ───────────────────────────────────────────────────────────────────────────

POLYGON_MIN = 3_100
POLYGON_MAX = 1_480_000
LOW_POLY_CEILING = 15_000
ULTRA_FLOOR = 1_000_000


def generate_defaults():
    return {
        "quality": "high-res", "output": "textured", "pbrMaterials": True, "polygonCount": "200000",
        "polygonType": "triangle", "textureQuality": "standard", "segmentParts": False, "realWorldScale": False,
        "textureAlignment": "geometry", "delight": False, "availability": None, "autofixImage": False,
        "compressGeometry": False, "exportUv": True, "imageFromPrompt": False, "removeBackground": True,
        "controlMode": True, "model": "n1", "imageUrl": "", "prompt": "", "title": "",
    }


def isolate_defaults():
    return {"identify": True, "sensitivity": "low", "maxParts": 6, "pose": "t", "extraParts": [], "model": None}


def texture_defaults():
    return {"tier": "retexture", "alignment": "original_image", "bake": True, "pbr": True, "fast": False,
            "delight": True, "removeBg": True}


def defaults(kind):
    if kind == "seed_picture":
        return {"source": None}
    if kind in ("seed_model", "seed_views", "remove_background"):
        return {}
    if kind == "ai_edit":
        return {"prompt": "", "model": None, "for3D": True}
    if kind == "draw_views":
        return {"views": ["back", "left", "right"]}
    if kind == "isolate_parts":
        return isolate_defaults()
    if kind in ("generate_3d", "generate_3d_multiview"):
        form = generate_defaults()
        form["model"] = "n25"  # spread, not a patch: the count stays 200,000, as on the web
        return form
    if kind in SMART_GENERATE_KINDS:
        return apply_patch(generate_defaults(), {"model": "np1"})
    if kind == "generate_splat":
        return {"subject": "object", "engine": "n25", "removeBackground": True, "ratio": "16:9", "resolution": "1K",
                "title": ""}
    if kind == "retopology":
        return {"engine": "r1", "faceLevel": "medium", "polygonType": "triangle", "faces": 8000, "bake": True}
    if kind == "segmentation":
        return {"mode": "segment", "granularity": "balanced", "splitByConnectivity": True, "hint": ""}
    if kind == "smart_segmentation":
        return {"mode": "smart", "granularity": "balanced", "splitByConnectivity": True, "hint": ""}
    if kind == "texture":
        return {**texture_defaults(), "prompt": ""}
    if kind == "texture_multiview":
        return {**texture_defaults(), "prompt": "", "referenceViews": {}}
    if kind == "smart_uv":
        return {"bake": True, "orientation": "as-is", "compress": False}
    if kind == "rigging":
        return {"rigType": "biped", "spec": "standard", "outFormat": "glb"}
    if kind == "export":
        return {"format": "glb"}
    return {}


def with_defaults(kind, settings):
    base = defaults(kind)
    if not isinstance(settings, dict):
        return base
    if is_generate(kind):
        return apply_patch(base, settings)
    merged = dict(base)
    merged.update(copy.deepcopy(settings))
    return merged


def settings_of(node):
    settings = node.get("settings")
    return settings if isinstance(settings, dict) else defaults(node.get("kind"))


# ─── The generate form (GenerateForm) ─────────────────────────────────────────────────────────────

_CAPS = {
    "n25": {"low_poly": True, "bare_low_poly": True, "quad_any_base": True, "extreme": False, "custom": True,
            "face_range": None, "quad_face_range": None,
            "ceiling": {"low-poly": 20_000, "high-res": 1_000_000, "ultra": 2_000_000},
            "floor": {"low-poly": 1_000, "high-res": 1_000, "ultra": 1_000},
            "quad_ceiling": {"low-poly": 10_000, "high-res": 150_000, "ultra": 150_000},
            "quad_floor": {"low-poly": 500, "high-res": 1_000, "ultra": 1_000}},
    "np1": {"low_poly": False, "bare_low_poly": False, "quad_any_base": False, "extreme": True, "custom": True,
            "face_range": (48, 20_000), "quad_face_range": None, "ceiling": None, "floor": None,
            "quad_ceiling": None, "quad_floor": None},
    "np15": {"low_poly": False, "bare_low_poly": False, "quad_any_base": True, "extreme": True, "custom": True,
             "face_range": (48, 50_000), "quad_face_range": (48, 25_000), "ceiling": None, "floor": None,
             "quad_ceiling": None, "quad_floor": None},
    "n1": {"low_poly": True, "bare_low_poly": False, "quad_any_base": False, "extreme": False, "custom": True,
           "face_range": None, "quad_face_range": None, "ceiling": None, "floor": None, "quad_ceiling": None,
           "quad_floor": None},
}
_NO_CAPS = {"low_poly": False, "bare_low_poly": False, "quad_any_base": False, "extreme": False, "custom": False,
            "face_range": None, "quad_face_range": None, "ceiling": None, "floor": None, "quad_ceiling": None,
            "quad_floor": None}


def _caps(model):
    return _CAPS.get(model, _NO_CAPS)


def _str(form, key):
    value = (form or {}).get(key)
    if isinstance(value, str):
        return value
    default = generate_defaults().get(key)
    return default if isinstance(default, str) else None


def _text(form, key):
    value = (form or {}).get(key)
    if value is None:
        return _str(None, key) or ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    return str(value)


def _truthy(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value != ""
    return value is not None


def _flag(form, key):
    value = (form or {}).get(key)
    if value is None:
        default = generate_defaults().get(key)
        return default is True
    return _truthy(value)


def _js_number(raw):
    raw = (raw or "").strip()
    if raw == "":
        return 0.0
    try:
        return float(raw)
    except ValueError:
        return None


def _is_integer(value):
    return value is not None and not math.isinf(value) and math.floor(value) == value


def polygon_bounds(form):
    caps = _caps(_str(form, "model"))
    quads = _str(form, "polygonType") == "quadrilateral"
    if caps["face_range"]:
        return caps["quad_face_range"] if quads and caps["quad_face_range"] else caps["face_range"]
    quality = _str(form, "quality")
    ceilings = caps["quad_ceiling"] if quads else caps["ceiling"]
    floors = caps["quad_floor"] if quads else caps["floor"]
    low = (floors or {}).get(quality, POLYGON_MIN)
    high = (ceilings or {}).get(quality, POLYGON_MAX)
    return low, high


def quality_polygons(quality):
    return {"low-poly": 9_000, "ultra": 1_000_000}.get(quality, 200_000)


def default_polygon_count(form):
    caps = _caps(_str(form, "model"))
    if caps["face_range"] or caps["ceiling"] is not None:
        return polygon_bounds(form)[1]
    return quality_polygons(_str(form, "quality"))


def quality_for_polygons(raw):
    value = _js_number(raw)
    if (raw or "").strip() == "" or not _is_integer(value):
        return None
    if value < POLYGON_MIN or value > POLYGON_MAX:
        return None
    if value >= ULTRA_FLOOR:
        return "ultra"
    if value > LOW_POLY_CEILING:
        return "high-res"
    return "low-poly"


def has_custom_polygon_count(form, may_choose=True):
    if not may_choose or not _caps(_str(form, "model"))["custom"]:
        return False
    raw = _text(form, "polygonCount").strip()
    if raw == "":
        return False
    value = _js_number(raw)
    return not (_is_integer(value) and value == default_polygon_count(form))


def parameter_problems(form, may_choose=True):
    if not has_custom_polygon_count(form, may_choose):
        return []
    value = _js_number(_text(form, "polygonCount"))
    if not _is_integer(value):
        return ["The polygon count must be a whole number."]
    low, high = polygon_bounds(form)
    if value < low or value > high:
        return [f"The polygon count must be between {low:,} and {high:,}."]
    return []


def apply_patch(current, patch):
    """Every change to a generate step's form goes through here, so coupled options settle as on the web."""
    current = current if isinstance(current, dict) else generate_defaults()
    patch = patch or {}
    nxt = copy.deepcopy(current)
    for key, value in patch.items():
        nxt[key] = copy.deepcopy(value)
    caps = _caps(_str(nxt, "model"))
    if _str(nxt, "quality") == "low-poly" and not caps["low_poly"]:
        nxt["quality"] = "high-res"
    if _str(nxt, "textureQuality") == "extreme" and not caps["extreme"]:
        nxt["textureQuality"] = "detailed"
    if _str(nxt, "polygonType") == "quadrilateral" and not caps["quad_any_base"] and not caps["low_poly"]:
        nxt["polygonType"] = "triangle"
    if _str(nxt, "quality") == "low-poly":
        if not caps["bare_low_poly"]:
            nxt["output"] = "textured"
        nxt["segmentParts"] = False
    elif _flag(nxt, "segmentParts"):
        nxt["output"] = "geometry"
        nxt["polygonType"] = "triangle"

    def changed(key):
        return key in patch and patch[key] is not None and patch[key] != current.get(key)

    if changed("quality"):
        nxt["polygonCount"] = str(default_polygon_count(nxt))
        nxt["polygonCountTyped"] = False
        return nxt
    if changed("polygonType") and not _flag(nxt, "polygonCountTyped"):
        nxt["polygonCount"] = str(default_polygon_count(nxt))
        return nxt
    if changed("model"):
        low, high = polygon_bounds(nxt)
        count = _js_number(_text(nxt, "polygonCount"))
        if count is None or math.isinf(count) or count < low or count > high:
            nxt["polygonCount"] = str(default_polygon_count(nxt))
            return nxt
    if patch.get("polygonCount") is not None and caps["ceiling"] is None:
        implied = quality_for_polygons(_text(patch, "polygonCount"))
        if implied is not None:
            nxt["quality"] = implied
    return nxt


def models_for(kind):
    return ("np1", "np15") if kind in SMART_GENERATE_KINDS else ("n1", "n25")


def _generate_options(form, may_choose):
    model = _str(form, "model")
    raw = _text(form, "polygonCount").strip()
    value = _js_number(raw)
    face_count = None
    if raw != "" and _is_integer(value) and may_choose and _caps(model)["custom"] and abs(value) <= 2**31 - 1:
        face_count = int(value)
    availability = form.get("availability") if isinstance(form.get("availability"), str) else None
    return requests.GenerateOptions(
        prompt="", image_url="https://pipeline.input/picture.png", image_from_prompt=False, model=model,
        quality=_str(form, "quality"), output=_str(form, "output"), pbr=_flag(form, "pbrMaterials"),
        texture_quality=_str(form, "textureQuality"), face_count=face_count, polygon_type=_str(form, "polygonType"),
        title=_text(form, "title"), segment_parts=_flag(form, "segmentParts"),
        real_world_scale=_flag(form, "realWorldScale"), delight=_flag(form, "delight"),
        autofix_image=_flag(form, "autofixImage"), compress_geometry=_flag(form, "compressGeometry"),
        texture_alignment=_str(form, "textureAlignment"), export_uv=_flag(form, "exportUv"),
        control_mode=_flag(form, "controlMode"), availability=availability or None,
    )


def generate_request(form, incoming, entitlements):
    """(request, problems) for a generate step — the Studio's buildCreateRequest, minus the inputs."""
    may_choose = (entitlements or {}).get("allowsCustomFaceCount") is not False
    problems = parameter_problems(form, may_choose)
    options = _generate_options(form, may_choose)
    built = requests.build(options, entitlements)
    if problems or not built.body:
        return {"model": _str(form, "model")}, problems
    body = copy.deepcopy(built.body)
    for key in ("imageUrl", "prompt", "imageFromPrompt", "origin"):
        body.pop(key, None)
    caps = _caps(_str(form, "model"))
    parts = "generate_parts" in built.modifiers
    polygon = _str(form, "polygonType")
    if (built.base == "lowpoly" or caps["quad_any_base"]) and not (parts and polygon == "quadrilateral"):
        body["polygonType"] = polygon
    else:
        body.pop("polygonType", None)
    if incoming == VIEWS:
        options.views = [("front", "https://pipeline.input/front.png")]
        base, modifiers = requests.spec(options, entitlements)
        body["spec"] = {"generation": {"base": base, "modifiers": modifiers}}
    body["removeBackground"] = form.get("removeBackground") is True
    return body, []


# ─── Requests per step (RequestBuilder) ───────────────────────────────────────────────────────────


def _post(code):
    return {"spec": {"postProcess": [code]}}


def _texture_request(settings, prompt):
    request = _post(settings.get("tier") or "retexture")
    if isinstance(prompt, str) and prompt.strip():
        request["prompt"] = prompt.strip()
    if settings.get("alignment"):
        request["textureAlignment"] = settings["alignment"]
    if not _truthy(settings.get("bake", True)):
        request["bake"] = False
    if "pbr" in settings:
        request["enablePBR"] = bool(settings["pbr"])
    if _truthy(settings.get("fast")) and (settings.get("tier") or "retexture") == "retexture":
        request["textureFast"] = True
    if "delight" in settings:
        request["delight"] = bool(settings["delight"])
    return request


def views_request(views):
    """A multi-view set keyed by side → the API's array, in slot order, each side once."""
    found = []
    for side in ("front", "back", "left", "right"):
        entry = (views or {}).get(side)
        if not isinstance(entry, dict):
            continue
        item = {"viewType": side}
        if isinstance(entry.get("uploadKey"), str) and entry["uploadKey"]:
            item["uploadKey"] = entry["uploadKey"]
        elif isinstance(entry.get("gen2dPostId"), int):
            item["gen2dPostId"] = entry["gen2dPostId"]
        else:
            continue
        if entry.get("name"):
            item["name"] = entry["name"]
        found.append(item)
    return found


def request_for(kind, settings, incoming=None, entitlements=None):
    """(request, problems) for one step."""
    settings = settings if isinstance(settings, dict) else defaults(kind)
    if kind in SEEDS or kind == "remove_background":
        return {}, []
    if kind == "ai_edit":
        request = {"prompt": settings.get("prompt") if isinstance(settings.get("prompt"), str) else "",
                   "for3D": settings.get("for3D") is not False}
        if settings.get("model"):
            request["model"] = settings["model"]
        return request, []
    if kind == "draw_views":
        views = settings.get("views")
        return {"views": list(views) if isinstance(views, list) else ["back", "left", "right"]}, []
    if kind == "isolate_parts":
        merged = {**isolate_defaults(), **settings}
        request = {k: merged.get(k) for k in ("identify", "sensitivity", "maxParts", "pose", "extraParts")}
        if merged.get("model"):
            request["model"] = merged["model"]
        return request, []
    if kind in ("generate_3d", "generate_smart_3d"):
        return generate_request(settings, incoming, entitlements)
    if kind in ("generate_3d_multiview", "generate_smart_3d_multiview"):
        return generate_request(settings, VIEWS, entitlements)
    if kind == "generate_splat":
        engine = settings.get("engine") or "n25"
        request = {"spec": {"generation": {"base": "gaussian_splat", "modifiers": []}}, "model": engine,
                   "removeBackground": settings.get("subject", "object") == "object" and engine == "n25"
                   and _truthy(settings.get("removeBackground", True))}
        title = (settings.get("title") or "").strip()
        if title:
            request["title"] = title
        return request, []
    if kind == "retopology":
        request = _post("smart_topology")
        engine = settings.get("engine")
        if engine:
            request["model"] = engine
        if engine == "r1":
            faces = settings.get("faces")
            if isinstance(faces, (int, float)) and not isinstance(faces, bool):
                request["faceCount"] = int(faces)
        elif settings.get("faceLevel"):
            request["faceLevel"] = settings["faceLevel"]
        if settings.get("polygonType"):
            request["polygonType"] = settings["polygonType"]
        if engine == "r1" and settings.get("bake") is False:
            request["bake"] = False
        return request, retopology_problems(settings)
    if kind in ("segmentation", "smart_segmentation"):
        smart = settings.get("mode") == "smart"
        request = _post("smart_segmentation" if smart else "ai_segmentation")
        hint = (settings.get("hint") or "").strip()
        if smart and hint:
            request["prompt"] = hint
        if settings.get("granularity"):
            request["segmentGranularity"] = settings["granularity"]
        if not smart and not _truthy(settings.get("splitByConnectivity", True)):
            request["splitByConnectivity"] = False
        return request, []
    if kind == "texture":
        reference = settings.get("reference") if isinstance(settings.get("reference"), dict) else None
        request = _texture_request(settings, settings.get("prompt") if reference is None else None)
        if reference is not None:
            if isinstance(reference.get("uploadKey"), str) and reference["uploadKey"]:
                request["referenceUploadKey"] = reference["uploadKey"]
            elif isinstance(reference.get("gen2dPostId"), int):
                request["referenceImageId"] = reference["gen2dPostId"]
        request["removeReferenceBackground"] = settings.get("removeBg") is not False
        return request, []
    if kind == "texture_multiview":
        request = _texture_request(settings, None)
        request["referenceViews"] = views_request(settings.get("referenceViews"))
        request["removeReferenceBackground"] = settings.get("removeBg") is not False
        return request, []
    if kind == "smart_uv":
        request = _post("smart_uv")
        orientation = settings.get("orientation")
        if orientation and orientation != "as-is":
            request["exportOrientation"] = orientation
        if _truthy(settings.get("compress")):
            request["compressGeometry"] = True
        if not _truthy(settings.get("bake", True)):
            request["bake"] = False
        return request, []
    if kind == "rigging":
        request = _post("auto_rigging")
        rig_type = settings.get("rigType") or "biped"
        spec = settings.get("spec") or "standard"
        if rig_type != "biped":
            request["rigType"] = rig_type
        elif spec != "standard" and spec in [s for s, _, _ in catalog.RIG_SPECS]:
            request["rigSpec"] = spec
        out = settings.get("outFormat")
        if out and out != "glb":
            request["rigOutFormat"] = out
        return request, []
    if kind == "export":
        return {"format": settings.get("format") or "glb"}, []
    return None, []


def retopology_problems(settings):
    if (settings.get("engine") or "r1") != "r1":
        return []
    high = 10_000 if settings.get("polygonType") == "quadrilateral" else 20_000
    faces = settings.get("faces")
    if isinstance(faces, bool) or not isinstance(faces, (int, float)) or not 500 <= faces <= high:
        return [f"The polygon count must be between 500 and {high:,}."]
    return []


# ─── Graph helpers ────────────────────────────────────────────────────────────────────────────────


def empty():
    return {"version": 1, "nodes": [], "edges": []}


def node(graph, node_id):
    return next((n for n in graph.get("nodes") or [] if n.get("id") == node_id), None)


def seed(graph):
    return next((n for n in graph.get("nodes") or [] if is_seed(n.get("kind"))), None)


def incoming_edge(graph, node_id, handle="in"):
    return next((e for e in graph.get("edges") or [] if e.get("target") == node_id
                 and (e.get("targetHandle") or "in") == handle), None)


def incoming_type(graph, node_id, handle="in"):
    edge = incoming_edge(graph, node_id, handle)
    if edge is None:
        return None
    source = node(graph, edge.get("source"))
    return output_type(source, edge.get("sourceHandle") or "out") if source else None


def children(graph, node_id):
    return [e for e in graph.get("edges") or [] if e.get("source") == node_id]


def new_id(prefix):
    alphabet = string.ascii_lowercase + string.digits
    return f"{prefix}-{''.join(random.choice(alphabet) for _ in range(7))}"


def in_scope(n):
    return offered(n.get("kind"))


def with_requests(graph, entitlements=None):
    graph = copy.deepcopy(graph)
    for n in graph.get("nodes") or []:
        if not in_scope(n):
            continue
        request, _ = request_for(n["kind"], settings_of(n), incoming_type(graph, n["id"]), entitlements)
        if request is not None:
            n["request"] = request
    return graph


def with_multi_view_steps(graph):
    for n in graph.get("nodes") or []:
        kind = n.get("kind")
        if kind in MULTIVIEW_OF and incoming_type(graph, n["id"]) == VIEWS:
            n["kind"] = MULTIVIEW_OF[kind]
        elif kind == "texture":
            settings = n.get("settings") if isinstance(n.get("settings"), dict) else {}
            request = n.get("request") if isinstance(n.get("request"), dict) else {}
            if request.get("referenceMode") == "multi-view" or settings.get("referenceMode") == "multi-view":
                n["kind"] = "texture_multiview"
                settings.pop("referenceMode", None)
                request.pop("referenceMode", None)
    return graph


def set_on_each_run(n):
    kind = n.get("kind")
    if kind in ("texture", "texture_multiview"):
        return True
    return kind == "ai_edit" and settings_of(n).get("askAtRun") is True


def per_run_in(graph, n):
    return set_on_each_run(n) and not (n.get("kind") == "texture_multiview" and incoming_edge(graph, n["id"], "views"))


def without_run_input(n):
    if not set_on_each_run(n):
        return
    settings = n.setdefault("settings", {}) if isinstance(n.get("settings"), dict) else n.setdefault("settings", {})
    if n.get("kind") == "texture_multiview":
        settings["referenceViews"] = {}
        return
    settings["prompt"] = ""
    if n.get("kind") == "texture":
        settings["reference"] = None
        settings["referenceViews"] = {}


def normalize(saved):
    """(graph, needs_rebuild) — on load and after "Describe it": defaults filled, seeds emptied, run inputs
    stripped, legacy edges dropped."""
    graph = copy.deepcopy(saved) if isinstance(saved, dict) else empty()
    graph.setdefault("version", 1)
    graph["nodes"] = [n for n in graph.get("nodes") or [] if isinstance(n, dict) and isinstance(n.get("id"), str)]
    edges = []
    for index, e in enumerate(graph.get("edges") or []):
        if not isinstance(e, dict) or not isinstance(e.get("source"), str) or not isinstance(e.get("target"), str):
            continue
        e.setdefault("id", f"e{index}")
        e["sourceHandle"] = e.get("sourceHandle") or "out"
        e["targetHandle"] = e.get("targetHandle") or "in"
        edges.append(e)
    graph["edges"] = edges
    with_multi_view_steps(graph)
    needs_rebuild = False
    for n in graph["nodes"]:
        if not isinstance(n.get("position"), dict):
            n["position"] = {"x": 0, "y": 0}
        if not in_scope(n):
            continue
        if is_seed(n["kind"]):
            n["settings"] = defaults(n["kind"])
            n["request"] = {}
            continue
        n["settings"] = with_defaults(n["kind"], n.get("settings"))
        without_run_input(n)
        if is_3d(n["kind"]) and not isinstance((n.get("request") or {}).get("spec"), dict):
            needs_rebuild = True
    kinds = {n["id"]: n.get("kind") for n in graph["nodes"]}
    graph["edges"] = [e for e in graph["edges"] if not (known(kinds.get(e["target"]))
                                                        and e["targetHandle"] not in inputs(kinds[e["target"]]))]
    return graph, needs_rebuild


def for_new_run(graph):
    graph = copy.deepcopy(graph)
    for n in graph.get("nodes") or []:
        if in_scope(n):
            without_run_input(n)
    return graph


# ─── Rules: connecting, problems ──────────────────────────────────────────────────────────────────


def reaches(graph, start_id, goal_id):
    """True when goal is downstream of start."""
    seen, stack = set(), [start_id]
    while stack:
        current = stack.pop()
        if current == goal_id:
            return True
        if current in seen:
            continue
        seen.add(current)
        stack.extend(e["target"] for e in graph.get("edges") or [] if e.get("source") == current)
    return False


def mismatch_message(target_kind, carries, accepts):
    name = label(target_kind)
    if target_kind in MULTIVIEW_OF and carries == VIEWS:
        return f"{name} takes one picture. For pictures of each side, use {label(MULTIVIEW_OF[target_kind])}."
    if target_kind in SINGLE_OF and carries == PICTURE:
        return (f"{name} takes pictures of each side — connect a Multi-view seed or Draw views, or use "
                f"{label(SINGLE_OF[target_kind])} for one picture.")
    wanted = " or ".join(PORT_LABELS.get(a, a).lower() for a in accepts)
    got = "views" if carries == VIEWS else "a " + PORT_LABELS.get(carries, carries or "").lower()
    return f"{name} takes a {wanted} here, not {got}."


def connection_problem(graph, source_id, source_handle, target_id, target_handle="in"):
    """Why the connection can't be made, or None."""
    source, target = node(graph, source_id), node(graph, target_id)
    if source is None or target is None or source_id == target_id:
        return "Those two steps cannot be connected that way."
    carries = output_type(source, source_handle)
    spec = inputs(target.get("kind")).get(target_handle)
    if carries is None or spec is None:
        return "Those two steps cannot be connected that way."
    accepts, _ = spec
    if carries not in accepts:
        return mismatch_message(target["kind"], carries, accepts)
    if reaches(graph, target_id, source_id):
        return "That would loop back on itself."
    return None


def node_problems(graph, n, entitlements=None):
    kind = n.get("kind")
    if is_seed(kind) or not in_scope(n):
        return []
    _, problems = request_for(kind, settings_of(n), incoming_type(graph, n["id"]), entitlements)
    return problems


def run_input_problems(graph):
    found = []
    for n in graph.get("nodes") or []:
        kind, request = n.get("kind"), n.get("request") or {}
        name = (n.get("label") or "").strip()
        if kind == "ai_edit" and not str(request.get("prompt") or "").strip():
            found.append((n["id"], f"{name or 'AI edit'}: say how to change the picture."))
        elif kind == "texture":
            given = (incoming_edge(graph, n["id"], "reference") is not None or str(request.get("prompt") or "").strip()
                     or isinstance(request.get("referenceUploadKey"), str)
                     or isinstance(request.get("referenceImageId"), int))
            if not given:
                found.append((n["id"], f"{name or 'AI texturing'}: describe the texture or add a reference picture."))
        elif kind == "texture_multiview":
            views = request.get("referenceViews") or []
            if incoming_edge(graph, n["id"], "views") is None and not any(
                    isinstance(v, dict) and v.get("viewType") == "front" for v in views):
                found.append((n["id"], f"{name or 'Multi-view - AI texturing'}: add the pictures of each side — a "
                                       "front one at least."))
    return found


def graph_problems(graph):
    """[(node_id or None, message)] for a graph whose requests are built."""
    found = []
    nodes = graph.get("nodes") or []
    if len(nodes) > MAX_NODES:
        found.append((None, "A pipeline holds at most 200 steps."))
    ids = set()
    for n in nodes:
        if not known(n.get("kind")):
            found.append((n["id"], "This step is not one Zetta runs."))
        if n["id"] in ids:
            found.append((n["id"], "Two steps share one id."))
        ids.add(n["id"])
    seeds = [n for n in nodes if is_seed(n.get("kind"))]
    if not seeds:
        found.append((None, "Add a seed — the picture or model the pipeline starts from."))
    for extra in seeds[1:]:
        found.append((extra["id"], "A pipeline starts from one seed."))
    filled = set()
    for e in graph.get("edges") or []:
        source, target = node(graph, e.get("source")), node(graph, e.get("target"))
        if source is None or target is None:
            found.append((None, "A connection points at a step that is gone."))
            continue
        if not known(target.get("kind")):
            continue
        spec = inputs(target["kind"]).get(e.get("targetHandle") or "in")
        carries = output_type(source, e.get("sourceHandle") or "out")
        if spec is None or (known(source.get("kind")) and carries is None):
            found.append((target["id"], "A connection uses a socket this step does not have."))
            continue
        if carries is not None and carries not in spec[0]:
            found.append((target["id"], mismatch_message(target["kind"], carries, spec[0])))
        slot = (target["id"], e.get("targetHandle") or "in")
        if slot in filled:
            found.append((target["id"], "An input takes one connection."))
        filled.add(slot)
    for n in nodes:
        for handle, (_, required) in inputs(n.get("kind")).items():
            if required and (n["id"], handle) not in filled:
                found.append((n["id"], "Connect this step to the one before it."))
                break
    if _has_loop(graph):
        found.append((None, "The pipeline loops back on itself."))
    run_inputs = dict(run_input_problems(graph))
    for n in nodes:
        kind = n.get("kind")
        if is_seed(kind) or not known(kind):
            continue
        request = n.get("request") or {}
        if not set_on_each_run(n) and kind == "ai_edit" and n["id"] in run_inputs:
            found.append((n["id"], "Say how to change the picture — or set it on each run."))
        elif has_engine_choice(kind) and not isinstance(request.get("model"), str):
            found.append((n["id"], "Choose an engine."))
        elif kind == "export" and not isinstance(request.get("format"), str):
            found.append((n["id"], "Choose a file format."))
    return found


def _has_loop(graph):
    edges = graph.get("edges") or []
    indegree = {n["id"]: 0 for n in graph.get("nodes") or []}
    for e in edges:
        if e.get("target") in indegree and e.get("source") in indegree:
            indegree[e["target"]] += 1
    ready = [k for k, v in indegree.items() if v == 0]
    visited = 0
    while ready:
        current = ready.pop()
        visited += 1
        for e in edges:
            if e.get("source") == current and e.get("target") in indegree:
                indegree[e["target"]] -= 1
                if indegree[e["target"]] == 0:
                    ready.append(e["target"])
    return visited < len(indegree)


def problems(graph, entitlements=None):
    """Everything that stops the recipe running: [(node_id or None, message)]."""
    built = with_requests(graph, entitlements)
    found = graph_problems(built)
    for n in graph.get("nodes") or []:
        for message in node_problems(graph, n, entitlements):
            found.append((n["id"], message))
    return found


def smart_uv_warning(graph, n):
    """A warning when the model reaching Smart UV may have more than 80,000 triangles."""
    current = n
    while current is not None:
        edge = incoming_edge(graph, current["id"])
        if edge is None:
            return None
        current = node(graph, edge.get("source"))
        if current is None:
            return None
        kind, settings = current.get("kind"), settings_of(current)
        triangles = None
        if kind == "retopology":
            if (settings.get("engine") or "r1") != "r1":
                return None
            faces = settings.get("faces")
            triangles = faces * (2 if settings.get("polygonType") == "quadrilateral" else 1) if isinstance(
                faces, (int, float)) else None
        elif is_generate(kind):
            count = _js_number(_text(settings, "polygonCount"))
            triangles = count * (2 if _str(settings, "polygonType") == "quadrilateral" else 1) if count else None
        elif kind == "seed_model":
            return None
        else:
            continue
        if triangles and triangles > catalog.SMART_UV_MAX_TRIANGLES:
            name = (current.get("label") or "").strip() or kind.replace("_", " ").capitalize()
            return (f"{name} can make about {int(triangles):,} triangles — over Smart UV's 80,000. Add Retopology "
                    "before it, or lower the polygon count.")
        return None
    return None


# ─── Prices and summaries ─────────────────────────────────────────────────────────────────────────


def price_for(graph, n, prices, entitlements=None):
    kind, settings = n.get("kind"), settings_of(n)
    if is_generate(kind):
        request, problems_found = request_for(kind, settings, incoming_type(graph, n["id"]), entitlements)
        generation = ((request or {}).get("spec") or {}).get("generation")
        if not generation:
            return None
        return requests.price_total(prices, request.get("model"), [generation["base"], *generation["modifiers"]])
    if kind == "generate_splat":
        return requests.price_total(prices, settings.get("engine") or "n25", ["gaussian_splat"])
    if kind == "retopology":
        return requests.price_of(prices, "smart_topology", settings.get("engine") or "r1")
    if kind in ("segmentation", "smart_segmentation"):
        code = "smart_segmentation" if settings.get("mode") == "smart" else "ai_segmentation"
        return requests.price_of(prices, code)
    if kind in ("texture", "texture_multiview"):
        return requests.price_of(prices, settings.get("tier") or "retexture")
    if kind == "smart_uv":
        return requests.price_of(prices, "smart_uv")
    if kind == "rigging":
        return requests.price_of(prices, "auto_rigging")
    return 0


def fan_out_nodes(graph):
    """Every step downstream of an Isolate parts "each" output: it runs once per part."""
    found = set()
    for e in graph.get("edges") or []:
        if e.get("sourceHandle") == "each":
            stack = [e["target"]]
            while stack:
                current = stack.pop()
                if current in found:
                    continue
                found.add(current)
                stack.extend(x["target"] for x in graph.get("edges") or [] if x.get("source") == current)
    return found


def estimate(graph, prices, entitlements=None):
    """(credits per run or None, per_part)."""
    per_part = fan_out_nodes(graph)
    max_parts = 1
    for n in graph.get("nodes") or []:
        if n.get("kind") == "isolate_parts":
            max_parts = max(max_parts, int(settings_of(n).get("maxParts") or 6))
    total = 0
    for n in graph.get("nodes") or []:
        value = price_for(graph, n, prices, entitlements)
        if value is None:
            return None, bool(per_part)
        total += value * (max_parts if n["id"] in per_part else 1)
    return total, bool(per_part)


def estimate_label(graph, prices, entitlements=None):
    credits, per_part = estimate(graph, prices, entitlements)
    if credits is None:
        return "—"
    return f"{'Up to ' if per_part else ''}{credits:,} credits a run"


def price_label(value, per_part=False):
    if value is None:
        return "—"
    if value == 0:
        return "Free"
    return f"{value:,} cr" + (" / part" if per_part else "")


def summary(n):
    kind, settings = n.get("kind"), settings_of(n)
    if kind == "seed_picture":
        return "A picture, chosen for each run"
    if kind == "seed_model":
        return "A model, chosen for each run"
    if kind == "seed_views":
        return "Pictures of each side, chosen for each run"
    if kind == "remove_background":
        return "Cut-out"
    if kind == "ai_edit":
        prompt = (settings.get("prompt") or "").strip()
        if settings.get("askAtRun") and not prompt:
            return "Set on each run"
        return (prompt[:47] + "…" if len(prompt) > 48 else prompt) or "Say how to change it"
    if kind == "draw_views":
        views = settings.get("views") or []
        return "Front + " + ", ".join(views) if views else "No views chosen"
    if kind == "isolate_parts":
        level = {"low": "Low", "medium": "Medium", "high": "High"}.get(settings.get("sensitivity"), "Low")
        if settings.get("identify") is False:
            level = "Your list"
        return f"{level} · up to {settings.get('maxParts') or 6} · {(settings.get('pose') or 't').upper()}-pose"
    if is_generate(kind):
        return f"{catalog.model_label(_str(settings, 'model'))} · {'Geometry' if _str(settings, 'output') == 'geometry' else 'Textured'}"
    if kind == "generate_splat":
        names = {"n25": "Zetta Splat", "zs2": "Zetta Splat 2", "zs21": "Zetta Splat 2.1"}
        return f"{names.get(settings.get('engine'), 'Zetta Splat')} · {(settings.get('subject') or 'object').capitalize()}"
    if kind == "retopology":
        if (settings.get("engine") or "r1") == "r1":
            faces = settings.get("faces")
            return f"Zetta R1 · {int(faces):,} faces" if isinstance(faces, (int, float)) else "Zetta R1"
        return f"{catalog.model_label(settings.get('engine'))} · {settings.get('faceLevel') or 'medium'}"
    if kind in ("segmentation", "smart_segmentation"):
        return "Smart" if settings.get("mode") == "smart" else "AI segmentation"
    tiers = {"retexture": "Standard", "retexture_hd": "Detailed", "retexture_8k": "8K"}
    if kind == "texture":
        tier = tiers.get(settings.get("tier"), "Standard")
        reference = settings.get("reference")
        prompt = (settings.get("prompt") or "").strip()
        if reference:
            return f"{tier} · from a picture"
        if prompt:
            return f"{tier} · {prompt[:31] + '…' if len(prompt) > 32 else prompt}"
        return f"{tier} · set on each run"
    if kind == "texture_multiview":
        tier = tiers.get(settings.get("tier"), "Standard")
        count = len(views_request(settings.get("referenceViews")))
        return f"{tier} · {count} view{'s' if count != 1 else ''}" if count else f"{tier} · pictures of each side"
    if kind == "smart_uv":
        return "Textures re-baked" if settings.get("bake", True) else "Layout only"
    if kind == "rigging":
        return f"{dict(catalog.RIG_TYPES).get(settings.get('rigType'), 'Humanoid')} rig"
    if kind == "export":
        return (settings.get("format") or "glb").upper()
    return ""


# ─── Edits (each returns a new graph) ─────────────────────────────────────────────────────────────


def ordered(graph):
    """[(node, depth, parent, parent output)] in reading order: the seed first, then each step after the one
    feeding it (depth first)."""
    nodes = graph.get("nodes") or []
    by_id = {n["id"]: n for n in nodes}
    result, seen = [], set()

    def visit(node_id, depth, parent, via):
        if node_id in seen or node_id not in by_id:
            return
        seen.add(node_id)
        result.append((by_id[node_id], depth, parent, via))
        outgoing = sorted(children(graph, node_id), key=lambda e: (by_id.get(e["target"], {}).get("position") or {})
                          .get("y", 0))
        for e in outgoing:
            visit(e["target"], depth + 1, by_id[node_id], e.get("sourceHandle") or "out")

    roots = [n for n in nodes if not any(e.get("target") == n["id"] for e in graph.get("edges") or [])]
    roots.sort(key=lambda n: (0 if is_seed(n.get("kind")) else 1, (n.get("position") or {}).get("x", 0)))
    for n in roots:
        visit(n["id"], 0, None, None)
    for n in nodes:
        visit(n["id"], 0, None, None)
    return result


def add_step(graph, kind, after=None, output=None, entitlements=None):
    """(new graph, new node id) or raises ValueError with the reason."""
    if not known(kind):
        raise ValueError(f"“{kind}” is not a step Zetta runs.")
    if not offered(kind):
        raise ValueError(f"{label(kind)} is not available in Blender.")
    if is_seed(kind) and seed(graph):
        raise ValueError("A pipeline starts from one seed.")
    graph = copy.deepcopy(graph)
    previous = node(graph, after) if after else None
    if after and previous is None:
        raise ValueError(f"There is no step {after}.")
    count = len(graph["nodes"])
    if previous is not None:
        position = {"x": (previous.get("position") or {}).get("x", 0) + 300,
                    "y": (previous.get("position") or {}).get("y", 0) + 180 * len(children(graph, after))}
    else:
        position = {"x": count * 60, "y": count * 40}
    new = {"id": new_id(kind.replace("_", "-")), "kind": kind, "position": position,
           "settings": with_defaults(kind, None), "request": {}}
    graph["nodes"].append(new)
    if previous is not None:
        handles = [output] if output else list(outputs(previous))
        if previous.get("kind") == "isolate_parts" and not output:
            handles = ["character"]
        for handle in handles:
            if connection_problem(graph, after, handle, new["id"]) is None:
                graph["edges"].append({"id": new_id("e"), "source": after, "sourceHandle": handle,
                                       "target": new["id"], "targetHandle": "in"})
                break
    request, _ = request_for(kind, new["settings"], incoming_type(graph, new["id"]), entitlements)
    new["request"] = request or {}
    return graph, new["id"]


def remove_step(graph, node_id, entitlements=None):
    graph = copy.deepcopy(graph)
    graph["nodes"] = [n for n in graph["nodes"] if n["id"] != node_id]
    graph["edges"] = [e for e in graph["edges"] if e.get("source") != node_id and e.get("target") != node_id]
    return with_requests(graph, entitlements)


def connect(graph, source_id, source_handle, target_id, target_handle="in", entitlements=None):
    reason = connection_problem(graph, source_id, source_handle, target_id, target_handle)
    if reason:
        raise ValueError(reason)
    graph = copy.deepcopy(graph)
    graph["edges"] = [e for e in graph["edges"] if not (e.get("target") == target_id
                                                        and (e.get("targetHandle") or "in") == target_handle)]
    graph["edges"].append({"id": new_id("e"), "source": source_id, "sourceHandle": source_handle,
                           "target": target_id, "targetHandle": target_handle})
    return with_requests(graph, entitlements)


def disconnect(graph, target_id, target_handle="in", entitlements=None):
    graph = copy.deepcopy(graph)
    graph["edges"] = [e for e in graph["edges"] if not (e.get("target") == target_id
                                                        and (e.get("targetHandle") or "in") == target_handle)]
    return with_requests(graph, entitlements)


def patch_settings(graph, node_id, patch, entitlements=None):
    graph = copy.deepcopy(graph)
    n = node(graph, node_id)
    if n is None:
        raise ValueError(f"There is no step {node_id}.")
    if not in_scope(n):
        raise ValueError(f"{node_name(n)} cannot be changed in Blender.")
    current = with_defaults(n["kind"], n.get("settings"))
    if is_generate(n["kind"]):
        n["settings"] = apply_patch(current, patch)
    else:
        current.update(copy.deepcopy(patch))
        n["settings"] = current
    request, _ = request_for(n["kind"], n["settings"], incoming_type(graph, node_id), entitlements)
    n["request"] = request or {}
    return graph


def set_label(graph, node_id, text):
    graph = copy.deepcopy(graph)
    n = node(graph, node_id)
    if n is not None:
        text = (text or "").strip()[:80]
        if text:
            n["label"] = text
        else:
            n.pop("label", None)
    return graph


def run_node_change(run_graph, node_id, patch, entitlements=None):
    """(settings, request) for a per-run change to one step of a run that has not reached it."""
    n = node(run_graph, node_id)
    settings = with_defaults(n["kind"], n.get("settings"))
    if is_generate(n["kind"]):
        settings = apply_patch(settings, patch)
    else:
        settings.update(copy.deepcopy(patch))
    request, _ = request_for(n["kind"], settings, incoming_type(run_graph, node_id), entitlements)
    return settings, request or {}


def learn_parts(graph, run):
    """Adds any part a run's Isolate parts step found to the recipe step's outputs. Returns (graph, changed)."""
    graph = copy.deepcopy(graph)
    changed = False
    for step in run.get("steps") or []:
        output = step.get("output") or {}
        if output.get("type") != "isolated":
            continue
        n = node(graph, step.get("nodeId"))
        if n is None or n.get("kind") != "isolate_parts":
            continue
        settings = n.setdefault("settings", {})
        known_parts = settings.setdefault("knownParts", [])
        keys = {p.get("key") for p in known_parts}
        for part in output.get("parts") or []:
            if part.get("key") and part["key"] not in keys:
                known_parts.append({"key": part["key"], "label": part.get("label") or part["key"]})
                keys.add(part["key"])
                changed = True
    return graph, changed


def same(a, b):
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
