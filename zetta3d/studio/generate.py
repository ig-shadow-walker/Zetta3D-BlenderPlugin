"""Generate 3D and Smart 3D: the form's rules, what it sends, and the submit flow.

A port of the Unity GeneratePanel / GenerateParameters (themselves ports of the web Studio). Generate offers
Zetta N1 and N2.5, Smart 3D Zetta Np1 and Np1.5; both share this module, each with its own form and pictures.
"""

from ..api import catalog, requests, zetta
from ..core import tasks
from . import common, context, pictures

VIEWS = ("front", "back", "left", "right")
SUBMISSION_ORDER = ("left", "right", "back", "front")  # the server promotes front to the primary image
PLACEHOLDER = "https://zetta3d.ai/placeholder.png"
N1_MAX = 1_480_000
_QUALITY_INDEX = {q: i for i, (q, _) in enumerate(catalog.QUALITIES)}
_POLYGON_INDEX = {"triangle": 0, "quadrilateral": 1}
_OUTPUT_INDEX = {"geometry": 0, "textured": 1}

EDIT_PREAMBLE = (
    "The attached image is a reference photograph of a single object. Re-render that SAME object from a different "
    "camera angle. Keep its shape, proportions, colours, materials and surface details identical — this is one "
    "object photographed again, not a new design, so do not restyle it and do not add or remove any part of it. "
    "Place the camera "
)
EDIT_ANGLES = {
    "front": "directly in front of the object at its own eye level, looking straight at it head-on.",
    "back": "directly behind the object at its own eye level. The face that pointed at the camera in the reference "
            "is now hidden, and what was hidden is now facing us.",
    "left": "directly beside the object's left flank at its own eye level, looking horizontally at it. A flat "
            "side-on profile: the front and the back are both hidden, and the opposite flank is entirely behind the "
            "object. In the frame the object's FRONT points to the LEFT edge and its BACK to the RIGHT edge.",
    "right": "directly beside the object's right flank at its own eye level, looking horizontally at it. A flat "
             "side-on profile: the front and the back are both hidden, and the opposite flank is entirely behind the "
             "object. In the frame the object's FRONT points to the RIGHT edge and its BACK to the LEFT edge — this "
             "is the mirror image of the left-hand view, not a repeat of it.",
}

fork_note = {}  # tab → "Forked from …" until the next job starts


def tab_of(form):
    return "smart" if form.is_smart else "generate"


def single_slot(form):
    return f"{tab_of(form)}_single"


def view_slot(form, view):
    return f"{tab_of(form)}_mv_{view}"


# ─── Form rules (written straight to the ID properties so they don't re-trigger the update callbacks) ─────


def _set_quality(form, quality):
    form["quality"] = _QUALITY_INDEX[quality]


def _set_polygon(form, polygon):
    form["polygon_type"] = _POLYGON_INDEX[polygon]


def _set_output(form, output):
    form["output"] = _OUTPUT_INDEX[output]


def _preset(form):
    return catalog.face_preset(form.model, form.quality, form.polygon_type == "quadrilateral")


def _reset_count(form):
    form["face_count"] = str(_preset(form))
    form["face_typed"] = False


def apply_invariants(form):
    model = catalog.model(form.model)
    if form.quality == "low-poly" and form.model in ("n1", "n25"):
        if form.model != "n25":
            _set_output(form, "textured")
        form["segment_parts"] = False
    elif form.segment_parts and model.parts:
        _set_output(form, "geometry")
        _set_polygon(form, "triangle")


def apply_model(form):
    model = catalog.model(form.model)
    if form.quality == "low-poly" and not model.low_poly:
        _set_quality(form, "high-res")
    if form.texture_quality == "extreme" and not model.extreme_texture:
        form["texture_quality"] = 1  # detailed
    if form.polygon_type == "quadrilateral" and not (model.quad_any_base or model.low_poly):
        _set_polygon(form, "triangle")
    if not model.parts:
        form["segment_parts"] = False
    apply_invariants(form)
    count = parse_count(form.face_count)
    low, high = face_bounds(form)
    if not form.face_typed or count is None or not low <= count <= high:
        _reset_count(form)


def apply_quality(form):
    apply_invariants(form)
    _reset_count(form)


def apply_polygon_type(form):
    if not form.face_typed:
        _reset_count(form)


