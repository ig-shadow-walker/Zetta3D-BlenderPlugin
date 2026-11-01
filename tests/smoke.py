"""Headless checks for the add-on, run inside Blender with the extension installed (see tests/run.sh).

Covers the request builders against the web's worked examples, the pipeline rules, picture checks, the importer
(export a cube as GLB, import it back, grounded at the cursor), the capture renderer, the HTTP client against the
live API (no sign-in needed), the operators, and every panel's draw code against a strict fake layout that checks
each property and operator argument against Blender's RNA.
"""

import importlib
import json
import os
import sys
import tempfile
import traceback

import bpy

PACKAGE = next(name for name in bpy.context.preferences.addons.keys() if name.endswith("zetta3d"))
z = importlib.import_module(PACKAGE)
graph = importlib.import_module(PACKAGE + ".pipelines.graph")
requests = importlib.import_module(PACKAGE + ".api.requests")
catalog = importlib.import_module(PACKAGE + ".api.catalog")
pictures = importlib.import_module(PACKAGE + ".studio.pictures")
importer = importlib.import_module(PACKAGE + ".importer")
capture = importlib.import_module(PACKAGE + ".studio.capture")
http = importlib.import_module(PACKAGE + ".core.http")
panels = importlib.import_module(PACKAGE + ".ui.panels")
prefs_ui = importlib.import_module(PACKAGE + ".ui.preferences")
auth = importlib.import_module(PACKAGE + ".auth")
studio = importlib.import_module(PACKAGE + ".studio.context")
generate = importlib.import_module(PACKAGE + ".studio.generate")
tools = importlib.import_module(PACKAGE + ".studio.tools")
library = importlib.import_module(PACKAGE + ".library")
pipes = importlib.import_module(PACKAGE + ".pipelines.state")
jobs = importlib.import_module(PACKAGE + ".jobs")
images = importlib.import_module(PACKAGE + ".studio.images")
providers = importlib.import_module(PACKAGE + ".providers")
props = importlib.import_module(PACKAGE + ".props")

failures = []


def check(name, fn):
    try:
        fn()
        print(f"  ok   {name}")
    except Exception:  # noqa: BLE001
        failures.append(name)
        print(f"  FAIL {name}")
        traceback.print_exc()


# ─── Requests ─────────────────────────────────────────────────────────────────────────────────────


def test_pipeline_generate_defaults():
    req, problems = graph.request_for("generate_3d", graph.defaults("generate_3d"))
    assert not problems, problems
    expected = {"spec": {"generation": {"base": "textured", "modifiers": ["pbr", "custom_face_count"]}},
                "faceCount": 200000, "polygonType": "triangle", "textureAlignment": "geometry",
                "advanceControl": True, "model": "n25", "removeBackground": True}
    assert req == expected, json.dumps(req)
    req, _ = graph.request_for("generate_smart_3d", graph.defaults("generate_smart_3d"))
    expected = {"spec": {"generation": {"base": "textured", "modifiers": ["pbr"]}}, "faceCount": 20000,
                "textureAlignment": "geometry", "advanceControl": True, "model": "np1", "removeBackground": True}
    assert req == expected, json.dumps(req)
    req, _ = graph.request_for("generate_3d_multiview", graph.defaults("generate_3d_multiview"))
    assert req["spec"]["generation"]["modifiers"] == ["multi_view", "pbr", "custom_face_count"], req


def test_studio_build():
    o = requests.GenerateOptions(prompt="a helmet", model="n1", face_count=200000)
    built = requests.build(o)
    assert built.ok, built.problems
    assert built.body == {"spec": {"generation": {"base": "textured", "modifiers": ["pbr"]}}, "prompt": "a helmet",
                          "faceCount": 200000, "model": "n1"}, built.body  # N1 above low-poly: no polygonType
    low = requests.build(requests.GenerateOptions(prompt="a crate", model="n1", quality="low-poly"))
    assert low.body["polygonType"] == "triangle" and low.base == "lowpoly", low.body
    o = requests.GenerateOptions(prompt="x", image_url="u", model="n1")
    assert "Use either a prompt or an image, not both." in requests.build(o).problems
    body = requests.post_process("smart_topology", 42, {"model": "r1", "faceCount": 8000, "bake": None})
    assert body == {"spec": {"postProcess": ["smart_topology"]}, "sourcePostId": 42, "model": "r1",
                    "faceCount": 8000}, body


