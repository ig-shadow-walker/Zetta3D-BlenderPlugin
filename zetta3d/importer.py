"""Brings a finished model into the open .blend, and sends Blender objects up to Zetta 3D.

Downloads go to <models folder>/<id>-<name>/ — next to the .blend by default (//zetta3d/models), or the
add-on's own folder while the file is unsaved. Each import lands in its own collection under "Zetta 3D", its
top objects tagged with the generation's id (`zetta_id`) so the library can say which models are already here.
"""

import contextlib
import os
import re
import tempfile

import bpy
from mathutils import Vector

from .api import models, zetta
from .core import prefs, state, tasks

ROOT_COLLECTION = "Zetta 3D"
ID_PROP = "zetta_id"

# id → {"progress": 0..1, "error": str|None}, for the library's tiles and the jobs list.
importing = {}


def slug(text, limit=40):
    """lower-case, runs of anything but a-z0-9 → "-", at most 40 characters; "model" when nothing is left."""
    text = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return text[:limit].strip("-") or "model"


def _folder(setting, fallback):
    configured = prefs.get(setting, "") or ""
    if bpy.data.filepath and configured.startswith("//"):
        return bpy.path.abspath(configured)
    if configured and not configured.startswith("//"):
        return os.path.abspath(os.path.expanduser(configured))
    # An unsaved .blend has no "next to it": keep downloads in the add-on's folder.
    return state.path(fallback)


def models_folder():
    return _folder("models_folder", "models")


def images_folder():
    return _folder("images_folder", "images")


def ids_in_scene():
    """The generation ids already imported into this .blend."""
    found = set()
    for obj in bpy.data.objects:
        value = obj.get(ID_PROP)
        if value is not None:
            try:
                found.add(int(value))
            except (TypeError, ValueError):
                pass
    return found


def objects_for(generation_id):
    return [o for o in bpy.data.objects if o.get(ID_PROP) == generation_id or o.get(ID_PROP) == str(generation_id)]


# ─── Import ───────────────────────────────────────────────────────────────────────────────────────


def pick_files(gen, preferred="glb"):
    """The files to import: every part of a segmentation, the GLB of a world mesh, else one file by format."""
    meshes = models.mesh_files(gen)
    glbs = [f for f in meshes if models.file_extension(f) == "glb"]
    feature = gen.get("selectedFeature")
    if feature in ("segmentation", "smartSegmentation") and len(glbs) > 1:

        def part_index(f):
            match = re.search(r"-(\d+)\.glb$", models.file_name(f), re.IGNORECASE)
            return (0, int(match.group(1))) if match else (1, 0)

        return sorted(glbs, key=part_index)
    if feature == "worldMeshExport" and glbs:
        return glbs[:1]
    order = ("fbx", "glb") if preferred == "fbx" else ("glb", "fbx")
    for ext in (*order, "gltf", "obj", "zip"):
        for f in meshes:
            if models.file_extension(f) == ext:
                return [f]
    return []


def _sniff_picture(data):
    if data[:2] == b"\xff\xd8":
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return "png"


def _extract(data, folder):
    """Extracts a zip's model files flat into folder; returns their paths."""
    import io
    import zipfile

    found = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for entry in archive.infolist():
            name = os.path.basename(entry.filename)
            if entry.is_dir() or not name or name.startswith(".") or "__MACOSX" in entry.filename:
                continue
            target = os.path.join(folder, name)
            with archive.open(entry) as source, open(target, "wb") as handle:
                handle.write(source.read())
            if os.path.splitext(name)[1].lower() in (".obj", ".fbx", ".glb", ".gltf"):
                found.append(target)
    return found


def _write_manifest(folder, gen):
    import datetime
    import json

    from . import config

    manifest = {
        "generationId": int(gen["id"]),
        "title": gen.get("title"),
        "feature": gen.get("selectedFeature"),
        "model": gen.get("selectedModel"),
        "prompt": gen.get("inputPrompt"),
        "faceCount": gen.get("faceCount"),
        "importedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "site": f"{config.site_base()}/3d-studio?generation={int(gen['id'])}",
    }
    with open(os.path.join(folder, "zetta.json"), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2)


def download_uv_layout(gen, folder=None, progress=None):
    """Saves a Smart UV result's layout picture beside the model; returns its path (worker thread)."""
    layout = models.uv_layout(gen)
    if layout is None:
        return None
    name = slug(models.display_title(gen))
    folder = folder or os.path.join(models_folder(), f"{int(gen['id'])}-{name}")
    url = zetta.download_url(int(gen["id"]), layout.get("key")) or layout.get("url")
    data = zetta.http.download(url, progress)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{name}-uv-layout.{_sniff_picture(data)}")
    with open(path, "wb") as handle:
        handle.write(data)
    return path