def apply_count(form):
    # Zetta N1 has no rungs of its own: a typed count picks the rung it belongs to.
    count = parse_count(form.face_count)
    if form.model == "n1" and count is not None and 3_100 <= count <= N1_MAX:
        _set_quality(form, "ultra" if count >= 1_000_000 else "high-res" if count > 15_000 else "low-poly")
        apply_invariants(form)


def apply_plan_defaults(form):
    """Once the plan is known: free plans default to geometry; a plan with N2.5 starts on it. Never over a choice
    the user made."""
    if not form.output_picked and context.gate3("allowsPrivateGenerations") is False:
        _set_output(form, "geometry")
    if not form.is_smart and not form.model_picked and context.gate3("allowsZettaN25") is True and form.model == "n1":
        form["model"] = 1  # n25
        apply_model(form)
    if not form.private_touched:
        form["private"] = context.gate3("allowsPrivateGenerations") is True
    if not form.face_count:
        _reset_count(form)


def parse_count(text):
    text = (text or "").replace(",", "").replace("_", "").strip()
    return int(text) if text.isdigit() else None


def face_bounds(form):
    low, high = catalog.face_range(form.model, form.quality, form.polygon_type == "quadrilateral")
    if form.model == "n1":
        high = min(high, N1_MAX)
    return low, high


def may_choose_count():
    return context.gate3("allowsCustomFaceCount") is not False


# ─── Locks and hints (for the panel) ──────────────────────────────────────────────────────────────


def engine_lock(model_id):
    if model_id == "n25" and context.gate3("allowsZettaN25") is False:
        return "Zetta N2.5 is available on a paid plan."
    if model_id in ("np1", "np15") and context.gate3("allowsZettaNp1") is False:
        return "Available on a paid plan."
    return None


def quality_lock(form, quality):
    if context.gate3("allowsCustomFaceCount") is False:
        return ("Your plan generates at the default quality — 500,000 polygons. Choosing a quality is available on "
                "a paid plan.")
    max_faces = context.gate3("maxFaceCount")
    if quality == "ultra" and isinstance(max_faces, int) and max_faces < 1_000_000:
        return "Ultra detail is available from Basic."
    if quality == "low-poly":
        if context.gate3("allowsLowPoly") is False:
            return "Low-poly generation is available on a paid plan."
        if not catalog.model(form.model).low_poly:
            return "Low-poly generation needs Zetta N1."
    return None


def multi_view_budget(_form=None):
    value = context.gate3("maxMultiViewImages")
    return max(0, min(4, value if isinstance(value, int) else 4))


def multi_view_locked():
    value = context.gate3("maxMultiViewImages")
    return isinstance(value, int) and value <= 0


# ─── What is sent ─────────────────────────────────────────────────────────────────────────────────


def views(form):
    """[(view, Picture)] for the filled multi-view slots, in slot order."""
    return [(v, pictures.get(view_slot(form, v))) for v in VIEWS if pictures.get(view_slot(form, v))]


def reference_view(form):
    filled = views(form)
    return filled[0] if filled else None


def has_own_picture(form):
    picture = pictures.get(single_slot(form))
    return picture is not None and picture.drawn_for is None


def drawn_is_current(form):
    picture = pictures.get(single_slot(form))
    return picture is not None and picture.drawn_for is not None and picture.drawn_for == form.prompt.strip()


def sends_bare_prompt(form):
    return catalog.model(form.model).bare_prompt and not form.control_mode and not drawn_is_current(form)


def next_is_drawing(form):
    return (
        form.source_mode == "single"
        and not has_own_picture(form)
        and not drawn_is_current(form)
        and bool(form.prompt.strip())
        and not sends_bare_prompt(form)
    )


