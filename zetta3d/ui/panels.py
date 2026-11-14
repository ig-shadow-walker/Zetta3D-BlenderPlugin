"""The Zetta 3D sidebar in the 3D View (N panel › Zetta 3D): account, 3D Studio, Jobs, My Generations, Pipelines."""

import json
import textwrap

import blf
import bpy

from .. import auth, config, importer, jobs, library, updates
from ..api import catalog, models
from ..pipelines import graph as g
from ..pipelines import state as pipes
from ..studio import common, generate, images, pictures, tools
from ..studio import context as studio

CATEGORY = "Zetta 3D"


# ─── Drawing helpers ──────────────────────────────────────────────────────────────────────────────


def btn(layout, action, text="", icon="NONE", arg="", arg2="", confirm="", hint="", depress=False, emboss=True):
    op = layout.operator("zetta.do", text=text, icon=icon, depress=depress, emboss=emboss)
    op.action, op.arg, op.arg2, op.confirm, op.hint = action, str(arg), str(arg2), confirm, hint
    return op


def wrap(layout, text, icon="NONE", alert=False, width=None):
    """A paragraph: Blender labels don't wrap, so the text is cut into lines that fit the sidebar.

    `width` is a line length in characters; without it the lines are measured in the UI font against the
    region, so they fit at any UI scale and display density.
    """
    if not text:
        return
    lines = textwrap.wrap(text, width) if width else _measured_lines(text, icon != "NONE")
    col = layout.column(align=True)
    col.alert = alert
    for index, line in enumerate(lines or [""]):
        col.label(text=line, icon=icon if index == 0 else "BLANK1" if icon != "NONE" else "NONE")


def _measured_lines(text, with_icon, region_width=None):
    region = getattr(bpy.context, "region", None)
    prefs = bpy.context.preferences
    # Everything in a region is drawn at the system scale: the UI scale times the display's pixel size.
    scale = prefs.system.ui_scale if prefs else 1.0
    if region_width is None:
        region_width = region.width if region else 300
    # The sidebar's strip of tabs (inside the region's width), the panel's margins, the box a paragraph usually
    # sits in, and the icon on its first line (blank on the rest).
    room = region_width - (84 + (22 if with_icon else 0)) * scale
    try:
        blf.size(0, prefs.ui_styles[0].widget.points * scale)
        fits = lambda line: blf.dimensions(0, line)[0] <= room  # noqa: E731
    except Exception:  # noqa: BLE001 — no font to measure with: fall back to an average character width
        chars = max(20, int(room / (7 * scale)))
        return textwrap.wrap(text, chars)
    lines, line = [], ""
    for word in text.split():
        candidate = f"{line} {word}" if line else word
        if line and not fits(candidate):
            lines.append(line)
            line = word
        else:
            line = candidate
    return lines + [line] if line else lines


def text_prop(layout, data, name, placeholder, **kwargs):
    """A text field with a hint inside it while empty (Blender 4.2 has no placeholder: it shows the field bare)."""
    try:
        layout.prop(data, name, text="", placeholder=placeholder, **kwargs)
    except TypeError:
        layout.prop(data, name, text="", **kwargs)


def status_lines(layout, status):
    if status.error:
        wrap(layout, status.error, "ERROR", alert=True)
    if status.done:
        wrap(layout, status.done, "CHECKMARK")
    if status.note:
        wrap(layout, status.note, "INFO")


def heading(layout, text, icon="NONE"):
    row = layout.row()
    row.label(text=text, icon=icon)
    return row


def price_text(value):
    return studio.price_label(value)


def menu_title(which, arg):
    if which == "generation":
        gen = library.find(int(arg)) if arg.isdigit() else None
        return models.display_title(gen) if gen else "Model"
    if which == "image_post":
        return "Picture"
    if which == "pipeline":
        return "Pipeline"
    if which == "run":
        return f"Run #{arg}"
    return "Zetta 3D"


# ─── Account (top panel) ──────────────────────────────────────────────────────────────────────────


