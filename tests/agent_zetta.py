"""The agent's Zetta 3D tools: requests and cards, declines and approvals, imports and their events, pictures and
pipelines. NO NETWORK: the API calls are replaced here and every worker runs at once on the main thread."""

import base64
import os
import sys
import tempfile

import bpy

sys.path.insert(0, os.path.dirname(__file__))
from agent_common import call, fresh, module, run_checks

tools = module("agent.tools")
zt = module("agent.tools.zetta")
zc = module("agent.zetta_common")
zi = module("agent.zetta_images")
zp = module("agent.zetta_pipelines")
events = module("agent.events")
attachments = module("agent.attachments")
client = module("agent.client")
zetta = module("api.zetta")
http = module("core.http")
tasks = module("core.tasks")
auth = module("auth")
context = module("studio.context")
jobs = module("jobs")
importer = module("importer")
pipeline_api = module("pipelines.api")
g = module("pipelines.graph")

# ─── No network, no threads ──────────────────────────────────────────────────────────────────────


def _offline(*_args, **_kw):
    raise AssertionError("a test reached the network")


http.request = _offline
http._raw = _offline
http.put_bytes = _offline


def _run_now(work, done=None, fail=None, token=None, name=""):
    try:
        result = work(token) if token is not None else work()
    except tasks.Cancelled:
        return None
    except Exception as error:  # noqa: BLE001
        if fail is None:
            raise
        fail(error)
        return None
    if done is not None:
        done(result)
    return None


tasks.run = _run_now
tasks.post = lambda callback, *args: callback(*args)
auth.signed_in = lambda: True
context.refresh_credits = lambda: None
jobs._follow = lambda job: None  # a job is finished by hand here (jobs._update + jobs._finish)

PRICES = [
    {"code": "textured", "model": None, "credits": 30},
    {"code": "geometry_only", "model": None, "credits": 20},
    {"code": "lowpoly", "model": None, "credits": 25},
    {"code": "pbr", "model": None, "credits": 10},
    {"code": "detailed_texture", "model": None, "credits": 15},
    {"code": "custom_face_count", "model": None, "credits": 5},
    {"code": "textured", "model": "n25", "credits": 38},
    {"code": "smart_topology", "model": "r1", "credits": 20},
    {"code": "smart_topology", "model": "n1", "credits": 12},
    {"code": "smart_uv", "model": None, "credits": 10},
    {"code": "retexture", "model": None, "credits": 25},
    {"code": "auto_rigging", "model": None, "credits": 15},
    {"code": "ai_segmentation", "model": None, "credits": 8},
]
context.prices = PRICES
context.gates_3d = {"allowsZettaN25": True, "allowsZettaNp1": True}


def _png(size=256, rgba=(1.0, 0.0, 0.0, 1.0)):
    image = bpy.data.images.new("tmp-png", size, size, alpha=True)
    image.pixels[:] = list(rgba) * (size * size)
    path = os.path.join(tempfile.mkdtemp(), "p.png")
    image.filepath_raw = path
    image.file_format = "PNG"
    image.save()
    bpy.data.images.remove(image)
    with open(path, "rb") as handle:
        return handle.read()


PNG = None


def png():
    global PNG
    if PNG is None:
        PNG = _png()
    return PNG


def _glb():
    bpy.ops.mesh.primitive_cube_add(size=1)
    cube = bpy.context.active_object
    path = os.path.join(tempfile.mkdtemp(), "cube.glb")
    bpy.ops.export_scene.gltf(filepath=path, export_format="GLB", use_selection=True)
    bpy.data.objects.remove(cube)
    with open(path, "rb") as handle:
        return handle.read()


def run_tool(name, args, answer=None, session=None):
    """Runs one tool with a card that answers `answer`; returns {"result", "cards"}."""
    box = {"cards": []}

    def card(kind, payload, on_answer):
        box["cards"].append((kind, payload))
        on_answer(answer)

    ctx = tools.Context({"toolUseId": "toolu_test", "name": name, "input": args, "effect": "generate"},
                        lambda result: box.setdefault("result", result), card=card, session=session or {"id": 7})
    tools.run(ctx.call, ctx)
    assert "result" in box, f"{name} did not finish"
    return box


def heard():
    """Collects what events.emit sends from now on."""
    got = []
    events.listen(lambda kind, payload: got.append((kind, payload)))
    return got


