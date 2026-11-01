"""The finishing tools — Segment, Retopology, Smart UV, Texture, Auto Rig — and the source model each one works on.

Each runs one post-process operation on a finished library row: `POST /3d/generations` with
`{spec: {postProcess: [code]}, sourcePostId, …options}`. The source is picked from the library, or taken from the
selection: a model imported from Zetta 3D is used directly; anything else is exported as GLB and uploaded to the
library first (free).
"""

import base64

import bpy

from .. import importer
from ..api import catalog, models, requests, zetta
from ..core import tasks
from . import capture, common, context, generate, pictures

TOOLS = ("segment", "retopology", "smart_uv", "texture", "rig")
TITLES = {"segment": "Segment", "retopology": "Retopology", "smart_uv": "Smart UV", "texture": "Texture",
          "rig": "Auto Rig"}
MAX_UPLOAD_BYTES = 100 * 1024 * 1024

sources = {}  # tool → the source generation (dict)

# ─── Sources ──────────────────────────────────────────────────────────────────────────────────────


def refusal(tool, gen):
    """Why a tool can't take this row, or None."""
    if gen.get("status") != "success":
        return "That model is not finished yet."
    if models.is_splat_row(gen):
        return "Gaussian splats can't be processed here."
    feature = gen.get("selectedFeature")
    if feature not in catalog.PROCESSABLE_FEATURES:
        return f"{catalog.feature_label(feature)} results cannot be used here. Pick a generated or uploaded model."
    if tool == "retopology" and feature == "retopology":
        return "This model is already a retopology result."
    if tool == "smart_uv" and feature == "uvUnwrap":
        return "This model already has a Smart UV layout."
    return None


def set_source(tool, gen):
    """Returns the refusal (and keeps the old source), or None."""
    reason = refusal(tool, gen)
    status = common.status(tool)
    if reason:
        status.fail(common.Problem(reason))
        return reason
    sources[tool] = gen
    status.error = status.done = None
    if tool == "texture":
        preview.update(picture=None, prompt=None, error=None)
    return None


def clear_source(tool):
    sources.pop(tool, None)


def selection_roots(context_objects):
    """The selected objects' top-level Zetta imports, or the selection itself."""
    return [o for o in context_objects if o.parent is None or o.parent not in context_objects]


def zetta_id_of(objects):
    for obj in objects:
        current = obj
        while current is not None:
            value = current.get(importer.ID_PROP)
            if value is not None:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return None
            current = current.parent
    return None


def source_from_selection(tool, objects):
    """Uses the selection as the source: a Zetta import is found in the library; anything else is uploaded."""
    status = common.status(tool)
    if not objects:
        status.fail(common.Problem("Select a model in the 3D view first."))
        return
    gen_id = zetta_id_of(objects)
    title = objects[0].get("zetta_title") or objects[0].name

    def upload():
        status.start("Uploading the model… 0%")

        def progress(fraction):
            tasks.post(status.stage, f"Uploading the model… {int(fraction * 100)}%")

        def done(gen):
            status.finish(None)
            set_source(tool, gen)

        importer.upload_objects(objects, title, done, lambda message: status.fail(common.Problem(message)), progress)

    if gen_id is None:
        upload()
        return
    status.start("Finding it in your library…")

    def found(result):
        gen, _ = result
        status.finish(None)
        if gen and gen.get("status") == "success":
            set_source(tool, gen)
        else:
            upload()

    def missing(error):
        if isinstance(error, zetta.ApiError) and error.status == 404:
            upload()  # another account's model, or deleted: upload it as it is
        else:
            status.fail(error)

    tasks.run(lambda: zetta.get_generation(gen_id), found, missing, name="zetta3d-find-source")


# The library browser inside a tool's source card.
browser = {"tool": None, "items": [], "page": 0, "total": 0, "loading": False, "error": None, "search": ""}


def browse(tool, search="", more=False):
    if browser["loading"]:
        return
    if not more or browser["tool"] != tool or browser["search"] != search:
        browser.update(tool=tool, items=[], page=0, total=0, search=search)
    page = browser["page"] + 1
    browser.update(loading=True, error=None)

    def work():
        return zetta.list_generations(page=page, page_size=24, search=search, statuses=["success"])

    def done(answer):
        rows = [g for g in answer.get("items") or [] if g.get("status") == "success"
                and g.get("selectedFeature") in catalog.PROCESSABLE_FEATURES and not models.is_splat_row(g)]
        browser["items"].extend(rows)
        browser.update(page=page, total=int(answer.get("total") or 0), loading=False)

    def fail(error):
        browser.update(loading=False, error=catalog.explain(error))

    tasks.run(work, done, fail, name="zetta3d-browse")


def close_browser():
    browser.update(tool=None, items=[], page=0, total=0)


def browser_has_more():
    return browser["total"] > browser["page"] * 24


# ─── Per-tool rules ───────────────────────────────────────────────────────────────────────────────


def code(tool, state):
    if tool == "segment":
        return "smart_segmentation" if state.seg_type == "smart" else "ai_segmentation"
    if tool == "retopology":
        return "smart_topology"
    if tool == "smart_uv":
        return "smart_uv"
    if tool == "texture":
        return state.tex_tier
    return "auto_rigging"


