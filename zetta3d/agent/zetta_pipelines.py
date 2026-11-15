"""The pure half of the agent's pipeline tools: the agent's steps and changes turned into the add-on's recipe graph
(pipelines/graph.py — the builders the Pipelines panel uses, so what the agent saves is priced and runnable as it
stands), the seeds and per-run inputs checked, and the text the model reads. No bpy, no network.
"""

import copy

from ..api import models
from ..pipelines import graph as g
from .tools import ToolError

# The step kinds the agent may use (the protocol's list): never splats or exports, never the seeds.
AGENT_KINDS = ("remove_background", "ai_edit", "draw_views", "isolate_parts", "generate_3d", "generate_smart_3d",
               "retopology", "segmentation", "texture", "smart_uv", "rigging")
MAX_STEPS = 24
MAX_SEEDS = 20
MAX_PROMPT = 2000
MAX_TITLE = 200
TEXTURE_TIERS = {"standard": "retexture", "hd": "retexture_hd", "detailed": "retexture_hd",
                 "8k": "retexture_8k", "extreme": "retexture_8k"}
STATUS_LABELS = {"draft": "Not started", "running": "Running", "awaiting": "Needs you", "paused": "Paused",
                 "failed": "Stopped", "done": "Done", "cancelled": "Cancelled"}


def _int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("#").isdigit():
        return int(value.strip().lstrip("#"))
    return None


def _short(text, limit):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _plural(count, one, many=None):
    return f"1 {one}" if count == 1 else f"{count} {many or one + 's'}"


# ─── Settings: the agent's words → the recipe's ──────────────────────────────────────────────────


def to_web(kind, settings):
    """Agent-shaped settings → the graph's: generate pbr → pbrMaterials, faceCount → polygonCount (a string, typed);
    retopology faceCount → faces; texture quality → tier. Anything else passes through."""
    web = {}
    for key, value in (settings or {}).items():
        value = copy.deepcopy(value)
        if g.is_generate(kind):
            if key == "pbr":
                web["pbrMaterials"] = value
                continue
            if key in ("faceCount", "polygonCount"):
                number = _int(value)
                web["polygonCount"] = "" if value is None else str(number if number is not None else value)
                web.setdefault("polygonCountTyped", True)
                continue
        elif kind == "retopology" and key == "faceCount":
            web["faces"] = value
            continue
        elif kind in ("texture", "texture_multiview") and key == "quality" and isinstance(value, str):
            web["tier"] = TEXTURE_TIERS.get(value.lower(), "retexture")
            continue
        web[key] = value
    return web


def check_kind(kind, where):
    if not isinstance(kind, str) or not kind.strip():
        raise ToolError(f"{where} needs a kind.")
    if g.is_seed(kind):
        raise ToolError(f"{where}: “{kind}” is a seed — build_pipeline adds it from startsFrom.")
    if kind not in AGENT_KINDS:
        if g.known(kind):
            raise ToolError(f"{where}: {g.label(kind)} ({kind}) is not available in Blender — it can be added on "
                            "zetta3d.ai only.")
        raise ToolError(f"{where}: “{kind}” is not a step kind. Use one of: {', '.join(AGENT_KINDS)}.")


def _check_output(output, where):
    if output is None:
        return None
    output = str(output).strip()
    if output not in ("character", "each") and not (output.startswith(g.PART_PREFIX) and len(output) > 5):
        raise ToolError(f"{where}.output must be character, each or part:<key>.")
    return output


def parse_steps(steps):
    """build_pipeline's steps (1–24): [{kind, after (an earlier index), output, settings}]."""
    if not isinstance(steps, list) or not steps:
        raise ToolError("'steps' is required: 1–24 of { kind, after?, output?, settings? }.")
    if len(steps) > MAX_STEPS:
        raise ToolError(f"A pipeline is built from at most {MAX_STEPS} steps ({len(steps)} given).")
    found = []
    for index, step in enumerate(steps):
        where = f"steps[{index}]"
        if not isinstance(step, dict):
            raise ToolError(f"{where} must be an object {{ kind, after?, output?, settings? }}.")
        kind = str(step.get("kind") or "").strip()
        check_kind(kind, f"{where}.kind")
        after = None
        if step.get("after") is not None:
            after = _int(step["after"])
            if after is None or after < 0 or after >= index:
                span = "step 0 has none" if index == 0 else f"0–{index - 1}"
                raise ToolError(f"{where}.after must be the index of an earlier step ({span}); omit it to follow "
                                "the seed.")
        output = _check_output(step.get("output"), where)
        if output is not None and (after is None or found[after]["kind"] != "isolate_parts"):
            raise ToolError(f"{where}.output is only for a step after isolate_parts.")
        settings = step.get("settings")
        if settings is not None and not isinstance(settings, dict):
            raise ToolError(f"{where}.settings must be an object.")
        found.append({"kind": kind, "after": after, "output": output, "settings": settings or {}})
    return found


