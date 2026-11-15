"""The add-on's operators.

Most buttons are one generic operator, `zetta.do`, naming an action registered below (with up to two string
arguments and an optional confirmation). Pictures come in through `zetta.pick_file` / `zetta.pick_image`, a
choice from a list through `zetta.choose`, and text through `zetta.edit_text` — each with a `target` saying
where the result goes.
"""

import json
import os
import webbrowser

import bpy
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, IntProperty, StringProperty

from .. import auth, config, importer, jobs, library, props, providers, updates
from ..api import models
from ..pipelines import graph as g
from ..pipelines import state as pipes
from ..studio import common, generate, images, pictures, tools
from ..studio import context as studio_context

ACTIONS = {}


def action(name):
    def register(fn):
        ACTIONS[name] = fn
        return fn

    return register


def _int(text):
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


class ZETTA_OT_do(bpy.types.Operator):
    bl_idname = "zetta.do"
    bl_label = "Zetta 3D"
    bl_options = {"INTERNAL"}

    action: StringProperty(options={"SKIP_SAVE"})
    arg: StringProperty(options={"SKIP_SAVE"})
    arg2: StringProperty(options={"SKIP_SAVE"})
    confirm: StringProperty(options={"SKIP_SAVE"})
    hint: StringProperty(options={"SKIP_SAVE"})

    @classmethod
    def description(cls, _context, properties):
        return properties.hint or None

    def invoke(self, context, event):
        if self.confirm:
            title, _, message = self.confirm.partition("|")
            try:
                return context.window_manager.invoke_confirm(self, event, title=title, message=message)
            except TypeError:  # Blender before 4.1: no title or message
                return context.window_manager.invoke_confirm(self, event)
        return self.execute(context)

    def execute(self, context):
        fn = ACTIONS.get(self.action)
        if fn is None:
            self.report({"ERROR"}, f"Unknown action {self.action}")
            return {"CANCELLED"}
        try:
            message = fn(context, self.arg, self.arg2)
        except ValueError as error:
            self.report({"WARNING"}, str(error))
            return {"CANCELLED"}
        if message:
            self.report({"INFO"}, message)
        from ..core import tasks

        tasks.redraw()
        return {"FINISHED"}


# ─── Account, updates, links ──────────────────────────────────────────────────────────────────────


@action("sign_in")
def _sign_in(context, *_):
    error = auth.begin_login()
    if error:
        raise ValueError(error)


@action("cancel_sign_in")
def _cancel_sign_in(context, *_):
    auth.cancel_login()


@action("sign_out")
def _sign_out(context, *_):
    auth.sign_out()
    context.window_manager.zetta.lib_selecting = False


@action("refresh_account")
def _refresh_account(context, *_):
    auth.refresh()
    studio_context.retry()
    providers.refresh()


@action("retry_context")
def _retry_context(context, *_):
    studio_context.retry()


@action("open_url")
def _open_url(_context, url, *_):
    webbrowser.open(url)


@action("open_site")
def _open_site(_context, path, *_):
    webbrowser.open(config.site_base() + path)


@action("copy")
def _copy(context, text, *_):
    context.window_manager.clipboard = text
    return "Copied."


@action("check_updates")
def _check_updates(_context, *_):
    updates.check(force=True)


@action("install_update")
def _install_update(_context, *_):
    updates.install()


@action("update_notes")
def _update_notes(_context, *_):
    updates.open_notes()


@action("provider_remove")
def _provider_remove(_context, provider, *_):
    providers.remove_key(provider)


@action("provider_check")
def _provider_check(_context, provider, *_):
    providers.check_key(provider)


@action("providers_refresh")
def _providers_refresh(_context, *_):
    providers.refresh()


# ─── Studio ───────────────────────────────────────────────────────────────────────────────────────


def _form(context, tab):
    zetta = context.window_manager.zetta
    return zetta.smart if tab == "smart" else zetta.gen


@action("tool")
def _tool(context, name, *_):
    context.window_manager.zetta.tool = name


@action("generate")
def _generate(context, tab, *_):
    generate.submit(_form(context, tab), context.window_manager.zetta)


@action("redraw_picture")
def _redraw_picture(context, tab, *_):
    generate.draw_picture(_form(context, tab))


@action("clear_picture")
def _clear_picture(context, slot, *_):
    pictures.clear(slot)
    if slot == tools.TEXTURE_SINGLE:
        tools.preview.update(picture=None, prompt=None)


