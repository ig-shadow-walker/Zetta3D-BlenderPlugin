"""The Zetta 3D tools: the library, 3D generation and finishing, pictures and pipelines (BlenderAgentProtocol.md,
the same tools as Unity's — UnityAgentProtocol.md › Generate, Pictures v1.8, Pipelines v1.3).

EVERY ONE REUSES THE SIDEBAR'S OWN CODE: requests.build and price_of for a generation, the finishing tools' refusals
and post_process bodies, the importer and its placement, the jobs module, the pipeline graph's builders and rules.
What is here is the agent's side of it: checking the arguments, the card, and the text the model reads.

THREADS. Every network call runs on a worker (core.tasks); the tool returns LATER and finishes on the main thread
(zetta_common.later / settle). A tool that spends credits shows the "generate" card first, with the price the panels
would show, and finishes with "The user declined." when it is declined — nothing is uploaded or drawn before that.

LATE IMPORTS. A job or run the agent starts is imported (and reported to the conversation) by zetta_common when it
finishes; import_generation, import_pipeline_results and save_image are undoable and finish only once the file has
changed, so the dispatcher names their undo step.
"""

import base64
import dataclasses
import os

import bpy

from ... import importer
from ...api import catalog, models, requests, zetta
from ...core import http
from ...pipelines import api as pipeline_api
from ...pipelines import graph as g
from ...studio import context, pictures
from ...studio import tools as finishing
from .. import changes, targets
from .. import zetta_common as zc
from .. import zetta_images as zi
from .. import zetta_pipelines as zp
from . import LATER, Result, ToolError, tool

zc.start()


def _limit(args, high=50, fallback=20):
    value = args.get("limit")
    if value is None:
        return fallback
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolError(f"'limit' is a number from 1 to {high}.")
    return max(1, min(high, int(value)))


def _text(args, key, limit, required=False):
    value = args.get(key)
    value = "" if value is None else str(value).strip()
    if required and not value:
        raise ToolError(f"'{key}' is required.")
    if len(value) > limit:
        raise ToolError(f"'{key}' is longer than {limit:,} characters.")
    return value


# ─── The library ─────────────────────────────────────────────────────────────────────────────────


@tool("list_generations")
def list_generations(args, ctx):
    zc.require_signed_in()
    search = _text(args, "search", 200)
    limit = _limit(args)

    def then(answer):
        items = (answer or {}).get("items") or []
        if not items:
            return "The Zetta 3D library is empty." if not search else f"No models match \"{search}\"."
        total = int((answer or {}).get("total") or len(items))
        here = importer.ids_in_scene()
        lines = [f"{len(items)} of {total} model(s), newest first:"]
        for gen in items:
            model = catalog.model_label(gen.get("selectedModel")) if gen.get("selectedModel") else None
            status = "" if models.is_success(gen) else f" ({gen.get('status')})"
            line = (f"#{gen.get('id')} {models.display_title(gen)} — kind: "
                    f"{'splat' if models.is_splat_row(gen) else 'mesh'}, {catalog.feature_label(gen.get('selectedFeature'))}"
                    f"{', ' + model if model else ''}, {models.status_label(gen)}{status}, {zc.date(gen.get('createdAt'))}")
            if gen.get("id") in here:
                line += " · already in this file"
            lines.append(line)
        lines.append("import_generation brings a Ready mesh into the open file (free); meshes can be processed with "
                     "process_model. Splat rows (Gaussian splats and worlds made on the web) cannot be used in Blender "
                     "— the user can open them on zetta3d.ai.")
        return "\n".join(lines)

    return zc.later(ctx, lambda: zetta.list_generations(page=1, page_size=limit, search=search or None), then)


@tool("list_images")
def list_images(args, ctx):
    zc.require_signed_in()
    search = _text(args, "search", 120)
    limit = _limit(args)
    labels = {"editImg": "edit", "composeImg": "composite"}

    def then(answer):
        items = [p for p in (answer or {}).get("items") or [] if not p.get("errorMessage") and p.get("images")]
        if not items:
            return "The user's 2D library is empty." if not search else f"No pictures match \"{search}\"."
        total = int((answer or {}).get("total") or len(items))
        lines = [f"{total} picture(s) in the user's 2D library, newest first (imageId · how it was made · frame · "
                 "date · prompt):"]
        for post in items:
            lines.append(f"#{post.get('id')} · {labels.get(post.get('feature'), 'generated')} · "
                         f"{post.get('aspectRatio') or '?'} {post.get('resolution') or ''}".rstrip()
                         + f" · {zc.date(post.get('createdAt'))[:10]} · \"{zc.short(post.get('prompt'), 140)}\"")
        if total > len(items):
            lines.append(f"…{total - len(items)} more — search to narrow it.")
        return "\n".join(lines)

    return zc.later(ctx, lambda: zetta.list_images(page=1, page_size=limit, search=search or None), then)


@tool("import_generation", undoable=True)
def import_generation(args, ctx):
    zc.require_signed_in()
    generation_id = zc.positive_id(args, "generationId", " from list_generations")
    place = zc.parse_place(args.get("place"))

    def then(gen):
        if models.is_splat_row(gen):
            raise ToolError(zc.SPLAT_REFUSAL)
        if not models.is_success(gen):
            running = "" if models.is_terminal(gen) else " Wait for it to finish."
            raise ToolError(f"#{generation_id} is {models.status_label(gen).lower()} ({gen.get('status')}); only "
                            f"finished models can be imported.{running}")
        title = models.display_title(gen)

        def done(objects, path):
            text = f"Imported #{generation_id} “{title}” into {path or 'the file'}: {zc.describe_import(objects)}."
            if gen.get("selectedFeature") == "uvUnwrap":
                layout = next((i for i in bpy.data.images if i.get("zetta_id") == generation_id), None)
                if layout is not None:
                    text += f" Its UV layout picture is \"image:{layout.name}\"."
            ctx.finish(Result(text, label=f"Imported {title}"[:60]))

        zc.import_for_agent(gen, place, done, lambda message: ctx.finish(Result(message, error=True)))
        return LATER

    return zc.later(ctx, lambda: zc.fetch_generation(generation_id), then)


@tool("save_image", undoable=True)
def save_image(args, ctx):
    zc.require_signed_in()
    image_id = zc.positive_id(args, "imageId", " — the id a picture tool returned")
    if not bpy.data.filepath:
        raise ToolError("save_image needs the file saved first.")
    folder = _text(args, "folder", 400) or "//zetta3d/images"
    full_folder = targets.file_path(folder, write=True)
    name = _text(args, "name", 63)
    replace = None
    if args.get("replace"):
        wanted = str(args["replace"]).strip()
        block = targets.datablock(wanted if wanted.startswith("image:") else f"image:{wanted}")
        if not isinstance(block, bpy.types.Image):
            raise ToolError(f"{wanted} is not an image.")
        replace = block.name

    def then(found):
        data, _ = found
        content_type = pictures.sniff(data)
        base = importer.slug(name or (os.path.splitext(replace)[0] if replace else f"image-{image_id}"), 60)
        ext = pictures.extension_for(content_type)
        os.makedirs(full_folder, exist_ok=True)
        path, counter = os.path.join(full_folder, base + ext), 1
        while os.path.exists(path):
            counter += 1
            path = os.path.join(full_folder, f"{base}-{counter}{ext}")
        changes.before_file(path)
        with open(path, "wb") as handle:
            handle.write(data)
        relative = targets.relative(path)
        with changes.running():
            if replace:
                image = bpy.data.images.get(replace)
                if image is None:
                    raise ToolError(f"The image {replace} is gone. Save it as a new image instead.")
                if image.packed_file:
                    image.filepath = relative
                    try:
                        image.unpack(method="USE_ORIGINAL")
                    except RuntimeError:
                        pass
                image.source = "FILE"
                image.filepath = relative
                image.reload()
                image["zetta_image_id"] = image_id
                users = image.users
                return Result(f"Replaced the picture of \"image:{image.name}\" with image #{image_id} (now {relative}, "
                              f"{len(data) // 1024} KB) — everything using it ({zc.plural(users, 'user')}) shows the "
                              "new picture.", label=f"Replaced {image.name}"[:60])
            image = bpy.data.images.load(path, check_existing=False)
            image.name = name or os.path.basename(path)
            image["zetta_image_id"] = image_id
        return Result(f"Saved image #{image_id} as {relative} ({len(data) // 1024} KB), loaded as \"image:{image.name}\" "
                      "— ready for a material (create_material textures, or edit_nodes).",
                      label=f"Saved image {image.name}"[:60])

    return zc.later(ctx, lambda: zi.library_bytes(image_id), then)