def finished_generation(gen_id, title="Helmet", feature="img23d"):
    return {"id": gen_id, "status": "success", "title": title, "selectedFeature": feature, "selectedModel": "n1",
            "result": {"files": [{"key": f"models/{gen_id}/{title.lower()}.glb", "type": "GLB",
                                  "url": f"https://files.test/{gen_id}.glb"}]}}


def serve_downloads(data):
    zetta.download_url = lambda generation_id, key=None: f"https://files.test/{generation_id}"
    http.download = lambda url, progress=None, token=None: data


# ─── Generate ────────────────────────────────────────────────────────────────────────────────────


def test_generate_card_prices_and_decline():
    fresh()
    sent = []
    zetta.create_generation = lambda body, idempotency_key=None: sent.append(body)
    box = run_tool("generate_3d_model", {"prompt": "stylised wooden crate, iron corners"}, answer=False)
    kind, payload = box["cards"][0]
    assert kind == "generate" and payload["button"] == "Generate", payload
    assert payload["credits"] == 40, payload  # textured 30 + pbr 10, the panel's own price list
    assert payload["lines"][0] == "Zetta N1 · High-Res · textured · PBR", payload["lines"]
    assert any(line.startswith("Prompt: stylised") for line in payload["lines"])
    assert box["result"].error and box["result"].text == "The user declined."
    assert not sent, "nothing is sent before the user agrees"


def test_generate_approved_starts_and_tracks():
    fresh()
    sent = []

    def create(body, idempotency_key=None):
        sent.append(body)
        return {"generation": {"id": 501, "status": "pending"}, "quote": {"credits": 48}, "charged": True}

    zetta.create_generation = create
    box = run_tool("generate_3d_model", {"prompt": "a knight's helmet", "title": "Helmet"}, answer=True)
    assert not box["result"].error, box["result"].text
    assert box["result"].text == "Started generation #501 (48 credits). It will be imported into the open file when done."
    body = sent[0]
    assert body["prompt"] == "a knight's helmet" and body["model"] == "n1" and "imageUrl" not in body, body
    assert body["spec"] == {"generation": {"base": "textured", "modifiers": ["pbr"]}}, body
    job = jobs.find(501)
    assert job is not None and job["autoImport"] is False, job
    assert zc.is_agent_job(501)


def test_generate_draws_a_picture_first_for_picture_engines():
    fresh()
    sent, drawn, uploaded = [], [], []
    zetta.generate_image = lambda prompt, *a, **kw: drawn.append((prompt, kw)) or {
        "images": [{"base64": base64.b64encode(png()).decode(), "contentType": "image/png"}]}
    zetta.upload = lambda data, content_type, *a, **kw: uploaded.append(content_type) or {
        "fileUrl": "https://up.test/ref.png", "key": "uploads/ref.png"}
    zetta.create_generation = lambda body, idempotency_key=None: sent.append(body) or {
        "generation": {"id": 502, "status": "pending"}, "quote": {"credits": 48}}
    box = run_tool("generate_3d_model", {"prompt": "brass lantern", "model": "n25"}, answer=True)
    assert not box["result"].error, box["result"].text
    assert "A reference picture is drawn from the prompt first." in box["cards"][0][1]["lines"]
    assert drawn and drawn[0][1].get("for_3d") is True
    assert sent[0]["imageUrl"] == "https://up.test/ref.png" and sent[0]["imageFromPrompt"] is True, sent[0]
    assert sent[0]["model"] == "n25"


def test_generate_respects_plan_gates_and_rules():
    fresh()
    saved = context.gates_3d
    context.gates_3d = {"allowsZettaN25": False}
    try:
        box = run_tool("generate_3d_model", {"prompt": "lamp", "model": "n25"}, answer=True)
        assert box["result"].error and "does not include Zetta N2.5" in box["result"].text, box["result"].text
        assert not box["cards"]
    finally:
        context.gates_3d = saved
    result = call("generate_3d_model", prompt="crate", model="np15", faceCount=90000)
    assert result.error and "between" in result.text, result.text
    result = call("generate_3d_model", model="n1")
    assert result.error and "prompt" in result.text