def build_graph(starts_from, steps, gates=None):
    """(graph, node ids in step order) — a seed, then each step after the one `after` names (the seed when none),
    laid out by depth. Generate steps after Draw views become their multi-view steps, as the server renames them."""
    seed_kind = "seed_model" if starts_from == "model" else "seed_picture"
    graph = {"version": 1, "edges": [], "nodes": [
        {"id": "seed", "kind": seed_kind, "position": {"x": 0, "y": 0}, "settings": g.defaults(seed_kind),
         "request": {}}]}
    ids, depth, rows = [], [], {}
    for index, step in enumerate(steps):
        after = step["after"]
        level = 1 if after is None else depth[after] + 1
        depth.append(level)
        rows[level] = rows.get(level, 0) + 1
        node_id = f"s{index}"
        source = "seed" if after is None else ids[after]
        source_kind = g.node(graph, source)["kind"]
        handle = "out"
        if source_kind == "isolate_parts":
            handle = step["output"] if step["output"] in ("each",) or (step["output"] or "").startswith(
                g.PART_PREFIX) else "character"
        graph["nodes"].append({"id": node_id, "kind": step["kind"],
                               "position": {"x": level * 300, "y": (rows[level] - 1) * 200},
                               "settings": g.with_defaults(step["kind"], to_web(step["kind"], step["settings"])),
                               "request": {}})
        graph["edges"].append({"id": f"e{index}", "source": source, "sourceHandle": handle, "target": node_id,
                               "targetHandle": "in"})
        ids.append(node_id)
    g.with_multi_view_steps(graph)
    return g.with_requests(graph, gates), ids


def parse_changes(changes):
    if not isinstance(changes, list) or not changes:
        raise ToolError("'changes' is required: 1–24 of { op: \"add\", kind, after, output?, settings? } | "
                        "{ op: \"set\", nodeId, settings } | { op: \"remove\", nodeId }.")
    if len(changes) > MAX_STEPS:
        raise ToolError(f"At most {MAX_STEPS} changes per call ({len(changes)} given).")
    found = []
    for index, change in enumerate(changes):
        where = f"changes[{index}]"
        if not isinstance(change, dict):
            raise ToolError(f"{where} must be an object.")
        op = str(change.get("op") or "").strip()
        settings = change.get("settings")
        if settings is not None and not isinstance(settings, dict):
            raise ToolError(f"{where}.settings must be an object.")
        if op == "add":
            check_kind(str(change.get("kind") or "").strip(), where)
            _check_output(change.get("output"), where)
        elif op == "set":
            if not change.get("nodeId"):
                raise ToolError(f"{where} (set) needs a nodeId.")
            if not settings:
                raise ToolError(f"{where} (set) needs settings (an object of what to change).")
        elif op == "remove":
            if not change.get("nodeId"):
                raise ToolError(f"{where} (remove) needs a nodeId.")
        else:
            raise ToolError(f"{where}.op must be add, set or remove (“{op}” given).")
        found.append(change)
    return found