def price(tool, state):
    return context.price(code(tool, state), state.retopo_engine if tool == "retopology" else None)


def retopo_max(state):
    return 10_000 if state.retopo_polygon == "quadrilateral" else 20_000


def retopo_faces(state):
    text = state.retopo_faces.replace(",", "").strip()
    return int(text) if text.isdigit() else None


def source_triangles(gen):
    count = gen.get("faceCount")
    if not isinstance(count, int) or count <= 0:
        return None
    return count * 2 if gen.get("polygonType") == "quadrilateral" else count


def tool_problem(tool, state):
    status = common.status(tool)
    if status.busy:
        return status.busy
    if context.gate3("allowsPostProcess") is False:
        return "Upgrade to use the finishing tools."
    source = sources.get(tool)
    if source is None:
        return "Choose a source model."
    if tool == "retopology" and state.retopo_engine == "r1":
        faces = retopo_faces(state)
        if faces is None or not 500 <= faces <= retopo_max(state):
            return f"Choose a polygon count between 500 and {retopo_max(state):,}."
    if tool == "smart_uv":
        triangles = source_triangles(source)
        if triangles is not None and triangles > catalog.SMART_UV_MAX_TRIANGLES:
            return (f"Smart UV takes models up to 80,000 triangles — this one has {triangles:,}. Run Retopology first "
                    "to bring it down.")
    if tool == "texture":
        return texture_problem(state, source)
    if tool == "rig" and detecting:
        return "Detecting the body type…"
    return None


def tool_options(tool, state):
    if tool == "segment":
        options = {"segmentGranularity": state.seg_granularity}
        if state.seg_type == "segment" and not state.seg_split:
            options["splitByConnectivity"] = False
        if state.seg_type == "smart" and state.seg_hint.strip():
            options["prompt"] = state.seg_hint.strip()
        return options
    if tool == "retopology":
        polygon = state.retopo_polygon
        if context.gate3("allowsQuadMesh") is False:
            polygon = "triangle"
        options = {"model": state.retopo_engine, "polygonType": polygon}
        if state.retopo_engine == "r1":
            options["faceCount"] = retopo_faces(state)
            if not state.retopo_bake:
                options["bake"] = False
        else:
            options["faceLevel"] = state.retopo_level
        return options
    if tool == "smart_uv":
        options = {}
        if state.uv_orientation != "as-is":
            options["exportOrientation"] = state.uv_orientation
        if not state.uv_bake:
            options["bake"] = False
        if state.uv_compress:
            options["compressGeometry"] = True
        return options
    if tool == "texture":
        options = {"textureAlignment": state.tex_alignment, "enablePBR": state.tex_pbr, "delight": state.tex_delight}
        if not state.tex_bake:
            options["bake"] = False
        if state.tex_fast and state.tex_tier == "retexture":
            options["textureFast"] = True
        return options
    options = {}
    if state.rig_type != catalog.HUMANOID_RIG_TYPE:
        options["rigType"] = state.rig_type
    elif state.rig_spec != "standard":
        options["rigSpec"] = state.rig_spec
    if state.rig_format == "fbx":
        options["rigOutFormat"] = "fbx"
    return options


def submit(tool, state):
    status = common.status(tool)
    reason = tool_problem(tool, state)
    if reason:
        status.fail(common.Problem(reason))
        return
    if tool == "texture":
        _submit_texture(state)
        return
    source = sources[tool]
    body = requests.post_process(code(tool, state), source["id"], tool_options(tool, state))
    _start(tool, state, body, source)


def _start(tool, state, body, source, work_before=None):
    status = common.status(tool)
    status.start("Starting…")

    def work():
        if work_before is not None:
            work_before(body)
        tasks.post(status.stage, "Starting…")
        return zetta.create_generation(body)

    def done(result):
        label = common.shorten(f"{TITLES[tool]} · {models.display_title(source)}")
        status.finish(common.track(result, label, state))

    tasks.run(work, done, status.fail, name=f"zetta3d-{tool}")


# ─── Texture ──────────────────────────────────────────────────────────────────────────────────────

TEXTURE_SINGLE = "tex_single"
preview = {"picture": None, "prompt": None, "busy": False, "error": None}


def texture_view_slot(view):
    return f"tex_mv_{view}"


def texture_reference_hint(state):
    source = sources.get("texture")
    if pictures.get(TEXTURE_SINGLE):
        return "Your picture is the reference."
    if source and _source_input_url(source):
        return "No picture chosen: the model's own input picture is the reference."
    if source and importer.objects_for(int(source["id"])):
        return "No picture chosen: a render of the imported model is the reference."
    return "Choose a reference picture, or describe the surface."


def _source_input_url(source):
    for image in source.get("inputImages") or []:
        if image.get("url"):
            return image["url"]
    return None