# ─── Generate ────────────────────────────────────────────────────────────────────────────────────

ENGINES = ("n1", "n25", "np1", "np15")
QUALITIES = ("low-poly", "high-res", "ultra")
PLACEHOLDER = "https://zetta3d.ai/placeholder.png"


def generate_options(args):
    """(GenerateOptions, image id, draw first) for generate_3d_model's arguments — checked, with a stand-in picture
    where the real one comes after approval."""
    prompt = _text(args, "prompt", 1024)
    image_id = None
    if args.get("imageId") is not None:
        image_id = zc.positive_id(args, "imageId", " from generate_image")
    if not prompt and image_id is None:
        raise ToolError("Give a prompt, or an imageId from generate_image.")
    model = zc.pick(args, "model", ENGINES, "n1")
    quality = zc.pick(args, "quality", QUALITIES, "high-res")
    if model in ("np1", "np15"):
        quality = "high-res"  # Smart 3D has one rung: the polygon budget is the faceCount
    face_count = args.get("faceCount")
    if face_count is not None:
        if isinstance(face_count, bool) or not isinstance(face_count, (int, float)) or int(face_count) != face_count:
            raise ToolError("'faceCount' must be a whole number.")
        face_count = int(face_count)
    title = _text(args, "title", 120) or (zc.short(prompt, 60) if prompt else f"Model from image #{image_id}")
    options = requests.GenerateOptions(
        prompt=prompt, model=model, quality=quality, output=zc.pick(args, "output", ("textured", "geometry"),
                                                                     "textured"),
        pbr=zc.flag(args, "pbr", True), face_count=face_count,
        polygon_type=zc.pick(args, "polygonType", ("triangle", "quadrilateral"), "triangle"), title=title)
    draw_first = image_id is None and not catalog.model(model).bare_prompt
    if image_id is not None:
        options.image_url = PLACEHOLDER
        options.image_from_prompt = bool(prompt)
    elif draw_first:
        options.image_url = PLACEHOLDER
        options.image_from_prompt = True
    return options, image_id, draw_first


def plan_problem(options, built, gates):
    """Why the user's plan refuses this generation, or None — the Generate panel's plan rules."""
    gate = (gates or {}).get
    label = catalog.model(options.model).label
    if options.model == "n25" and gate("allowsZettaN25") is False:
        return f"The user's plan does not include {label}. Use model n1, or ask them to upgrade."
    if options.model in ("np1", "np15") and gate("allowsZettaNp1") is False:
        return f"The user's plan does not include {label}. Use model n1, or ask them to upgrade."
    if built.base == "lowpoly" and gate("allowsLowPoly") is False:
        return "The user's plan does not include Low-Poly. Use high-res."
    if (built.body or {}).get("polygonType") == "quadrilateral" and gate("allowsQuadMesh") is False:
        return "The user's plan does not include quad meshes. Use triangle."
    most = gate("maxFaceCount")
    if isinstance(most, int) and most < 1_000_000:
        if "detailed_geometry" in built.modifiers:
            return "Ultra detail is available from Basic. Use high-res, or ask the user to upgrade."
        if (built.body or {}).get("faceCount", 0) > most:
            return f"The user's plan generates up to {most:,} polygons. Lower faceCount, or ask them to upgrade."
    return None


def _first_picture(result, what):
    found = [i for i in (result or {}).get("images") or [] if i.get("base64")]
    if not found:
        text = ((result or {}).get("text") or "").strip()
        raise ToolError(f"{what} could not be drawn" + (f": {zc.short(text, 200)}" if text else "; try again.")
                        + " Nothing was charged for the 3D job.")
    data = base64.b64decode(found[0]["base64"])
    return data, found[0].get("contentType") or pictures.sniff(data)


def _upload(data, content_type):
    return zetta.upload(data, content_type)["fileUrl"]


@tool("generate_3d_model")
def generate_3d_model(args, ctx):
    zc.require_signed_in()
    options, image_id, draw_first = generate_options(args)
    place = zc.parse_place(args.get("place"))
    built = requests.build(options, context.gates_3d)
    if not built.ok:
        raise ToolError(" ".join(built.problems))

    def check():
        gates, prices = zc.plan()
        if image_id is not None:
            zi.library_url(image_id)
        return gates, prices

    def then(found):
        gates, prices = found
        checked = requests.build(options, gates)
        if not checked.ok:
            raise ToolError(" ".join(checked.problems))
        refusal = plan_problem(options, checked, gates)
        if refusal:
            raise ToolError(refusal)
        price = requests.price(prices, checked, options.model)
        model = catalog.model(options.model)
        detail = f"{model.label} · {dict(catalog.QUALITIES).get(options.quality, options.quality)} · {options.output}"
        if options.output == "textured":
            detail += " · PBR" if options.pbr else " · no PBR"
        if options.face_count is not None:
            detail += f" · {options.face_count:,} faces"
        if options.polygon_type == "quadrilateral":
            detail += " · quads"
        lines = [detail,
                 f"Prompt: {zc.short(options.prompt, 300)}" if options.prompt else None,
                 f"From picture #{image_id}" if image_id is not None else None,
                 "A reference picture is drawn from the prompt first." if draw_first else None,
                 f"Imports into the open file when done — {zc.place_text(place)}."]

        def approved():
            def work():
                final = dataclasses.replace(options)
                if image_id is not None:
                    final.image_url = _upload(*zi.library_bytes(image_id))
                elif draw_first:
                    final.image_url = _upload(*_first_picture(zetta.generate_image(options.prompt, for_3d=True),
                                                              "The reference picture"))
                body = requests.build(final, gates)
                if not body.ok:
                    raise ToolError(" ".join(body.problems))
                return zetta.create_generation(body.body)

            def started(created):
                generation = zc.track_job(created, options.title, place, ctx.session)
                credits = ((created or {}).get("quote") or {}).get("credits")
                credits = credits if credits is not None else price
                return (f"Started generation #{generation['id']}{zc.credits_text(credits)}. It will be imported into "
                        "the open file when done.")

            return zc.later(ctx, work, started)

        return zc.confirm(ctx, f"Generate 3D: {options.title}", lines, price, "Generate", approved)

    return zc.later(ctx, check, then)


# ─── process_model ───────────────────────────────────────────────────────────────────────────────