def options(form, image_url="", view_urls=None):
    """The GenerateOptions the form stands for (Unity's Snapshot)."""
    model = catalog.model(form.model)
    multi = form.source_mode == "multi"
    textured = form.output == "textured"
    file_attached = (multi and bool(views(form))) or (not multi and has_own_picture(form))
    count = parse_count(form.face_count) if may_choose_count() else None
    availability = None
    if form.private_touched and context.gate3("allowsPrivateGenerations") is not False:
        availability = "private" if form.private else "public"
    return requests.GenerateOptions(
        prompt=form.prompt if not multi else "",
        image_url=image_url,
        views=list(view_urls or []),
        model=form.model,
        quality="high-res" if form.is_smart else form.quality,
        output=form.output,
        pbr=form.pbr,
        texture_quality=form.texture_quality,
        face_count=count,
        polygon_type=form.polygon_type,
        title=form.title if file_attached else "",
        segment_parts=form.segment_parts and model.parts and form.quality != "low-poly",
        real_world_scale=form.real_world_scale and textured,
        delight=form.delight and textured,
        autofix_image=form.autofix_image,
        compress_geometry=form.compress_geometry,
        texture_alignment=form.texture_alignment if textured else "original_image",
        export_uv=(not textured) or form.export_uv,
        control_mode=form.control_mode and not multi,
        availability=availability,
    )


def preview(form):
    """(built request, price) with placeholder URLs in place of the uploads still to come."""
    multi = form.source_mode == "multi"
    if multi:
        view_urls = [(v, PLACEHOLDER) for v in SUBMISSION_ORDER if pictures.get(view_slot(form, v))]
        o = options(form, view_urls=view_urls)
    elif has_own_picture(form):
        o = options(form, image_url=PLACEHOLDER)
        o.prompt = ""
    elif next_is_drawing(form) or drawn_is_current(form):
        o = options(form, image_url=PLACEHOLDER)
        o.image_from_prompt = True
    else:
        o = options(form)
    built = requests.build(o, context.gates_3d)
    return built, requests.price(context.prices, built, form.model)


def problem(form):
    """Why the button is disabled, or None."""
    status = common.status(tab_of(form))
    if status.busy:
        return status.busy
    multi = form.source_mode == "multi"
    if multi and not views(form):
        return "Add at least one view to generate from."
    if not multi and not form.prompt.strip() and pictures.get(single_slot(form)) is None:
        return "Describe what to make, or attach an image."
    if multi and generating_views.get(tab_of(form)):
        return "Generating views…"
    if may_choose_count() and form.face_count.strip():
        count = parse_count(form.face_count)
        low, high = face_bounds(form)
        if count is None:
            return "The polygon count must be a whole number."
        if not low <= count <= high:
            return f"The polygon count must be between {low:,} and {high:,}."
    built, _ = preview(form)
    for item in built.problems:
        if "draw one from the prompt first" not in item:
            return item
    plan = plan_problems(form, built)
    if plan:
        return " ".join(plan) + " Upgrade at zetta3d.ai/pricing."
    if context.prices is None and context.loading():
        return "Loading prices…"
    return None


def plan_problems(form, built):
    found = []
    if form.model == "n25" and context.gate3("allowsZettaN25") is False:
        found.append("Zetta N2.5 is available on a paid plan.")
    if form.model in ("np1", "np15") and context.gate3("allowsZettaNp1") is False:
        found.append("Generate Smart 3D is available on a paid plan.")
    if built.base == "lowpoly" and context.gate3("allowsLowPoly") is False:
        found.append("Low-poly generation is available on a paid plan.")
    if (built.body or {}).get("polygonType") == "quadrilateral" and context.gate3("allowsQuadMesh") is False:
        found.append("Quad meshes are available from Basic.")
    max_faces = context.gate3("maxFaceCount")
    if isinstance(max_faces, int) and max_faces < 1_000_000:
        if "detailed_geometry" in built.modifiers:
            found.append("Ultra detail is available from Basic.")
        elif (built.body or {}).get("faceCount", 0) >= min(1_000_000, max_faces + 1):
            found.append(f"Your plan generates up to {max_faces:,} polygons. Lower the polygon count, or upgrade.")
    max_views = context.gate3("maxMultiViewImages")
    count = len(views(form))
    if form.source_mode == "multi" and isinstance(max_views, int) and count > max_views:
        found.append(f"You have {count} views and your plan uses {max_views}.")
    return found


def prompt_hint(form):
    picture = pictures.get(single_slot(form))
    if has_own_picture(form):
        return "The picture is used; the prompt is ignored. Remove the picture to generate from text."
    if picture is not None and picture.drawn_for is not None and not drawn_is_current(form) and form.prompt.strip():
        return "The prompt changed — the picture will be drawn again."
    if next_is_drawing(form) and form.control_mode:
        return ("Control Mode: a picture is drawn from your prompt first, so you can check it before any 3D credits "
                "are spent.")
    if next_is_drawing(form):
        return "A picture is drawn from your prompt, then built into 3D."
    return None