def apply_changes(graph, changes, gates=None):
    """(graph, added [(index, node id)], skipped [(index, reason)]) — each change as the editor's own controls make it.
    A change that cannot be made is skipped, never half-applied."""
    added, skipped = [], []
    for index, change in enumerate(changes):
        op = change["op"]
        try:
            if op == "remove":
                n = g.node(graph, change["nodeId"])
                if n is None:
                    raise ValueError(f"There is no step {change['nodeId']}.")
                if g.is_seed(n.get("kind")):
                    raise ValueError("The seed stays.")
                graph = g.remove_step(graph, n["id"], gates)
            elif op == "set":
                n = g.node(graph, change["nodeId"])
                if n is None:
                    raise ValueError(f"There is no step {change['nodeId']}.")
                if g.is_seed(n.get("kind")):
                    raise ValueError("The seed has no settings.")
                if n.get("kind") not in AGENT_KINDS and not g.in_scope(n):
                    raise ValueError(f"{g.node_name(n)} cannot be changed in Blender — change it on zetta3d.ai.")
                graph = g.patch_settings(graph, n["id"], to_web(n["kind"], change["settings"]), gates)
            else:
                kind = change["kind"].strip()
                after_id = change.get("after")
                parent = g.node(graph, str(after_id)) if after_id is not None else g.seed(graph)
                if parent is None:
                    raise ValueError(f"There is no step {after_id}." if after_id is not None
                                     else "The pipeline has no seed to follow — name the step it follows.")
                output = _check_output(change.get("output"), f"changes[{index}]")
                if output is not None and parent.get("kind") != "isolate_parts":
                    raise ValueError("output is only for a step after isolate_parts.")
                handle = output or ("character" if parent.get("kind") == "isolate_parts" else "out")
                if kind in g.MULTIVIEW_OF and g.output_type(parent, handle) == g.VIEWS:
                    kind = g.MULTIVIEW_OF[kind]
                graph, node_id = g.add_step(graph, kind, parent["id"], output, gates)
                if change.get("settings"):
                    graph = g.patch_settings(graph, node_id, to_web(kind, change["settings"]), gates)
                added.append((index, node_id))
        except ValueError as error:
            skipped.append((index, str(error)))
    return g.with_requests(graph, gates), added, skipped


# ─── Seeds and per-run inputs ────────────────────────────────────────────────────────────────────


def parse_seeds(seeds, seed_kind):
    """[{index, imageId | generationId | assetPath}] — exactly one each, of the type the pipeline starts from."""
    if not isinstance(seeds, list) or not seeds:
        raise ToolError("'seeds' is required: 1–20 of { imageId } | { generationId } | { assetPath }.")
    if len(seeds) > MAX_SEEDS:
        raise ToolError(f"At most {MAX_SEEDS} seeds per call ({len(seeds)} given) — split them over several "
                        "run_pipeline calls.")
    if seed_kind == "seed_views":
        raise ToolError("This pipeline starts from pictures of each side (a Multi-view seed). Those are given per run "
                        "in the Pipelines panel — ask the user to press New run there; run_pipeline cannot give them.")
    wants = "model" if seed_kind == "seed_model" else "picture"
    found = []
    for index, seed in enumerate(seeds):
        where = f"seeds[{index}]"
        if not isinstance(seed, dict):
            raise ToolError(f"{where} must be an object: {{ imageId }} | {{ generationId }} | {{ assetPath }}.")
        given = [key for key in ("imageId", "generationId", "assetPath") if seed.get(key) not in (None, "")]
        if len(given) != 1:
            raise ToolError(f"{where} must name exactly one of imageId, generationId, assetPath "
                            f"({' and '.join(given) or 'none'} given).")
        key = given[0]
        entry = {"index": index}
        if key == "assetPath":
            path = str(seed["assetPath"]).strip()
            ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
            if ext not in ("png", "jpg", "jpeg", "glb"):
                raise ToolError(f"{where}.assetPath must be a .png or .jpg picture or a .glb model (\"{path}\" given).")
            entry.update(assetPath=path, type="model" if ext == "glb" else "picture", label=path)
        else:
            value = _int(seed[key])
            if value is None or value <= 0:
                raise ToolError(f"{where}.{key} must be a positive id.")
            entry[key] = value
            entry["type"] = "picture" if key == "imageId" else "model"
            entry["label"] = f"{entry['type']} #{value}"
        if entry["type"] != wants:
            raise ToolError(f"{where} ({entry['label']}) is a {entry['type']}, but this pipeline starts from a {wants} "
                            + ("— give an imageId or a .png/.jpg assetPath." if wants == "picture"
                               else "— give a generationId or a .glb assetPath."))
        found.append(entry)
    return found