@action("remove_from_list")
def _remove_from_list(context, prefix, index):
    images.remove_picture(prefix, _int(index) or 0)


@action("generate_views")
def _generate_views(context, tab, *_):
    if tab == "texture":
        generate.generate_missing_views(None, budget=4, slot_of=tools.texture_view_slot, status_name="texture")
    else:
        form = _form(context, tab)
        generate.generate_missing_views(form, budget=generate.multi_view_budget(form))


@action("ai_edit_begin")
def _ai_edit_begin(context, slot, *_):
    generate.begin_ai_edit(slot)
    context.window_manager.zetta.aiedit_instruction = ""


@action("ai_edit_submit")
def _ai_edit_submit(context, *_):
    generate.submit_ai_edit(context.window_manager.zetta.aiedit_instruction)
    context.window_manager.zetta.aiedit_instruction = ""


@action("ai_edit_accept")
def _ai_edit_accept(context, *_):
    generate.accept_ai_edit()


@action("ai_edit_cancel")
def _ai_edit_cancel(context, *_):
    generate.end_ai_edit()


@action("tool_submit")
def _tool_submit(context, tool, *_):
    tools.submit(tool, context.window_manager.zetta)


@action("source_selection")
def _source_selection(context, tool, *_):
    tools.source_from_selection(tool, tools.selection_roots(list(context.selected_objects)))


@action("source_browse")
def _source_browse(context, tool, more):
    if not more and tools.browser["tool"] == tool and tools.browser["items"]:
        tools.close_browser()
        return
    tools.browse(tool, context.window_manager.zetta.src_search, more=bool(more))


@action("source_pick")
def _source_pick(context, tool, gen_id):
    gen = next((r for r in tools.browser["items"] if r.get("id") == _int(gen_id)), None)
    if gen is not None and tools.set_source(tool, gen) is None:
        tools.close_browser()


@action("source_clear")
def _source_clear(context, tool, *_):
    tools.clear_source(tool)


@action("texture_preview")
def _texture_preview(context, *_):
    tools.make_preview(context.window_manager.zetta)


@action("rig_detect")
def _rig_detect(context, *_):
    tools.detect_body(context.window_manager.zetta)


@action("image_submit")
def _image_submit(context, *_):
    images.submit(context.window_manager.zetta)


@action("image_result")
def _image_result(context, index, what):
    found = images.results[_int(index)] if _int(index) is not None and _int(index) < len(images.results) else None
    if found is None:
        return None
    zetta = context.window_manager.zetta
    picture = found["picture"]
    if what == "save":
        image = images.save(picture, found.get("prompt"))
        return f"Saved {image.name}"
    if what == "use3d":
        reason = images.use_for_3d(zetta, picture)
        if reason:
            raise ValueError(reason)
    elif what == "edit":
        images.edit_this(zetta, picture)
    elif what == "compose":
        reason = images.compose_with(zetta, picture)
        if reason:
            raise ValueError(reason)
    elif what == "dismiss":
        images.results.pop(_int(index))
    return None


@action("recent_open")
def _recent_open(context, post_id, *_):
    post = next((p for p in images.recent["items"] if p.get("id") == _int(post_id)), None)
    if post:
        images.open_stored(post)


@action("recent_refresh")
def _recent_refresh(context, *_):
    images.load_recent()


# ─── Jobs ─────────────────────────────────────────────────────────────────────────────────────────


@action("job_cancel")
def _job_cancel(context, job_id, *_):
    jobs.cancel(_int(job_id))


@action("job_dismiss")
def _job_dismiss(context, job_id, *_):
    jobs.dismiss(_int(job_id))


@action("jobs_clear")
def _jobs_clear(context, *_):
    jobs.clear_finished()


@action("job_import")
def _job_import(context, job_id, *_):
    job = jobs.find(_int(job_id))
    if job is None:
        return
    zetta = context.window_manager.zetta
    if job.get("latest") and models.is_success(job["latest"]):
        importer.import_generation(job["latest"], place=zetta.place, location=common.cursor_location(), job=job)
        return

    def found(result):
        job["latest"] = result[0]
        importer.import_generation(result[0], place=zetta.place, location=common.cursor_location(), job=job)

    from ..api import zetta as api
    from ..core import tasks

    tasks.run(lambda: api.get_generation(job["id"]), found, lambda e: job.__setitem__("error", str(e)))


# ─── Library ──────────────────────────────────────────────────────────────────────────────────────