class ZETTA_PT_main(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = CATEGORY
    bl_label = "Zetta 3D"
    bl_idname = "ZETTA_PT_main"

    def draw_header(self, _context):
        self.layout.label(icon_value=pictures.mark())

    def draw(self, context):
        layout = self.layout
        draw_update_banner(layout)
        if not bpy.app.online_access:
            box = layout.box()
            wrap(box, "Online access is turned off in Blender. Zetta 3D needs it to talk to zetta3d.ai.", "ERROR",
                 alert=True)
            box.operator("screen.userpref_show", text="Open Preferences › System", icon="PREFERENCES")
            return
        draw_account(layout)
        if auth.signed_in():
            studio.ensure_loaded()
            if studio.load_error:
                box = layout.box()
                wrap(box, f"Some account details did not load ({studio.load_error}). Prices and plan limits may be "
                          "missing.", "ERROR", alert=True)
                btn(box, "retry_context", "Retry", "FILE_REFRESH")
        for notice in pipes.notices[:2]:
            layout.label(text=notice, icon="INFO")


def draw_update_banner(layout):
    if updates.installed_version:
        box = layout.box()
        wrap(box, f"Zetta 3D {updates.installed_version} is installed. Restart Blender to finish updating.",
             "CHECKMARK")
        return
    if not updates.available():
        return
    box = layout.box()
    box.alert = updates.required()
    latest = updates.known["latest"]
    text = (f"Zetta 3D {latest} is required — this version no longer works with Zetta 3D." if updates.required()
            else f"Zetta 3D {latest} is out (you have {config.VERSION}).")
    wrap(box, text, "ERROR" if updates.required() else "IMPORT")
    if updates.known.get("message"):
        wrap(box, updates.known["message"])
    row = box.row(align=True)
    btn(row, "update_notes", "What's new", "URL")
    if updates.can_install():
        sub = row.row(align=True)
        sub.enabled = not updates.installing
        btn(sub, "install_update", "Updating…" if updates.installing else "Update", "IMPORT")
    if updates.problem:
        wrap(box, updates.problem, "ERROR", alert=True)


def draw_account(layout):
    box = layout.box()
    if auth.current == auth.SIGNED_IN and auth.user:
        row = box.row()
        row.label(text=auth.display_name(), icon="USER")
        btn(row, "refresh_account", "", "FILE_REFRESH", hint="Refresh your plan and credits")
        btn(row, "sign_out", "", "QUIT", hint="Sign out")
        box.label(text=f"{auth.plan_label()} plan · {auth.credits():,} credits")
        btn(box, "open_site", "Buy credits / upgrade", "FUND", arg="/pricing")
    elif auth.current == auth.WAITING:
        wrap(box, "Finish signing in in your browser, then press Connect Blender.", "URL")
        row = box.row(align=True)
        url = auth.login_url()
        if url:
            btn(row, "open_url", "Open the page again", "URL", arg=url)
        btn(row, "cancel_sign_in", "Cancel", "CANCEL")
    elif auth.current == auth.CHECKING:
        box.label(text="Checking your account…", icon="TIME")
    else:
        wrap(box, "Generate, finish and import 3D models and pictures with your Zetta 3D account.")
        btn(box, "sign_in", "Sign in with your browser", "URL")
        btn(box, "open_site", "Create a free account", "ADD", arg="/signup")
    if auth.last_error:
        wrap(box, auth.last_error, "ERROR", alert=True)


def signed_in_or_hint(layout):
    if auth.signed_in():
        return True
    layout.label(text="Sign in above to use this.", icon="LOCKED")
    return False


# ─── 3D Studio ────────────────────────────────────────────────────────────────────────────────────


class ZETTA_PT_studio(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = CATEGORY
    bl_label = "3D Studio"
    bl_idname = "ZETTA_PT_studio"

    def draw_header(self, context):
        self.layout.label(icon="MESH_MONKEY")

    def draw(self, context):
        layout = self.layout
        if not signed_in_or_hint(layout):
            return
        zetta = context.window_manager.zetta
        layout.prop(zetta, "tool", text="")
        tool = zetta.tool
        if tool in ("generate", "smart"):
            draw_generate(layout, zetta, zetta.smart if tool == "smart" else zetta.gen)
        elif tool == "images":
            draw_image_studio(layout, zetta)
        else:
            draw_tool(layout, zetta, tool)


def draw_picture(layout, slot, empty_text="Choose a picture", ai_edit=False, removable=True, scale=6.0):
    picture = pictures.get(slot)
    col = layout.column(align=True)
    if picture is not None:
        col.template_icon(icon_value=picture.icon(), scale=scale)
        label = picture.name
        if picture.drawn_for is not None:
            label = "Drawn from your prompt"
        col.label(text=label)
        row = col.row(align=True)
        op = row.operator("zetta.pick_file", text="Replace", icon="FILEBROWSER")
        op.target = f"slot:{slot}"
        op = row.operator("zetta.pick_image", text="", icon="IMAGE_DATA")
        op.target = f"slot:{slot}"
        if ai_edit:
            locked = studio.gate2("allowsImageEditing") is False
            sub = row.row(align=True)
            sub.enabled = not locked
            btn(sub, "ai_edit_begin", "AI edit", "SHADERFX", arg=slot,
                hint="Change this picture with a sentence on a paid plan." if locked else
                "Change this picture with a sentence")
        if removable:
            btn(row, "clear_picture", "", "X", arg=slot, hint="Remove the picture")
    else:
        row = col.row(align=True)
        op = row.operator("zetta.pick_file", text=empty_text, icon="FILEBROWSER")
        op.target = f"slot:{slot}"
        op = row.operator("zetta.pick_image", text="", icon="IMAGE_DATA")
        op.target = f"slot:{slot}"
    error = pictures.errors.get(slot)
    if error:
        wrap(col, error, "ERROR", alert=True)
    if ai_edit and generate.ai_edit["slot"] == slot:
        draw_ai_edit(layout)


def draw_ai_edit(layout):
    box = layout.box()
    state = generate.ai_edit
    box.label(text="AI edit", icon="SHADERFX")
    wrap(box, "Describe a change to the picture. Each edit builds on the last.")
    if state["result"] is not None:
        box.template_icon(icon_value=state["result"].icon(), scale=6.0)
        box.label(text="Edited")
    zetta = bpy.context.window_manager.zetta
    text_prop(box, zetta, "aiedit_instruction", "Make the stripes blue, add a flag on top…")
    row = box.row(align=True)
    sub = row.row(align=True)
    sub.enabled = not state["busy"] and bool(zetta.aiedit_instruction.strip())
    btn(sub, "ai_edit_submit", "Submitting…" if state["busy"] else "Submit", "PLAY")
    sub = row.row(align=True)
    sub.enabled = state["result"] is not None
    btn(sub, "ai_edit_accept", "Use this", "CHECKMARK")
    btn(row, "ai_edit_cancel", "Cancel", "CANCEL", hint="Keep the original picture")
    if state["error"]:
        wrap(box, state["error"], "ERROR", alert=True)


def draw_views(layout, slot_of, budget=4, ai_edit=True):
    filled = [v for v in generate.VIEWS if pictures.get(slot_of(v))]
    grid = layout.grid_flow(columns=2, even_columns=True, align=False)
    for view in generate.VIEWS:
        cell = grid.box()
        title = view.capitalize()
        if filled and filled[0] == view:
            title += " · Reference"
        cell.label(text=title)
        slot = slot_of(view)
        if pictures.get(slot) is None and len(filled) >= budget:
            cell.enabled = False
            cell.label(text=f"Your plan uses up to {budget} views.")
            continue
        draw_picture(cell, slot, "Choose", ai_edit=ai_edit, scale=4.0)


def submit_bar(layout, zetta, price, problem, label, action, arg, status):
    box = layout.box()
    col = box.column(align=True)
    col.prop(zetta, "auto_import")
    sub = col.row()
    sub.enabled = zetta.auto_import
    sub.prop(zetta, "place")
    row = box.row()
    row.label(text="Total")
    row.label(text=price_text(price))
    balance = studio.credits()
    if price and price > balance:
        wrap(box, f"You have {balance:,} credits — this needs {price:,}.", "ERROR", alert=True)
    else:
        box.label(text=f"You have {balance:,} credits")
    if problem and not status.busy:
        wrap(box, problem, "INFO")
    status_lines(box, status)
    row = box.row()
    row.scale_y = 1.5
    row.enabled = not status.busy and problem is None
    btn(row, action, label, "PLAY", arg=arg)


def draw_generate(layout, zetta, form):
    tab = generate.tab_of(form)
    status = common.status(tab)
    wrap(layout, "Clean, production-ready topology from a picture or a prompt." if form.is_smart
         else "Turn a prompt or a picture into a textured 3D model.")
    if form.is_smart and studio.gate3("allowsZettaNp1") is False:
        wrap(layout, "Generate Smart 3D is available on a paid plan. Zetta N1 generates from text and images on every "
                     "plan.", "LOCKED", alert=True)
    note = generate.fork_note.get(tab)
    if note:
        wrap(layout, note, "DUPLICATE")

    box = layout.box()
    row = box.row()
    row.enabled = not generate.multi_view_locked()
    row.prop(form, "source_mode", expand=True)
    if generate.multi_view_locked():
        box.label(text="Multi-view input is available on a paid plan.", icon="LOCKED")
    if form.source_mode == "single" or generate.multi_view_locked():
        col = box.column()
        col.enabled = not generate.has_own_picture(form) and not status.busy
        text_prop(col, form, "prompt", "Describe the model — e.g. a weathered medieval helmet")
        hint = generate.prompt_hint(form)
        if hint:
            wrap(box, hint, "INFO")
        box.label(text="…or use a picture")
        draw_picture(box, generate.single_slot(form), ai_edit=True)
        picture = pictures.get(generate.single_slot(form))
        if picture is not None and picture.drawn_for is not None and not status.busy:
            btn(box, "redraw_picture", "Redraw", "FILE_REFRESH", arg=tab,
                hint="Generate a different image from this prompt")
    else:
        budget = generate.multi_view_budget(form)
        draw_views(box, lambda view: generate.view_slot(form, view), budget)
        filled = len(generate.views(form))
        reference = generate.reference_view(form)
        if reference is None:
            wrap(box, "Upload one view — the rest can be generated from it.", "INFO")
        else:
            missing = generate.missing_views(form, budget)
            if missing:
                wrap(box, f"{filled} views ready. {len(missing)} more can be generated from your "
                          f"{reference[0].capitalize()} view.", "INFO")
            else:
                box.label(text=f"{filled} views ready.")
            sub = box.row()
            sub.enabled = bool(missing) and not generate.generating_views.get(tab)
            btn(sub, "generate_views", "Generating views…" if generate.generating_views.get(tab)
                else "Generate missing views", "IMAGE_BACKGROUND", arg=tab,
                hint="Draws the empty angles from your reference view, keeping the object the same.")

    draw_generate_settings(layout, form)
    built, price = generate.preview(form)
    submit_bar(layout, zetta, price, generate.problem(form), generate.button_label(form), "generate", tab, status)


def draw_generate_settings(layout, form):
    model = catalog.model(form.model)
    textured = form.output == "textured"
    multi = form.source_mode == "multi"
    box = layout.box()
    box.label(text="Settings", icon="PREFERENCES")
    file_attached = (multi and generate.views(form)) or (not multi and generate.has_own_picture(form))
    if file_attached:
        box.prop(form, "title")
    box.prop(form, "remove_background")
    if model.image_tuning:
        box.prop(form, "autofix_image")

    box.label(text="Generation Engine")
    row = box.row(align=True)
    for info in (catalog.SMART_MODELS if form.is_smart else catalog.GENERATE_MODELS):
        cell = row.row(align=True)
        cell.enabled = generate.engine_lock(info.id) is None
        cell.prop_enum(form, "model", info.id, text=info.label + (" · New" if info.id == "np15" else ""))
    wrap(box, model.blurb)
    for info in (catalog.SMART_MODELS if form.is_smart else catalog.GENERATE_MODELS):
        lock = generate.engine_lock(info.id)
        if lock:
            wrap(box, f"{info.label}: {lock}", "LOCKED")

    row = box.row(align=True)
    row.label(text="Output")
    sub = row.row(align=True)
    sub.enabled = not (form.quality == "low-poly" and form.model == "n1")
    sub.prop_enum(form, "output", "geometry")
    sub = row.row(align=True)
    sub.enabled = not form.segment_parts
    sub.prop_enum(form, "output", "textured")
    if model.parts and form.quality != "low-poly":
        hint = studio.price_hint("generate_parts", form.model)
        box.prop(form, "segment_parts", text=f"Segmented mesh{' · ' + hint if hint else ''}")
    if form.model in ("n1", "n25"):
        box.label(text="Mesh quality" if form.model == "n25" else "Quality")
        row = box.row(align=True)
        lock_line = None
        for quality, label in catalog.QUALITIES:
            lock = generate.quality_lock(form, quality)
            cell = row.row(align=True)
            cell.enabled = lock is None
            cell.prop_enum(form, "quality", quality, text=label)
            if lock and (quality == form.quality or lock_line is None):
                lock_line = lock
        if lock_line:
            wrap(box, lock_line, "LOCKED")

    box.prop(form, "show_polygons", icon="TRIA_DOWN" if form.show_polygons else "TRIA_RIGHT", emboss=False)
    if form.show_polygons:
        sub = box.box()
        if generate.may_choose_count():
            preset = catalog.face_preset(form.model, form.quality, form.polygon_type == "quadrilateral")
            count = generate.parse_count(form.face_count)
            hint = studio.price_hint("custom_face_count", form.model) if count is not None and count != preset else ""
            sub.prop(form, "face_count", text=f"Polygon count{' · ' + hint if hint else ''}")
            low, high = generate.face_bounds(form)
            if form.face_count.strip() and (count is None or not low <= count <= high):
                sub.label(text=f"Out of range — {low:,} to {high:,}.", icon="ERROR")
            elif form.model != "n1":
                sub.label(text=f"Minimum {low:,}, up to {high:,}")
        else:
            wrap(sub, "Model's default face count (choosing a count is available on a paid plan)", "LOCKED")
        if model.quad_any_base or model.low_poly:
            row = sub.row(align=True)
            row.label(text="Polygons")
            unused = form.quality != "low-poly" and not model.quad_any_base
            cell = row.row(align=True)
            cell.enabled = not unused
            cell.prop_enum(form, "polygon_type", "triangle")
            cell = row.row(align=True)
            cell.enabled = not unused and not form.segment_parts and studio.gate3("allowsQuadMesh") is not False
            cell.prop_enum(form, "polygon_type", "quadrilateral")
            if unused:
                sub.label(text="Not used at this mesh quality.")
            elif studio.gate3("allowsQuadMesh") is False:
                sub.label(text="Quad meshes are available from Basic.", icon="LOCKED")

    if textured:
        hint = studio.price_hint("pbr", form.model) if form.pbr else ""
        box.prop(form, "pbr", text=f"PBR{' · ' + hint if hint else ''}")
        if model.detailed_texture:
            row = box.row(align=True)
            row.prop_enum(form, "texture_quality", "standard")
            row.prop_enum(form, "texture_quality", "detailed")
            if model.extreme_texture:
                row.prop_enum(form, "texture_quality", "extreme")
            code = {"detailed": "detailed_texture", "extreme": "extreme_texture"}.get(form.texture_quality)
            resolution = {"detailed": "4K", "extreme": "8K"}.get(form.texture_quality, "2K")
            box.label(text=f"Up to {resolution} resolution ({studio.price_hint(code, form.model) if code else 'free'})")

    row = box.row()
    allowed = studio.gate3("allowsPrivateGenerations") is not False
    row.enabled = allowed
    row.prop(form, "private", text="Keep this model private" if allowed else "Keep this model private (paid plan)")

    if model.image_tuning or not multi:
        box.prop(form, "show_advanced", icon="TRIA_DOWN" if form.show_advanced else "TRIA_RIGHT", emboss=False)
        if form.show_advanced:
            sub = box.box()
            if model.image_tuning:
                row = sub.row()
                row.enabled = textured
                row.prop(form, "real_world_scale")
                sub.prop(form, "compress_geometry")
                if textured:
                    sub.prop(form, "export_uv")
                    sub.prop(form, "delight")
                    sub.prop(form, "texture_alignment")
            if not multi:
                sub.prop(form, "control_mode")


def draw_source(layout, zetta, tool):
    box = layout.box()
    box.label(text="Source model", icon="MESH_DATA")
    source = tools.sources.get(tool)
    if source is not None:
        row = box.row()
        icon = library.generation_icon(source)
        if icon:
            row.template_icon(icon_value=icon, scale=3.0)
        col = row.column(align=True)
        col.label(text=models.display_title(source))
        chips = [catalog.feature_label(source.get("selectedFeature"))]
        if isinstance(source.get("faceCount"), int) and source["faceCount"] > 0:
            chips.append(f"{source['faceCount']:,} faces")
        col.label(text=" · ".join(chips))
        btn(col, "source_clear", "Clear", "X", arg=tool)
    else:
        wrap(box, "Choose a finished model from your library, or select a model in the 3D view.")
    row = box.row(align=True)
    btn(row, "source_browse", "Pick from library", "ASSET_MANAGER", arg=tool)
    btn(row, "source_selection", "Use selection", "RESTRICT_SELECT_OFF", arg=tool,
        hint="Models imported from Zetta 3D are used directly; anything else is uploaded to your library (free)")
    if tools.browser["tool"] == tool:
        sub = box.box()
        row = sub.row(align=True)
        text_prop(row, zetta, "src_search", "Search your library", icon="VIEWZOOM")
        btn(row, "source_browse", "", "FILE_REFRESH", arg=tool, arg2="")
        if tools.browser["loading"] and not tools.browser["items"]:
            sub.label(text="Loading your library…", icon="TIME")
        elif tools.browser["error"]:
            wrap(sub, tools.browser["error"], "ERROR", alert=True)
        elif not tools.browser["items"]:
            wrap(sub, "Nothing matches that search." if zetta.src_search else
                 "No finished models yet. Generate one first, or use a model from your scene.")
        grid = sub.grid_flow(columns=2, even_columns=True)
        for gen in tools.browser["items"]:
            cell = grid.column(align=True)
            refusal = tools.refusal(tool, gen)
            icon = library.generation_icon(gen)
            if icon:
                cell.template_icon(icon_value=icon, scale=3.5)
            cell.label(text=common.shorten(models.display_title(gen), 22))
            r = cell.row()
            r.enabled = refusal is None
            btn(r, "source_pick", "Use", "CHECKMARK", arg=tool, arg2=gen["id"], hint=refusal or "")
        if tools.browser_has_more():
            btn(sub, "source_browse", "Load more", "TRIA_DOWN", arg=tool, arg2="1")


def draw_tool(layout, zetta, tool):
    intros = {
        "segment": "Split a model into separate, named parts — each one imports as its own object.",
        "retopology": "Rebuild the mesh with clean, even topology — lighter for games, easier to rig and UV.",
        "smart_uv": "Lays the model's UVs out again as a clean new layout. Its textures, rig and animation are "
                    "carried across onto it.",
        "texture": "Paint a new texture onto a model from a picture, or from a description of the surface.",
        "rig": "Add a skeleton and skin weights so the model is ready to animate.",
    }
    wrap(layout, intros[tool])
    if studio.gate3("allowsPostProcess") is False:
        wrap(layout, "The finishing tools are available on a paid plan. Generate 3D works on every plan.", "LOCKED",
             alert=True)
    draw_source(layout, zetta, tool)
    box = layout.box()
    box.label(text="Options", icon="PREFERENCES")
    source = tools.sources.get(tool)
    label = {"segment": "Smart Segment" if zetta.seg_type == "smart" else "Segment", "retopology": "Retopologize",
             "smart_uv": "Smart UV", "texture": "Apply texture", "rig": "Auto Rig"}[tool]
    if tool == "segment":
        box.row().prop(zetta, "seg_type", expand=True)
        wrap(box, "Rebuilds as it splits, for cleaner parts on a rough or incomplete mesh." if zetta.seg_type == "smart"
             else "Splits the model you have into named parts.")
        box.prop(zetta, "seg_granularity")
        if zetta.seg_type == "segment":
            box.prop(zetta, "seg_split")
        else:
            text_prop(box, zetta, "seg_hint", "What is this? e.g. game character with sword and armor")
    elif tool == "retopology":
        box.row().prop(zetta, "retopo_engine", expand=True)
        wrap(box, "Up to 20,000 polygons. Faster, and less than half the price." if zetta.retopo_engine == "r1"
             else "No polygon ceiling — the option for a dense mesh.")
        row = box.row(align=True)
        row.prop_enum(zetta, "retopo_polygon", "triangle")
        sub = row.row(align=True)
        sub.enabled = studio.gate3("allowsQuadMesh") is not False
        sub.prop_enum(zetta, "retopo_polygon", "quadrilateral")
        if studio.gate3("allowsQuadMesh") is False:
            box.label(text="Quad meshes are available from Basic.", icon="LOCKED")
        if zetta.retopo_engine == "r1":
            box.prop(zetta, "retopo_faces", text=f"Polygon count (500–{tools.retopo_max(zetta):,})")
            wrap(box, "Quads halve the ceiling. Zetta R1 aims for your count, then settles on the cleanest topology "
                      "near it." if zetta.retopo_polygon == "quadrilateral" else
                 "Zetta R1 aims for this count, then settles on the cleanest topology near it.")
            box.prop(zetta, "retopo_bake")
        else:
            box.prop(zetta, "retopo_level")
    elif tool == "smart_uv":
        triangles = tools.source_triangles(source) if source else None
        if triangles is None:
            box.label(text="Takes models up to 80,000 triangles.")
        elif triangles <= catalog.SMART_UV_MAX_TRIANGLES:
            box.label(text=f"{triangles:,} / 80,000 triangles")
        box.prop(zetta, "uv_orientation")
        box.prop(zetta, "uv_bake")
        box.label(text="Keeps the textures, baked onto the new layout." if zetta.uv_bake
                  else "Returns the geometry with its new UVs only — no textures.")
        box.prop(zetta, "uv_compress")
        wrap(box, "The result comes with a picture of its UV layout, saved beside the model.", "INFO")
    elif tool == "texture":
        draw_texture_options(box, zetta)
    elif tool == "rig":
        box.prop(zetta, "rig_type")
        sub = box.row()
        sub.enabled = zetta.rig_type == catalog.HUMANOID_RIG_TYPE
        sub.prop(zetta, "rig_spec")
        if zetta.rig_type != catalog.HUMANOID_RIG_TYPE:
            box.label(text="Skeleton presets apply to humanoids.")
        box.row().prop(zetta, "rig_format", expand=True)
        sub = box.row()
        sub.enabled = source is not None and not tools.detecting
        btn(sub, "rig_detect", "Detecting…" if tools.detecting else "Detect the body", "VIEWZOOM",
            hint="Looks at the imported model to tell what kind of body it has")
    price = tools.price(tool, zetta)
    submit_bar(layout, zetta, price, tools.tool_problem(tool, zetta), common.status(tool).busy or label,
               "tool_submit", tool, common.status(tool))


def draw_texture_options(box, zetta):
    box.row().prop(zetta, "tex_mode", expand=True)
    if zetta.tex_mode == "single":
        draw_picture(box, tools.TEXTURE_SINGLE, "Use my own picture")
        wrap(box, tools.texture_reference_hint(zetta), "INFO")
        text_prop(box, zetta, "tex_prompt", "Describe the surface — e.g. worn brass with green patina")
        wrap(box, "Optional. A description is turned into a new reference picture you can check before applying.")
        row = box.row()
        source = tools.sources.get("texture")
        row.enabled = bool(zetta.tex_prompt.strip()) and source is not None and not tools.preview["busy"]
        label = "Drawing…" if tools.preview["busy"] else "Try again" if tools.preview["picture"] else "Preview"
        btn(row, "texture_preview", label, "IMAGE_DATA")
        if tools.preview["picture"] is not None and tools.preview["prompt"] == zetta.tex_prompt.strip():
            box.template_icon(icon_value=tools.preview["picture"].icon(), scale=6.0)
            wrap(box, "This picture is applied as the new surface. Press Preview again for a different one.")
        if tools.preview["error"]:
            wrap(box, tools.preview["error"], "ERROR", alert=True)
    else:
        draw_views(box, tools.texture_view_slot, 4, ai_edit=False)
        filled = [v for v in generate.VIEWS if pictures.get(tools.texture_view_slot(v))]
        if filled and len(filled) < 4:
            row = box.row()
            row.enabled = not generate.generating_views.get("texture")
            btn(row, "generate_views", "Generate missing views", "IMAGE_BACKGROUND", arg="texture")
    box.prop(zetta, "tex_remove_bg")
    box.prop(zetta, "tex_tier")
    price = studio.price(zetta.tex_tier)
    resolution = {"retexture": "2K", "retexture_hd": "4K", "retexture_8k": "8K"}[zetta.tex_tier]
    box.label(text=f"Up to {resolution} resolution" + (f" ({price:,} credits)" if price is not None else ""))
    if zetta.tex_tier == "retexture":
        box.prop(zetta, "tex_fast")
    box.prop(zetta, "tex_alignment")
    box.prop(zetta, "tex_pbr")
    box.prop(zetta, "tex_delight")
    box.prop(zetta, "tex_bake")


def draw_image_list(layout, prefix, limit, title, hint=None, numbered=False):
    box = layout.box()
    box.label(text=title)
    if hint:
        wrap(box, hint)
    items = pictures.with_prefix(prefix)
    grid = box.grid_flow(columns=3, even_columns=True)
    for index, picture in enumerate(items):
        cell = grid.column(align=True)
        cell.template_icon(icon_value=picture.icon(), scale=3.0)
        row = cell.row(align=True)
        row.label(text=f"{index + 1}" if numbered else "")
        btn(row, "remove_from_list", "", "X", arg=prefix, arg2=index)
    if len(items) < limit:
        row = box.row(align=True)
        op = row.operator("zetta.pick_file", text="Add" if limit > 1 or not items else "Replace", icon="ADD")
        op.target, op.multiple = f"list:{prefix}:{limit}", limit > 1
        op = row.operator("zetta.pick_image", text="", icon="IMAGE_DATA")
        op.target = f"list:{prefix}:{limit}"


def draw_image_studio(layout, zetta):
    status = common.status("images")
    wrap(layout, "Make pictures for your scene — textures, references, concept art — or a reference to build a 3D "
                 "model from.")
    layout.row().prop(zetta, "img_mode", expand=True)
    if images.locked() and zetta.img_mode != "generate":
        wrap(layout, images.LOCK_TEXT, "LOCKED", alert=True)
    if zetta.img_mode == "edit":
        box = layout.box()
        box.label(text="Image to edit")
        draw_picture(box, images.EDIT_SLOT, "Choose the picture", removable=True)
    elif zetta.img_mode == "compose":
        draw_image_list(layout, images.COMPOSE_PREFIX, images.MAX_COMPOSE, "Images to combine",
                        "At least two. They are built into one new picture, in this order.", numbered=True)
    text_prop(layout, zetta, "img_prompt", {
        "generate": "A brushed-brass compass on weathered oak, morning light",
        "edit": "Make the sky stormy and add rain on the stone",
        "compose": "Put the cat from the first image onto the throne in the second"}[zetta.img_mode])
    if zetta.img_mode == "generate":
        draw_image_list(layout, images.STYLE_PREFIX, images.MAX_STYLE, "Style references (optional)",
                        "Used for look and feel only. The picture is still made from your prompt.")
    col = layout.column(align=True)
    col.prop(zetta, "img_aspect")
    row = col.row(align=True)
    for resolution in catalog.RESOLUTIONS:
        cell = row.row(align=True)
        cell.enabled = images.resolution_lock(zetta, resolution) is None
        cell.prop_enum(zetta, "img_resolution", resolution)
    lock = images.resolution_lock(zetta, zetta.img_resolution)
    if lock:
        wrap(col, lock, "LOCKED")
    if studio.image_model_list():
        col.prop(zetta, "img_model")
    problem = images.problem(zetta)
    if problem and not status.busy:
        wrap(layout, problem, "INFO")
    status_lines(layout, status)
    row = layout.row()
    row.scale_y = 1.5
    row.enabled = problem is None
    label = {"generate": "Generate image", "edit": "Edit image", "compose": "Compose image"}[zetta.img_mode]
    btn(row, "image_submit", status.busy or label, "PLAY")

    for index, result in enumerate(images.results):
        box = layout.box()
        picture = result["picture"]
        box.template_icon(icon_value=picture.icon(), scale=7.0)
        box.label(text=" · ".join(p for p in (result.get("aspect"), result.get("resolution"),
                                              images.size_label(picture)) if p))
        row = box.row(align=True)
        btn(row, "image_result", "Save", "FILE_TICK", arg=index, arg2="save",
            hint="Save into the images folder and load it as a Blender image")
        btn(row, "image_result", "Use for 3D", "MESH_MONKEY", arg=index, arg2="use3d")
        row = box.row(align=True)
        sub = row.row(align=True)
        sub.enabled = not images.locked()
        btn(sub, "image_result", "Edit this", "GREASEPENCIL", arg=index, arg2="edit")
        btn(sub, "image_result", "Compose", "NODE_COMPOSITING", arg=index, arg2="compose")
        btn(row, "image_result", "", "X", arg=index, arg2="dismiss")

    box = layout.box()
    row = box.row()
    row.label(text="Recent", icon="TIME")
    btn(row, "recent_refresh", "", "FILE_REFRESH")
    if not images.recent["loaded"] and not images.recent["loading"]:
        images.load_recent()
    if images.recent["error"]:
        wrap(box, images.recent["error"], "ERROR", alert=True)
    elif not images.recent["items"]:
        box.label(text="Images you generate appear here.")
    else:
        grid = box.grid_flow(columns=3, even_columns=True)
        for post in images.recent["items"]:
            cell = grid.column(align=True)
            icon = library.image_icon(post)
            if icon:
                cell.template_icon(icon_value=icon, scale=3.0)
            else:
                cell.label(text="Failed" if not post.get("images") else "…")
            btn(cell, "recent_open", "Open", arg=post["id"])


# ─── Jobs ─────────────────────────────────────────────────────────────────────────────────────────


class ZETTA_PT_jobs(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = CATEGORY
    bl_label = "Jobs"
    bl_idname = "ZETTA_PT_jobs"

    @classmethod
    def poll(cls, _context):
        return bool(jobs.all_jobs())

    def draw_header_preset(self, _context):
        # On the header's right, after its title: draw_header would put it in front ("· 2 running  Jobs").
        running = len(jobs.running())
        self.layout.label(text=f"{running} running" if running else f"{len(jobs.all_jobs())}")

    def draw(self, context):
        layout = self.layout
        if any(j.get("finished") for j in jobs.all_jobs()):
            btn(layout, "jobs_clear", "Clear finished", "TRASH")
        for job in jobs.all_jobs():
            draw_job(layout, job)


def job_text(job):
    status = job.get("status")
    if status == "pending":
        return "Waiting to start"
    if status == "queued":
        position = job.get("queuePosition")
        return f"Queued · #{position} in line" if position else "Queued"
    if status == "processing":
        return "Generating…"
    if status == "saving":
        return "Saving the result…"
    if status == "success":
        if job.get("error"):
            return job["error"]
        latest = job.get("latest") or {}
        if latest and models.is_splat_row(latest):
            return "Ready · Gaussian splats can't be imported into Blender — open it on zetta3d.ai."
        if job.get("imported"):
            text = "Ready · imported"
        elif job.get("progress") is not None:
            text = f"Downloading {int(job['progress'] * 100)}%"
        else:
            text = "Ready · importing…" if job.get("autoImport") and not job.get("finished") else "Ready"
        if latest and models.uv_layout(latest):
            text += " · UV layout picture ready"
        return text
    if status == "failed":
        return job.get("error") or "Failed. Your credits were refunded."
    if status == "cancelled":
        return "Cancelled"
    return status or ""


def draw_job(layout, job):
    box = layout.box()
    status = job.get("status")
    icon = {"success": "CHECKMARK", "failed": "ERROR", "cancelled": "CANCEL"}.get(status, "TIME")
    row = box.row()
    row.label(text=common.shorten(job.get("label") or f"Job #{job['id']}", 34), icon=icon)
    if job.get("finished"):
        btn(row, "job_dismiss", "", "X", arg=job["id"], emboss=False, hint="Forget this job")
    wrap(box, job_text(job), alert=status == "failed")
    row = box.row(align=True)
    if status in ("pending", "queued"):
        btn(row, "job_cancel", "Cancel", "CANCEL", arg=job["id"])
    if status == "success":
        latest = job.get("latest") or {}
        if latest and models.is_splat_row(latest):
            btn(row, "gen_open_web", "Open on zetta3d.ai", "URL", arg=job["id"])
        elif job.get("progress") is not None:
            row.label(text="Importing…")
        elif importer.objects_for(job["id"]):
            btn(row, "gen_show", "Show", "RESTRICT_SELECT_OFF", arg=job["id"])
            btn(row, "gen_place", "Place again", "OBJECT_ORIGIN", arg=job["id"])
        else:
            btn(row, "job_import", "Import", "IMPORT", arg=job["id"])
        if latest and models.uv_layout(latest):
            btn(row, "gen_uv", "UV layout", "UV", arg=job["id"])


# ─── My Generations ───────────────────────────────────────────────────────────────────────────────


class ZETTA_PT_library(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = CATEGORY
    bl_label = "My Generations"
    bl_idname = "ZETTA_PT_library"
    bl_options = {"DEFAULT_CLOSED"}

    def draw_header(self, _context):
        self.layout.label(icon="ASSET_MANAGER")

    def draw(self, context):
        layout = self.layout
        if not signed_in_or_hint(layout):
            return
        zetta = context.window_manager.zetta
        row = layout.row(align=True)
        row.prop(zetta, "lib_kind", expand=True)
        btn(row, "library_refresh", "", "FILE_REFRESH")
        if library.notice["text"]:
            wrap(layout, library.notice["text"], "INFO")
        if library.notice["error"]:
            wrap(layout, library.notice["error"], "ERROR", alert=True)
        if zetta.lib_kind == "images":
            draw_image_library(layout, zetta)
        else:
            draw_model_library(layout, zetta)


def draw_model_library(layout, zetta):
    library.ensure_loaded()
    text_prop(layout, zetta, "lib_search", "Search by title or prompt", icon="VIEWZOOM")
    row = layout.row(align=True)
    row.prop(zetta, "lib_feature", text="")
    row.prop(zetta, "lib_status", text="")
    row = layout.row(align=True)
    row.prop(zetta, "place", text="Place when imported")
    row.prop(zetta, "lib_columns", text="")
    row.prop(zetta, "lib_selecting", text="", icon="RESTRICT_SELECT_OFF", toggle=True)
    btn(layout, "upload_selection", "Upload selection to library", "EXPORT",
        hint="Export the selected objects as GLB and add them to your Zetta 3D library (free)")
    if zetta.lib_selecting:
        box = layout.box()
        count = len(library.selected)
        box.label(text=f"{count:,} selected" if count else "Click the models to select")
        row = box.row(align=True)
        btn(row, "library_select_page", "Select page", arg="1")
        btn(row, "library_select_page", "Clear page", arg="0")
        row = box.row(align=True)
        sub = row.row(align=True)
        sub.enabled = count > 0
        btn(sub, "library_delete_selected", f"Delete {count}", "TRASH",
            confirm=f"Delete {count:,} model(s)?|Their model files will be removed from storage and the records "
                    "deleted. This cannot be undone, and the credits spent on them are not refunded.")
        btn(row, "library_select_done", "Done", "CHECKMARK")

    page = library.page
    if page["loading"] and not page["items"]:
        layout.label(text="Loading your library…", icon="TIME")
        return
    if page["error"]:
        wrap(layout, page["error"], "ERROR", alert=True)
    if not page["items"]:
        layout.label(text="Nothing matches those filters." if zetta.lib_search or zetta.lib_feature != "all"
                     or zetta.lib_status != "all" else "Models you generate appear here.")
        return

    group = library.open_run["group"]
    if group is not None:
        draw_run_group(layout, zetta, group)

    grid = layout.grid_flow(columns=zetta.lib_columns, even_columns=True, even_rows=False)
    for gen in page["items"]:
        run = gen.get("runGroup")
        if run and run.get("members"):
            draw_run_tile(grid.box(), zetta, run)
        else:
            draw_tile(grid.box(), zetta, gen)

    row = layout.row(align=True)
    sub = row.row(align=True)
    sub.enabled = page["page"] > 1
    btn(sub, "library_page", "", "TRIA_LEFT", arg=-1)
    row.label(text=f"Page {page['page']} of {library.pages()} · {page['total']:,} models")
    sub = row.row(align=True)
    sub.enabled = page["page"] < library.pages()
    btn(sub, "library_page", "", "TRIA_RIGHT", arg=1)


def draw_tile(box, zetta, gen, step_text=None):
    gen_id = int(gen["id"])
    icon = library.generation_icon(gen)
    col = box.column(align=True)
    if icon:
        col.template_icon(icon_value=icon, scale=4.0)
    else:
        col.label(text="", icon="MESH_MONKEY")
    col.label(text=common.shorten(models.display_title(gen), 26))
    chips = []
    if step_text:
        chips.append(step_text)
    if gen.get("status") != "success":
        chips.append(models.status_label(gen))
    chips.append(catalog.feature_label(gen.get("selectedFeature")))
    if gen.get("selectedModel") in catalog.MODEL_LABELS:
        chips.append(catalog.model_label(gen["selectedModel"]))
    col.label(text=" · ".join(chips))
    splat = models.is_splat_row(gen)
    in_scene = bool(importer.objects_for(gen_id))
    if zetta.lib_selecting:
        btn(col, "gen_select", "Selected" if gen_id in library.selected else "Select",
            "CHECKBOX_HLT" if gen_id in library.selected else "CHECKBOX_DEHLT", arg=gen_id,
            depress=gen_id in library.selected)
        return
    row = col.row(align=True)
    progress = (importer.importing.get(gen_id) or {}).get("progress")
    if splat:
        btn(row, "gen_open_web", "Open on web", "URL", arg=gen_id)
    elif progress is not None:
        row.label(text=f"Importing… {int(progress * 100)}%")
    elif in_scene:
        btn(row, "gen_show", "Show", "RESTRICT_SELECT_OFF", arg=gen_id)
        btn(row, "gen_place", "Place", "OBJECT_ORIGIN", arg=gen_id)
    elif gen.get("status") == "success":
        btn(row, "gen_import", "Import", "IMPORT", arg=gen_id)
    error = (importer.importing.get(gen_id) or {}).get("error")
    if error:
        wrap(col, error, "ERROR", alert=True, width=28)
    op = row.operator("zetta.menu", text="", icon="THREE_DOTS")
    op.which, op.arg = "generation", str(gen_id)


def draw_run_tile(box, zetta, run):
    members = run.get("members") or []
    cover = next((m for m in members if models.thumbnail_url(m)), None)
    col = box.column(align=True)
    icon = library.generation_icon(cover) if cover else 0
    if icon:
        col.template_icon(icon_value=icon, scale=4.0)
    col.label(text=common.shorten(library.run_name(run), 26), icon="NODETREE")
    still = any(not models.is_terminal(m) for m in members)
    col.label(text=f"{library.run_count(run)} models" + (" · Still making" if still else "") + " · Pipeline")
    if zetta.lib_selecting:
        ids = [m["id"] for m in members]
        chosen = all(i in library.selected for i in ids)
        btn(col, "gen_select_run", "Unselect this run" if chosen else "Select all in this run",
            "CHECKBOX_HLT" if chosen else "CHECKBOX_DEHLT", arg=",".join(str(i) for i in ids), depress=chosen)
        return
    open_now = library.open_run["group"] is not None and library.open_run["group"].get("runId") == run.get("runId")
    btn(col, "run_group_open", "Close" if open_now else "Open", "FULLSCREEN_ENTER", arg=run.get("runId"),
        depress=open_now)


def draw_run_group(layout, zetta, group):
    box = layout.box()
    row = box.row()
    row.label(text=library.run_name(group), icon="NODETREE")
    btn(row, "run_group_open", "", "X", arg=group.get("runId"), emboss=False)
    box.label(text=library.run_detail(group))
    row = box.row(align=True)
    if any(models.is_success(m) and not models.is_splat_row(m) for m in group.get("members") or []):
        btn(row, "run_group_import", "Import all", "IMPORT")
    count = library.run_count(group)
    btn(row, "run_group_delete", "Delete all", "TRASH",
        confirm=f"Delete all {count:,} model(s) from “{library.run_name(group)}”?|Their model files will be removed "
                "from storage and the records deleted. This cannot be undone, and the credits spent on them are "
                "not refunded.")
    if group.get("pipelineId"):
        btn(row, "run_group_open_pipeline", "Open run", "NODETREE", arg=group["pipelineId"], arg2=group["runId"])
    grid = box.grid_flow(columns=zetta.lib_columns, even_columns=True)
    for index, member in enumerate(library.in_run_order(group.get("members") or [])):
        draw_tile(grid.box(), zetta, member, library.step_text(member, index))


def draw_image_library(layout, zetta):
    library.ensure_images_loaded()
    layout.prop(zetta, "lib_image_feature", text="")
    data = library.images
    if data["error"]:
        wrap(layout, data["error"], "ERROR", alert=True)
    if data["loading"] and not data["items"]:
        layout.label(text="Loading your images…", icon="TIME")
        return
    if not data["items"]:
        layout.label(text="Images you generate appear here.")
        return
    grid = layout.grid_flow(columns=zetta.lib_columns, even_columns=True)
    for post in data["items"]:
        box = grid.box()
        col = box.column(align=True)
        icon = library.image_icon(post)
        if icon:
            col.template_icon(icon_value=icon, scale=4.0)
        col.label(text=common.shorten(post.get("prompt") or "Untitled", 26))
        if not post.get("images"):
            col.label(text="Failed", icon="ERROR")
            continue
        row = col.row(align=True)
        btn(row, "image_post", "Save", "FILE_TICK", arg=post["id"], arg2="save")
        op = row.operator("zetta.menu", text="", icon="THREE_DOTS")
        op.which, op.arg = "image_post", str(post["id"])
    layout.label(text=f"Showing {len(data['items'])} of {data['total']}.")
    if data["total"] > len(data["items"]):
        btn(layout, "images_more", "Load more", "TRIA_DOWN")


# ─── Menus (zetta.menu) ───────────────────────────────────────────────────────────────────────────


def draw_menu(layout, which, arg):
    if which == "generation":
        gen = library.find(int(arg))
        if gen is None:
            layout.label(text="Refresh the library.")
            return
        gen_id = int(gen["id"])
        if models.is_splat_row(gen):
            btn(layout, "gen_open_web", "Open on zetta3d.ai", "URL", arg=gen_id)
        elif models.is_success(gen):
            if importer.objects_for(gen_id):
                btn(layout, "gen_show", "Show in scene", "RESTRICT_SELECT_OFF", arg=gen_id)
                btn(layout, "gen_place", "Place again at the 3D cursor", "OBJECT_ORIGIN", arg=gen_id)
                btn(layout, "gen_import", "Import again (download a fresh copy)", "IMPORT", arg=gen_id, arg2="1")
            else:
                btn(layout, "gen_import", "Import and place at the 3D cursor", "IMPORT", arg=gen_id, arg2="1")
                btn(layout, "gen_import", "Import (don't place)", "IMPORT", arg=gen_id, arg2="0")
                btn(layout, "gen_import_child", "Import as child of the active object", "CON_CHILDOF", arg=gen_id)
            if models.uv_layout(gen):
                btn(layout, "gen_uv", "Save UV layout picture", "UV", arg=gen_id)
            layout.separator()
            for tool, enabled in library.send_to_options(gen):
                row = layout.row()
                row.enabled = enabled
                btn(row, "gen_send", f"Send to {tools.TITLES[tool]}", "FORWARD", arg=gen_id, arg2=tool)
        if generate.fork_tab(gen):
            btn(layout, "gen_fork", "Fork (new generation from these settings)", "DUPLICATE", arg=gen_id)
        layout.separator()
        btn(layout, "gen_open_web", "Open on zetta3d.ai", "URL", arg=gen_id)
        op = layout.operator("zetta.edit_text", text="Rename…", icon="GREASEPENCIL")
        op.target, op.title, op.text = f"rename:{gen_id}", "Rename", gen.get("title") or ""
        btn(layout, "gen_delete", "Delete…", "TRASH", arg=gen_id,
            confirm=f"Delete model|Delete “{models.display_title(gen)}” from your Zetta 3D library? Copies already "
                    "imported into this file are kept.")
    elif which == "image_post":
        btn(layout, "image_post", "Save to images folder", "FILE_TICK", arg=arg, arg2="save")
        btn(layout, "image_post", "Use for 3D", "MESH_MONKEY", arg=arg, arg2="use3d")
        btn(layout, "image_post", "Edit this", "GREASEPENCIL", arg=arg, arg2="edit")
        btn(layout, "image_post", "Delete…", "TRASH", arg=arg, arg2="delete",
            confirm="Delete image|Delete this image from your Zetta 3D library? Copies saved into this file are kept.")
    elif which == "pipeline":
        btn(layout, "pipeline_open", "Open", "FILE_FOLDER", arg=arg)
        btn(layout, "pipeline_duplicate", "Duplicate", "DUPLICATE", arg=arg)
        btn(layout, "open_site", "Open on zetta3d.ai", "URL", arg=f"/pipelines?pipeline={arg}")
        btn(layout, "pipeline_delete", "Delete…", "TRASH", arg=arg,
            confirm="Delete pipeline?|The recipe goes. Models its runs made stay in your library.")
    elif which == "run":
        summary = next((r for r in pipes.runs["items"] if str(r.get("id")) == arg), None)
        if summary is None:
            return
        btn(layout, "run_open", "Open", "FILE_FOLDER", arg=arg)
        row = layout.row()
        row.enabled = summary.get("status") in pipes.DELETABLE
        draft = summary.get("status") == "draft"
        name = pipes.run_name(summary)
        btn(row, "run_delete", "Discard run…" if draft else "Delete run…", "TRASH", arg=arg,
            confirm=(f"Discard run #{arg}?|“{name}” never started — it goes, and nothing was spent." if draft else
                     f"Delete run #{arg}?|“{name}” and its steps go. The models it made stay in your library."))


# ─── Pipelines ────────────────────────────────────────────────────────────────────────────────────


class ZETTA_PT_pipelines(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = CATEGORY
    bl_label = "Pipelines"
    bl_idname = "ZETTA_PT_pipelines"
    bl_options = {"DEFAULT_CLOSED"}

    def draw_header(self, _context):
        self.layout.label(icon="NODETREE")

    def draw(self, context):
        layout = self.layout
        if not signed_in_or_hint(layout):
            return
        zetta = context.window_manager.zetta
        if pipes.editor["loading"]:
            layout.label(text="Opening…", icon="TIME")
        if pipes.editor["pipeline"] is None:
            draw_pipeline_list(layout)
            return
        draw_pipeline_editor(layout, zetta)


def draw_pipeline_list(layout):
    pipes.ensure_list()
    row = layout.row(align=True)
    btn(row, "pipeline_new", "New pipeline", "ADD")
    btn(row, "pipelines_refresh", "", "FILE_REFRESH")
    if pipes.listing["error"]:
        wrap(layout, pipes.listing["error"], "ERROR", alert=True)
    if pipes.editor["error"]:
        wrap(layout, pipes.editor["error"], "ERROR", alert=True)
    items = pipes.listing["items"]
    if pipes.listing["loading"] and not items:
        layout.label(text="Loading…", icon="TIME")
        return
    if not items:
        wrap(layout, "Build your first pipeline. Chain the Studio's tools into one run from a picture or a model — "
                     "generate, isolate parts, retopologise, unwrap, texture and rig — automatically to the end, or a "
                     "step at a time.")
        return
    for summary in items:
        box = layout.box()
        row = box.row()
        row.label(text=common.shorten(summary.get("title") or pipes.UNTITLED, 30), icon="NODETREE")
        op = row.operator("zetta.menu", text="", icon="THREE_DOTS")
        op.which, op.arg = "pipeline", str(summary["id"])
        steps = int(summary.get("steps") or 0)
        detail = f"{steps} step{'s' if steps != 1 else ''}" + ("" if summary.get("lastRun") else " · Not run yet")
        counts = pipes.run_counts_label(summary)
        box.label(text=detail + (f" · {counts}" if counts else ""))
        btn(box, "pipeline_open", "Open", "FILE_FOLDER", arg=summary["id"])


def draw_pipeline_editor(layout, zetta):
    pipeline = pipes.editor["pipeline"]
    row = layout.row(align=True)
    btn(row, "pipeline_close", "Pipelines", "BACK")
    btn(row, "open_site", "", "URL", arg=f"/pipelines?pipeline={pipeline['id']}", hint="Open on zetta3d.ai")
    layout.prop(zetta, "pipe_title", text="")
    row = layout.row(align=True)
    row.prop(zetta, "pipe_view", expand=True)
    live = sum(1 for r in pipes.runs["items"] if r.get("status") in pipes.LIVE)
    if live:
        row.label(text=f"{live} live", icon="TIME")
    if zetta.pipe_view == "runs":
        draw_runs(layout, zetta)
    else:
        draw_recipe(layout, zetta)


def draw_recipe(layout, zetta):
    graph = pipes.editor["graph"] or g.empty()
    row = layout.row(align=True)
    sub = row.row(align=True)
    sub.enabled = bool(pipes.editor["undo"])
    btn(sub, "pipeline_undo", "", "LOOP_BACK", hint="Undo")
    sub = row.row(align=True)
    sub.enabled = bool(pipes.editor["redo"])
    btn(sub, "pipeline_redo", "", "LOOP_FORWARDS", hint="Redo")
    row.label(text=pipes.save_status())
    btn(row, "pipeline_save", "", "FILE_TICK", hint="Save now")

    box = layout.box()
    box.label(text="Describe it", icon="OUTLINER_DATA_LIGHTPROBE")
    text_prop(box, zetta, "pipe_describe", "Change the recipe — e.g. add AI texturing at the end"
              if pipes.editing() else "e.g. a game-ready character from a photo, rigged")
    wrap(box, "Change the recipe — e.g. add AI texturing at the end, or use quads" if pipes.editing()
         else "Describe the recipe — e.g. a game-ready character from a photo, rigged for Blender")
    sub = box.row()
    sub.enabled = not pipes.editor["describe_busy"] and len(zetta.pipe_describe.strip()) >= 3
    label = ("Changing your recipe…" if pipes.editing() else "Designing your recipe…") if pipes.editor[
        "describe_busy"] else ("Change it" if pipes.editing() else "Build it")
    btn(sub, "pipeline_describe", label, "PLAY")
    if pipes.editor["describe_note"]:
        wrap(box, pipes.editor["describe_note"], "CHECKMARK")
    if pipes.editor["describe_error"]:
        wrap(box, pipes.editor["describe_error"], "ERROR", alert=True)

    problems = pipes.recipe_problems()
    by_node = {}
    for node_id, message in problems:
        by_node.setdefault(node_id, []).append(message)
    per_part = g.fan_out_nodes(graph)

    box = layout.box()
    row = box.row()
    row.label(text="Steps", icon="NODETREE")
    selected = pipes.editor["selected"]
    sel_node = g.node(graph, selected) if selected else None
    if sel_node is not None and sel_node.get("kind") == "isolate_parts":
        op = row.operator("zetta.choose", text="Add step", icon="ADD")
        op.source = f"after_output:{selected}"
    else:
        op = row.operator("zetta.choose", text="Add step after" if sel_node else "Add step", icon="ADD")
        op.source = "step_kind"
    if not graph.get("nodes"):
        wrap(box, "Start with a seed — the picture, model or pictures of each side each run starts from.")
    for n, depth, parent, via in g.ordered(graph):
        node_id = n["id"]
        row = box.row(align=True)
        if depth:
            row.separator(factor=1.5 * min(depth, 6))
        icon = "ERROR" if node_id in by_node else "DOT"
        text = g.node_name(n)
        if parent is not None and via not in (None, "out"):
            text = f"{g.output_label(parent, via)} › {text}"
        btn(row, "pipeline_select", text, icon, arg=node_id, depress=node_id == selected)
        price = g.price_for(graph, n, studio.prices, pipes.entitlements())
        row.label(text=g.price_label(price, node_id in per_part))
        if node_id == selected:
            draw_step_settings(box, graph, n, by_node.get(node_id, []))
    for message in by_node.get(None, []):
        wrap(box, message, "ERROR", alert=True)
    if pipes.editor["error"]:
        wrap(box, pipes.editor["error"], "ERROR", alert=True)

    box = layout.box()
    box.label(text=g.estimate_label(graph, studio.prices, pipes.entitlements()), icon="FUND")
    if problems:
        count = len(problems)
        wrap(box, f"{count} thing{'s' if count != 1 else ''} to finish before it can run — {problems[0][1]}", "INFO")
    row = box.row()
    row.scale_y = 1.4
    row.enabled = not problems and g.seed(graph) is not None
    btn(row, "pipeline_new_run", "New run…", "PLAY",
        hint="Choose the pictures or models to run on — one run each")
    if pipes.new_run["open"]:
        draw_new_run(layout, zetta, graph)


def _setting(layout, node_id, key, value, text, current, run=False, depress=None):
    payload = {key: value}
    if run:
        payload["__run__"] = True
    op = layout.operator("zetta.do", text=text, depress=(current == value) if depress is None else depress)
    op.action, op.arg, op.arg2 = "pipeline_set", node_id, json.dumps(payload)


def _choice_row(layout, node_id, key, options, current, run=False, title=None):
    if title:
        layout.label(text=title)
    row = layout.row(align=True)
    for value, text in options:
        _setting(row, node_id, key, value, text, current, run)


def _toggle(layout, node_id, key, current, text, run=False):
    _setting(layout, node_id, key, not current, text, current, run, depress=bool(current))


def _text_field(layout, node_id, key, current, title, run=False, numeric=False, as_list=False):
    row = layout.row(align=True)
    shown = ", ".join(current) if as_list and isinstance(current, list) else ("" if current is None else str(current))
    row.label(text=f"{title}:")
    op = row.operator("zetta.edit_setting", text=common.shorten(shown, 28) or "—", icon="GREASEPENCIL")
    op.node_id, op.key, op.title, op.numeric, op.run, op.as_list = node_id, key, title, numeric, run, as_list


def draw_step_settings(layout, graph, n, messages, run=False, editable=True):
    box = layout.box()
    kind, node_id = n.get("kind"), n["id"]
    wrap(box, g.blurb(kind))
    wrap(box, g.summary(n), "INFO")
    for message in messages:
        wrap(box, message, "ERROR", alert=True)
    if kind == "smart_uv":
        warning = g.smart_uv_warning(graph, n)
        if warning:
            wrap(box, warning, "ERROR")
    if not g.known(kind):
        wrap(box, "This step is newer than this version of the add-on — it is kept as it is. Update the add-on to "
                  "edit it.", "ERROR")
        return
    if not g.offered(kind):
        wrap(box, "This step is not available in Blender — it is kept as it is; open the pipeline on zetta3d.ai to "
                  "change it.", "ERROR")
        return
    if not run:
        row = box.row(align=True)
        op = row.operator("zetta.edit_text", text="Rename", icon="GREASEPENCIL")
        op.target, op.title, op.text = f"step_label:{node_id}", "Name this step", n.get("label") or ""
        btn(row, "pipeline_remove_step", "Remove", "TRASH", arg=node_id)
        for handle, (accepts, required) in g.inputs(kind).items():
            edge = g.incoming_edge(graph, node_id, handle)
            sub = box.row(align=True)
            source = g.node(graph, edge["source"]) if edge else None
            name = f"{g.node_name(source)} › {g.output_label(source, edge.get('sourceHandle') or 'out')}" if source \
                else ("Not connected" if required else "Nothing (optional)")
            sub.label(text=f"Input from: {name}")
            op = sub.operator("zetta.choose", text="", icon="LINKED")
            op.source = f"connect:{node_id}:{handle}"
            if edge:
                btn(sub, "pipeline_disconnect", "", "UNLINKED", arg=node_id, arg2=handle)
    if not editable:
        return
    settings = g.with_defaults(kind, n.get("settings"))
    col = box.column(align=True)
    if kind == "remove_background" or g.is_seed(kind):
        col.label(text="Nothing to set." if not g.is_seed(kind) else "Chosen for each run.")
    elif kind == "ai_edit":
        if not run:
            _toggle(col, node_id, "askAtRun", settings.get("askAtRun") is True, "Set on each run", run)
        if run or not settings.get("askAtRun"):
            _text_field(col, node_id, "prompt", settings.get("prompt"), "Change", run)
        _toggle(col, node_id, "for3D", settings.get("for3D") is not False,
                "Frame it for 3D — one object, centred, plain background", run)
        _image_model_row(col, node_id, settings, run)
        wrap(col, "Image editing needs a plan that includes it, as in the Image Studio.")
    elif kind == "draw_views":
        views = settings.get("views") or []
        row = col.row(align=True)
        for side in ("back", "left", "right"):
            wanted = [v for v in views if v != side] if side in views else [*views, side]
            _setting(row, node_id, "views", [v for v in ("back", "left", "right") if v in wanted], side.capitalize(),
                     None, run, depress=side in views)
    elif kind == "isolate_parts":
        _choice_row(col, node_id, "identify", [(True, "Identify"), (False, "Only my list")], settings.get("identify"),
                    run)
        if settings.get("identify") is not False:
            _choice_row(col, node_id, "sensitivity", [("low", "Low"), ("medium", "Medium"), ("high", "High")],
                        settings.get("sensitivity"), run, "Sensitivity")
        _text_field(col, node_id, "maxParts", settings.get("maxParts"), "Up to (1–16) parts", run, numeric=True)
        _choice_row(col, node_id, "pose", [("t", "T-pose"), ("a", "A-pose")], settings.get("pose"), run)
        _text_field(col, node_id, "extraParts", settings.get("extraParts"),
                    "Also isolate" if settings.get("identify") is not False else "Parts to isolate", run,
                    as_list=True)
        _image_model_row(col, node_id, settings, run)
    elif g.is_generate(kind):
        draw_generate_step(col, graph, n, settings, run)
    elif kind == "retopology":
        _choice_row(col, node_id, "engine", [("r1", "Zetta R1"), ("n1", "Zetta N1")], settings.get("engine"), run)
        quads_ok = studio.gate3("allowsQuadMesh") is not False
        options = [("triangle", "Triangles")] + ([("quadrilateral", "Quads")] if quads_ok else [])
        _choice_row(col, node_id, "polygonType", options, settings.get("polygonType"), run)
        if settings.get("engine") == "r1":
            high = 10_000 if settings.get("polygonType") == "quadrilateral" else 20_000
            _text_field(col, node_id, "faces", settings.get("faces"), f"Polygon count (500–{high:,})", run,
                        numeric=True)
            _toggle(col, node_id, "bake", settings.get("bake") is not False, "Bake textures onto the rebuilt mesh", run)
        else:
            _choice_row(col, node_id, "faceLevel", [("low", "Low"), ("medium", "Medium"), ("high", "High")],
                        settings.get("faceLevel"), run, "Level of detail")
    elif kind in ("segmentation", "smart_segmentation"):
        _choice_row(col, node_id, "mode", [("segment", "Segment"), ("smart", "Smart Segment")], settings.get("mode"),
                    run)
        _choice_row(col, node_id, "granularity", [("simple", "Few parts"), ("balanced", "Balanced"),
                                                  ("detailed", "Max parts")], settings.get("granularity"), run)
        if settings.get("mode") == "smart":
            _text_field(col, node_id, "hint", settings.get("hint"), "What is this?", run)
        else:
            _toggle(col, node_id, "splitByConnectivity", settings.get("splitByConnectivity") is not False,
                    "Also split pieces that are not joined", run)
    elif kind in ("texture", "texture_multiview"):
        _choice_row(col, node_id, "tier", [("retexture", "Standard"), ("retexture_hd", "Detailed"),
                                           ("retexture_8k", "8K")], settings.get("tier"), run)
        if settings.get("tier") == "retexture":
            _toggle(col, node_id, "fast", bool(settings.get("fast")), "Faster, at some detail — same price", run)
        _choice_row(col, node_id, "alignment", [("geometry", "Fit to mesh"), ("original_image", "Fit to picture")],
                    settings.get("alignment"), run)
        _toggle(col, node_id, "pbr", settings.get("pbr") is not False, "PBR materials", run)
        _toggle(col, node_id, "delight", settings.get("delight") is not False,
                "Remove the reference's baked-in lighting", run)
        _toggle(col, node_id, "bake", settings.get("bake") is not False, "Bake effects into the textures", run)
        _toggle(col, node_id, "removeBg", settings.get("removeBg") is not False, "Remove background before applying",
                run)
        if not run:
            wrap(col, "↻ The texture's description or reference picture is given in each run. New run opens the run "
                      "without starting it, with this step marked Needs input — fill it in there, then press Start.")
        elif kind == "texture":
            reference = settings.get("reference")
            if reference:
                row = col.row(align=True)
                row.label(text=f"Reference: {reference.get('name') or 'picture'}", icon="IMAGE_DATA")
                _setting(row, node_id, "reference", None, "", None, run, depress=False)
            else:
                _text_field(col, node_id, "prompt", settings.get("prompt"), "Describe the texture", run)
            row = col.row(align=True)
            op = row.operator("zetta.pick_file", text="Reference picture…", icon="FILEBROWSER")
            op.target = f"runref:{node_id}"
            op = row.operator("zetta.pick_image", text="", icon="IMAGE_DATA")
            op.target = f"runref:{node_id}"
        elif g.incoming_edge(graph, node_id, "views") is None:
            views = settings.get("referenceViews") or {}
            for side in ("front", "back", "left", "right"):
                row = col.row(align=True)
                entry = views.get(side)
                row.label(text=f"{side.capitalize()}{' · required' if side == 'front' else ''}: "
                               f"{(entry or {}).get('name') or '—'}")
                op = row.operator("zetta.pick_file", text="", icon="FILEBROWSER")
                op.target = f"runview:{node_id}:{side}"
    elif kind == "smart_uv":
        _toggle(col, node_id, "bake", settings.get("bake") is not False, "Bake textures", run)
        _choice_row(col, node_id, "orientation", [("as-is", "As is"), ("+x", "+X"), ("-x", "−X"), ("+y", "+Y"),
                                                  ("-y", "−Y")], settings.get("orientation"), run, "Orientation")
        _toggle(col, node_id, "compress", bool(settings.get("compress")), "Compress geometry", run)
    elif kind == "rigging":
        col.label(text="Body")
        grid = col.grid_flow(columns=2, align=True)
        for value, text in catalog.RIG_TYPES:
            _setting(grid, node_id, "rigType", value, text, settings.get("rigType"), run)
        if settings.get("rigType") == "biped":
            col.label(text="Bone naming")
            grid = col.grid_flow(columns=2, align=True)
            for value, text, _ in catalog.RIG_SPECS:
                _setting(grid, node_id, "spec", value, text, settings.get("spec"), run)
        _choice_row(col, node_id, "outFormat", [("glb", "GLB"), ("fbx", "FBX")], settings.get("outFormat"), run)


def _image_model_row(layout, node_id, settings, run):
    models_list = studio.image_model_list()
    if not models_list:
        return
    layout.label(text="Image model")
    row = layout.row(align=True)
    _setting(row, node_id, "model", None, "Default", settings.get("model"), run)
    for model in models_list:
        cell = row.row(align=True)
        cell.enabled = not model.get("locked")
        _setting(cell, node_id, "model", model["id"], model.get("label") or model["id"], settings.get("model"), run)


def draw_generate_step(layout, graph, n, settings, run):
    node_id, kind = n["id"], n["kind"]
    layout.label(text="Engine")
    row = layout.row(align=True)
    for model_id in g.models_for(kind):
        cell = row.row(align=True)
        cell.enabled = generate.engine_lock(model_id) is None
        _setting(cell, node_id, "model", model_id, catalog.model_label(model_id), settings.get("model"), run)
    model = settings.get("model")
    if model in ("n1", "n25"):
        options = [(q, label) for q, label in catalog.QUALITIES]
        _choice_row(layout, node_id, "quality", options, settings.get("quality"), run, "Quality")
    _choice_row(layout, node_id, "output", [("geometry", "Geometry"), ("textured", "Textured")],
                settings.get("output"), run)
    if model == "n25" and settings.get("quality") != "low-poly":
        _toggle(layout, node_id, "segmentParts", bool(settings.get("segmentParts")), "Segmented mesh", run)
    if studio.gate3("allowsCustomFaceCount") is not False:
        low, high = g.polygon_bounds(settings)
        _text_field(layout, node_id, "polygonCount", settings.get("polygonCount"),
                    f"Polygons ({low:,}–{high:,})", run)
    caps_quads = model in ("n25", "np15") or (model == "n1" and settings.get("quality") == "low-poly")
    if caps_quads:
        _choice_row(layout, node_id, "polygonType", [("triangle", "Triangles"), ("quadrilateral", "Quads")],
                    settings.get("polygonType"), run)
    if settings.get("output") == "textured":
        _toggle(layout, node_id, "pbrMaterials", settings.get("pbrMaterials") is not False, "PBR", run)
        if model != "n1":
            options = [("standard", "Standard"), ("detailed", "Detailed")]
            if model in ("np1", "np15"):
                options.append(("extreme", "8K"))
            _choice_row(layout, node_id, "textureQuality", options, settings.get("textureQuality"), run,
                        "Texture quality")
    _toggle(layout, node_id, "removeBackground", settings.get("removeBackground") is True, "Remove background", run)
    if model != "n1":
        _toggle(layout, node_id, "autofixImage", bool(settings.get("autofixImage")), "Enhance input image · Free",
                run)
        if settings.get("output") == "textured":
            _toggle(layout, node_id, "realWorldScale", bool(settings.get("realWorldScale")), "Real-world scale", run)
            _toggle(layout, node_id, "delight", bool(settings.get("delight")), "Remove baked-in lighting", run)
            _choice_row(layout, node_id, "textureAlignment", [("geometry", "Fit to mesh"),
                                                              ("original_image", "Fit to picture")],
                        settings.get("textureAlignment"), run)
        _toggle(layout, node_id, "compressGeometry", bool(settings.get("compressGeometry")), "Smaller file", run)
    _text_field(layout, node_id, "title", settings.get("title"), "Name", run)


def draw_new_run(layout, zetta, graph):
    box = layout.box()
    kind = pipes.seed_kind()
    noun = {"seed_views": "set of pictures", "seed_model": "model"}.get(kind, "picture")
    row = box.row()
    row.label(text="New run", icon="PLAY")
    btn(row, "run_new_cancel", "", "X", emboss=False)
    wrap(box, f"Each {noun} gets its own run of this pipeline, up to 20 at once. The runs open ready to check — each "
              "starts when you press Start.")
    if kind == "seed_picture":
        row = box.row(align=True)
        op = row.operator("zetta.pick_file", text="Upload pictures", icon="FILEBROWSER")
        op.target, op.multiple = "seed", True
        op = row.operator("zetta.pick_image", text="", icon="IMAGE_DATA")
        op.target = "seed"
        op = row.operator("zetta.choose", text="From your images", icon="ASSET_MANAGER")
        op.source = "image_seed"
    elif kind == "seed_model":
        row = box.row(align=True)
        op = row.operator("zetta.choose", text="From your library", icon="ASSET_MANAGER")
        op.source = "model_seed"
        btn(row, "run_add_selection", "Use selection", "RESTRICT_SELECT_OFF")
    else:
        btn(box, "run_add_set", "Add another set", "ADD")
    for index, seed in enumerate(pipes.new_run["seeds"]):
        sub = box.box()
        row = sub.row()
        if seed["type"] == "views":
            row.label(text=f"Set {index + 1}", icon="IMAGE_BACKGROUND")
            btn(row, "run_remove_seed", "", "X", arg=index, emboss=False)
            for side in ("front", "back", "left", "right"):
                line = sub.row(align=True)
                entry = seed["views"].get(side)
                line.label(text=f"{side.capitalize()}{' · required' if side == 'front' else ''}: "
                                f"{entry.get('name') if entry else '—'}")
                op = line.operator("zetta.pick_file", text="", icon="FILEBROWSER")
                op.target = f"view:{index}:{side}"
                op = line.operator("zetta.pick_image", text="", icon="IMAGE_DATA")
                op.target = f"view:{index}:{side}"
        else:
            picture = seed.get("picture")
            if picture is not None:
                row.template_icon(icon_value=picture.icon(), scale=2.0)
            elif seed.get("thumb"):
                icon = library.thumbnail(seed["thumb"], seed.get("thumbUrl"))
                if icon:
                    row.template_icon(icon_value=icon, scale=2.0)
            row.label(text=common.shorten(seed.get("name") or "", 30))
            btn(row, "run_remove_seed", "", "X", arg=index, emboss=False)
    if pipes.new_run["uploading"]:
        box.label(text=f"Uploading {pipes.new_run['uploading']}…", icon="TIME")
    row = box.row(align=True)
    btn(row, "run_new_mode", "Manual", arg="manual", depress=pipes.new_run["mode"] == "manual",
        hint="Waits for you before each step — you run it from Runs.")
    btn(row, "run_new_mode", "Auto", arg="auto", depress=pipes.new_run["mode"] == "auto",
        hint="Every step submits itself, to the end.")
    btn(box, "run_new_import", "Import results when done", "CHECKBOX_HLT" if pipes.new_run["import"]
        else "CHECKBOX_DEHLT", depress=pipes.new_run["import"],
        hint="Each run's final models are imported into this file as they finish (never a splat)")
    count = len(pipes.ready_seeds())
    credits, _ = g.estimate(graph, studio.prices, pipes.entitlements())
    if credits is not None and count:
        box.label(text=f"Up to {credits * count:,} credits")
    problem = pipes.new_run_problem()
    if pipes.new_run["error"]:
        wrap(box, pipes.new_run["error"], "ERROR", alert=True)
    row = box.row()
    row.scale_y = 1.3
    row.enabled = problem is None and not pipes.new_run["busy"]
    label = "Creating…" if pipes.new_run["busy"] else "Create the run" if count == 1 else f"Create {count} runs"
    btn(row, "run_create", label, "PLAY", hint=problem or "")


def draw_runs(layout, zetta):
    run = pipes.run_view["run"]
    if run is not None:
        draw_run(layout, zetta, run)
        return
    row = layout.row(align=True)
    row.prop(zetta, "pipe_run_filter", text="")
    btn(row, "pipeline_open", "", "FILE_REFRESH", arg=pipes.editor["pipeline"]["id"])
    if pipes.runs["error"]:
        wrap(layout, pipes.runs["error"], "ERROR", alert=True)
    items = pipes.filtered_runs(zetta.pipe_run_filter)
    if not items:
        wrap(layout, "No runs yet. Press New run in Recipe and pick a picture or a model." if not pipes.runs["items"]
             else "No runs here.")
        return
    for summary in items:
        box = layout.box()
        row = box.row()
        seed = summary.get("seed") or {}
        if seed.get("thumbnailUrl"):
            icon = library.thumbnail(f"r{summary['id']}", seed["thumbnailUrl"])
            if icon:
                row.template_icon(icon_value=icon, scale=1.6)
        col = row.column(align=True)
        col.label(text=common.shorten(pipes.run_name(summary), 28))
        col.label(text=f"#{summary['id']} · {pipes.run_progress(summary)}")
        col.label(text=pipes.RUN_LABELS.get(summary.get("status"), summary.get("status") or ""))
        row = box.row(align=True)
        btn(row, "run_open", "Open", "FILE_FOLDER", arg=summary["id"])
        op = row.operator("zetta.menu", text="", icon="THREE_DOTS")
        op.which, op.arg = "run", str(summary["id"])


def draw_run(layout, zetta, run):
    view = pipes.run_view
    status = run.get("status")
    row = layout.row(align=True)
    btn(row, "run_close", "All runs", "BACK")
    row.label(text=f"#{run['id']} · {pipes.RUN_LABELS.get(status, status)}")
    seed = run.get("seed") or {}
    layout.label(text=f"{'Manual' if run.get('mode') == 'manual' else 'Auto'} · {run.get('spentCredits') or 0} cr spent"
                      f" · up to {run.get('estimatedCredits') or 0} cr" + (
                          " · Importing results" if pipes.importing_results(run["id"]) else ""))
    if seed.get("name"):
        layout.label(text=f"Seed: {seed['name']}", icon="IMAGE_DATA")
    if view["busy"]:
        layout.label(text=view["busy"], icon="TIME")
    if view["error"]:
        wrap(layout, view["error"], "ERROR", alert=True)

    box = layout.box()
    if status == "draft":
        needs = pipes.local_needs(run)
        if needs:
            wrap(box, f"This run hasn't started. {'One step needs' if len(needs) == 1 else f'{len(needs)} steps need'}"
                      " its own input first:", "INFO")
            for node_id, message in needs:
                btn(box, "run_select", message, "ERROR", arg=node_id)
        else:
            wrap(box, "This run hasn't started. Everything it needs is set — check the steps, then press Start.")
        row = box.row(align=True)
        btn(row, "run_action", "Manual", arg="mode", arg2="manual", depress=run.get("mode") == "manual")
        btn(row, "run_action", "Auto", arg="mode", arg2="auto", depress=run.get("mode") == "auto")
        row = box.row(align=True)
        row.scale_y = 1.3
        btn(row, "run_action", "Start", "PLAY", arg="start")
        btn(row, "run_delete", "Discard", "TRASH", arg=run["id"],
            confirm="Discard this run?|It never started — it goes, and nothing was spent.")
    elif status in pipes.OVER:
        box.label(text="This run is over.")
        btn(box, "run_delete", "Delete run", "TRASH", arg=run["id"],
            confirm="Delete this run?|Its steps go. The models it made stay in your library.")
    else:
        if status == "awaiting":
            wrap(box, "This run is waiting for you. Select the step marked Waiting for you to run it, or press Run "
                      "the next step below.", "INFO")
        row = box.row(align=True)
        btn(row, "run_action", "Manual", arg="mode", arg2="manual", depress=run.get("mode") == "manual")
        btn(row, "run_action", "Auto", arg="mode", arg2="auto", depress=run.get("mode") == "auto",
            hint="Switching to Auto releases every waiting step")
        waiting = pipes.waiting_steps(run)
        if waiting:
            total = sum(g.price_for(run.get("graph") or {}, g.node(run["graph"], s["nodeId"]) or {}, studio.prices) or 0
                        for s in waiting)
            btn(box, "run_action", "Run the next step" if len(waiting) == 1 else f"Run {len(waiting)} waiting",
                "PLAY", arg="run_waiting", confirm=f"Run {len(waiting)} step(s)?|Up to {total:,} credits.")
        row = box.row(align=True)
        if run.get("paused"):
            btn(row, "run_action", "Resume", "PLAY", arg="resume")
        else:
            btn(row, "run_action", "Pause", "PAUSE", arg="pause")
        btn(row, "run_action", "Cancel", "CANCEL", arg="cancel",
            confirm="Cancel this run?|Steps not started are cancelled, and a 3D job still in the queue is cancelled "
                    "and refunded. What it has made stays in your library.")

    graph = run.get("graph") or {}
    needs = {node_id for node_id, _ in pipes.local_needs(run)} if status == "draft" else set()
    box = layout.box()
    box.label(text="Steps", icon="NODETREE")
    for n, depth, _parent, _via in g.ordered(graph):
        node_id = n["id"]
        state, steps = pipes.node_state(run, node_id)
        row = box.row(align=True)
        if depth:
            row.separator(factor=1.5 * min(depth, 6))
        icon = {"success": "CHECKMARK", "failed": "ERROR", "running": "TIME", "waiting": "PAUSE",
                "review": "QUESTION", "cancelled": "CANCEL"}.get(state, "DOT")
        if node_id in needs:
            icon = "ERROR"
        btn(row, "run_select", g.node_name(n), icon, arg=node_id, depress=view["selected"] == node_id)
        row.label(text="Needs input" if node_id in needs else pipes.STEP_LABELS.get(state, state))
        if view["selected"] == node_id:
            draw_run_step(box, run, n, steps)


def draw_run_step(layout, run, n, steps):
    box = layout.box()
    status = run.get("status")
    graph = run.get("graph") or {}
    if not steps and not g.is_seed(n.get("kind")):
        if status == "draft":
            wrap(box, "This run hasn't started. Check this step's settings for this run below, then press Start.")
        elif status in pipes.OVER:
            wrap(box, "This step did not run — the step before it did not reach it.")
        else:
            wrap(box, "Not reached yet. It starts when the step before it finishes.")
    for step in steps:
        sub = box.box()
        title = pipes.part_label(run, step.get("partKey")) if step.get("partKey") else "Result"
        credits = step.get("credits")
        sub.label(text=f"{title} · {pipes.STEP_LABELS.get(step.get('status'), step.get('status'))}"
                       + (f" · {credits} cr" if credits else ""))
        draw_step_result(sub, run, n, step)
    editable = pipes.node_editable(run, n["id"])
    if not g.is_seed(n.get("kind")):
        box.label(text="Settings for this run" if editable else "Settings it ran with")
        if not editable:
            wrap(box, f"{g.summary(n)}. A step that has run keeps the settings it ran with — change the pipeline for "
                      "the next run, or retry a failed step with new settings.")
        draw_step_settings(box, graph, n, [], run=True, editable=editable)


def draw_step_result(layout, run, n, step):
    status, step_id = step.get("status"), step.get("id")
    if status == "waiting":
        btn(layout, "run_step", "Run this step", "PLAY", arg=step_id, arg2="run")
        return
    if status == "review":
        draw_review(layout, step)
        return
    if status == "failed":
        wrap(layout, step.get("errorMessage") or "This step failed.", "ERROR", alert=True)
        if run.get("status") != "cancelled":
            row = layout.row(align=True)
            btn(row, "run_step", "Retry", "FILE_REFRESH", arg=step_id, arg2="retry")
            btn(row, "run_step", "Retry with the settings below", "FILE_REFRESH", arg=step_id, arg2="retry_settings")
        return
    if status in ("running", "ready", "submitted"):
        gen = step.get("generation") or {}
        layout.label(text=f"The 3D job is {gen.get('status')}." if status == "submitted" and gen else "Working…",
                     icon="TIME")
        return
    output = step.get("output") or {}
    kind = output.get("type")
    if kind in ("picture", "views", "isolated"):
        found = []
        if kind == "picture":
            found.append(("Result", output.get("picture") or {}))
        elif kind == "views":
            found.extend((v.get("viewType", "").capitalize(), v.get("picture") or {}) for v in output.get("views") or [])
        else:
            found.append(("Character", output.get("character") or {}))
            found.extend((p.get("label") or p.get("key"), p.get("picture") or {}) for p in output.get("parts") or [])
        grid = layout.grid_flow(columns=2, even_columns=True)
        for caption, picture in found:
            cell = grid.column(align=True)
            url = picture.get("url")
            key = f"p{step_id}-{caption}"
            icon = library.thumbnail(key, url) if url else 0
            if icon:
                cell.template_icon(icon_value=icon, scale=3.0)
            cell.label(text=caption)
            if url:
                btn(cell, "run_save_picture", "Save", "FILE_TICK", arg=url, arg2=caption.lower())
    elif kind == "mesh":
        gen = step.get("generation") or {}
        layout.label(text=models.display_title(gen) if gen else f"#{output.get('gen3dPostId')}", icon="MESH_DATA")
        if not gen or gen.get("status") != "success":
            layout.label(text="The model is still being prepared.")
        elif models.is_splat_row(gen):
            layout.label(text="A splat — not available in Blender.")
        elif importer.objects_for(int(gen["id"])):
            row = layout.row(align=True)
            btn(row, "gen_show", "Show", "RESTRICT_SELECT_OFF", arg=gen["id"])
            btn(row, "run_step", "Place again", "OBJECT_ORIGIN", arg=step_id, arg2="place")
        elif int(gen["id"]) in importer.importing and importer.importing[int(gen["id"])].get("error") is None:
            layout.label(text="Importing…", icon="TIME")
        else:
            row = layout.row(align=True)
            btn(row, "run_step", "Import", "IMPORT", arg=step_id, arg2="import")
            btn(row, "run_step", "Import and place", "OBJECT_ORIGIN", arg=step_id, arg2="place")
    elif kind in ("splat", "file"):
        layout.label(text="A splat — not available in Blender." if kind == "splat"
                     else "A file export — not available in Blender.")
        btn(layout, "open_site", "Open on web", "URL",
            arg=f"/pipelines?pipeline={run.get('pipelineId')}&view=runs")


def draw_review(layout, step):
    review = step.get("review") or {}
    identification = review.get("identification") or {}
    parts = identification.get("parts") or []
    step_id = step["id"]
    chosen = pipes.run_view["review"].setdefault(step_id, set(review.get("proposedKeys") or []))
    extras = pipes.run_view["extras"].setdefault(step_id, [])
    subject = identification.get("subject")
    wrap(layout, (f"Found in {subject}:" if subject else "Choose the parts to isolate.") +
         " The character is always made, re-posed with its hands empty.")
    if not parts:
        wrap(layout, "Nothing was found to isolate — type the parts you want.")
    for part in parts:
        key = part.get("key")
        kind = {"held": "Held", "attached": "Attached"}.get(part.get("kind"), "Part")
        btn(layout, "run_part_toggle", f"{part.get('label') or key} · {kind} · {part.get('level') or ''}",
            "CHECKBOX_HLT" if key in chosen else "CHECKBOX_DEHLT", arg=step_id, arg2=key, depress=key in chosen)
    for text in extras:
        btn(layout, "run_part_remove", f"{text} (typed)", "X", arg=step_id, arg2=text)
    row = layout.row(align=True)
    text_prop(row, bpy.context.window_manager.zetta, "pipe_extra_part", "Another part")
    btn(row, "run_part_add", "Add", "ADD", arg=step_id)
    count = len(chosen) + len(extras)
    sub = layout.row()
    sub.enabled = count > 0
    btn(sub, "run_step", f"Isolate {count} part{'s' if count != 1 else ''} and the character" if count
        else "Choose a part", "CHECKMARK", arg=step_id, arg2="parts")


CLASSES = (ZETTA_PT_main, ZETTA_PT_studio, ZETTA_PT_jobs, ZETTA_PT_library, ZETTA_PT_pipelines)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