def parse_inputs(inputs):
    """{nodeId: {prompt?, imageId?}} → [{nodeId, prompt, imageId}]."""
    if inputs is None:
        return []
    if not isinstance(inputs, dict):
        raise ToolError("'inputs' is an object keyed by node id: { \"<nodeId>\": { prompt?, imageId? } }.")
    found = []
    for node_id, value in inputs.items():
        if not isinstance(value, dict):
            raise ToolError(f"inputs[\"{node_id}\"] must be {{ prompt?, imageId? }}.")
        prompt = str(value.get("prompt") or "").strip() or None
        if prompt and len(prompt) > MAX_PROMPT:
            raise ToolError(f"inputs[\"{node_id}\"].prompt is longer than {MAX_PROMPT:,} characters.")
        image_id = None
        if value.get("imageId") is not None:
            image_id = _int(value["imageId"])
            if image_id is None or image_id <= 0:
                raise ToolError(f"inputs[\"{node_id}\"].imageId must be a picture id from generate_image.")
        if prompt is None and image_id is None:
            raise ToolError(f"inputs[\"{node_id}\"] gives neither a prompt nor an imageId.")
        found.append({"nodeId": node_id, "prompt": prompt, "imageId": image_id})
    return found


def input_patch(node, given):
    """The settings change a per-run input makes to its step."""
    node_id = given["nodeId"]
    if node is None:
        raise ToolError(f"inputs[\"{node_id}\"]: the pipeline has no step {node_id} — see get_pipeline for node ids.")
    name = f"{node_id} ({g.node_name(node)})"
    if not g.set_on_each_run(node):
        if node.get("kind") == "ai_edit":
            raise ToolError(f"inputs[\"{node_id}\"]: {name} is not set on each run — change its prompt with "
                            "edit_pipeline (or set askAtRun: true).")
        raise ToolError(f"inputs[\"{node_id}\"]: {name} takes no per-run input (only AI texturing and AI edit with "
                        "askAtRun do).")
    if node.get("kind") == "ai_edit":
        if given["imageId"] is not None:
            raise ToolError(f"inputs[\"{node_id}\"]: {name} takes a prompt (the instruction), not an imageId.")
        return {"prompt": given["prompt"]}
    if given["imageId"] is not None and given["prompt"] is not None:
        raise ToolError(f"inputs[\"{node_id}\"]: give {name} a prompt or an imageId, not both — a reference picture "
                        "replaces the description.")
    if given["imageId"] is not None:
        return {"prompt": "", "reference": {"gen2dPostId": given["imageId"], "name": f"Picture #{given['imageId']}"}}
    return {"prompt": given["prompt"], "reference": None}


def run_graph(recipe, gates=None):
    return g.with_requests(g.for_new_run(recipe), gates)


def with_inputs(graph, inputs, gates=None):
    """(graph with the inputs applied, [(node id, settings, request)])."""
    graph = copy.deepcopy(graph)
    patches = []
    for given in inputs:
        node = g.node(graph, given["nodeId"])
        settings, request = g.run_node_change(graph, given["nodeId"], input_patch(node, given), gates)
        node["settings"], node["request"] = settings, request
        patches.append((given["nodeId"], settings, request))
    return graph, patches


def missing_inputs(graph):
    found = []
    for node_id, _message in g.run_input_problems(graph):
        node = g.node(graph, node_id)
        kind = (node or {}).get("kind")
        if kind == "texture":
            what = (f"inputs[\"{node_id}\"].prompt (a texture description) or inputs[\"{node_id}\"].imageId "
                    "(a reference picture)")
        elif kind == "texture_multiview":
            what = ("its pictures of each side — given in the Pipelines panel, or wire Draw views into its views "
                    "input")
        else:
            what = f"inputs[\"{node_id}\"].prompt (how to change the picture)"
        found.append(f"{node_id} ({g.node_name(node) if node else 'step'}): give {what}")
    return found


# ─── Text for the model ──────────────────────────────────────────────────────────────────────────


def settings_brief(node, limit=220):
    """A step's settings in the agent's words (pbr, faceCount), as it would write them back in a set."""
    settings = node.get("settings") if isinstance(node.get("settings"), dict) else {}
    kind = node.get("kind")
    if g.is_seed(kind) or not settings:
        return ""
    parts = []

    def add(key, value):
        if value is None or value == "" or value == [] or value == {}:
            return
        if isinstance(value, bool):
            text = "true" if value else "false"
        elif isinstance(value, list):
            text = "/".join(str(v.get("key") if isinstance(v, dict) else v) for v in value)
        else:
            text = str(value)
        parts.append(f"{key}={'“' + _short(text, 60) + '”' if key in ('prompt', 'hint') else text}")

    if g.is_generate(kind):
        for key in ("model", "quality", "output"):
            add(key, settings.get(key))
        add("pbr", settings.get("pbrMaterials"))
        count = _int(settings.get("polygonCount"))
        if count is not None:
            parts.append(f"faceCount={count}")
        add("polygonType", settings.get("polygonType"))
        add("textureQuality", settings.get("textureQuality"))
        if settings.get("segmentParts") is True:
            parts.append("segmentParts=true")
    else:
        for key, value in settings.items():
            if kind in ("texture", "texture_multiview") and key in ("prompt", "reference", "referenceViews"):
                continue  # given per run
            if key == "model" and value is None:
                continue
            add("parts" if key == "knownParts" else key, value)
    joined = ", ".join(parts)
    return joined if len(joined) <= limit else joined[: limit - 1] + "…"