OPERATIONS = ("retopologize", "smart_uv", "texture", "rig", "segment")
_OPERATION_ALIASES = {
    "retopologize": "retopologize", "retopology": "retopologize", "retopo": "retopologize",
    "smart_topology": "retopologize", "remesh": "retopologize",
    "smart_uv": "smart_uv", "uv": "smart_uv", "uvs": "smart_uv", "uv_unwrap": "smart_uv", "unwrap": "smart_uv",
    "texture": "texture", "retexture": "texture", "texturing": "texture", "paint": "texture",
    "rig": "rig", "auto_rig": "rig", "autorig": "rig", "auto_rigging": "rig", "rigging": "rig",
    "segment": "segment", "segmentation": "segment", "ai_segmentation": "segment", "smart_segmentation": "segment",
    "split": "segment",
}
_ALLOWED = {
    "retopologize": ("engine", "model", "polygonType", "faceCount", "detail", "faceLevel", "bake"),
    "smart_uv": ("bake", "exportOrientation", "compressGeometry"),
    "texture": ("prompt", "imageId", "quality", "pbr", "fast", "textureAlignment", "delight"),
    "rig": ("rigType", "rigSpec", "rigOutFormat"),
    "segment": ("smart", "granularity", "hint", "splitByConnectivity"),
}
LABELS = {"retopologize": "Retopology", "smart_uv": "Smart UV", "texture": "AI Texturing", "rig": "Auto Rig",
          "segment": "Segmentation"}
BUTTONS = {"retopologize": "Retopologize", "smart_uv": "Run Smart UV", "texture": "Apply texture", "rig": "Auto Rig",
           "segment": "Segment"}
STUDIO_TOOL = {"retopologize": "retopology", "smart_uv": "smart_uv", "texture": "texture", "rig": "rig",
               "segment": "segment"}
RETOPOLOGY_DEFAULT = 8_000


def _alias(value, table):
    return table.get(value, value)


def process_plan(operation, options):
    """{operation, code, price_model, options (the job's fields), summary [lines], prompt, image_id} — validated the
    way the finishing panels validate, unknown options refused rather than dropped."""
    op = _OPERATION_ALIASES.get(str(operation or "").strip().lower().replace("-", "_").replace(" ", "_"))
    if op is None:
        raise ToolError(f"'operation' must be one of {', '.join(OPERATIONS)}.")
    options = options or {}
    unknown = [key for key in options if key not in _ALLOWED[op]]
    if unknown:
        raise ToolError(f"Unknown option{'s' if len(unknown) > 1 else ''} {', '.join(repr(k) for k in unknown)} for "
                        f"{op}. It takes: {', '.join(_ALLOWED[op])}.")
    plan = {"operation": op, "code": None, "price_model": None, "options": {}, "summary": [], "prompt": "",
            "image_id": None}
    out, summary = plan["options"], plan["summary"]

    def choose(key, allowed, fallback, aliases=None):
        value = options.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            return fallback
        value = str(value).strip().lower()
        value = _alias(value, aliases or {})
        if value not in allowed:
            raise ToolError(f"'{key}' must be one of {', '.join(allowed)}.")
        return value

    polygons = {"quad": "quadrilateral", "quads": "quadrilateral", "tri": "triangle", "tris": "triangle",
                "triangles": "triangle"}
    if op == "retopologize":
        if options.get("engine") and options.get("model") and str(options["engine"]).lower() != str(
                options["model"]).lower():
            raise ToolError("Give the engine once ('engine': r1 or n1).")
        engine = choose("engine" if options.get("engine") else "model", ("r1", "n1"), "r1")
        polygon = choose("polygonType", ("triangle", "quadrilateral"), "triangle", polygons)
        quads = polygon == "quadrilateral"
        level = choose("detail" if options.get("detail") else "faceLevel", ("low", "medium", "high"), None)
        count = options.get("faceCount")
        if count is not None and (isinstance(count, bool) or not isinstance(count, (int, float))
                                  or int(count) != count):
            raise ToolError("'faceCount' must be a whole number.")
        bake = options.get("bake")
        plan.update(code="smart_topology", price_model=engine)
        out["model"], out["polygonType"] = engine, polygon
        if engine == "r1":
            if level is not None:
                raise ToolError("'detail' (low/medium/high) is a Zetta N1 option. Zetta R1 takes 'faceCount' "
                                "(500–20,000) — send one or the other, or set engine \"n1\".")
            most = 10_000 if quads else 20_000
            faces = int(count) if count is not None else RETOPOLOGY_DEFAULT
            if not 500 <= faces <= most:
                raise ToolError(f"'faceCount' must be between 500 and {most:,} for Zetta R1"
                                + (" in quads (quads halve the ceiling)" if quads else "") + "."
                                + (" For a denser mesh use engine \"n1\" with 'detail'." if faces > most else ""))
            out["faceCount"] = faces
            if bake is False:
                out["bake"] = False
            summary.append(f"Zetta R1 · {faces:,} {'quads' if quads else 'triangles'}"
                           + (" (default)" if count is None else ""))
            summary.append("Bare geometry (textures not baked onto the new mesh)" if bake is False
                           else "The original textures are baked onto the rebuilt mesh")
        else:
            if count is not None:
                raise ToolError("'faceCount' is a Zetta R1 option — Zetta N1 takes 'detail' (low, medium or high).")
            if bake is not None:
                raise ToolError("'bake' is a Zetta R1 option — Zetta N1 rebuilds topology without one.")
            out["faceLevel"] = level or "medium"
            summary.append(f"Zetta N1 · {out['faceLevel']} detail · {'quads' if quads else 'triangles'}")
    elif op == "smart_uv":
        orientation = options.get("exportOrientation")
        if orientation:
            orientation = str(orientation).strip().lower()
            orientation = "+" + orientation if orientation in ("x", "y") else orientation
            if orientation not in catalog.ORIENTATIONS:
                raise ToolError(f"'exportOrientation' must be one of {', '.join(catalog.ORIENTATIONS)} (leave it out "
                                "to keep the model's own facing).")
            out["exportOrientation"] = orientation
        plan["code"] = "smart_uv"
        if options.get("bake") is False:
            out["bake"] = False
        if options.get("compressGeometry") is True:
            out["compressGeometry"] = True
        summary.append("A clean new UV layout, bare (textures not re-baked)" if options.get("bake") is False
                       else "A clean new UV layout, with the textures, rig and animation carried across")
        if orientation:
            summary.append(f"Facing {orientation}")
        if out.get("compressGeometry"):
            summary.append("Geometry compression (smaller file)")
    elif op == "texture":
        prompt = str(options.get("prompt") or "").strip()
        if len(prompt) > 1024:
            raise ToolError("'prompt' is longer than 1,024 characters.")
        image_id = options.get("imageId")
        if image_id is not None and (isinstance(image_id, bool) or not isinstance(image_id, int) or image_id <= 0):
            raise ToolError("'imageId' must be the number generate_image returned.")
        if not prompt and image_id is None:
            raise ToolError("texture needs a 'prompt' describing the surface, or an 'imageId' from generate_image (or "
                            "both: the picture, changed as described).")
        quality = choose("quality", ("standard", "hd", "8k"), "standard",
                         {"detailed": "hd", "4k": "hd", "extreme": "8k", "2k": "standard", "normal": "standard"})
        pbr = options.get("pbr") is not False
        fast = options.get("fast")
        if fast is True and quality != "standard":
            raise ToolError("'fast' only applies to standard quality (it is the quicker standard tier).")
        alignment = options.get("textureAlignment")
        if alignment:
            alignment = _alias(str(alignment).strip().lower(),
                               {"image": "original_image", "picture": "original_image", "mesh": "geometry"})
            if alignment not in ("original_image", "geometry"):
                raise ToolError("'textureAlignment' must be original_image (fit to the picture) or geometry (fit to "
                                "the mesh).")
            out["textureAlignment"] = alignment
        out["enablePBR"] = pbr
        if options.get("delight") is not None:
            out["delight"] = bool(options["delight"])
        if fast is True:
            out["textureFast"] = True
        plan.update(code={"hd": "retexture_hd", "8k": "retexture_8k"}.get(quality, "retexture"), prompt=prompt,
                    image_id=image_id)
        line = (f"{'Standard' if quality == 'standard' else 'Detailed' if quality == 'hd' else '8K'} texture (up to "
                f"{ {'hd': '4K', '8k': '8K'}.get(quality, '2K') })")
        line += (", faster" if fast else "") + (" · PBR maps" if pbr else " · colour only")
        if alignment:
            line += " · fit to the mesh" if alignment == "geometry" else " · fit to the picture"
        if options.get("delight") is True:
            line += " · baked-in lighting removed"
        elif options.get("delight") is False:
            line += " · keeps the picture's lighting"
        summary.append(line)
        if image_id is not None:
            summary.append(f"Reference: picture #{image_id}")
        if prompt:
            summary.append(f"Surface: {zc.short(prompt, 300)}")
            summary.append("The picture is changed as described first, then applied." if image_id is not None
                           else "A reference picture is drawn from the description first, then applied.")
    elif op == "rig":
        rig_types = tuple(key for key, _ in catalog.RIG_TYPES)
        rig_type = choose("rigType", rig_types, "biped", {"humanoid": "biped", "human": "biped", "bird": "avian",
                                                          "snake": "serpentine", "fish": "aquatic"})
        specs = tuple(key for key, _, _ in catalog.RIG_SPECS)
        spec = choose("rigSpec", specs, "standard", {"ue5": "unreal", "mannequin": "unreal", "mecanim": "unity"})
        if rig_type != catalog.HUMANOID_RIG_TYPE:
            spec = "standard"  # a preset is a humanoid skeleton
        out_format = choose("rigOutFormat", ("glb", "fbx"), "fbx")
        plan["code"] = "auto_rigging"
        if rig_type != "biped":
            out["rigType"] = rig_type
        if spec != "standard":
            out["rigSpec"] = spec
        if out_format != "glb":
            out["rigOutFormat"] = out_format
        summary.append(f"{dict(catalog.RIG_TYPES)[rig_type]} skeleton · "
                       f"{'standard' if spec == 'standard' else dict((s, l) for s, l, _ in catalog.RIG_SPECS)[spec]} "
                       f"bone naming · {out_format.upper()} file — it comes in as an Armature with the mesh skinned "
                       "to it")
    else:
        smart = options.get("smart") is True
        granularity = choose("granularity", ("simple", "balanced", "detailed"), "balanced",
                             {"few": "simple", "coarse": "simple", "max": "detailed", "fine": "detailed",
                              "many": "detailed", "medium": "balanced"})
        split = options.get("splitByConnectivity")
        hint = str(options.get("hint") or "").strip()
        if smart and split is not None:
            raise ToolError("'splitByConnectivity' applies to plain segmentation, not smart: true.")
        if not smart and hint:
            raise ToolError("'hint' applies to smart segmentation — set smart: true, or leave the hint out.")
        if len(hint) > 1024:
            raise ToolError("'hint' is longer than 1,024 characters.")
        plan["code"] = "smart_segmentation" if smart else "ai_segmentation"
        out["segmentGranularity"] = granularity
        if not smart and split is False:
            out["splitByConnectivity"] = False
        if smart and hint:
            out["prompt"] = hint
        parts = {"simple": "few parts", "detailed": "as many parts as it finds"}.get(granularity,
                                                                                     "a balanced number of parts")
        summary.append(f"{'Smart Segment (names the parts)' if smart else 'Segment'} · {parts}")
        if not smart:
            summary.append("Pieces that are not joined stay together" if split is False
                           else "Pieces that are not joined become separate parts")
        if hint:
            summary.append(f"What it is: {zc.short(hint, 200)}")
    return plan