def import_generation(gen, place=True, location=None, job=None, done=None, fail=None, parent=None, around=None):
    """Downloads the generation's mesh(es) and imports them. done(objects) / fail(message) on the main thread.

    around: a context manager the main-thread import runs inside — the Zetta Agent passes its changes record's
    `running`, so the import counts as the agent's change, not the user's."""
    gen_id = int(gen["id"])
    if gen_id in importing and importing[gen_id].get("error") is None:
        return  # already on its way
    if models.is_splat_row(gen):
        _failed(gen_id, job, fail, "Gaussian splats can't be imported into Blender — open it on zetta3d.ai.")
        return
    if not models.is_success(gen):
        _failed(gen_id, job, fail, "That model is not ready yet.")
        return
    chosen = pick_files(gen, prefs.get("preferred_format", "glb") or "glb")
    if not chosen:
        _failed(gen_id, job, fail, "This generation has no mesh file Blender can import.")
        return
    name = slug(models.display_title(gen))
    folder = os.path.join(models_folder(), f"{gen_id}-{name}")
    importing[gen_id] = {"progress": 0.0, "error": None}
    if job is not None:
        job["progress"] = 0.0

    def progress(fraction):
        def set_progress():
            if gen_id in importing:
                importing[gen_id]["progress"] = fraction
            if job is not None:
                job["progress"] = fraction

        tasks.post(set_progress)

    def work():
        os.makedirs(folder, exist_ok=True)
        paths = []
        count = len(chosen)
        for index, f in enumerate(chosen):
            url = zetta.download_url(gen_id, f.get("key")) or f.get("url")
            if not url:
                raise zetta.ApiError(0, "Zetta 3D did not give a download link for this model.", "NO_DOWNLOAD")
            data = zetta.http.download(url, lambda p, i=index: progress(0.95 * (i + p) / count))
            ext = models.file_extension(f)
            if ext == "zip":
                extracted = _extract(data, folder)
                if not extracted:
                    raise ValueError(f"The archive for {name} has no model Blender can import.")
                paths.extend(extracted)
                continue
            if ext not in ("glb", "gltf", "fbx", "obj"):
                ext = "fbx" if data[:18] == b"Kaydara FBX Binary" else "glb"
            file_name = f"{name}.{ext}" if count == 1 else f"{name}-part{index + 1}.{ext}"
            path = os.path.join(folder, file_name)
            with open(path, "wb") as handle:
                handle.write(data)
            paths.append(path)
        _write_manifest(folder, gen)
        uv_path = None
        if gen.get("selectedFeature") == "uvUnwrap" and models.uv_layout(gen) is not None:
            try:
                uv_path = download_uv_layout(gen, folder)
            except Exception as error:  # noqa: BLE001 — the picture never fails the import
                print(f"[Zetta 3D] The UV layout picture could not be downloaded: {error}")
        return paths, uv_path

    def downloaded(result):
        paths, uv_path = result
        try:
            with around() if around else contextlib.nullcontext():
                objects = import_files(paths, gen, place=place, location=location, parent=parent)
        except Exception as error:  # noqa: BLE001 — the importer's own errors are the user's to see
            _failed(gen_id, job, fail, f"Finished, but the import failed: {error}")
            return
        if uv_path:
            try:
                image = bpy.data.images.load(uv_path, check_existing=True)
                image["zetta_id"] = gen_id
            except RuntimeError:
                pass
        importing.pop(gen_id, None)
        if job is not None:
            job["imported"] = [o.name for o in objects]
            job["progress"] = None
        if done:
            done(objects)

    def failed(error):
        _failed(gen_id, job, fail, zetta_error_text(error))

    tasks.run(work, downloaded, failed, name=f"zetta3d-import-{gen_id}")


def zetta_error_text(error):
    from .api import catalog

    return catalog.explain(error) if isinstance(error, zetta.ApiError) else str(error)


def _failed(gen_id, job, fail, message):
    importing[gen_id] = {"progress": None, "error": message}
    if job is not None:
        job["error"] = message
        job["progress"] = None
    if fail:
        fail(message)
    else:
        print(f"[Zetta 3D] {message}")


def _context_override():
    """A window and 3D view for operators run from a timer, when there is one."""
    wm = bpy.context.window_manager
    for window in wm.windows if wm else ():
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                region = next((r for r in area.regions if r.type == "WINDOW"), None)
                return {"window": window, "area": area, "region": region}
    return {}