def _gen(gen_id):
    gen = library.find(_int(gen_id))
    if gen is None:
        for job in jobs.all_jobs():
            if job["id"] == _int(gen_id) and job.get("latest"):
                return job["latest"]
        for row in tools.sources.values():
            if row.get("id") == _int(gen_id):
                return row
    if gen is None:
        raise ValueError("That model is no longer on this page — refresh the library.")
    return gen


@action("library_refresh")
def _library_refresh(context, *_):
    if context.window_manager.zetta.lib_kind == "images":
        library.reload_images(reset=True)
    else:
        library.reload()


@action("library_page")
def _library_page(context, delta, *_):
    library.go_to(library.page["page"] + (_int(delta) or 0))


@action("gen_import")
def _gen_import(context, gen_id, place):
    gen = _gen(gen_id)
    place = place == "1" if place else context.window_manager.zetta.place
    library.import_row(gen, place, common.cursor_location())


@action("gen_import_child")
def _gen_import_child(context, gen_id, *_):
    parent = context.active_object
    if parent is None:
        raise ValueError("Select the object to put it under first.")
    library.import_row(_gen(gen_id), True, tuple(parent.matrix_world.translation), parent)


@action("gen_place")
def _gen_place(context, gen_id, *_):
    objects = importer.place_existing(_gen(gen_id), common.cursor_location())
    if objects is None:
        library.import_row(_gen(gen_id), True, common.cursor_location())


@action("gen_show")
def _gen_show(context, gen_id, *_):
    if not importer.show(_int(gen_id)):
        folder = importer.local_folder(_int(gen_id))
        if folder:
            bpy.ops.wm.path_open(filepath=folder)
        else:
            raise ValueError("That model is not in this file.")


@action("gen_send")
def _gen_send(context, gen_id, tool):
    gen = _gen(gen_id)
    context.window_manager.zetta.tool = tool
    reason = tools.set_source(tool, gen)
    if reason:
        raise ValueError(reason)
    return f"{models.display_title(gen)} is the source for {tools.TITLES[tool]}."


@action("gen_fork")
def _gen_fork(context, gen_id, *_):
    reason = generate.fork(_gen(gen_id), context.window_manager.zetta)
    if reason:
        raise ValueError(reason)


@action("gen_delete")
def _gen_delete(context, gen_id, *_):
    library.delete([_int(gen_id)])


@action("gen_open_web")
def _gen_open_web(context, gen_id, *_):
    webbrowser.open(f"{config.site_base()}/3d-studio?generation={_int(gen_id)}")


@action("gen_uv")
def _gen_uv(context, gen_id, *_):
    library.save_uv_layout(_gen(gen_id))


@action("gen_select")
def _gen_select(context, gen_id, *_):
    value = _int(gen_id)
    if value in library.selected:
        library.selected.discard(value)
    else:
        library.selected.add(value)


@action("gen_select_run")
def _gen_select_run(context, ids, *_):
    ids = {_int(i) for i in ids.split(",") if _int(i) is not None}
    if ids <= library.selected:
        library.selected.difference_update(ids)
    else:
        library.selected.update(ids)


@action("library_select_page")
def _library_select_page(context, on, *_):
    ids = {g_["id"] for g_ in library.flatten(library.page["items"])}
    if on == "1":
        library.selected.update(ids)
    else:
        library.selected.difference_update(ids)


@action("library_delete_selected")
def _library_delete_selected(context, *_):
    library.delete(list(library.selected))


@action("library_select_done")
def _library_select_done(context, *_):
    library.selected.clear()
    context.window_manager.zetta.lib_selecting = False


@action("run_group_open")
def _run_group_open(context, run_id, *_):
    group = next(((g_.get("runGroup")) for g_ in library.page["items"]
                  if (g_.get("runGroup") or {}).get("runId") == _int(run_id)), None)
    library.open_run["group"] = None if library.open_run["group"] is group else group


@action("run_group_import")
def _run_group_import(context, *_):
    if library.open_run["group"]:
        library.import_run(library.open_run["group"])


@action("run_group_delete")
def _run_group_delete(context, *_):
    group = library.open_run["group"]
    if group:
        library.delete([m["id"] for m in group.get("members") or []])
        library.open_run["group"] = None


@action("run_group_open_pipeline")
def _run_group_open_pipeline(context, pipeline_id, run_id):
    zetta = context.window_manager.zetta
    zetta.pipe_view = "runs"
    pipes.open_pipeline(_int(pipeline_id), then=lambda: pipes.open_run(_int(run_id)))
    return "Opened in Pipelines."