def source_refusal(op, gen):
    """Why process_model cannot take this row — the finishing panels' rules, naming the row."""
    gen_id = gen.get("id")
    if models.is_splat_row(gen):
        return (f"#{gen_id} is a Gaussian splat, not a mesh — process_model works on meshes only. "
                f"{zc.SPLAT_REFUSAL}")
    reason = finishing.refusal(STUDIO_TOOL[op], gen)
    if reason:
        running = "" if models.is_terminal(gen) or gen.get("status") == "success" else " Wait for it to finish."
        return f"#{gen_id}: {reason}{running}"
    if op == "smart_uv":
        triangles = finishing.source_triangles(gen)
        if triangles is not None and triangles > catalog.SMART_UV_MAX_TRIANGLES:
            return (f"#{gen_id} has {triangles:,} triangles; Smart UV takes at most "
                    f"{catalog.SMART_UV_MAX_TRIANGLES:,}. Retopologize it first (operation retopologize), then run "
                    "smart_uv on the result.")
    return None


def _source_picture_url(gen):
    return next((i["url"] for i in gen.get("inputImages") or [] if i.get("url")), None)


@tool("process_model")
def process_model(args, ctx):
    zc.require_signed_in()
    generation_id = zc.positive_id(args, "generationId", " from list_generations")
    options = args.get("options")
    if options is not None and not isinstance(options, dict):
        raise ToolError("'options' must be an object.")
    if options is None:  # the model flattened them
        options = {k: v for k, v in args.items() if k not in ("generationId", "operation", "options") and v is not None}
    plan = process_plan(args.get("operation"), options)
    op, label = plan["operation"], LABELS[plan["operation"]]

    def check():
        source = zc.fetch_generation(generation_id)
        gates, prices = zc.plan()
        if plan["image_id"] is not None:
            zi.library_url(plan["image_id"])
        return source, gates, prices

    def then(found):
        source, gates, prices = found
        refusal = source_refusal(op, source)
        if refusal:
            raise ToolError(refusal)
        if (gates or {}).get("allowsPostProcess") is False:
            raise ToolError(f"{label} is a finishing tool, which needs a paid plan. Ask the user to upgrade at "
                            "zetta3d.ai/pricing.")
        if plan["options"].get("polygonType") == "quadrilateral" and (gates or {}).get("allowsQuadMesh") is False:
            raise ToolError("The user's plan does not include quad meshes. Use polygonType triangle.")
        price = requests.price_of(prices, plan["code"], plan["price_model"])
        title = models.display_title(source)
        lines = [f"{label} on #{source['id']} “{title}” ({catalog.feature_label(source.get('selectedFeature'))})",
                 *plan["summary"]]
        source_picture = _source_picture_url(source)
        if op == "texture" and plan["image_id"] is None and plan["prompt"] and source_picture:
            lines.append("The description is applied to the model's own input picture.")
        lines.append("Imports into the open file when done — at the 3D cursor, in its own collection under "
                     "\"Zetta 3D\".")

        def approved():
            def work():
                body = requests.post_process(plan["code"], source["id"], plan["options"])
                if op == "texture":
                    data = content_type = None
                    if plan["image_id"] is not None:
                        data, content_type = zi.library_bytes(plan["image_id"])
                    if plan["prompt"]:
                        if data is None and source_picture:
                            try:
                                data = zetta.http.fetch_bytes(source_picture, timeout=60)
                                content_type = pictures.sniff(data)
                            except Exception:  # noqa: BLE001 — an expired input picture: draw from the words
                                data = None
                        drawn = (zetta.edit_image(plan["prompt"], image=zetta.inline_image(data, content_type),
                                                  for_3d=True, pipeline_step=True) if data is not None
                                 else zetta.generate_image(plan["prompt"], for_3d=True))
                        data, content_type = _first_picture(drawn, "The reference picture")
                    body["imageUrl"] = _upload(data, content_type)
                return zetta.create_generation(body)

            def started(created):
                generation = zc.track_job(created, f"{label} · {title}"[:60], {}, ctx.session)
                credits = ((created or {}).get("quote") or {}).get("credits")
                credits = credits if credits is not None else price
                extra = {"segment": " The parts import as separate objects under one empty.",
                         "rig": " It comes in as an Armature with the mesh skinned to it.",
                         "smart_uv": " The UV layout picture comes with it when the job makes one."}.get(op, "")
                return (f"Started {label.lower()} #{generation['id']} on #{source['id']}{zc.credits_text(credits)}. "
                        f"It will be imported into the open file when done.{extra}")

            return zc.later(ctx, work, started)

        return zc.confirm(ctx, f"{label}: {title}", lines, price, BUTTONS[op], approved)

    return zc.later(ctx, check, then)