def _import_one(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in (".glb", ".gltf"):
        bpy.ops.import_scene.gltf(filepath=path)
    elif ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=path)
    elif ext == ".obj":
        bpy.ops.wm.obj_import(filepath=path)
    else:
        raise ValueError(f"Unsupported file type {ext}")


def import_files(paths, gen=None, place=True, location=None, parent=None):
    """Imports model files into their own collection under "Zetta 3D" and returns the new objects. Several files
    (a segmentation's parts) go under one empty named after the model."""
    if isinstance(paths, str):
        paths = [paths]
    before = set(bpy.data.objects)
    with bpy.context.temp_override(**_context_override()):
        if bpy.context.mode != "OBJECT" and bpy.context.object is not None:
            bpy.ops.object.mode_set(mode="OBJECT")
        for path in paths:
            _import_one(path)
    new_objects = [o for o in bpy.data.objects if o not in before]
    if not new_objects:
        raise ValueError("the file had no objects in it")

    title = models.display_title(gen) if gen else os.path.splitext(os.path.basename(paths[0]))[0]
    target = _collection(f"{title} #{gen['id']}" if gen else title)
    for obj in new_objects:
        for owner in list(obj.users_collection):
            owner.objects.unlink(obj)
        target.objects.link(obj)

    roots = [o for o in new_objects if o.parent is None or o.parent not in new_objects]
    if len(paths) > 1:
        group = bpy.data.objects.new(title, None)
        group.empty_display_type = "PLAIN_AXES"
        target.objects.link(group)
        for obj in roots:
            obj.parent = group
        new_objects.append(group)
        roots = [group]
    if gen:
        for obj in new_objects:
            obj[ID_PROP] = int(gen["id"])
        for obj in roots:
            obj["zetta_feature"] = gen.get("selectedFeature") or ""
            obj["zetta_title"] = title
            if len(paths) == 1:
                obj.name = title

    if parent is not None:
        for obj in roots:
            world = obj.matrix_world.copy()
            obj.parent = parent
            obj.matrix_world = world
    if place:
        _rest_on_ground(roots, new_objects, location)

    _select(roots)
    return new_objects


def _select(objects):
    layer = bpy.context.view_layer
    for obj in layer.objects:
        obj.select_set(False)
    visible = [o for o in objects if o.name in layer.objects]
    for obj in visible:
        obj.select_set(True)
    if visible:
        layer.objects.active = visible[0]


def local_folder(generation_id):
    """The folder an earlier import of this generation downloaded into, when it is still there."""
    root = models_folder()
    try:
        names = os.listdir(root)
    except OSError:
        return None
    prefix = f"{int(generation_id)}-"
    for name in sorted(names):
        folder = os.path.join(root, name)
        if name.startswith(prefix) and os.path.isfile(os.path.join(folder, "zetta.json")):
            return folder
    return None


def local_model_files(generation_id):
    folder = local_folder(generation_id)
    if not folder:
        return []
    return sorted(
        os.path.join(folder, f) for f in os.listdir(folder)
        if os.path.splitext(f)[1].lower() in (".glb", ".gltf", ".fbx", ".obj")
    )


def in_project(generation_id):
    """Imported into this .blend, or downloaded beside it."""
    return generation_id in ids_in_scene() or local_folder(generation_id) is not None


def place_existing(gen, location=None, parent=None):
    """Places another copy of a model already downloaded — no download. Returns the objects, or None."""
    files = local_model_files(int(gen["id"]))
    if not files:
        return None
    return import_files(files, gen, place=True, location=location, parent=parent)


def show(generation_id):
    """Selects the model's objects and frames them in the 3D view."""
    objects = [o for o in objects_for(generation_id) if o.parent is None or o.parent.get(ID_PROP) is None]
    if not objects:
        return False
    _select(objects)
    override = _context_override()
    if override.get("region") is not None:
        with bpy.context.temp_override(**override):
            bpy.ops.view3d.view_selected()
    return True


def _collection(name):
    scene = bpy.context.scene
    root = bpy.data.collections.get(ROOT_COLLECTION)
    if root is None:
        root = bpy.data.collections.new(ROOT_COLLECTION)
    if root.name not in scene.collection.children:
        scene.collection.children.link(root)
    collection = bpy.data.collections.new(name)
    root.children.link(collection)
    return collection