@action("image_post")
def _image_post(context, post_id, what):
    post = next((p for p in library.images["items"] if p.get("id") == _int(post_id)), None)
    if post is None:
        return None
    zetta = context.window_manager.zetta
    if what == "delete":
        library.delete_image(post)
        return None

    def then(picture):
        if what == "save":
            images.save(picture, post.get("prompt"))
        elif what == "use3d":
            images.use_for_3d(zetta, picture)
        elif what == "edit":
            images.edit_this(zetta, picture)
        elif what == "seed":
            pipes.add_picture_seed(picture)

    if what == "seed":
        pipes.add_image_seed(post)
        return None
    images.open_stored(post, then)
    if what in ("use3d", "edit"):
        zetta.tool = "generate" if what == "use3d" else "images"
    return None


@action("images_more")
def _images_more(context, *_):
    library.reload_images(more=True)


@action("upload_selection")
def _upload_selection(context, *_):
    objects = tools.selection_roots(list(context.selected_objects))
    if not objects:
        raise ValueError("Select the objects to upload first.")
    title = objects[0].name
    library.notice.update(text=f"Uploading {title}…", error=None)

    def done(gen):
        library.notice.update(text=f"{title} was added to your Zetta 3D library.", error=None)
        library.reload()

    importer.upload_objects(objects, title, done, lambda message: library.notice.update(text=None, error=message))


# ─── Pipelines ────────────────────────────────────────────────────────────────────────────────────


@action("pipelines_refresh")
def _pipelines_refresh(context, *_):
    pipes.load_list()


@action("pipeline_new")
def _pipeline_new(context, *_):
    pipes.create_pipeline()


@action("pipeline_open")
def _pipeline_open(context, pipeline_id, *_):
    pipes.open_pipeline(_int(pipeline_id))


@action("pipeline_close")
def _pipeline_close(context, *_):
    pipes.close()
    pipes.load_list()


@action("pipeline_duplicate")
def _pipeline_duplicate(context, pipeline_id, *_):
    pipes.duplicate_pipeline(_int(pipeline_id))


@action("pipeline_delete")
def _pipeline_delete(context, pipeline_id, *_):
    pipes.delete_pipeline(_int(pipeline_id))


@action("pipeline_undo")
def _pipeline_undo(context, *_):
    pipes.undo()


@action("pipeline_redo")
def _pipeline_redo(context, *_):
    pipes.redo()


@action("pipeline_save")
def _pipeline_save(context, *_):
    pipes.save()


@action("pipeline_describe")
def _pipeline_describe(context, *_):
    pipes.describe(context.window_manager.zetta.pipe_describe)


@action("pipeline_select")
def _pipeline_select(context, node_id, *_):
    pipes.editor["selected"] = None if pipes.editor["selected"] == node_id else node_id


@action("pipeline_remove_step")
def _pipeline_remove_step(context, node_id, *_):
    pipes.edit(lambda graph: g.remove_step(graph, node_id, pipes.entitlements()))
    if pipes.editor["selected"] == node_id:
        pipes.editor["selected"] = None


@action("pipeline_disconnect")
def _pipeline_disconnect(context, node_id, handle):
    pipes.edit(lambda graph: g.disconnect(graph, node_id, handle or "in", pipes.entitlements()))


@action("pipeline_set")
def _pipeline_set(context, node_id, payload):
    """payload: JSON {key: value} — a recipe step's settings patch, or with "run" set, a draft run's."""
    patch = json.loads(payload)
    if patch.pop("__run__", False):
        pipes.patch_run_node(node_id, patch)
        return None
    pipes.edit(lambda graph: g.patch_settings(graph, node_id, patch, pipes.entitlements()), coalesce=f"settings:{node_id}")
    return None


@action("pipeline_new_run")
def _pipeline_new_run(context, *_):
    reasons = pipes.recipe_problems()
    if reasons:
        raise ValueError(reasons[0][1])
    pipes.flush()
    pipes.open_new_run()


@action("run_new_cancel")
def _run_new_cancel(context, *_):
    pipes.new_run["open"] = False


@action("run_new_mode")
def _run_new_mode(context, mode, *_):
    pipes.new_run["mode"] = mode


@action("run_new_import")
def _run_new_import(context, *_):
    pipes.new_run["import"] = not pipes.new_run["import"]


@action("run_add_set")
def _run_add_set(context, *_):
    pipes.add_views_set()


@action("run_remove_seed")
def _run_remove_seed(context, index, *_):
    pipes.remove_seed(_int(index))