def button_label(form):
    status = common.status(tab_of(form))
    if status.busy:
        return status.busy
    if next_is_drawing(form):
        return "Draw the picture" if form.control_mode else "Generate 3D"
    return "Generate Smart 3D" if form.is_smart else "Generate 3D"


# ─── Submitting ───────────────────────────────────────────────────────────────────────────────────


def draw_picture(form, then=None):
    """Draws a picture from the prompt (square, framed for 3D). then() runs after it lands."""
    tab = tab_of(form)
    status = common.status(tab)
    prompt = form.prompt.strip()
    status.start("Generating image…")

    def work():
        result = zetta.generate_image(prompt, for_3d=True)
        images = [i for i in result.get("images") or [] if i.get("base64")]
        if not images:
            raise common.Problem(result.get("text") or "The image service returned no image for that prompt.")
        import base64

        data = base64.b64decode(images[0]["base64"])
        return pictures.Picture(data, images[0].get("contentType") or pictures.sniff(data),
                                "prompt" + pictures.extension_for(images[0].get("contentType")), drawn_for=prompt)

    def done(picture):
        pictures.put(single_slot(form), picture, for_3d=False)
        context.refresh_credits()
        if then is not None:
            then()
        else:
            status.finish("Here is the picture. Press Generate 3D to build it, or Redraw for a different one.")

    tasks.run(work, done, status.fail, name="zetta3d-draw")


def submit(form, state):
    tab = tab_of(form)
    status = common.status(tab)
    reason = problem(form)
    if reason:
        status.fail(common.Problem(reason))
        return
    if form.source_mode == "multi":
        _submit_multi(form, state)
        return
    if next_is_drawing(form):
        if form.control_mode:
            draw_picture(form)
        else:
            draw_picture(form, then=lambda: _submit_single(form, state))
        return
    _submit_single(form, state)


def _progress(status):
    return lambda text: tasks.post(status.stage, text)


def _submit_single(form, state):
    tab = tab_of(form)
    status = common.status(tab)
    picture = pictures.get(single_slot(form))
    remove_background = form.remove_background
    title, prompt = form.title.strip(), form.prompt.strip()
    status.start("Removing background…" if picture and remove_background else "Uploading…" if picture else "Starting…")
    base = options(form)

    def work():
        o = base
        if picture is not None:
            o.image_url = common.upload_picture(picture, remove_background, _progress(status))
            if picture.drawn_for is not None:
                o.prompt = picture.drawn_for
                o.image_from_prompt = True
            else:
                o.prompt = ""
        built = requests.build(o, context.gates_3d)
        if not built.ok:
            raise common.Problem(built.problems[0])
        tasks.post(status.stage, "Starting…")
        return zetta.create_generation(built.body)

    def done(result):
        label = common.shorten(title or prompt or (picture.drawn_for if picture else "") or "Image to 3D")
        message = common.track(result, label, state)
        pictures.clear(single_slot(form))
        form.prompt = ""
        form.title = ""
        form.title_touched = False
        fork_note.pop(tab, None)
        status.finish(message)

    tasks.run(work, done, status.fail, name="zetta3d-generate")


def _submit_multi(form, state):
    tab = tab_of(form)
    status = common.status(tab)
    filled = {v: pictures.get(view_slot(form, v)) for v in VIEWS if pictures.get(view_slot(form, v))}
    for view, picture in filled.items():
        reason = picture.problem()
        if reason:
            status.fail(common.Problem(f"{view.capitalize()} view: {reason}"))
            return
    remove_background = form.remove_background
    title = form.title.strip()
    status.start(f"Removing backgrounds (0/{len(filled)})…" if remove_background else "Uploading views… 0%")
    base = options(form)

    def work():
        urls = []
        for index, view in enumerate(v for v in SUBMISSION_ORDER if v in filled):
            tasks.post(status.stage, f"Uploading views… {int(100 * index / len(filled))}%")
            urls.append((view, common.upload_picture(filled[view], remove_background)))
        base.views = urls
        built = requests.build(base, context.gates_3d)
        if not built.ok:
            raise common.Problem(built.problems[0])
        tasks.post(status.stage, "Starting…")
        return zetta.create_generation(built.body)

    def done(result):
        label = common.shorten(title or f"Multi-view to 3D ({len(filled)} views)")
        message = common.track(result, label, state)
        for view in VIEWS:
            pictures.clear(view_slot(form, view))
        form.title = ""
        form.title_touched = False
        fork_note.pop(tab, None)
        status.finish(message)

    tasks.run(work, done, status.fail, name="zetta3d-generate-mv")