def _rest_on_ground(roots, objects, location):
    """Moves the import so it stands on the ground (z = 0) at the location (the 3D cursor), centred on it."""
    bpy.context.view_layer.update()
    corners = []
    for obj in objects:
        if obj.type in {"MESH", "CURVE", "SURFACE", "META", "FONT"}:
            corners.extend(obj.matrix_world @ Vector(c) for c in obj.bound_box)
    if not corners:
        return
    low = Vector((min(c.x for c in corners), min(c.y for c in corners), min(c.z for c in corners)))
    high = Vector((max(c.x for c in corners), max(c.y for c in corners), max(c.z for c in corners)))
    target = Vector(location) if location is not None else bpy.context.scene.cursor.location.copy()
    centre = (low + high) / 2
    offset = Vector((target.x - centre.x, target.y - centre.y, target.z - low.z))
    for obj in roots:
        obj.location += offset


# ─── Pictures ─────────────────────────────────────────────────────────────────────────────────────


def save_image(data, name, content_type="image/png"):
    """Writes picture bytes to the images folder and loads them as a Blender image (packed into nothing —
    the file on disk is the source). Returns the image datablock."""
    ext = {"image/jpeg": ".jpg", "image/webp": ".webp"}.get(content_type, ".png")
    folder = images_folder()
    os.makedirs(folder, exist_ok=True)
    base = slug(name, 60)
    path = os.path.join(folder, base + ext)
    counter = 1
    while os.path.exists(path):
        counter += 1
        path = os.path.join(folder, f"{base}-{counter}{ext}")
    with open(path, "wb") as handle:
        handle.write(data)
    image = bpy.data.images.load(path, check_existing=False)
    image.name = os.path.basename(path)
    return image


def image_bytes(image):
    """PNG bytes of a Blender image (its file when it has one, else a temporary PNG of its pixels)."""
    if image.filepath and not image.packed_file:
        path = bpy.path.abspath(image.filepath)
        if os.path.isfile(path):
            with open(path, "rb") as handle:
                data = handle.read()
            return data, _content_type(path)
    temp = os.path.join(tempfile.mkdtemp(prefix="zetta3d-"), "image.png")
    copy = image.copy()
    try:
        copy.filepath_raw = temp
        copy.file_format = "PNG"
        copy.save()
    finally:
        bpy.data.images.remove(copy)
    with open(temp, "rb") as handle:
        return handle.read(), "image/png"


def _content_type(path):
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }.get(os.path.splitext(path)[1].lower(), "image/png")


def file_bytes(path):
    with open(bpy.path.abspath(path), "rb") as handle:
        return handle.read(), _content_type(path)


# ─── Sending Blender objects up ───────────────────────────────────────────────────────────────────


def export_glb(objects):
    """Exports the objects (and their children) as one GLB; returns its bytes. Main thread only."""
    path = os.path.join(tempfile.mkdtemp(prefix="zetta3d-"), "selection.glb")
    override = _context_override()
    with bpy.context.temp_override(**override):
        previous = list(bpy.context.selected_objects)
        active = bpy.context.view_layer.objects.active
        for obj in bpy.context.view_layer.objects:
            obj.select_set(False)
        chosen = set()
        for obj in objects:
            chosen.add(obj)
            chosen.update(obj.children_recursive)
        for obj in chosen:
            if obj.name in bpy.context.view_layer.objects:
                obj.select_set(True)
        try:
            bpy.ops.export_scene.gltf(filepath=path, export_format="GLB", use_selection=True, export_apply=True)
        finally:
            for obj in bpy.context.view_layer.objects:
                obj.select_set(obj in previous)
            bpy.context.view_layer.objects.active = active
    with open(path, "rb") as handle:
        return handle.read()


def triangle_count(objects):
    count = 0
    depsgraph = bpy.context.evaluated_depsgraph_get()
    for obj in objects:
        for item in [obj, *obj.children_recursive]:
            if item.type != "MESH":
                continue
            evaluated = item.evaluated_get(depsgraph)
            mesh = evaluated.to_mesh()
            try:
                count += sum(len(p.vertices) - 2 for p in mesh.polygons)
            finally:
                evaluated.to_mesh_clear()
    return count


def upload_objects(objects, title, done, fail, progress=None):
    """Exports objects as GLB, uploads it and makes it a library row. done(generation) on the main thread."""
    try:
        data = export_glb(objects)
    except Exception as error:  # noqa: BLE001
        fail(f"Could not export the selection: {error}")
        return
    if len(data) > 100 * 1024 * 1024:
        fail("That model is larger than 100 MB, the most the library accepts. Retopologize or decimate it first.")
        return

    def work():
        ticket = zetta.upload(data, "model/gltf-binary", progress)
        return zetta.create_upload_post(ticket["key"], len(data), title)

    def failed(error):
        fail(zetta_error_text(error))

    tasks.run(work, done, failed, name="zetta3d-upload-model")