# ─── Pictures ────────────────────────────────────────────────────────────────────────────────────


def _frame(args):
    aspect = str(args.get("aspectRatio") or "1:1").strip()
    if aspect not in catalog.ASPECT_RATIOS:
        raise ToolError(f"'aspectRatio' must be one of {', '.join(catalog.ASPECT_RATIOS)}.")
    resolution = str(args.get("resolution") or "1K").strip().upper()
    if resolution not in ("1K", "2K"):
        raise ToolError("'resolution' must be 1K or 2K.")
    return aspect, resolution


def _sources(args, key, low, high):
    found = args.get(key)
    if not isinstance(found, list):
        raise ToolError(f"'{key}' is required: a list of {{ imageId }} or {{ path }}.")
    if not low <= len(found) <= high:
        raise ToolError(f"'{key}' takes {low} to {high} pictures; got {len(found)}.")
    return [zi.resolve(source) for source in found]


def _made(result, aspect, resolution, what):
    images = [i for i in (result or {}).get("images") or [] if i.get("base64")]
    if not images:
        text = ((result or {}).get("text") or "").strip()
        raise ToolError(text or "No picture came back. Try describing it differently.")
    post_id = (result or {}).get("postId")
    if post_id is None:
        raise ToolError("The picture was made but could not be saved to the library, so it cannot be used. Try again.")
    data = base64.b64decode(images[0]["base64"])
    content_type = images[0].get("contentType") or pictures.sniff(data)
    zi.remember(int(post_id), data, content_type)
    zc.refresh_credits()
    return Result(f"Made image #{post_id} ({aspect}, {resolution}), {what}. It is in the user's 2D library and shown "
                  f"to them. Use imageId {post_id} with save_image to put it in the file (replace: \"image:Name\" swaps "
                  "an existing image's picture), with edit_image or compose_images to change it further, or with "
                  "generate_3d_model or process_model.", images=[(data, content_type)])


def _editing_locked():
    if context.gate2("allowsImageEditing") is False:
        raise ToolError("Editing and composing pictures are on paid plans — say so, and offer generate_image instead.")


@tool("generate_image_with_references")
def generate_image_with_references(args, ctx):
    zc.require_signed_in()
    prompt = _text(args, "prompt", 5000, required=True)
    aspect, resolution = _frame(args)
    sources = _sources(args, "references", 1, 3)

    def work():
        references = [zi.inline(source) for source in sources]
        return zetta.generate_image(prompt, aspect, resolution, None, references)

    what = f"in the look of {zc.plural(len(sources), 'reference')}"
    return zc.later(ctx, work, lambda result: _made(result, aspect, resolution, what))


@tool("edit_image")
def edit_image(args, ctx):
    zc.require_signed_in()
    _editing_locked()
    prompt = _text(args, "prompt", 5000, required=True)
    aspect, resolution = _frame(args)
    if not isinstance(args.get("image"), dict):
        raise ToolError("'image' is required: { imageId } or { path }.")
    source = zi.resolve(args["image"])

    def work():
        if source.id is not None:
            return zetta.edit_image(prompt, source_post_id=source.id, aspect_ratio=aspect, resolution=resolution)
        return zetta.edit_image(prompt, image=source.picture.inline(), aspect_ratio=aspect, resolution=resolution)

    return zc.later(ctx, work, lambda result: _made(result, aspect, resolution, f"an edit of {source.label}"))


@tool("compose_images")
def compose_images(args, ctx):
    zc.require_signed_in()
    _editing_locked()
    prompt = _text(args, "prompt", 5000, required=True)
    aspect, resolution = _frame(args)
    sources = _sources(args, "images", 2, 14)

    def work():
        # The server puts library pictures before uploaded ones, so a mix goes up all inline: the order stays true.
        if all(source.id is not None for source in sources):
            return zetta.compose_images(prompt, [], [s.id for s in sources], aspect, resolution)
        return zetta.compose_images(prompt, [zi.inline(s) for s in sources], None, aspect, resolution)

    what = "composed from " + ", ".join(source.label for source in sources)
    return zc.later(ctx, work, lambda result: _made(result, aspect, resolution, what))


# ─── Pipelines ───────────────────────────────────────────────────────────────────────────────────


def _get_pipeline(pipeline_id):
    try:
        return pipeline_api.get_pipeline(pipeline_id)
    except http.ApiError as error:
        if error.status == 404:
            raise ToolError(f"Pipeline #{pipeline_id} was not found. Use list_pipelines for the ids.") from None
        raise


def _get_run(run_id):
    try:
        return pipeline_api.get_run(run_id)
    except http.ApiError as error:
        if error.status == 404:
            raise ToolError(f"Run #{run_id} was not found. pipeline_run_status takes the run ids run_pipeline "
                            "returned.") from None
        raise


def _recipe(pipeline):
    return g.normalize((pipeline or {}).get("graph"))[0]


def _title(args, required):
    title = _text(args, "title", zp.MAX_TITLE)
    if required and not title:
        raise ToolError("'title' is required (≤200 characters).")
    return title


def _show_pipeline(pipeline_id):
    """Opens (or reloads) the pipeline in the sidebar's Pipelines panel; False when it could not."""
    from ...pipelines import state as pipelines

    try:
        if pipelines.editor.get("pipeline") and pipelines.editor["pipeline"].get("id") == pipeline_id:
            pipelines.editor["dirty"] = False  # the agent's save wins over the panel's copy
        pipelines.open_pipeline(pipeline_id)
        pipelines.load_list()
        return True
    except Exception as error:  # noqa: BLE001 — the panel's trouble; the save stands
        print(f"[Zetta 3D] The Pipelines panel could not open #{pipeline_id}: {error}")
        return False


@tool("list_pipelines")
def list_pipelines(args, ctx):
    zc.require_signed_in()

    def then(found):
        found = found or []
        if not found:
            return "The user has no 3D pipelines yet. build_pipeline makes one."
        lines = [f"{zc.plural(len(found), 'pipeline')}, newest first:"]
        lines.extend(zp.pipeline_line(p) for p in found)
        lines.append("get_pipeline shows a pipeline's steps; run_pipeline runs it on seeds.")
        return "\n".join(lines)

    return zc.later(ctx, pipeline_api.list_pipelines, then)