def test_finished_job_imports_at_cursor_and_emits():
    fresh()
    serve_downloads(_glb())
    got = heard()
    zetta.create_generation = lambda body, idempotency_key=None: {"generation": {"id": 601, "status": "pending"},
                                                                   "quote": {"credits": 40}}
    bpy.context.scene.cursor.location = (2.0, 0.0, 0.0)
    box = run_tool("generate_3d_model", {"prompt": "helmet", "title": "Helmet"}, answer=True)
    assert not box["result"].error, box["result"].text
    gen = finished_generation(601)
    jobs._update(601, gen, None)
    jobs._finish(601)
    assert ("job_finished", {"generationId": 601, "status": "success",
                             "importedPath": 'collection "Zetta 3D/Helmet #601"'}) in got, got
    placed = importer.objects_for(601)
    assert placed, "the model was imported"
    root = next(o for o in placed if o.parent is None)
    assert abs(root.location.x - 2.0) < 1e-3, root.location
    assert not zc.is_agent_job(601)


def test_a_job_the_user_started_emits_nothing():
    fresh()
    got = heard()
    jobs.track({"id": 650, "status": "pending"}, "User's own", auto_import=False)
    jobs._update(650, {"id": 650, "status": "failed"}, None)
    jobs._finish(650)
    assert not [e for e in got if e[1].get("generationId") == 650], got


# ─── The library ─────────────────────────────────────────────────────────────────────────────────


def test_list_generations_names_splats():
    fresh()
    zetta.list_generations = lambda **kw: {"total": 2, "items": [
        finished_generation(7, "Crate"),
        {"id": 8, "status": "success", "title": "Garden", "selectedFeature": "imageToSplat",
         "createdAt": "2026-11-14T10:00:00Z"}]}
    text = call("list_generations").text
    assert "#7 Crate — kind: mesh" in text and "#8 Garden — kind: splat" in text, text
    assert "cannot be used in Blender" in text


def test_import_refuses_splats():
    fresh()
    zetta.get_generation = lambda generation_id: ({"id": generation_id, "status": "success",
                                                    "selectedFeature": "imageToSplat"}, None)
    result = call("import_generation", generationId=9)
    assert result.error and result.text == "Gaussian splats are not supported in Blender here — open it on zetta3d.ai."


def test_import_generation_into_a_collection_under_a_parent():
    fresh()
    serve_downloads(_glb())
    bpy.ops.object.empty_add()
    shelf = bpy.context.active_object
    shelf.name = "Shelf"
    props = bpy.data.collections.new("Props")
    bpy.context.scene.collection.children.link(props)
    zetta.get_generation = lambda generation_id: (finished_generation(generation_id, "Vase"), None)
    result = call("import_generation", generationId=702, place={"parent": "Shelf", "collection": "Props"})
    assert not result.error, result.text
    assert 'into collection "Props"' in result.text and result.label.startswith("Imported Vase"), result.text
    placed = importer.objects_for(702)
    assert placed and all(props in o.users_collection for o in placed)
    assert any(o.parent == shelf for o in placed)
    assert not any(c.name.startswith("Vase #702") for c in bpy.data.collections), "the importer's own collection went"
    result = call("import_generation", generationId=703, place={"parent": "Nope"})
    assert result.error and "There is no object" in result.text


# ─── Pictures ────────────────────────────────────────────────────────────────────────────────────


def test_image_sources_read_blender_images_files_and_attachments():
    fresh()
    image = bpy.data.images.new("Hero", 256, 256, alpha=True)
    image.pixels[:] = [0.0, 1.0, 0.0, 1.0] * (256 * 256)
    source = zi.resolve({"path": "image:Hero"})
    assert source.picture.content_type == "image/png" and source.picture.size() == (256, 256)
    back = os.path.join(tempfile.mkdtemp(), "back.png")
    with open(back, "wb") as handle:
        handle.write(source.picture.data)
    loaded = bpy.data.images.load(back)
    assert loaded.pixels[1] > 0.9 and loaded.pixels[0] < 0.1, list(loaded.pixels[:4])  # the painted pixels, not blank

    folder = tempfile.mkdtemp()
    bpy.ops.wm.save_as_mainfile(filepath=os.path.join(folder, "scene.blend"))
    os.makedirs(os.path.join(folder, "tex"))
    with open(os.path.join(folder, "tex", "a.png"), "wb") as handle:
        handle.write(png())
    assert zi.resolve({"path": "//tex/a.png"}).picture.data == png()
    name = attachments.store("ref.png", png())
    assert zi.resolve({"path": f"attached:{name}"}).picture.size() == (256, 256)
    assert zi.resolve({"imageId": 12}).id == 12

    small = bpy.data.images.new("Tiny", 64, 64)
    for bad, words in [({"path": "image:Tiny"}, "at least 128"), ({"imageId": 3, "path": "//x.png"}, "not both"),
                       ({"path": "//missing.png"}, "no file"), ({}, "needs an imageId")]:
        try:
            zi.resolve(bad)
        except tools.ToolError as error:
            assert words in str(error), (bad, error)
        else:
            raise AssertionError(f"{bad} should fail")
    bpy.data.images.remove(small)