@action("run_add_selection")
def _run_add_selection(context, *_):
    objects = tools.selection_roots(list(context.selected_objects))
    if not objects:
        raise ValueError("Select a model first.")
    gen_id = tools.zetta_id_of(objects)
    if gen_id is not None:
        from ..api import zetta as api
        from ..core import tasks

        def found(result):
            if result[0] and result[0].get("status") == "success":
                pipes.add_model_seed(result[0])

        tasks.run(lambda: api.get_generation(gen_id), found, lambda e: pipes.new_run.__setitem__("error", str(e)))
        return None
    pipes.new_run["uploading"] += 1

    def done(gen):
        pipes.new_run["uploading"] -= 1
        pipes.add_model_seed(gen)

    def fail(message):
        pipes.new_run["uploading"] -= 1
        pipes.new_run["error"] = message

    importer.upload_objects(objects, objects[0].name, done, fail)
    return None


@action("run_create")
def _run_create(context, *_):
    pipes.create_runs()


@action("run_open")
def _run_open(context, run_id, *_):
    context.window_manager.zetta.pipe_view = "runs"
    pipes.open_run(_int(run_id))


@action("run_close")
def _run_close(context, *_):
    pipes.close_run()


@action("run_action")
def _run_action(context, what, arg):
    {"start": pipes.start_run, "pause": pipes.pause_run, "resume": pipes.resume_run, "cancel": pipes.cancel_run,
     "run_waiting": pipes.run_waiting}.get(what, lambda: None)()
    if what == "mode":
        pipes.set_mode(arg)


@action("run_delete")
def _run_delete(context, run_id, *_):
    pipes.delete_run(_int(run_id))


@action("run_select")
def _run_select(context, node_id, *_):
    view = pipes.run_view
    view["selected"] = None if view["selected"] == node_id else node_id


@action("run_step")
def _run_step(context, step_id, what):
    step_id = _int(step_id)
    if what == "run":
        pipes.run_step(step_id)
    elif what == "retry":
        pipes.retry_step(step_id)
    elif what == "retry_settings":
        pipes.retry_step(step_id, with_settings=True)
    elif what == "parts":
        pipes.choose_parts(step_id)
    elif what in ("import", "place"):
        run = pipes.run_view["run"] or {}
        step = next((s for s in run.get("steps") or [] if s.get("id") == step_id), None)
        if step:
            pipes.import_step(step, place=what == "place")


@action("run_part_toggle")
def _run_part_toggle(context, step_id, key):
    chosen = pipes.run_view["review"].setdefault(_int(step_id), set())
    if key in chosen:
        chosen.discard(key)
    else:
        chosen.add(key)


@action("run_part_add")
def _run_part_add(context, step_id, *_):
    zetta = context.window_manager.zetta
    text = zetta.pipe_extra_part.strip()[:80]
    extras = pipes.run_view["extras"].setdefault(_int(step_id), [])
    if text and text.lower() not in [e.lower() for e in extras] and len(extras) < 16:
        extras.append(text)
    zetta.pipe_extra_part = ""


@action("run_part_remove")
def _run_part_remove(context, step_id, text):
    extras = pipes.run_view["extras"].setdefault(_int(step_id), [])
    if text in extras:
        extras.remove(text)


@action("run_save_picture")
def _run_save_picture(context, url, name):
    from ..api import zetta as api
    from ..core import tasks

    def done(data):
        image = importer.save_image(data, name or "pipeline-picture", pictures.sniff(data))
        pipes.run_view["error"] = None
        pipes._notify(f"Saved {image.name}")

    tasks.run(lambda: api.http.fetch_bytes(url, timeout=60), done,
              lambda e: pipes.run_view.__setitem__("error", str(e)))


# ─── Pictures in ──────────────────────────────────────────────────────────────────────────────────


def deliver(target, picture, context):
    """Puts a picture where a picker's target says. Returns an error string, or None."""
    kind, _, rest = target.partition(":")
    zetta = context.window_manager.zetta
    if kind == "slot":
        reason = pictures.put(rest, picture)
        if reason:
            return reason
        if rest in ("generate_single", "smart_single") or "_mv_" in rest:
            form = zetta.smart if rest.startswith("smart") else zetta.gen
            if not form.title_touched:
                base = os.path.splitext(picture.name)[0].replace("_", " ").strip()
                form["title"] = " ".join(base.split())[:255]
        if rest == tools.TEXTURE_SINGLE:
            tools.preview.update(picture=None, prompt=None)
        return None
    if kind == "list":
        prefix, _, limit = rest.partition(":")
        return images.add_picture(prefix, picture, _int(limit) or 1)
    if kind == "seed":
        pipes.add_picture_seed(picture)
        return None
    if kind == "view":
        index, _, side = rest.partition(":")
        pipes.set_view(_int(index), side, picture)
        return None
    if kind == "runref":
        pipes.set_run_reference(rest, picture)
        return None
    if kind == "runview":
        node_id, _, side = rest.rpartition(":")
        pipes.set_run_view(node_id, side, picture)
        return None
    return f"Unknown picture target {target}"