@tool("get_pipeline")
def get_pipeline(args, ctx):
    zc.require_signed_in()
    pipeline_id = zc.positive_id(args, "pipelineId", " from list_pipelines")

    def work():
        pipeline = _get_pipeline(pipeline_id)
        gates, prices = zc.plan()
        try:
            runs = pipeline_api.list_runs(pipeline_id)
        except http.ApiError:
            runs = None
        return pipeline, gates, prices, runs

    def then(found):
        pipeline, gates, prices, runs = found
        graph = _recipe(pipeline)
        steps = sum(1 for n in graph["nodes"] if not g.is_seed(n.get("kind")))
        lines = [f"Pipeline #{pipeline.get('id')} “{pipeline.get('title')}” — starts from {zp.starts_from_text(graph)}, "
                 f"{zc.plural(steps, 'step')}, edited {zc.date(pipeline.get('updatedAt'))}.",
                 zp.report(graph, prices, gates)]
        per_run = [n["id"] for n in graph["nodes"] if g.set_on_each_run(n)]
        if per_run:
            lines.append(f"Per-run inputs (run_pipeline inputs): {', '.join(per_run)}.")
        if runs is None:
            lines.append("Recent runs: could not be read.")
        elif not runs:
            lines.append("Recent runs: none.")
        else:
            lines.append(f"Recent runs ({min(5, len(runs))} of {len(runs)}):")
            lines.extend("- " + zp.run_line(run) for run in runs[:5])
        return "\n".join(lines)

    return zc.later(ctx, work, then)


@tool("build_pipeline")
def build_pipeline(args, ctx):
    zc.require_signed_in()
    title = _title(args, required=True)
    starts_from = zc.pick(args, "startsFrom", ("picture", "model"), None)
    if starts_from is None:
        raise ToolError("'startsFrom' must be picture or model.")
    steps = zp.parse_steps(args.get("steps"))
    show = zc.flag(args, "open", True)

    def work():
        gates, prices = zc.plan()
        graph, ids = zp.build_graph(starts_from, steps, gates)
        return gates, prices, graph, ids, pipeline_api.create_pipeline(title, graph)

    def then(found):
        gates, prices, graph, ids, saved = found
        saved = saved or {}
        if (saved.get("graph") or {}).get("nodes"):
            graph = _recipe(saved)
        opened = _show_pipeline(saved["id"]) if show else False
        lines = [f"Created pipeline #{saved['id']} “{saved.get('title') or title}” (starts from a {starts_from})."
                 + (" Opened in the Zetta 3D sidebar's Pipelines panel." if opened
                    else " (The Pipelines panel could not open it.)" if show else ""),
                 "Node ids by step: " + ", ".join(f"[{i}] {steps[i]['kind']} → {node_id}"
                                                  for i, node_id in enumerate(ids)),
                 zp.report(graph, prices, gates)]
        return "\n".join(lines)

    return zc.later(ctx, work, then)


@tool("edit_pipeline")
def edit_pipeline(args, ctx):
    zc.require_signed_in()
    pipeline_id = zc.positive_id(args, "pipelineId", " from list_pipelines")
    title = _title(args, required=False) if "title" in args else None
    if title is not None and not title:
        raise ToolError("'title' may not be empty.")
    raw = args.get("changes")
    changes_given = zp.parse_changes(raw) if not (title and not raw) else []

    def work():
        pipeline = _get_pipeline(pipeline_id)
        gates, prices = zc.plan()
        graph, added, skipped = zp.apply_changes(_recipe(pipeline), changes_given, gates)
        if changes_given and len(skipped) == len(changes_given) and title is None:
            raise ToolError("Nothing was changed: " + "; ".join(f"changes[{i}]: {why}" for i, why in skipped))
        saved = pipeline_api.update_pipeline(pipeline_id, title, graph)
        return pipeline, gates, prices, graph, added, skipped, saved

    def then(found):
        pipeline, gates, prices, graph, added, skipped, saved = found
        saved = saved or {}
        if (saved.get("graph") or {}).get("nodes"):
            graph = _recipe(saved)
        from ...pipelines import state as pipelines

        if pipelines.editor.get("pipeline") and pipelines.editor["pipeline"].get("id") == pipeline_id:
            _show_pipeline(pipeline_id)  # the panel shows the agent's version, not its own older copy
        lines = [f"Saved pipeline #{pipeline_id} “{saved.get('title') or title or pipeline.get('title')}”."
                 + (" Renamed." if title else "")]
        if added:
            lines.append("Added: " + ", ".join(f"changes[{i}] {changes_given[i]['kind']} → {node_id}"
                                               for i, node_id in added))
        lines.extend(f"Skipped changes[{i}]: {why}" for i, why in skipped)
        lines.append(zp.report(graph, prices, gates))
        return "\n".join(lines)

    return zc.later(ctx, work, then)


@tool("run_pipeline")
def run_pipeline(args, ctx):
    zc.require_signed_in()
    pipeline_id = zc.positive_id(args, "pipelineId", " from list_pipelines")
    mode = zc.pick(args, "mode", ("manual", "auto"), "manual")
    inputs = zp.parse_inputs(args.get("inputs"))
    raw_seeds = args.get("seeds")

    def check():
        pipeline = _get_pipeline(pipeline_id)
        gates, prices = zc.plan()
        return pipeline, gates, prices

    def then(found):
        pipeline, gates, prices = found
        recipe = _recipe(pipeline)
        seed_node = g.seed(recipe)
        if seed_node is None:
            raise ToolError(f"Pipeline #{pipeline_id} has no seed step — rebuild it with build_pipeline.")
        seeds = zp.parse_seeds(raw_seeds, seed_node["kind"])
        problems = g.problems(recipe, gates)
        if problems:
            raise ToolError(f"Pipeline #{pipeline_id} cannot run yet — fix it with edit_pipeline: "
                            + " ".join(f"[{n}] {m}" if n else m for n, m in problems))
        graph = zp.run_graph(recipe, gates)
        applied, patches = zp.with_inputs(graph, inputs, gates)
        missing = zp.missing_inputs(applied)
        if missing:
            raise ToolError("Every run needs these per-run inputs: " + "; ".join(missing) + ".")
        # Files are read now (the main thread); nothing goes up until the user agrees.
        files = {}
        for seed in seeds:
            if "assetPath" in seed:
                full = targets.file_path(seed["assetPath"], must_exist=True)
                if seed["type"] == "picture":
                    picture = pictures.downscaled(pictures.from_file(full))
                    reason = picture.problem()
                    if reason:
                        raise ToolError(f"seeds[{seed['index']}] ({seed['assetPath']}): {reason}")
                    files[seed["index"]] = picture
                else:
                    with open(full, "rb") as handle:
                        files[seed["index"]] = handle.read()
        credits, per_part = g.estimate(recipe, prices, gates) if prices is not None else (None, False)
        total = credits * len(seeds) if credits is not None else None
        lines = [f"Pipeline #{pipeline_id} “{pipeline.get('title')}” — starts from {zp.starts_from_text(recipe)}"]
        fan = g.fan_out_nodes(recipe)
        for n, _depth, _parent, _via in g.ordered(recipe):
            if g.is_seed(n.get("kind")):
                continue
            price = g.price_for(recipe, n, prices, gates) if prices is not None else None
            summary = g.summary(n)
            lines.append(f"• {g.node_name(n)}{' — ' + summary if summary else ''} "
                         f"({g.price_label(price, n['id'] in fan)})")
        shown = [s["label"] for s in seeds[:6]]
        lines.append(f"Seeds ({len(seeds)}): {', '.join(shown)}" + (f", +{len(seeds) - 6} more" if len(seeds) > 6
                                                                    else ""))
        for given in inputs:
            node = g.node(recipe, given["nodeId"])
            what = (f"reference picture #{given['imageId']}" if given["imageId"] is not None
                    else f"“{zc.short(given['prompt'], 120)}”")
            lines.append(f"Per-run input — {g.node_name(node) if node else given['nodeId']}: {what}")
        lines.append("Mode: Auto — every step submits itself, to the end." if mode == "auto"
                     else "Mode: Manual — waits for you before each step (credits are spent only when you run one).")
        if credits is None:
            lines.append("Cost: unknown (prices not loaded).")
        else:
            up = "up to " if per_part or mode != "auto" else ""
            lines.append(f"Cost: {up}{credits:,} credits." if len(seeds) == 1
                         else f"Cost: {up}{credits:,} credits a run × {len(seeds)} = {up}{total:,} credits.")
        lines.append("Finished models import into the open file — at the 3D cursor, each in its own collection.")

        def approved():
            def work():
                ready = []
                for seed in seeds:
                    if "imageId" in seed:
                        ready.append({"gen2dPostId": seed["imageId"]})
                    elif "generationId" in seed:
                        gen = zc.fetch_generation(seed["generationId"])
                        if models.is_splat_row(gen):
                            raise ToolError(f"seeds[{seed['index']}]: #{gen['id']} is a Gaussian splat — pipelines "
                                            "here start from a mesh.")
                        if not models.is_success(gen):
                            raise ToolError(f"seeds[{seed['index']}]: #{gen['id']} is "
                                            f"{models.status_label(gen).lower()} — only finished models can seed a run.")
                        ready.append({"gen3dPostId": int(gen["id"])})
                    elif seed["type"] == "picture":
                        picture = files[seed["index"]]
                        key = zetta.upload(picture.data, picture.content_type)["key"]
                        ready.append({"uploadKey": key, "name": os.path.basename(seed["assetPath"])})
                    else:
                        data = files[seed["index"]]
                        ticket = zetta.upload(data, "model/gltf-binary")
                        row = zetta.create_upload_post(ticket["key"], len(data),
                                                       os.path.splitext(os.path.basename(seed["assetPath"]))[0])
                        ready.append({"gen3dPostId": int(row["id"])})
                created = (pipeline_api.batch(pipeline_id, mode, ready, graph) or {}).get("runs") or []
                started, not_started = [], []
                for run in created:
                    try:
                        for node_id, settings, request in patches:
                            pipeline_api.patch_run_node(run["id"], node_id, request, settings)
                        live = pipeline_api.run_action(run["id"], "start")
                        started.append(live if isinstance(live, dict) and live.get("id") else
                                       {"id": run["id"], "pipelineId": pipeline_id, "status": "running"})
                    except Exception as error:  # noqa: BLE001 — the others still run
                        not_started.append(f"#{run['id']}: {zc.explain(error)}")
                if not started and not_started:
                    raise ToolError("The runs were created as drafts but none started: " + "; ".join(not_started))
                return started, not_started

            def done(found_runs):
                started, not_started = found_runs
                for run in started:
                    zc.follow_run(run, {}, ctx.session)
                text = (f"Started {zc.plural(len(started), 'run')} of pipeline #{pipeline_id} “{pipeline.get('title')}”: "
                        + ", ".join(f"#{run['id']}" for run in started)
                        + f" — {'Auto' if mode == 'auto' else 'Manual'} mode, "
                        + zp.estimate_text(recipe, prices, gates).rstrip(".").lower() + " each"
                        + (f" (up to {total:,} credits in all)" if total else "") + ".")
                if not_started:
                    text += "\nNot started (left as drafts): " + "; ".join(not_started)
                text += ("\nManual: each paid step waits for the user — pipeline_run_status lists them; "
                         "manage_pipeline_run run_step / run_waiting runs them (with a card). " if mode == "manual"
                         else "\nAuto: every step submits itself. ")
                text += ("Finished models import into the open file. A pipeline_run_finished event arrives when a run "
                         "finishes or needs the user.")
                return text

            return zc.later(ctx, work, done)

        button = "Run" if len(seeds) == 1 else f"Run {len(seeds)}"
        return zc.confirm(ctx, f"Run pipeline: {pipeline.get('title')} on {zc.plural(len(seeds), 'seed')}", lines,
                          total, button, approved)

    return zc.later(ctx, check, then)