def step_tree(graph, prices=None, gates=None):
    """The recipe as an indented tree from the seed: id, kind, where its input comes from, the Studio summary, its
    settings, price, and whether it runs per part or takes a per-run input."""
    nodes = graph.get("nodes") or []
    if not nodes:
        return "(no steps)"
    per_part = g.fan_out_nodes(graph)
    lines = []
    for n, depth, parent, via in g.ordered(graph):
        line = "  " * depth + f"- {n['id']} {n.get('kind')}"
        line += f" “{n['label']}”" if n.get("label") else f" ({g.label(n.get('kind'))})"
        if parent is not None:
            line += f" ← {parent['id']}.{via}"
        elif not g.is_seed(n.get("kind")):
            line += " ← NOT CONNECTED"
        summary = g.summary(n) if g.known(n.get("kind")) else ""
        if summary:
            line += f" · {summary}"
        brief = settings_brief(n)
        if brief:
            line += " · {" + brief + "}"
        if g.known(n.get("kind")) and not g.is_seed(n.get("kind")):
            price = g.price_for(graph, n, prices, gates) if prices is not None else None
            line += " · " + ("price unknown" if price is None else g.price_label(price, n["id"] in per_part))
        elif not g.known(n.get("kind")):
            line += " · unknown step (kept as it is)"
        if n["id"] in per_part:
            line += " · runs once per part"
        if g.set_on_each_run(n):
            line += " · input given on each run"
        if g.known(n.get("kind")) and not g.offered(n.get("kind")):
            line += " · web-only (cannot be changed in Blender)"
        lines.append(line)
    return "\n".join(lines)


def estimate_text(graph, prices=None, gates=None):
    if prices is None:
        return "Estimate unknown (prices not loaded)."
    credits, per_part = g.estimate(graph, prices, gates)
    if credits is None:
        return "Estimate unknown (prices not loaded)."
    text = "Free to run (no paid steps)" if credits == 0 else f"{'Up to ' if per_part else ''}{credits:,} credits a run"
    if per_part:
        most = max([int(g.settings_of(n).get("maxParts") or 6) for n in graph.get("nodes") or []
                    if n.get("kind") == "isolate_parts"] or [1])
        text += f" (steps after Isolate parts › each run once per part — counted for up to {most} parts)"
    return text + "."


def warnings(graph):
    found = []
    for n in graph.get("nodes") or []:
        if n.get("kind") == "smart_uv":
            warning = g.smart_uv_warning(graph, n)
            if warning:
                found.append(f"- [{n['id']}] {warning}")
    return found


def problems_text(problems, graph):
    lines = []
    for node_id, message in problems:
        node = g.node(graph, node_id) if node_id else None
        lines.append(f"- [{node_id}{' ' + g.node_name(node) if node else ''}] {message}" if node_id else f"- {message}")
    return "\n".join(lines)


def report(graph, prices=None, gates=None):
    """Problems, warnings, estimate and the step tree — the tail of build / edit / get results."""
    problems = g.problems(graph, gates)
    lines = ["Problems: none — runnable." if not problems
             else "Problems (it cannot run until fixed):\n" + problems_text(problems, graph)]
    found = warnings(graph)
    if found:
        lines.append("Warnings:\n" + "\n".join(found))
    lines.append("Estimate: " + estimate_text(graph, prices, gates))
    lines.append("Steps:\n" + step_tree(graph, prices, gates))
    return "\n".join(lines)


def starts_from_text(graph):
    seed = g.seed(graph)
    kind = (seed or {}).get("kind")
    return {"seed_model": "a model", "seed_views": "pictures of each side", "seed_picture": "a picture"}.get(
        kind, "(no seed)")