def test_edit_and_compose_send_ids_or_pictures_and_show_the_result():
    fresh()
    made = {"postId": 123, "images": [{"base64": base64.b64encode(png()).decode(), "contentType": "image/png"}]}
    edits, composes = [], []
    zetta.edit_image = lambda prompt, **kw: edits.append((prompt, kw)) or made
    zetta.compose_images = lambda prompt, images, ids=None, *a: composes.append((images, ids)) or made
    result = call("edit_image", image={"imageId": 5}, prompt="make it night")
    assert not result.error, result.text
    assert result.text.startswith("Made image #123 (1:1, 1K), an edit of image #5.") and "save_image" in result.text
    assert result.images and result.images[0][1] == "image/png"
    assert edits[0][1]["source_post_id"] == 5 and "image" not in edits[0][1]

    bpy.data.images.new("Scene", 200, 200)
    client.image = lambda image_id: {"url": f"https://img.test/{image_id}.png", "contentType": "image/png"}
    http.fetch_bytes = lambda url, timeout=30: png()
    zi._made.clear()
    result = call("compose_images", images=[{"imageId": 8}, {"path": "image:Scene"}], prompt="the first in the second")
    assert not result.error, result.text
    images, ids = composes[0]
    assert ids is None and len(images) == 2, composes  # a mix goes up all inline, in order
    result = call("compose_images", images=[{"imageId": 8}], prompt="x")
    assert result.error and "2 to 14" in result.text


def test_save_image_writes_next_to_the_blend_and_replaces():
    fresh()
    folder = tempfile.mkdtemp()
    bpy.ops.wm.save_as_mainfile(filepath=os.path.join(folder, "scene.blend"))
    zi.remember(123, png(), "image/png")
    result = run_tool("save_image", {"imageId": 123, "name": "Hero Albedo"})["result"]  # name clashes with call()
    assert not result.error, result.text
    saved = os.path.join(folder, "zetta3d", "images", "hero-albedo.png")
    assert os.path.isfile(saved), result.text
    image = bpy.data.images["Hero Albedo"]
    assert image["zetta_image_id"] == 123 and "//zetta3d/images/hero-albedo.png" in result.text

    old = bpy.data.images.new("Old", 32, 32)
    material = bpy.data.materials.new("Uses Old")
    material.use_fake_user = True
    result = call("save_image", imageId=123, replace="image:Old")
    assert not result.error, result.text
    assert old.source == "FILE" and old.filepath.endswith(".png") and bpy.data.images.get("Old") is old
    assert old.size[0] == 256, tuple(old.size)

    bpy.ops.wm.read_homefile(use_empty=True)
    result = call("save_image", imageId=123)
    assert result.error and result.text == "save_image needs the file saved first."


# ─── process_model ───────────────────────────────────────────────────────────────────────────────


def test_process_plans_validate_like_the_panels():
    plan = zt.process_plan("retopologize", {})
    assert plan["code"] == "smart_topology" and plan["options"] == {"model": "r1", "polygonType": "triangle",
                                                                    "faceCount": 8000}, plan
    assert zt.process_plan("rig", {"rigType": "quadruped", "rigSpec": "mixamo"})["options"] == {
        "rigType": "quadruped", "rigOutFormat": "fbx"}
    assert zt.process_plan("segment", {"smart": True, "hint": "wheels"})["options"] == {
        "segmentGranularity": "balanced", "prompt": "wheels"}
    for operation, options, words in [("retopologize", {"detail": "low"}, "Zetta N1 option"),
                                      ("retopologize", {"faceCount": 15000, "polygonType": "quad"}, "10,000"),
                                      ("texture", {}, "needs a 'prompt'"), ("rig", {"spin": 1}, "Unknown option"),
                                      ("melt", {}, "must be one of")]:
        try:
            zt.process_plan(operation, options)
        except tools.ToolError as error:
            assert words in str(error), (operation, error)
        else:
            raise AssertionError(f"{operation} {options} should fail")