def test_apply_patch():
    form = graph.apply_patch(graph.generate_defaults(), {"model": "n25", "quality": "ultra"})
    assert form["polygonCount"] == "2000000", form["polygonCount"]
    form = graph.apply_patch(form, {"polygonType": "quadrilateral"})
    assert form["polygonCount"] == "150000", form["polygonCount"]
    form = graph.apply_patch(graph.generate_defaults(), {"polygonCount": "9000"})
    assert form["quality"] == "low-poly", form


def test_pipeline_rules():
    g = graph.empty()
    g, seed = graph.add_step(g, "seed_picture")
    g, gen = graph.add_step(g, "generate_3d", after=seed)
    g, retopo = graph.add_step(g, "retopology", after=gen)
    assert not graph.problems(g), graph.problems(g)
    assert graph.connection_problem(g, seed, "out", retopo) == \
        "Retopology takes a model here, not a picture.", graph.connection_problem(g, seed, "out", retopo)
    try:
        graph.add_step(g, "seed_model")
        raise AssertionError("a second seed was accepted")
    except ValueError as error:
        assert str(error) == "A pipeline starts from one seed."
    g2, tex = graph.add_step(g, "texture", after=retopo)
    needs = graph.run_input_problems(graph.with_requests(g2))
    assert needs and needs[0][0] == tex, needs
    credits, per_part = graph.estimate(g2, [{"code": "textured", "model": None, "credits": 10},
                                            {"code": "pbr", "model": None, "credits": 5},
                                            {"code": "custom_face_count", "model": None, "credits": 1},
                                            {"code": "smart_topology", "model": "r1", "credits": 7},
                                            {"code": "retexture", "model": None, "credits": 3}])
    assert credits == 26 and not per_part, credits
    normalized, rebuild = graph.normalize({"version": 1, "nodes": [
        {"id": "s", "kind": "seed_views", "settings": {}}, {"id": "g", "kind": "generate_3d", "settings": {}}],
        "edges": [{"source": "s", "target": "g"}]})
    assert graph.node(normalized, "g")["kind"] == "generate_3d_multiview" and rebuild


def test_picture_checks():
    path = os.path.join(tempfile.mkdtemp(), "p.png")
    image = bpy.data.images.new("probe", 256, 200)
    image.filepath_raw = path
    image.file_format = "PNG"
    image.save()
    picture = pictures.from_file(path)
    assert picture.size() == (256, 200), picture.size()
    assert picture.problem() is None, picture.problem()
    small = bpy.data.images.new("tiny", 64, 64)
    small_picture = pictures.from_image(small)
    assert "at least 128 px" in small_picture.problem(), small_picture.problem()
    big = bpy.data.images.new("big", 2048, 1024)
    big_picture = pictures.from_image(big)
    scaled = pictures.downscaled(big_picture)
    assert scaled.size() is None or max(scaled.size()) <= 1536, scaled.size()


# ─── Import, export, capture ──────────────────────────────────────────────────────────────────────


def test_import_roundtrip():
    bpy.ops.mesh.primitive_cube_add(size=2, location=(0, 0, 5))
    cube = bpy.context.active_object
    data = importer.export_glb([cube])
    assert data[:4] == b"glTF", data[:4]
    path = os.path.join(tempfile.mkdtemp(), "cube.glb")
    with open(path, "wb") as handle:
        handle.write(data)
    bpy.context.scene.cursor.location = (3, 4, 0)
    gen = {"id": 777, "title": "Test cube", "selectedFeature": "img23d", "status": "success"}
    objects = importer.import_files([path], gen, place=True)
    assert objects and all(o.get(importer.ID_PROP) == 777 for o in objects)
    assert 777 in importer.ids_in_scene()
    mesh = next(o for o in objects if o.type == "MESH")
    bpy.context.view_layer.update()
    from mathutils import Vector

    zs = [(mesh.matrix_world @ Vector(c)).z for c in mesh.bound_box]
    xs = [(mesh.matrix_world @ Vector(c)).x for c in mesh.bound_box]
    assert abs(min(zs)) < 1e-4, min(zs)
    assert abs((min(xs) + max(xs)) / 2 - 3) < 1e-4
    assert bpy.data.collections.get("Zetta 3D") is not None
    assert importer.triangle_count([cube]) == 12
    files = importer.pick_files({"selectedFeature": "segmentation", "result": {"files": [
        {"key": "a/model-2.glb"}, {"key": "a/model-1.glb"}, {"key": "a/thumb.png", "folder": "thumbnail"}]}})
    assert [f["key"] for f in files] == ["a/model-1.glb", "a/model-2.glb"], files


def test_capture():
    cube = next(o for o in bpy.data.objects if o.type == "MESH")
    shots = capture.shots([cube], yaws=(0, 90))
    assert len(shots) == 2 and shots[0][:2] == b"\xff\xd8", [s[:4] for s in shots]