def pipeline_line(summary):
    line = f"#{summary.get('id')} “{summary.get('title') or 'Untitled pipeline'}” — {_plural(max(0, int(summary.get('steps') or 0) - 1), 'step')}"
    last = summary.get("lastRun")
    if last:
        line += f" · last run #{last.get('id')} {STATUS_LABELS.get(last.get('status'), last.get('status')).lower()} " \
                f"({last.get('status')}) {_date(last.get('createdAt'))}"
    else:
        line += " · never run"
    counts = [f"{count} {status}" for status, count in (summary.get("runCounts") or {}).items() if count]
    if counts:
        line += " · runs: " + ", ".join(counts)
    return line + f" · edited {_date(summary.get('updatedAt'))}"


def run_line(run):
    line = f"#{run.get('id')} {run.get('status')}{' (paused)' if run.get('paused') and run.get('status') != 'paused' else ''}"
    line += f" · {run.get('mode')}"
    progress = run.get("progress") or {}
    if int(progress.get("total") or 0):
        line += f" · {progress.get('done') or 0}/{progress['total']} steps done"
        if progress.get("waiting"):
            line += f", {progress['waiting']} waiting"
        if progress.get("failed"):
            line += f", {progress['failed']} failed"
    if int(run.get("inputsNeeded") or 0):
        line += f" · {_plural(int(run['inputsNeeded']), 'input')} needed"
    line += f" · {run.get('spentCredits') or 0}/{run.get('estimatedCredits') or 0} cr"
    seed = run.get("seed") or {}
    if seed:
        line += f" · seed: {seed.get('type')}" + (f" “{_short(seed['name'], 40)}”" if seed.get("name") else "")
    return line + f" · {_date(run.get('createdAt'))}"


def _date(value):
    text = str(value or "")
    return text[:16].replace("T", " ") if len(text) >= 16 else text or "—"


def step_price(run, step, prices, gates=None):
    graph = run.get("graph") or {}
    node = g.node(graph, step.get("nodeId"))
    if node is None or prices is None:
        return None
    return g.price_for(graph, node, prices, gates)


def steps_price(run, steps, prices, gates=None):
    total = 0
    for step in steps:
        price = step_price(run, step, prices, gates)
        if price is None:
            return None
        total += price
    return total


def step_name(run, step):
    from ..pipelines import state as pipelines

    node = g.node(run.get("graph") or {}, step.get("nodeId"))
    part = f" [part {pipelines.part_label(run, step['partKey'])}]" if step.get("partKey") else ""
    return f"step {step.get('id')} · {step.get('nodeId')} {g.node_name(node) if node else step.get('nodeId')}{part}"


def _seed_text(seed):
    if not seed:
        return "—"
    kind = seed.get("type") or ("model" if seed.get("gen3dPostId") else "picture")
    ident = seed.get("gen3dPostId") or seed.get("gen2dPostId")
    if seed.get("name"):
        return f"{kind} “{_short(seed['name'], 60)}”" + (f" (#{ident})" if ident else "")
    return f"{kind} #{ident}" if ident else kind