def test_process_model_card_decline_and_refusals():
    fresh()
    zetta.get_generation = lambda generation_id: (finished_generation(generation_id), None)
    sent = []
    zetta.create_generation = lambda body, idempotency_key=None: sent.append(body)
    box = run_tool("process_model", {"generationId": 501, "operation": "retopologize"}, answer=False)
    payload = box["cards"][0][1]
    assert payload["credits"] == 20 and payload["button"] == "Retopologize", payload
    assert box["result"].text == "The user declined." and not sent

    dense = finished_generation(502)
    dense.update(faceCount=50_000, polygonType="quadrilateral")
    zetta.get_generation = lambda generation_id: (dense, None)
    result = call("process_model", generationId=502, operation="smart_uv")
    assert result.error and "Retopologize it first" in result.text, result.text

    zetta.get_generation = lambda generation_id: (finished_generation(503, feature="retopology"), None)
    result = call("process_model", generationId=503, operation="retopologize")
    assert result.error and "already a retopology result" in result.text, result.text


def test_process_model_approved_sends_the_post_process_body():
    fresh()
    zetta.get_generation = lambda generation_id: (finished_generation(generation_id), None)
    sent = []
    zetta.create_generation = lambda body, idempotency_key=None: sent.append(body) or {
        "generation": {"id": 801, "status": "pending"}, "quote": {"credits": 15}}
    box = run_tool("process_model", {"generationId": 501, "operation": "rig", "options": {"rigType": "avian"}},
                   answer=True)
    assert not box["result"].error, box["result"].text
    assert box["result"].text.startswith("Started auto rig #801 on #501 (15 credits). It will be imported into the "
                                         "open file when done.")
    assert sent[0] == {"spec": {"postProcess": ["auto_rigging"]}, "sourcePostId": 501, "rigType": "avian",
                       "rigOutFormat": "fbx"}, sent[0]
    assert zc.is_agent_job(801)


# ─── Pipelines ───────────────────────────────────────────────────────────────────────────────────


STEPS = [{"kind": "draw_views"}, {"kind": "generate_3d", "after": 0},
         {"kind": "retopology", "after": 1, "settings": {"faceCount": 5000}},
         {"kind": "generate_smart_3d", "settings": {"model": "np15", "faceCount": 30000, "pbr": False}},
         {"kind": "texture", "after": 2, "settings": {"quality": "hd"}}]


def test_pipeline_graph_from_steps():
    graph, ids = zp.build_graph("picture", zp.parse_steps(STEPS))
    assert ids == ["s0", "s1", "s2", "s3", "s4"]
    kinds = {n["id"]: n["kind"] for n in graph["nodes"]}
    assert kinds["seed"] == "seed_picture" and kinds["s1"] == "generate_3d_multiview", kinds  # fed views
    edges = {(e["source"], e["target"]) for e in graph["edges"]}
    assert edges == {("seed", "s0"), ("s0", "s1"), ("s1", "s2"), ("seed", "s3"), ("s2", "s4")}, edges
    smart = g.node(graph, "s3")
    assert smart["settings"]["polygonCount"] == "30000" and smart["settings"]["pbrMaterials"] is False
    assert smart["request"]["model"] == "np15" and smart["request"]["faceCount"] == 30000, smart["request"]
    assert g.node(graph, "s2")["request"]["faceCount"] == 5000
    assert g.node(graph, "s4")["settings"]["tier"] == "retexture_hd"
    assert g.problems(graph) == [], g.problems(graph)
    for steps, words in [([{"kind": "texture", "after": 0}], "earlier step"), ([{"kind": "generate_splat"}],
                                                                                "not available in Blender"),
                         ([{"kind": "seed_model"}], "is a seed"),
                         ([{"kind": "generate_3d"}, {"kind": "retopology", "after": 0, "output": "each"}],
                          "only for a step after isolate_parts")]:
        try:
            zp.parse_steps(steps)
        except tools.ToolError as error:
            assert words in str(error), (steps, error)
        else:
            raise AssertionError(f"{steps} should fail")