# ─── Network (live API, no sign-in) ───────────────────────────────────────────────────────────────


def test_http_errors():
    try:
        http.get("/3d/pricing")
        raise AssertionError("pricing answered without a sign-in")
    except http.ApiError as error:
        assert error.status == 401 and error.code == "NO_SESSION", (error.status, error.code, error.message)


# ─── Operators and UI ─────────────────────────────────────────────────────────────────────────────


ICONS = {i.identifier for i in bpy.types.UILayout.bl_rna.functions["label"].parameters["icon"].enum_items}
bad_icons = set()


def check_icon(kwargs):
    icon = kwargs.get("icon")
    if icon is not None and icon not in ICONS:
        bad_icons.add(icon)


class FakeLayout:
    """Accepts any layout call, checking props, icons and operator properties against RNA."""

    def __init__(self):
        self.calls = 0

    def __getattr__(self, name):
        if name in ("enabled", "alert", "scale_y", "scale_x", "active", "use_property_split"):
            return True

        def call(*args, **kwargs):
            self.calls += 1
            check_icon(kwargs)
            return FakeLayout()

        return call

    def __setattr__(self, key, value):
        object.__setattr__(self, key, value)

    def prop(self, data, name, **kwargs):
        check_icon(kwargs)
        assert name in data.bl_rna.properties, f"{type(data).__name__} has no property {name}"
        return None

    def prop_enum(self, data, name, value, **kwargs):
        items = [i.identifier for i in data.bl_rna.properties[name].enum_items]
        if items:  # dynamic enums list no items in RNA
            assert value in items, f"{name} has no item {value} ({items})"
        return None

    def operator(self, idname, **kwargs):
        check_icon(kwargs)
        module, name = idname.split(".")
        rna = getattr(getattr(bpy.ops, module), name).get_rna_type()
        return FakeOp(rna)

    def template_icon(self, icon_value=0, scale=1.0):
        assert isinstance(icon_value, int)


class FakeOp:
    def __init__(self, rna):
        object.__setattr__(self, "_rna", rna)

    def __setattr__(self, key, value):
        assert key in self._rna.properties, f"{self._rna.identifier} has no property {key}"


def draw_all(label):
    zetta = bpy.context.window_manager.zetta
    fake = FakeLayout()
    panel = type("P", (), {"layout": fake})()
    panels.ZETTA_PT_main.draw(panel, bpy.context)
    for tool in [t[0] for t in props.TOOLS]:
        zetta.tool = tool
        panels.ZETTA_PT_studio.draw(panel, bpy.context)
    for mode in ("multi", "single"):
        zetta.gen.source_mode = mode
        panels.ZETTA_PT_studio.draw(panel, bpy.context)
    for mode in ("generate", "edit", "compose"):
        zetta.img_mode = mode
        zetta.tool = "images"
        panels.ZETTA_PT_studio.draw(panel, bpy.context)
    zetta.tex_mode = "multi"
    zetta.tool = "texture"
    panels.ZETTA_PT_studio.draw(panel, bpy.context)
    panels.ZETTA_PT_jobs.draw(panel, bpy.context)
    for kind in ("models", "images"):
        zetta.lib_kind = kind
        panels.ZETTA_PT_library.draw(panel, bpy.context)
    zetta.lib_selecting = True
    zetta.lib_kind = "models"
    panels.ZETTA_PT_library.draw(panel, bpy.context)
    zetta.lib_selecting = False
    panels.ZETTA_PT_pipelines.draw(panel, bpy.context)
    for which, arg in (("generation", "101"), ("image_post", "5"), ("pipeline", "9"), ("run", "33")):
        panels.draw_menu(fake, which, arg)
    print(f"       {label}: {fake.calls} layout calls")