@tool("pipeline_run_status")
def pipeline_run_status(args, ctx):
    zc.require_signed_in()
    run_id = zc.positive_id(args, "runId", " from run_pipeline or pipeline_run_status")

    def work():
        run = _get_run(run_id)
        gates, prices = zc.plan()
        return run, gates, prices

    return zc.later(ctx, work, lambda found: zp.run_status_text(found[0], found[2], found[1]))


MANAGE_ACTIONS = ("pause", "resume", "cancel", "set_mode", "run_waiting", "run_step", "retry_step", "choose_parts")


def _part_list(args, key):
    value = args.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 16:
        raise ToolError(f"'{key}' is a list of at most 16 names.")
    found = [str(v).strip() for v in value if str(v).strip()]
    for item in found:
        if len(item) > 80:
            raise ToolError(f"'{key}' entries are at most 80 characters.")
    return found


@tool("manage_pipeline_run")
def manage_pipeline_run(args, ctx):
    zc.require_signed_in()
    run_id = zc.positive_id(args, "runId", " from run_pipeline or pipeline_run_status")
    action = zc.pick(args, "action", MANAGE_ACTIONS, None)
    if action is None:
        raise ToolError(f"'action' must be one of {', '.join(MANAGE_ACTIONS)}.")
    step_id = None
    if args.get("stepId") is not None:
        step_id = zc.positive_id(args, "stepId")
    if step_id is None and action in ("run_step", "retry_step", "choose_parts"):
        raise ToolError(f"'{action}' needs a stepId (the numeric step id pipeline_run_status lists).")
    mode = None
    if action == "set_mode":
        mode = zc.pick(args, "mode", ("manual", "auto"), None)
        if mode is None:
            raise ToolError("set_mode needs 'mode': manual or auto.")
    keys = extra = []
    if action == "choose_parts":
        keys, extra = _part_list(args, "keys"), _part_list(args, "extraParts")
        if not keys and not extra:
            raise ToolError("choose_parts needs keys (from the review's parts) and/or extraParts.")

    def check():
        run = _get_run(run_id)
        gates, prices = zc.plan()
        return run, gates, prices

    def then(found):
        run, gates, prices = found
        steps = run.get("steps") or []
        step = None
        if step_id is not None:
            step = next((s for s in steps if s.get("id") == step_id), None)
            if step is None:
                raise ToolError(f"Run #{run_id} has no step {step_id}. pipeline_run_status lists its steps.")
        live = run.get("status") in ("running", "awaiting", "paused")

        def price_lines(chosen):
            out = []
            for s in chosen:
                price = zp.step_price(run, s, prices, gates)
                out.append(f"• {zp.step_name(run, s)} — "
                           f"{'price unknown' if price is None else 'free' if price == 0 else f'{price} credits'}")
            return out

        def act(call):
            def work():
                after = call()
                if not (isinstance(after, dict) and after.get("id")):
                    after = _get_run(run_id)
                return after

            def done(after):
                if after.get("status") in ("running", "awaiting", "paused"):
                    zc.follow_run(after, None, ctx.session)
                verb = {"pause": "Paused", "resume": "Resumed", "cancel": "Cancelled",
                        "set_mode": f"Switched to {'Auto' if mode == 'auto' else 'Manual'} mode:",
                        "run_waiting": "Released the waiting steps of", "run_step": f"Started step {step_id} of",
                        "retry_step": f"Retrying step {step_id} of"}.get(action, f"Chose the parts for step {step_id} of")
                follow = (" Followed — a pipeline_run_finished event arrives when it finishes or needs the user."
                          if after.get("status") in ("running", "awaiting", "paused") else "")
                return f"{verb} run #{run_id}.{follow}\n" + zp.run_status_text(after, prices, gates)

            return zc.later(ctx, work, done)

        if action == "pause":
            return act(lambda: pipeline_api.run_action(run_id, "pause"))
        if action == "resume":
            return act(lambda: pipeline_api.run_action(run_id, "resume"))
        if action == "cancel":
            if not live and run.get("status") != "draft":
                raise ToolError(f"Run #{run_id} is already over ({run.get('status')}).")
            lines = [f"Stops run #{run_id} of pipeline #{run.get('pipelineId')} "
                     f"({zp.STATUS_LABELS.get(run.get('status'), run.get('status'))}, {run.get('spentCredits') or 0} "
                     "credits spent).",
                     "Steps not started are cancelled; a queued 3D job is cancelled and refunded. Finished models "
                     "stay in the library."]
            return zc.confirm(ctx, f"Cancel run #{run_id}", lines, None, "Cancel run",
                              lambda: act(lambda: pipeline_api.run_action(run_id, "cancel")))
        if action == "set_mode":
            switch = lambda: act(lambda: pipeline_api.run_action(run_id, "mode", {"mode": mode}))  # noqa: E731
            if mode == "auto" and run.get("mode") != "auto" and (live or run.get("status") == "draft"):
                remaining = max(0, int(run.get("estimatedCredits") or 0) - int(run.get("spentCredits") or 0))
                waiting = [s for s in steps if s.get("status") == "waiting"]
                lines = ["Auto submits every step by itself, to the end" + (" — starting now with:" if waiting else "."),
                         *price_lines(waiting),
                         f"Up to {remaining:,} more credits (the run's estimate less what it has spent)."]
                return zc.confirm(ctx, f"Switch run #{run_id} to Auto", lines, remaining, "Switch to Auto", switch)
            return switch()
        if action == "run_waiting":
            waiting = [s for s in steps if s.get("status") == "waiting"]
            if not waiting:
                raise ToolError(f"Run #{run_id} has no steps waiting for a Run "
                                f"({zp.STATUS_LABELS.get(run.get('status'), run.get('status'))}).")
            credits = zp.steps_price(run, waiting, prices, gates)
            return zc.confirm(ctx, f"Run {zc.plural(len(waiting), 'waiting step')} of run #{run_id}",
                              price_lines(waiting), credits, "Run",
                              lambda: act(lambda: pipeline_api.run_action(run_id, "run-waiting")))
        node = g.node(run.get("graph") or {}, step.get("nodeId"))
        name = g.node_name(node) if node else step.get("nodeId")
        if action == "run_step":
            if step.get("status") != "waiting":
                hint = (" (use retry_step)" if step.get("status") == "failed"
                        else " (use choose_parts)" if step.get("status") == "review" else "")
                raise ToolError(f"{zp.step_name(run, step)} is {step.get('status')}, not waiting — only a waiting step "
                                f"can be run{hint}.")
            return zc.confirm(ctx, f"Run step: {name} (run #{run_id})", price_lines([step]),
                              zp.step_price(run, step, prices, gates), "Run",
                              lambda: act(lambda: pipeline_api.run_step(run_id, step_id)))
        if action == "retry_step":
            if step.get("status") != "failed":
                raise ToolError(f"{zp.step_name(run, step)} is {step.get('status')} — only a failed step can be "
                                "retried.")
            lines = price_lines([step])
            if step.get("errorMessage"):
                lines.append(f"Last error: {zc.short(step['errorMessage'], 200)}")
            return zc.confirm(ctx, f"Retry step: {name} (run #{run_id})", lines,
                              zp.step_price(run, step, prices, gates), "Retry",
                              lambda: act(lambda: pipeline_api.retry_step(run_id, step_id)))
        # choose_parts
        if step.get("status") != "review":
            raise ToolError(f"{zp.step_name(run, step)} is {step.get('status')} — choose_parts is for an Isolate parts "
                            "step waiting for review.")
        known = [p.get("key") for p in (((step.get("review") or {}).get("identification") or {}).get("parts") or [])
                 if p.get("key")]
        unknown = [k for k in keys if known and k not in known]
        if unknown:
            raise ToolError(f"Unknown part key(s) {', '.join(unknown)} — the review found: {', '.join(known)}. Name new "
                            "parts in extraParts.")
        return act(lambda: pipeline_api.choose_parts(run_id, step_id, keys, extra))

    return zc.later(ctx, check, then)