def test_build_and_edit_pipeline_tools():
    fresh()
    saved = {}

    def create(title, graph=None):
        saved.update(id=77, title=title, graph=graph)
        return dict(saved)

    def update(pipeline_id, title=None, graph=None):
        saved.update(graph=graph, **({"title": title} if title else {}))
        return dict(saved)

    pipeline_api.create_pipeline = create
    pipeline_api.update_pipeline = update
    pipeline_api.get_pipeline = lambda pipeline_id: dict(saved)
    text = call("build_pipeline", title="Props: game-ready", startsFrom="picture", steps=STEPS[:3], open=False).text
    assert "Created pipeline #77 “Props: game-ready”" in text and "[1] generate_3d → s1" in text, text
    assert "Problems: none — runnable." in text and "Estimate:" in text and "- s2 retopology" in text, text

    result = call("edit_pipeline", pipelineId=77, changes=[
        {"op": "add", "kind": "rigging", "after": "s2", "settings": {"rigType": "quadruped"}},
        {"op": "set", "nodeId": "s2", "settings": {"faceCount": 3000}},
        {"op": "remove", "nodeId": "seed"}, {"op": "remove", "nodeId": "s9"}])
    assert not result.error, result.text
    assert "Added: changes[0] rigging → rigging-" in result.text, result.text
    assert "Skipped changes[2]: The seed stays." in result.text and "Skipped changes[3]: There is no step s9." in result.text
    graph = saved["graph"]
    assert g.node(graph, "s2")["settings"]["faces"] == 3000
    rig = next(n for n in graph["nodes"] if n["kind"] == "rigging")
    assert rig["settings"]["rigType"] == "quadruped" and any(e["source"] == "s2" and e["target"] == rig["id"]
                                                             for e in graph["edges"])


def test_pipeline_reads_and_opening_in_the_panel():
    fresh()
    graph, _ = zp.build_graph("picture", zp.parse_steps([{"kind": "generate_smart_3d"}, {"kind": "rigging", "after": 0}]))
    pipeline_api.list_pipelines = lambda: [{"id": 81, "title": "Rigged", "steps": 3, "updatedAt": "2026-11-14T09:00:00Z",
                                            "lastRun": None, "runCounts": {"done": 2}}]
    pipeline_api.get_pipeline = lambda pipeline_id: {"id": 81, "title": "Rigged", "graph": graph,
                                                     "updatedAt": "2026-11-14T09:00:00Z"}
    pipeline_api.list_runs = lambda pipeline_id: [{"id": 5, "status": "done", "mode": "auto", "spentCredits": 40,
                                                   "estimatedCredits": 40, "progress": {"done": 2, "total": 2}}]
    text = call("list_pipelines").text
    assert "#81 “Rigged” — 2 steps · never run · runs: 2 done" in text, text
    text = call("get_pipeline", pipelineId=81).text
    assert "starts from a picture, 2 steps" in text and "- s1 rigging" in text and "#5 done · auto" in text, text
    pipeline_api.create_pipeline = lambda title, graph=None: {"id": 81, "title": title, "graph": graph}
    text = call("build_pipeline", title="Rigged", startsFrom="picture", steps=[{"kind": "generate_smart_3d"}]).text
    assert "Opened in the Zetta 3D sidebar's Pipelines panel." in text, text
    state = module("pipelines.state")
    assert state.editor["pipeline"]["id"] == 81
    state.close()


def test_list_images():
    zetta.list_images = lambda **kw: {"total": 1, "items": [
        {"id": 44, "feature": "editImg", "aspectRatio": "1:1", "resolution": "1K", "prompt": "night\nsky",
         "createdAt": "2026-11-14T10:00:00Z", "images": [{"url": "https://img.test/44.png"}]}]}
    text = call("list_images", search="sky").text
    assert '#44 · edit · 1:1 1K · 2026-11-14 · "night sky"' in text, text