def fake_account():
    auth.user = {"id": 1, "email": "test@example.com", "displayName": "Test", "paymentPlan": "pro",
                 "credits": {"threeD": {"monthly": 500, "purchased": 0, "bonusMonthly": 0, "bonus": 0}}}
    auth.current = auth.SIGNED_IN
    studio.gates_3d = {"allowsZettaN25": True, "allowsZettaNp1": True, "maxMultiViewImages": 4,
                       "allowsCustomFaceCount": True, "maxFaceCount": 2000000, "allowsPostProcess": True,
                       "allowsQuadMesh": True, "allowsPrivateGenerations": True, "allowsLowPoly": True}
    studio.gates_2d = {"allowsImageEditing": True, "allows4kImages": False}
    studio.prices = [{"code": "textured", "model": None, "credits": 20}, {"code": "pbr", "model": None, "credits": 5}]
    studio.image_models = {"models": [{"id": "m1", "label": "Model 1", "resolutions": ["1K", "2K"]},
                                      {"id": "m2", "label": "Pro", "locked": True, "lockedReason": "Paid"}],
                           "default": "m1"}
    studio._loaded_for = studio._account_key()
    gen = {"id": 101, "status": "success", "selectedFeature": "img23d", "selectedModel": "n25", "title": "Helmet",
           "faceCount": 20000, "polygonType": "triangle", "inputImages": [], "thumbnailURL": None,
           "result": {"files": [{"key": "x/model.glb", "type": "GLB"}, {"key": "x/uv.png", "label": "UV layout"}]},
           "jobSpec": {"generation": {"base": "textured", "modifiers": ["pbr"]}}}
    run_gen = dict(gen, id=102, pipelineRunId=8, runStep={"position": 2, "label": "Retopology"})
    library.page.update(items=[gen, {"id": 103, "runGroup": {"runId": 8, "pipelineId": 9, "pipelineTitle": "P",
                                                             "count": 1, "members": [run_gen]}}],
                        total=2, loaded=True)
    library.open_run["group"] = library.page["items"][1]["runGroup"]
    library.images.update(items=[{"id": 5, "prompt": "a cat", "images": [{"url": "https://x/cat.png"}]}], total=1,
                          loaded=True)
    images.recent.update(items=[], loaded=True)
    tools.sources["texture"] = gen
    tools.sources["rig"] = gen
    tools.sources["smart_uv"] = gen
    jobs.all_jobs()[:] = [{"id": 101, "label": "Helmet", "status": "success", "finished": True, "latest": gen},
                          {"id": 104, "label": "Queued one", "status": "queued", "queuePosition": 3}]
    providers._providers = [{"key": "openai", "label": "OpenAI", "keyUrl": "https://example.com", "models": []},
                            {"key": "glm", "label": "GLM", "models": []}]


def fake_pipeline():
    g = graph.empty()
    g, seed = graph.add_step(g, "seed_picture")
    g, iso = graph.add_step(g, "isolate_parts", after=seed)
    g, gen = graph.add_step(g, "generate_3d", after=iso, output="each")
    for kind in ("retopology", "segmentation", "texture", "smart_uv", "rigging"):
        g, gen = graph.add_step(g, kind, after=gen)
    g, _ = graph.add_step(g, "ai_edit", after=seed)
    g, dv = graph.add_step(g, "draw_views", after=seed)
    g, mv = graph.add_step(g, "generate_smart_3d_multiview", after=dv)
    g, _ = graph.add_step(g, "texture_multiview", after=mv)
    pipes.editor.update(pipeline={"id": 9, "title": "Test"}, graph=g, selected=None)
    run_graph = graph.with_requests(graph.for_new_run(g))
    steps = [{"id": i + 1, "nodeId": n["id"], "status": s, "partKey": "", "output": None, "generation": None}
             for i, (n, s) in enumerate(zip(run_graph["nodes"], ["success", "review", "waiting", "failed", "running",
                                                                 "success", "success", "success", "success", "success",
                                                                 "success", "success"]))]
    steps[1]["review"] = {"identification": {"subject": "knight", "parts": [{"key": "sword", "label": "Sword",
                                                                             "kind": "held", "level": "low"}]},
                          "proposedKeys": ["sword"]}
    steps[5]["output"] = {"type": "mesh", "gen3dPostId": 101}
    steps[5]["generation"] = library.page["items"][0]
    steps[0]["output"] = {"type": "isolated", "character": {"url": "https://x/c.png"},
                          "parts": [{"key": "sword", "label": "Sword", "picture": {"url": "https://x/s.png"}}]}
    run = {"id": 33, "pipelineId": 9, "mode": "manual", "status": "awaiting", "graph": run_graph, "steps": steps,
           "seed": {"type": "picture", "name": "knight"}}
    pipes.runs["items"] = [{"id": 33, "status": "awaiting", "progress": {"done": 1, "total": 10, "waiting": 2},
                            "seed": {"name": "knight"}}]
    return g, run


def test_operators():
    ops = bpy.ops.zetta
    assert ops.do(action="tool", arg="smart") == {"FINISHED"}
    assert bpy.context.window_manager.zetta.tool == "smart"
    zetta = bpy.context.window_manager.zetta
    zetta.gen.model = "n25"
    zetta.gen.quality = "ultra"
    assert zetta.gen.face_count == "2000000", zetta.gen.face_count  # N2.5 Ultra: the range's top
    zetta.gen.polygon_type = "quadrilateral"
    assert zetta.gen.face_count == "150000", zetta.gen.face_count
    zetta.gen.model = "n1"
    zetta.gen.face_count = "9000"
    assert zetta.gen.quality == "low-poly", zetta.gen.quality