class ZETTA_OT_pick_file(bpy.types.Operator):
    bl_idname = "zetta.pick_file"
    bl_label = "Choose a picture"
    bl_description = "Choose a PNG, JPEG or WebP picture"
    bl_options = {"INTERNAL"}

    target: StringProperty(options={"SKIP_SAVE"})
    multiple: BoolProperty(default=False, options={"SKIP_SAVE"})
    filepath: StringProperty(subtype="FILE_PATH")
    directory: StringProperty(subtype="DIR_PATH")
    files: CollectionProperty(type=bpy.types.OperatorFileListElement)
    filter_image: BoolProperty(default=True, options={"HIDDEN"})
    filter_folder: BoolProperty(default=True, options={"HIDDEN"})
    filter_glob: StringProperty(default="*.png;*.jpg;*.jpeg;*.webp", options={"HIDDEN"})

    def invoke(self, context, _event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        paths = [os.path.join(self.directory, f.name) for f in self.files if f.name] if self.multiple else []
        paths = paths or [self.filepath]
        problems = []
        for path in paths:
            try:
                picture = pictures.from_file(path)
            except (ValueError, OSError) as error:
                problems.append(str(error))
                continue
            reason = deliver(self.target, picture, context)
            if reason:
                problems.append(reason)
        if problems:
            self.report({"WARNING"}, problems[0])
        from ..core import tasks

        tasks.redraw()
        return {"FINISHED"}


def _image_items(_self, _context):
    return [(image.name, image.name, "") for image in bpy.data.images if image.type in {"IMAGE", "UV_TEST"}
            or image.has_data] or [("NONE", "No images in this file", "")]


class ZETTA_OT_pick_image(bpy.types.Operator):
    bl_idname = "zetta.pick_image"
    bl_label = "Use a Blender image"
    bl_description = "Use one of this file's images"
    bl_options = {"INTERNAL"}
    bl_property = "image"

    target: StringProperty(options={"SKIP_SAVE"})
    image: EnumProperty(items=_image_items)

    def invoke(self, context, _event):
        context.window_manager.invoke_search_popup(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        image = bpy.data.images.get(self.image) if self.image != "NONE" else None
        if image is None:
            return {"CANCELLED"}
        try:
            picture = pictures.from_image(image)
        except Exception as error:  # noqa: BLE001
            self.report({"WARNING"}, f"That image could not be read: {error}")
            return {"CANCELLED"}
        reason = deliver(self.target, picture, context)
        if reason:
            self.report({"WARNING"}, reason)
        from ..core import tasks

        tasks.redraw()
        return {"FINISHED"}


# ─── Choosing from a list ─────────────────────────────────────────────────────────────────────────

_choices = {}  # the last items built, kept alive (Blender needs the strings to outlive the call)


def _choice_items(self, _context):
    items = build_choices(self.source)
    _choices[self.source] = items
    return items or [("NONE", "Nothing to choose", "")]


def build_choices(source):
    kind, _, rest = source.partition(":")
    if kind == "step_kind":
        has_seed = pipes.seed_kind() is not None
        return [(k, g.label(k), g.blurb(k)) for k in g.KINDS
                if g.offered(k) and not (has_seed and g.is_seed(k))]
    if kind == "connect":
        node_id, _, handle = rest.partition(":")
        graph = pipes.editor["graph"] or {}
        found = []
        for n in graph.get("nodes") or []:
            for out in g.outputs(n):
                if g.connection_problem(graph, n["id"], out, node_id, handle or "in") is None:
                    found.append((f"{n['id']}|{out}", f"{g.node_name(n)} › {g.output_label(n, out)}", g.summary(n)))
        return found
    if kind == "after_output":
        n = g.node(pipes.editor["graph"] or {}, rest)
        return [(out, g.output_label(n, out), "") for out in g.outputs(n)] if n else []
    if kind == "model_seed":
        return [(str(gen["id"]), models.display_title(gen), "") for gen in library.flatten(library.page["items"])
                if models.is_success(gen) and not models.is_splat_row(gen)]
    if kind == "image_seed":
        return [(str(p["id"]), (p.get("prompt") or f"Picture #{p['id']}")[:60], "") for p in library.images["items"]
                if (p.get("images") or [{}])[0].get("url")]
    return []


class ZETTA_OT_choose(bpy.types.Operator):
    bl_idname = "zetta.choose"
    bl_label = "Choose"
    bl_options = {"INTERNAL"}
    bl_property = "choice"

    source: StringProperty(options={"SKIP_SAVE"})
    arg: StringProperty(options={"SKIP_SAVE"})
    choice: EnumProperty(items=_choice_items)

    def invoke(self, context, _event):
        kind = self.source.partition(":")[0]
        if kind == "model_seed":
            library.ensure_loaded()
        if kind == "image_seed":
            library.ensure_images_loaded()
        context.window_manager.invoke_search_popup(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        kind, _, rest = self.source.partition(":")
        value = self.choice
        if not value or value == "NONE":
            return {"CANCELLED"}
        if kind == "step_kind":
            after = pipes.editor["selected"]
            output = self.arg or None

            def add(graph):
                new_graph, new_id = g.add_step(graph, value, after, output, pipes.entitlements())
                pipes.editor["selected"] = new_id
                return new_graph

            pipes.edit(add)
        elif kind == "connect":
            node_id, _, handle = rest.partition(":")
            source_id, _, out = value.partition("|")
            pipes.edit(lambda graph: g.connect(graph, source_id, out, node_id, handle or "in", pipes.entitlements()))
        elif kind == "after_output":
            bpy.ops.zetta.choose("INVOKE_DEFAULT", source="step_kind", arg=value)
        elif kind == "model_seed":
            gen = library.find(int(value))
            if gen:
                pipes.add_model_seed(gen)
        elif kind == "image_seed":
            post = next((p for p in library.images["items"] if p.get("id") == int(value)), None)
            if post:
                pipes.add_image_seed(post)
        if pipes.editor["error"]:
            self.report({"WARNING"}, pipes.editor["error"])
        from ..core import tasks

        tasks.redraw()
        return {"FINISHED"}


# ─── Text dialogs ─────────────────────────────────────────────────────────────────────────────────


class ZETTA_OT_edit_text(bpy.types.Operator):
    bl_idname = "zetta.edit_text"
    bl_label = "Edit"
    bl_options = {"INTERNAL"}

    target: StringProperty(options={"SKIP_SAVE"})
    title: StringProperty(options={"SKIP_SAVE"})
    text: StringProperty(name="", options={"SKIP_SAVE"})

    def invoke(self, context, _event):
        return context.window_manager.invoke_props_dialog(self, width=420, title=self.title or "Edit")

    def draw(self, _context):
        self.layout.prop(self, "text", text="")

    def execute(self, context):
        kind, _, rest = self.target.partition(":")
        if kind == "rename":
            gen = library.find(_int(rest))
            if gen:
                library.rename(gen, self.text)
        elif kind == "step_label":
            pipes.edit(lambda graph: g.set_label(graph, rest, self.text))
        from ..core import tasks

        tasks.redraw()
        return {"FINISHED"}


class ZETTA_OT_edit_setting(bpy.types.Operator):
    """Edits one text or number setting of a pipeline step (or of a draft run's step)."""

    bl_idname = "zetta.edit_setting"
    bl_label = "Edit setting"
    bl_options = {"INTERNAL"}

    node_id: StringProperty(options={"SKIP_SAVE"})
    key: StringProperty(options={"SKIP_SAVE"})
    title: StringProperty(options={"SKIP_SAVE"})
    numeric: BoolProperty(default=False, options={"SKIP_SAVE"})
    run: BoolProperty(default=False, options={"SKIP_SAVE"})
    as_list: BoolProperty(default=False, options={"SKIP_SAVE"})
    text: StringProperty(name="", options={"SKIP_SAVE"})
    number: IntProperty(name="", options={"SKIP_SAVE"})

    def invoke(self, context, _event):
        graph = (pipes.run_view["run"] or {}).get("graph") if self.run else pipes.editor["graph"]
        n = g.node(graph or {}, self.node_id)
        if n is None:
            return {"CANCELLED"}
        value = g.with_defaults(n["kind"], n.get("settings")).get(self.key)
        if self.numeric:
            try:
                self.number = int(value)
            except (TypeError, ValueError):
                self.number = 0
        elif self.as_list:
            self.text = ", ".join(value or [])
        else:
            self.text = "" if value is None else str(value)
        return context.window_manager.invoke_props_dialog(self, width=460, title=self.title or self.key)

    def draw(self, _context):
        self.layout.prop(self, "number" if self.numeric else "text", text="")
        if self.as_list:
            self.layout.label(text="Separate them with commas.")

    def execute(self, context):
        if self.numeric:
            value = self.number
        elif self.as_list:
            value = [p.strip()[:80] for p in self.text.split(",") if p.strip()][:16]
        else:
            value = self.text
        payload = {self.key: value}
        if self.key == "polygonCount":
            payload["polygonCountTyped"] = True
        if self.run:
            payload["__run__"] = True
        bpy.ops.zetta.do(action="pipeline_set", arg=self.node_id, arg2=json.dumps(payload))
        return {"FINISHED"}


class ZETTA_OT_provider_key(bpy.types.Operator):
    bl_idname = "zetta.provider_key"
    bl_label = "Save API key"
    bl_description = "Paste your API key for this provider. It is kept in your system's key store"
    bl_options = {"INTERNAL"}

    provider: StringProperty(options={"SKIP_SAVE"})
    key: StringProperty(name="API key", subtype="PASSWORD", options={"SKIP_SAVE"})

    def invoke(self, context, _event):
        found = providers.provider(self.provider) or {}
        return context.window_manager.invoke_props_dialog(
            self, width=460, title=f"Your {found.get('label') or self.provider} API key")

    def draw(self, _context):
        self.layout.prop(self, "key", text="")

    def execute(self, _context):
        error = providers.save_key(self.provider, self.key)
        self.key = ""
        if error:
            self.report({"WARNING"}, error)
            return {"CANCELLED"}
        return {"FINISHED"}


# ─── Menus ────────────────────────────────────────────────────────────────────────────────────────


class ZETTA_OT_menu(bpy.types.Operator):
    """More actions"""

    bl_idname = "zetta.menu"
    bl_label = "More"
    bl_options = {"INTERNAL"}

    which: StringProperty(options={"SKIP_SAVE"})
    arg: StringProperty(options={"SKIP_SAVE"})

    def invoke(self, context, _event):
        from . import panels

        which, arg = self.which, self.arg

        def draw(menu, _context):
            panels.draw_menu(menu.layout, which, arg)

        context.window_manager.popup_menu(draw, title=panels.menu_title(which, arg))
        return {"FINISHED"}


def object_menu(self, _context):
    layout = self.layout
    layout.separator()
    op = layout.operator("zetta.do", text="Upload to Zetta 3D library", icon_value=pictures.mark())
    op.action = "upload_selection"
    for tool, _ in library.send_to_options({"selectedFeature": "upload", "status": "success"}):
        op = layout.operator("zetta.do", text=f"Zetta 3D › {tools.TITLES[tool]}", icon_value=pictures.mark())
        op.action, op.arg = "source_selection_tool", tool


@action("source_selection_tool")
def _source_selection_tool(context, tool, *_):
    context.window_manager.zetta.tool = tool
    _source_selection(context, tool)


def image_menu(self, context):
    image = getattr(context.space_data, "image", None)
    if image is None:
        return
    layout = self.layout
    layout.separator()
    op = layout.operator("zetta.do", text="Generate 3D from this image (Zetta 3D)", icon_value=pictures.mark())
    op.action, op.arg = "image_to_3d", image.name


@action("image_to_3d")
def _image_to_3d(context, name, *_):
    image = bpy.data.images.get(name)
    if image is None:
        return None
    zetta = context.window_manager.zetta
    reason = images.use_for_3d(zetta, pictures.from_image(image))
    if reason:
        raise ValueError(reason)
    return "The picture is in Generate 3D — open the Zetta 3D sidebar (N) to build it."


CLASSES = (ZETTA_OT_do, ZETTA_OT_pick_file, ZETTA_OT_pick_image, ZETTA_OT_choose, ZETTA_OT_edit_text,
           ZETTA_OT_edit_setting, ZETTA_OT_provider_key, ZETTA_OT_menu)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.VIEW3D_MT_object_context_menu.append(object_menu)
    bpy.types.IMAGE_MT_image.append(image_menu)


def unregister():
    bpy.types.IMAGE_MT_image.remove(image_menu)
    bpy.types.VIEW3D_MT_object_context_menu.remove(object_menu)
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


__all__ = ["props"]