@tool("import_pipeline_results", undoable=True)
def import_pipeline_results(args, ctx):
    from ...pipelines import state as pipelines

    zc.require_signed_in()
    run_id = zc.positive_id(args, "runId", " from run_pipeline or pipeline_run_status")
    node_ids = args.get("nodeIds")
    if node_ids is not None:
        if not isinstance(node_ids, list) or len(node_ids) > 50:
            raise ToolError("'nodeIds' is a list of at most 50 node ids.")
        node_ids = list(dict.fromkeys(str(n).strip() for n in node_ids if str(n).strip())) or None
    place = zc.parse_place(args.get("place"))

    def then(run):
        graph = run.get("graph") or {}
        if node_ids:
            unknown = [n for n in node_ids if g.node(graph, n) is None]
            if unknown:
                raise ToolError(f"Run #{run_id} has no node(s) {', '.join(unknown)}. Its nodes: "
                                + ", ".join(n["id"] for n in graph.get("nodes") or []) + ".")
        importable = [s for s in pipelines.importable_steps(run, final_only=node_ids is None)
                      if node_ids is None or s.get("nodeId") in node_ids]
        notes = []
        for step in run.get("steps") or []:
            if step in importable or (node_ids and step.get("nodeId") not in node_ids):
                continue
            node = g.node(graph, step.get("nodeId"))
            name = zp.step_name(run, step)
            output = (step.get("output") or {}).get("type")
            gen = step.get("generation")
            if output == "splat" or (gen and models.is_splat_row(gen)):
                notes.append(f"{name}: a Gaussian splat — not supported in Blender here (open it on zetta3d.ai)")
            elif output == "file":
                notes.append(f"{name}: an export file — open it on zetta3d.ai")
            elif output == "mesh" and node_ids:
                notes.append(f"{name}: its model is not ready ({(gen or {}).get('status') or step.get('status')})")
            elif output is None and node_ids and node is not None and g.is_3d(node.get("kind")):
                notes.append(f"{name}: {step.get('status')} — not finished yet")
            elif node_ids and node is not None and not g.is_seed(node.get("kind")) and output != "mesh":
                notes.append(f"{name}: a picture step — nothing to import (pipeline_run_status lists its picture)")
        if not importable:
            live = run.get("status") in ("running", "awaiting", "paused")
            raise ToolError(f"Nothing to import from run #{run_id} ({zp.STATUS_LABELS.get(run.get('status'))})"
                            + (": " + "; ".join(notes) if notes else " — no model step has finished yet." if live
                               else " — it made no models."))
        chosen, seen = [], set()
        for step in importable:
            gen_id = (step.get("generation") or {}).get("id")
            if gen_id not in seen:
                seen.add(gen_id)
                chosen.append(step)
        paths, failures = [], []

        def next_one(index):
            if index >= len(chosen):
                if not paths:
                    ctx.finish(Result("The import failed: " + "; ".join(failures), error=True))
                    return
                text = f"Imported from run #{run_id} into {', '.join(paths)} — {zc.place_text(place)}."
                if failures:
                    text += "\nFailed: " + "; ".join(failures)
                if notes:
                    text += "\nSkipped: " + "; ".join(notes)
                ctx.finish(Result(text, label=f"Imported run #{run_id}"))
                return
            step = chosen[index]

            def done(objects, path):
                paths.append(f"{path or 'the file'} ({step['nodeId']}: {zc.describe_import(objects)})")
                next_one(index + 1)

            def fail(message):
                failures.append(f"{step['nodeId']}: {message}")
                next_one(index + 1)

            zc.import_for_agent(step["generation"], place, done, fail)

        next_one(0)
        return LATER

    return zc.later(ctx, lambda: _get_run(run_id), then)


def register():
    zc.register()


def unregister():
    zc.unregister()