def test_draw_signed_out():
    auth.current, auth.user = auth.SIGNED_OUT, None
    draw_all("signed out")


def test_draw_signed_in():
    fake_account()
    draw_all("signed in")
    g, run = fake_pipeline()
    zetta = bpy.context.window_manager.zetta
    for node, _, _, _ in graph.ordered(g):
        pipes.editor["selected"] = node["id"]
        draw_all("pipeline recipe") if node is g["nodes"][0] else panels.draw_recipe(FakeLayout(), zetta)
    pipes.open_new_run()
    for kind in ("seed_picture", "seed_model", "seed_views"):
        g["nodes"][0]["kind"] = kind
        if kind == "seed_views":
            pipes.add_views_set()
        panels.draw_recipe(FakeLayout(), zetta)
    g["nodes"][0]["kind"] = "seed_picture"
    zetta.pipe_view = "runs"
    panels.draw_runs(FakeLayout(), zetta)
    pipes.run_view["run"] = run
    for node, _, _, _ in graph.ordered(run["graph"]):
        pipes.run_view["selected"] = node["id"]
        panels.draw_runs(FakeLayout(), zetta)
    run["status"] = "draft"
    panels.draw_runs(FakeLayout(), zetta)


def test_preferences_draw():
    prefs = bpy.context.preferences.addons[PACKAGE].preferences
    prefs.agent_account = "own"
    prefs.show_developer = True
    fake = FakeLayout()

    class Wrapper:
        layout = fake

        def __getattr__(self, name):
            return getattr(prefs, name)

    prefs_ui.ZettaPreferences.draw(Wrapper(), bpy.context)


def test_icons():
    enum_icons = {item[3] for item in props.TOOLS}
    assert not (enum_icons - ICONS), enum_icons - ICONS
    assert not bad_icons, sorted(bad_icons)


def test_sign_in_handshake():
    import urllib.request
    import webbrowser

    tasks = importlib.import_module(PACKAGE + ".core.tasks")
    session = importlib.import_module(PACKAGE + ".core.session")
    opened = []
    real_open = webbrowser.open
    webbrowser.open = lambda url: opened.append(url)
    try:
        assert auth.begin_login() is None
        assert auth.current == auth.WAITING and "client=blender" in opened[0], opened
        port = auth._server.server_address[1]
        state = opened[0].split("state=")[1].split("&")[0]
        preflight = urllib.request.Request(f"http://127.0.0.1:{port}/callback", method="OPTIONS",
                                           headers={"Origin": "https://zetta3d.ai"})
        with urllib.request.urlopen(preflight, timeout=5) as answer:
            assert answer.status == 204
            assert answer.headers["Access-Control-Allow-Private-Network"] == "true"
            assert answer.headers["Access-Control-Allow-Origin"] == "https://zetta3d.ai"
        wrong = urllib.request.Request(f"http://127.0.0.1:{port}/callback", method="POST",
                                       data=json.dumps({"token": "t", "state": "nope"}).encode(),
                                       headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(wrong, timeout=5)
            raise AssertionError("a wrong state was accepted")
        except urllib.error.HTTPError as error:
            assert error.code == 400
        right = urllib.request.Request(f"http://127.0.0.1:{port}/callback", method="POST",
                                       data=json.dumps({"token": "a.eyJleHAiOjk5OTk5OTk5OTl9.c", "state": state}).encode(),
                                       headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(right, timeout=5) as answer:
            assert json.loads(answer.read())["ok"] is True
        tasks._pump()
        assert session.token() == "a.eyJleHAiOjk5OTk5OTk5OTl9.c" and session._expiry == 9999999999
        assert auth.current == auth.CHECKING
        # The fake token is refused by /auth/me: the add-on signs itself out again.
        import time

        for _ in range(40):
            time.sleep(0.25)
            tasks._pump()
            if auth.current == auth.SIGNED_OUT:
                break
        assert auth.current == auth.SIGNED_OUT and session.token() is None, auth.current
    finally:
        webbrowser.open = real_open
        auth.cancel_login()


print(f"Zetta 3D smoke tests ({PACKAGE}, Blender {bpy.app.version_string})")
for name, fn in list(globals().items()):
    if name.startswith("test_") and callable(fn):
        check(name, fn)
print(f"{len(failures)} failed" if failures else "all passed")
sys.exit(1 if failures else 0)