def run_status_text(run, prices=None, gates=None):
    """pipeline_run_status: state, mode, spend, every step (status, credits, error, model, pictures), the reviews
    waiting for a choice, the steps waiting for a Run, and what can be imported."""
    from ..pipelines import state as pipelines

    graph = run.get("graph") or {}
    status = run.get("status")
    lines = [f"Run #{run.get('id')} of pipeline #{run.get('pipelineId')} — {STATUS_LABELS.get(status, status)} "
             f"({status}{', paused' if run.get('paused') and status != 'paused' else ''}) · "
             f"{'Auto' if run.get('mode') == 'auto' else 'Manual'} mode · spent {run.get('spentCredits') or 0} of an "
             f"estimated {run.get('estimatedCredits') or 0} credits · seed: {_seed_text(run.get('seed'))} · created "
             f"{_date(run.get('createdAt'))}" + (f" · finished {_date(run['finishedAt'])}" if run.get("finishedAt")
                                                  else "")]
    order = {n["id"]: index for index, (n, _d, _p, _v) in enumerate(g.ordered(graph))}
    steps = sorted(run.get("steps") or [], key=lambda s: (order.get(s.get("nodeId"), 10 ** 6), s.get("partKey") or "",
                                                          s.get("id") or 0))
    lines.append(("No steps yet — the run has not started." if status == "draft" else "No steps yet.")
                 if not steps else "Steps:")
    picture_lines = []
    for step in steps:
        line = f"- {step_name(run, step)} · {step.get('status')}"
        if int(step.get("attempt") or 1) > 1:
            line += f" (attempt {step['attempt']})"
        if int(step.get("credits") or 0) > 0:
            line += f" · {step['credits']} cr"
        elif step.get("status") == "waiting":
            price = step_price(run, step, prices, gates)
            line += " · price unknown" if price is None else " · free" if price == 0 else f" · will cost {price} cr"
        output = step.get("output") or {}
        gen = step.get("generation")
        gen_id = step.get("gen3dPostId") or output.get("gen3dPost") or (gen or {}).get("id")
        if gen_id:
            line += f" · model #{gen_id}"
            if gen:
                line += f" “{models.display_title(gen)}”" + ("" if models.is_success(gen) else f" ({gen.get('status')})")
        if output.get("type") == "splat" or (gen and models.is_splat_row(gen)):
            line += " · Gaussian splat (not usable in Blender)"
        if output.get("type") == "file":
            line += f" · export file {output.get('format') or ''} (web only)".replace("  ", " ")
        if step.get("errorMessage"):
            line += f" · error: {_short(step['errorMessage'], 300)}"
        lines.append(line)
        kind = output.get("type")
        if kind == "picture" and (output.get("picture") or {}).get("url"):
            picture_lines.append(f"- step {step['id']} ({step['nodeId']}): {output['picture']['url']}")
        elif kind == "views":
            for view in output.get("views") or []:
                if (view.get("picture") or {}).get("url"):
                    picture_lines.append(f"- step {step['id']} ({step['nodeId']}) {view.get('viewType')}: "
                                         f"{view['picture']['url']}")
        elif kind == "isolated":
            if (output.get("character") or {}).get("url"):
                picture_lines.append(f"- step {step['id']} ({step['nodeId']}) character: {output['character']['url']}")
            for part in output.get("parts") or []:
                if (part.get("picture") or {}).get("url"):
                    picture_lines.append(f"- step {step['id']} ({step['nodeId']}) part {part.get('key')} "
                                         f"“{part.get('label')}”: {part['picture']['url']}")
    for step in (s for s in steps if s.get("status") == "review"):
        review = step.get("review") or {}
        found = (review.get("identification") or {}).get("parts") or []
        lines.append(f"Isolate parts review — {step_name(run, step)} waits for parts to be chosen:")
        subject = (review.get("identification") or {}).get("subject")
        if subject:
            lines.append(f"  subject: {subject}")
        if found:
            lines.append("  parts found: " + ", ".join(f"{p.get('key')} “{p.get('label')}”" for p in found))
        lines.append(f"  proposedKeys: [{', '.join(review.get('proposedKeys') or [])}]")
        lines.append(f"  → manage_pipeline_run {{ runId: {run.get('id')}, action: \"choose_parts\", stepId: "
                     f"{step['id']}, keys: [...], extraParts?: [...] }}")
    waiting = [s for s in steps if s.get("status") == "waiting"]
    if waiting and status != "draft":
        total = steps_price(run, waiting, prices, gates)
        lines.append(f"Waiting for a Run: {_plural(len(waiting), 'step')} "
                     f"({'price unknown' if total is None else f'{total} credits'}) — manage_pipeline_run run_step "
                     "(stepId) or run_waiting (all); the user approves the credits.")
    failed = [s for s in steps if s.get("status") == "failed"]
    if failed:
        lines.append("Failed: " + ", ".join(f"step {s['id']}" for s in failed) + " — retry_step (stepId) tries one "
                                                                                 "again.")
    if picture_lines:
        lines.append("Picture outputs (signed URLs, they expire):")
        lines.extend(picture_lines[:40])
        if len(picture_lines) > 40:
            lines.append(f"… +{len(picture_lines) - 40} more")
    importable = pipelines.importable_steps(run)
    if importable:
        names = []
        for step in importable:
            name = f"{step['nodeId']} (model #{(step.get('generation') or {}).get('id')})"
            if name not in names:
                names.append(name)
        lines.append(f"Importable results: {', '.join(names)} — import_pipeline_results.")
    return "\n".join(lines)