def texture_problem(state, source):
    if state.tex_mode == "multi":
        if not pictures.get(texture_view_slot("front")):
            return "Add a front view — it is required."
        if generate.generating_views.get("texture"):
            return "Generating views…"
        return None
    if preview["busy"]:
        return "Drawing the preview…"
    description = state.tex_prompt.strip()
    if description and preview["prompt"] != description:
        return "Press Preview to see the new surface first."
    if (not description and not pictures.get(TEXTURE_SINGLE) and not _source_input_url(source)
            and not importer.objects_for(int(source["id"]))):
        return "Choose a reference picture, or describe the surface."
    return None


def _render_reference(source):
    """A front render of the imported copy, as a Picture (main thread), or None."""
    objects = [o for o in importer.objects_for(int(source["id"])) if o.parent is None
               or o.parent.get(importer.ID_PROP) is None]
    if not objects:
        return None
    try:
        data = capture.shots(objects, yaws=(0,), size=768, quality=90)[0]
    except Exception as error:  # noqa: BLE001
        print(f"[Zetta 3D] Could not render the reference: {error}")
        return None
    return pictures.Picture(data, "image/jpeg", "render.jpg")


def make_preview(state):
    """Draws the described surface (from the reference when there is one) for approval."""
    source = sources.get("texture")
    description = state.tex_prompt.strip()
    if not description or source is None:
        return
    own = pictures.get(TEXTURE_SINGLE)
    rendered = None if own or _source_input_url(source) else _render_reference(source)
    url = None if own or rendered else _source_input_url(source)
    preview.update(busy=True, error=None)

    def work():
        reference = own or rendered
        if reference is None and url:
            data = zetta.http.fetch_bytes(url, timeout=60)
            reference = pictures.Picture(data, pictures.sniff(data), "reference")
        if reference is not None:
            result = zetta.edit_image(description, image=reference.inline(), for_3d=True, pipeline_step=True)
        else:
            result = zetta.generate_image(description, for_3d=True)
        images = [i for i in result.get("images") or [] if i.get("base64")]
        if not images:
            raise common.Problem(result.get("text") or "No picture came back. Try again.")
        data = base64.b64decode(images[0]["base64"])
        return pictures.Picture(data, images[0].get("contentType") or pictures.sniff(data), "surface.png")

    def done(picture):
        preview.update(picture=picture, prompt=description, busy=False)
        context.refresh_credits()

    def fail(error):
        preview.update(busy=False, error=catalog.explain(error) if isinstance(error, zetta.ApiError) else str(error))

    tasks.run(work, done, fail, name="zetta3d-texture-preview")


def _submit_texture(state):
    source = sources["texture"]
    body = requests.post_process(state.tex_tier, source["id"], tool_options("texture", state))
    remove_background = state.tex_remove_bg
    status = common.status("texture")
    if state.tex_mode == "multi":
        filled = [(v, pictures.get(texture_view_slot(v))) for v in generate.SUBMISSION_ORDER
                  if pictures.get(texture_view_slot(v))]

        def before(b):
            views = []
            for index, (view, picture) in enumerate(filled):
                tasks.post(status.stage, f"Uploading views… {int(100 * index / len(filled))}%")
                views.append({"viewType": view, "imageUrl": common.upload_picture(picture, remove_background)})
            b["multiViewImages"] = views

        _start("texture", state, body, source, before)
        return

    description = state.tex_prompt.strip()
    picture = preview["picture"] if description else pictures.get(TEXTURE_SINGLE)
    if picture is None and not description:
        picture = _render_reference(source) if not _source_input_url(source) else None
    url = None if picture is not None else _source_input_url(source)

    def before(b):
        reference = picture
        if reference is None and url:
            data = zetta.http.fetch_bytes(url, timeout=60)
            reference = pictures.Picture(data, pictures.sniff(data), "reference")
        if reference is None:
            raise common.Problem("Choose a reference picture, or describe the surface.")
        b["imageUrl"] = common.upload_picture(reference, remove_background,
                                              lambda text: tasks.post(status.stage, text))

    _start("texture", state, body, source, before)


# ─── Auto Rig: detect the body ────────────────────────────────────────────────────────────────────

detecting = False


def detect_body(state):
    global detecting
    status = common.status("rig")
    source = sources.get("rig")
    if source is None:
        return
    objects = [o for o in importer.objects_for(int(source["id"]))
               if o.parent is None or o.parent.get(importer.ID_PROP) is None]
    if not objects:
        status.note = "Import the model first to detect its body — or choose the body yourself."
        return
    try:
        frames = capture.shots(objects)
    except Exception:  # noqa: BLE001
        status.note = "That could not be detected. Choose the body yourself."
        return
    detecting = True
    status.note = None
    window_manager = bpy.context.window_manager

    def done(result):
        global detecting
        detecting = False
        body_type, _confidence, reason = result
        labels = dict(catalog.RIG_TYPES)
        if body_type in labels:
            window_manager.zetta.rig_type = body_type
            status.note = f"Detected: {labels[body_type]}. {reason or ''}".strip()
        else:
            status.note = f"Could not tell what this is. {reason or ''}".strip()

    def fail(_error):
        global detecting
        detecting = False
        status.note = "That could not be detected. Choose the body yourself."

    tasks.run(lambda: zetta.rigging_precheck(frames), done, fail, name="zetta3d-rig-detect")