# ─── Generate missing views ───────────────────────────────────────────────────────────────────────

generating_views = {}  # tab → True while drawing


def missing_views(form, budget=None):
    filled = [v for v in VIEWS if pictures.get(view_slot(form, v))]
    budget = multi_view_budget(form) if budget is None else budget
    return [v for v in VIEWS if v not in filled][: max(0, budget - len(filled))]


def generate_missing_views(form, budget=None, slot_of=None, status_name=None):
    """Draws the empty angles from the reference view, keeping the object the same."""
    slot_of = slot_of or (lambda view: view_slot(form, view))
    tab = status_name or tab_of(form)
    status = common.status(tab)
    reference = next((pictures.get(slot_of(v)) for v in VIEWS if pictures.get(slot_of(v))), None)
    filled = [v for v in VIEWS if pictures.get(slot_of(v))]
    budget = 4 if budget is None else budget
    targets = [v for v in VIEWS if v not in filled][: max(0, budget - len(filled))]
    if reference is None or not targets:
        return
    reference = pictures.downscaled(reference)
    inline = reference.inline()
    generating_views[tab] = True
    status.start("Generating views…")

    def work():
        import base64

        made, last_error = 0, None
        for view in targets:
            try:
                result = zetta.edit_image(EDIT_PREAMBLE + EDIT_ANGLES[view], image=inline, for_3d=True,
                                          pipeline_step=True)
            except zetta.ApiError as error:
                last_error = catalog.explain(error)
                if error.status in (402, 403, 429):
                    break
                continue
            images = [i for i in result.get("images") or [] if i.get("base64")]
            if not images:
                last_error = result.get("text") or "The image service returned no picture for this view."
                continue
            data = base64.b64decode(images[0]["base64"])
            content_type = images[0].get("contentType") or pictures.sniff(data)
            picture = pictures.Picture(data, content_type, view + pictures.extension_for(content_type))
            tasks.post(pictures.put, slot_of(view), picture, False)
            made += 1
        return made, last_error

    def done(result):
        made, last_error = result
        generating_views.pop(tab, None)
        context.refresh_credits()
        if made == 0:
            status.fail(common.Problem(last_error or "The views could not be generated. Try again."))
        elif made < len(targets):
            status.fail(common.Problem(f"{len(targets) - made} view(s) could not be generated. {last_error or ''}"))
        else:
            status.finish(None)

    def fail(error):
        generating_views.pop(tab, None)
        status.fail(error)

    tasks.run(work, done, fail, name="zetta3d-views")


# ─── AI edit (a Generate picture or a view, changed with a sentence) ──────────────────────────────

ai_edit = {"slot": None, "result": None, "busy": False, "error": None}


def begin_ai_edit(slot):
    ai_edit.update(slot=slot, result=None, busy=False, error=None)


def end_ai_edit():
    ai_edit.update(slot=None, result=None, busy=False, error=None)


def submit_ai_edit(instruction):
    slot = ai_edit["slot"]
    current = ai_edit["result"] or pictures.get(slot)
    if current is None or not instruction.strip():
        return
    ai_edit.update(busy=True, error=None)
    inline = current.inline()

    def work():
        import base64

        # A paid edit: never a pipeline step.
        result = zetta.edit_image(instruction.strip(), image=inline, for_3d=True)
        images = [i for i in result.get("images") or [] if i.get("base64")]
        if not images:
            raise common.Problem((result.get("text") or "").strip() or "The image service returned no image for that edit.")
        data = base64.b64decode(images[0]["base64"])
        return data, images[0].get("contentType") or pictures.sniff(data)

    def done(answer):
        data, content_type = answer
        ai_edit.update(busy=False, result=current.renamed(data, content_type))
        context.refresh_credits()

    def fail(error):
        ai_edit.update(busy=False, error=catalog.explain(error) if isinstance(error, zetta.ApiError) else str(error))

    tasks.run(work, done, fail, name="zetta3d-ai-edit")


def accept_ai_edit():
    slot, result = ai_edit["slot"], ai_edit["result"]
    if slot and result is not None:
        pictures.put(slot, result, for_3d=False)
    end_ai_edit()