def test_run_pipeline_card_and_runs():
    fresh()
    graph, _ = zp.build_graph("picture", zp.parse_steps([{"kind": "generate_3d"},
                                                         {"kind": "texture", "after": 0}]))
    pipeline_api.get_pipeline = lambda pipeline_id: {"id": 78, "title": "Textured", "graph": graph}
    box = run_tool("run_pipeline", {"pipelineId": 78, "seeds": [{"imageId": 5}]}, answer=True)
    assert box["result"].error and "s1 (AI texturing): give inputs" in box["result"].text, box["result"].text

    batches, patched, started = [], [], []
    pipeline_api.batch = lambda pid, mode, seeds, run_graph: batches.append((mode, seeds)) or {
        "runs": [{"id": 900}, {"id": 901}]}
    pipeline_api.patch_run_node = lambda run_id, node_id, request=None, settings=None: patched.append(
        (run_id, node_id, request))
    pipeline_api.run_action = lambda run_id, action, body=None: started.append((run_id, action)) or {
        "id": run_id, "pipelineId": 78, "status": "running"}
    args = {"pipelineId": 78, "seeds": [{"imageId": 5}, {"imageId": 6}], "inputs": {"s1": {"prompt": "rusty iron"}}}
    declined = run_tool("run_pipeline", args, answer=False)
    assert declined["result"].text == "The user declined." and not batches
    box = run_tool("run_pipeline", args, answer=True)
    payload = box["cards"][0][1]
    assert payload["button"] == "Run 2", payload
    credits, _ = g.estimate(graph, PRICES)
    assert payload["credits"] == credits * 2, payload
    assert not box["result"].error, box["result"].text
    assert box["result"].text.startswith("Started 2 runs of pipeline #78 “Textured”: #900, #901"), box["result"].text
    assert batches[0] == ("manual", [{"gen2dPostId": 5}, {"gen2dPostId": 6}]), batches
    assert patched[0][1] == "s1" and patched[0][2]["prompt"] == "rusty iron", patched
    assert started == [(900, "start"), (901, "start")]
    assert zc.following_run(900) and zc.following_run(901)
    zc._agent_runs().clear()


def test_finished_run_imports_its_final_models_and_emits():
    fresh()
    serve_downloads(_glb())
    got = heard()
    graph, _ = zp.build_graph("model", zp.parse_steps([{"kind": "retopology"}, {"kind": "smart_uv", "after": 0}]))
    zc.follow_run({"id": 950, "pipelineId": 79, "status": "running"})
    mid, final = finished_generation(951, "Mid", "retopology"), finished_generation(952, "Final", "uvUnwrap")
    run = {"id": 950, "pipelineId": 79, "status": "done", "graph": graph, "steps": [
        {"id": 1, "nodeId": "s0", "status": "success", "output": {"type": "mesh"}, "generation": mid},
        {"id": 2, "nodeId": "s1", "status": "success", "output": {"type": "mesh"}, "generation": final}]}
    zc.run_landed(run)
    assert ("pipeline_run_finished", {"runId": 950, "status": "done",
                                      "importedPaths": ['collection "Zetta 3D/Final #952"']}) in got, got
    assert importer.objects_for(952) and not importer.objects_for(951), "only the final model is imported"
    assert not zc.following_run(950)


def test_manage_and_status():
    fresh()
    graph, _ = zp.build_graph("picture", zp.parse_steps([{"kind": "generate_3d"}]))
    run = {"id": 960, "pipelineId": 80, "status": "awaiting", "mode": "manual", "graph": graph,
           "estimatedCredits": 38, "spentCredits": 0, "seed": {"type": "picture", "name": "knight.png"},
           "steps": [{"id": 31, "nodeId": "s0", "status": "waiting", "credits": 0}]}
    pipeline_api.get_run = lambda run_id: run
    text = call("pipeline_run_status", runId=960).text
    assert "Run #960 of pipeline #80 — Needs you" in text and "step 31 · s0 Generate 3D · waiting · will cost" in text
    acted = []
    pipeline_api.run_step = lambda run_id, step_id: acted.append(step_id) or dict(run, status="running")
    box = run_tool("manage_pipeline_run", {"runId": 960, "action": "run_step", "stepId": 31}, answer=False)
    assert box["result"].text == "The user declined." and not acted and box["cards"][0][1]["button"] == "Run"
    box = run_tool("manage_pipeline_run", {"runId": 960, "action": "run_step", "stepId": 31}, answer=True)
    assert not box["result"].error and acted == [31], box["result"].text
    assert box["result"].text.startswith("Started step 31 of run #960. Followed")
    result = call("manage_pipeline_run", runId=960, action="retry_step", stepId=31)
    assert result.error and "only a failed step can be retried" in result.text
    zc._agent_runs().clear()


run_checks(globals())