# ─── Fork: a new generation from an existing one ──────────────────────────────────────────────────

FORKABLE = {"txt23d", "img23d", "multiView23d"}


def fork_tab(gen):
    """The tab a row can be forked into, or None."""
    if gen.get("availability") == "deleted" or gen.get("selectedFeature") not in FORKABLE:
        return None
    model = gen.get("selectedModel")
    if model in ("n1", "n25"):
        return "generate"
    if model in ("np1", "np15"):
        return "smart"
    return None


def fork(gen, state):
    """Loads a row's settings and pictures into its form. Returns an error string, or None."""
    from .. import importer
    from ..api import models

    tab = fork_tab(gen)
    if tab is None:
        return "This model cannot be forked."
    form = state.smart if tab == "smart" else state.gen
    status = common.status(tab)
    if status.busy:
        return "Wait for this job to start, then fork again."
    state.tool = tab
    base = models.spec_base(gen)
    mods = models.spec_modifiers(gen)
    feature = gen.get("selectedFeature")
    model_ids = [m.id for m in (catalog.SMART_MODELS if form.is_smart else catalog.GENERATE_MODELS)]
    form["model"] = model_ids.index(gen["selectedModel"]) if gen.get("selectedModel") in model_ids else 0
    form["model_picked"] = True
    quality = "low-poly" if base == "lowpoly" else "ultra" if "detailed_geometry" in mods else "high-res"
    if quality == "low-poly" and not catalog.model(form.model).low_poly:
        quality = "high-res"
    _set_quality(form, quality)
    if base == "geometry_only":
        _set_output(form, "geometry")
    elif base == "textured":
        _set_output(form, "textured")
    form["output_picked"] = True
    form["pbr"] = "pbr" in mods
    form["segment_parts"] = "generate_parts" in mods
    if base == "textured":
        texture = "extreme" if "extreme_texture" in mods else "detailed" if "detailed_texture" in mods else "standard"
        if texture == "extreme" and not catalog.model(form.model).extreme_texture:
            texture = "detailed"
        form["texture_quality"] = ("standard", "detailed", "extreme").index(texture)
    if gen.get("polygonType") in _POLYGON_INDEX:
        _set_polygon(form, gen["polygonType"])
    count = gen.get("faceCount")
    if "custom_face_count" in mods and isinstance(count, int) and count > 0:
        form["face_count"] = str(count)
        form["face_typed"] = True
    else:
        _reset_count(form)
    form.prompt = gen.get("inputPrompt") or "" if feature == "txt23d" else ""

    pictures.clear(single_slot(form))
    for view in VIEWS:
        pictures.clear(view_slot(form, view))
    title = gen.get("title") or f"#{gen.get('id')}"
    images = gen.get("inputImages") or []
    if feature == "multiView23d":
        form.source_mode = "multi"
        wanted = [(i.get("viewType"), i.get("url")) for i in images if i.get("url") and i.get("viewType") in VIEWS]
    elif feature == "img23d" and images and images[0].get("url"):
        form.source_mode = "single"
        wanted = [(None, images[0]["url"])]
    else:
        form.source_mode = "single"
        wanted = []
    fork_note[tab] = (f"Forked from “{models.display_title(gen)}”. Nothing has run yet — change anything you like, "
                      f"then press {'Generate Smart 3D' if form.is_smart else 'Generate 3D'}.")
    if not wanted:
        return None
    status.start("Loading the reference angles…" if len(wanted) > 1 else "Loading the reference picture…")

    def work():
        found = []
        for view, url in wanted:
            data = zetta.http.fetch_bytes(url, timeout=60)
            content_type = pictures.sniff(data)
            name = f"{title} – {view}.png" if view else f"{title}.png"
            found.append((view, pictures.Picture(data, content_type, importer.slug(name) + pictures.extension_for(content_type))))
        return found

    def done(found):
        for view, picture in found:
            pictures.put(view_slot(form, view) if view else single_slot(form), picture, for_3d=False)
        status.finish(None)

    def fail(_error):
        status.fail(common.Problem("Some of the reference angles could not be loaded — add them again." if len(wanted) > 1
                                   else "The reference picture could not be loaded — attach it again."))

    tasks.run(work, done, fail, name="zetta3d-fork")
    return None
