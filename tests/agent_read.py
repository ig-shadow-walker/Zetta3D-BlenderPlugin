"""The agent's reading and viewing tools: agent/tools/read.py and agent/tools/view.py.

Background Blender has no window, so the window-only paths (capture_view current, viewport, ui_navigate) are
checked for their clear error, and render runs its blocking background path.
"""

import os
import sys
import tempfile

import bpy

sys.path.insert(0, os.path.dirname(__file__))
from agent_common import call, err, fresh, module, ok, run_checks

tools = module("agent.tools")

read = module("agent.tools.read")
view = module("agent.tools.view")
errors = module("agent.errors")
changes = module("agent.changes")
values = module("agent.values")
targets = module("agent.targets")

JPEG = b"\xff\xd8"


def scene():
    """Props/Chair (Bevel, Subdivision, Oak) with a child Cushion, a hidden Lamp, an instance of Props."""
    fresh()
    props = bpy.data.collections.new("Props")
    bpy.context.scene.collection.children.link(props)
    mesh = bpy.data.meshes.new("Chair")
    import bmesh

    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=2)
    bm.to_mesh(mesh)
    bm.free()
    mesh.uv_layers.new(name="UVMap")
    chair = bpy.data.objects.new("Chair", mesh)
    props.objects.link(chair)
    chair.modifiers.new("Bevel", "BEVEL").width = 0.02
    chair.modifiers.new("Subdivision", "SUBSURF")
    oak = bpy.data.materials.new("Oak")
    chair.data.materials.append(oak)
    cushion = bpy.data.objects.new("Cushion", bpy.data.meshes.new("Cushion"))
    props.objects.link(cushion)
    cushion.parent = chair
    lamp = bpy.data.objects.new("Lamp", bpy.data.lights.new("Key", "POINT"))
    bpy.context.scene.collection.objects.link(lamp)
    lamp.hide_render = True
    lamp.hide_set(True)
    copy = bpy.data.objects.new("Props copy", None)
    copy.instance_type = "COLLECTION"
    copy.instance_collection = props
    bpy.context.scene.collection.objects.link(copy)
    bpy.context.view_layer.objects.active = chair
    chair.select_set(True)
    return chair


def saved_scene():
    """scene(), saved into a fresh folder with a scripts/ folder next to it. Returns the folder."""
    chair = scene()
    folder = tempfile.mkdtemp(prefix="zetta-read-")
    bpy.ops.wm.save_as_mainfile(filepath=os.path.join(folder, "chair.blend"))
    os.makedirs(os.path.join(folder, "scripts"))
    with open(os.path.join(folder, "scripts", "rig.py"), "w") as handle:
        handle.write("import bpy\n\ndef build_rig():\n    return 'spine'\n")
    with open(os.path.join(folder, "notes.txt"), "w") as handle:
        handle.write("the spine is long\n")
    return folder, chair


# ─── read.py ─────────────────────────────────────────────────────────────────────────────────────


def test_blender_info():
    scene()
    text = ok("blender_info")
    assert f"Blender {bpy.app.version_string}" in text, text
    assert "never saved" in text and "Units: METRIC" in text and "Frames:" in text, text
    assert "4 objects" in text and "1 materials" in text, text
    assert "no window" in text, text


def test_scene_outline_and_outline_text():
    scene()
    text = ok("scene_outline")
    lines = text.splitlines()
    assert lines[0] == "Scene Scene", lines
    assert "  Collection Props" in lines, text
    assert "    Chair [MESH · Bevel, Subdivision · Oak]" in lines, text
    assert "      Cushion [MESH]" in lines, text  # a child sits under its parent
    assert any(line.startswith("  Lamp [LIGHT POINT") and "(hidden) (render off)" in line for line in lines), text
    assert any("(instance of Props)" in line for line in lines), text
    shallow = read.outline_text(depth=1)
    assert "Collection Props (… 2 objects inside)" in shallow, shallow
    paged = read.outline_text(limit=1)
    assert "more lines: call scene_outline with offset=1" in paged, paged
    assert ok("scene_outline", root="collection:Props").splitlines()[0] == "Collection Props"
    assert "Cushion" in ok("scene_outline", root="Chair")
    assert "Did you mean: Props" in err("scene_outline", root="collection:Prop")
    assert "A target is" in err("scene_outline", root=" ")
    assert "between 1 and 8" in err("scene_outline", depth="deep")


def test_inspect_object_paths_work_with_set_properties():
    chair = scene()
    text = ok("inspect", target="Chair")
    assert 'modifiers["Bevel"].width = 0.02' in text, text
    assert "8 verts, 12 edges, 6 faces, 12 tris (6 quads, 0 n-gons)" in text, text
    assert "non-manifold edges: 0" in text, text
    assert "UV maps: UVMap (active)" in text, text
    assert 'material_slots[0].material = {"id": "material:Oak"}' in text, text
    assert "after modifiers:" in text, text
    # Every "path = value" line can go straight back through set_properties' writer.
    line = next(l for l in text.splitlines() if l.startswith('modifiers["Bevel"].segments = '))
    path = line.split(" = ")[0]
    values.assign(chair, path, 3)
    assert chair.modifiers["Bevel"].segments == 3
    only = ok("inspect", target="Chair", sections=["modifiers"])
    assert "# — transform —" not in only and 'modifiers["Subdivision"].levels' in only, only
    part = ok("inspect", target="Chair/modifiers/Bevel")
    assert 'modifiers["Bevel"].width = 0.02' in part and 'target "Chair"' in part, part
    assert "Did you mean: Chair" in err("inspect", target="Chiar")
    assert "sections takes" in err("inspect", target="Chair", sections=["colour"])


def test_inspect_rich_data():
    chair = scene()
    chair["note"] = "hello"
    chair.shape_key_add(name="Basis")
    chair.shape_key_add(name="Squash").value = 0.3
    chair.vertex_groups.new(name="Legs")
    chair.keyframe_insert("location", index=0, frame=1)
    chair.location.x = 2
    chair.keyframe_insert("location", index=0, frame=20)
    text = ok("inspect", target="Chair", sections=["data", "custom", "animation"])
    assert 'data.shape_keys.key_blocks["Squash"].value = 0.3' in text, text
    assert "vertex groups (1): Legs" in text, text
    assert '["note"] = "hello"' in text, text
    assert "location[0]: 2 keys, frames 1–20" in text, text  # also on layered actions (4.4+ / 5.x)
    arm = bpy.data.armatures.new("Rig")
    rig = bpy.data.objects.new("Rig", arm)
    bpy.context.scene.collection.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode="EDIT")
    bone = arm.edit_bones.new("spine")
    bone.tail = (0, 0, 1)
    bpy.ops.object.mode_set(mode="OBJECT")
    rig_text = ok("inspect", target="Rig", sections=["data"])
    assert "bones (1):" in rig_text and "spine: parent none" in rig_text, rig_text
    assert 'data.bones["spine"]' in ok("inspect", target="Rig/bones/spine")
    key = ok("inspect", target="light:Key")
    assert "energy = " in key and 'target "light:Key"' in key, key


def test_inspect_nodes():
    scene()
    oak = bpy.data.materials["Oak"]
    if hasattr(oak, "use_nodes"):
        oak.use_nodes = True
    principled = oak.node_tree.nodes["Principled BSDF"]
    principled.inputs["Roughness"].default_value = 0.3
    texture = oak.node_tree.nodes.new("ShaderNodeTexImage")
    texture.name = "Grain"
    oak.node_tree.links.new(texture.outputs["Color"], principled.inputs["Base Color"])
    groups_before = len(bpy.data.node_groups)
    text = ok("inspect_nodes", owner="material:Oak")
    assert "Principled BSDF [ShaderNodeBsdfPrincipled]" in text, text
    assert "in Principled BSDF.Roughness = 0.3" in text, text
    assert "in Principled BSDF.Metallic" not in text, text  # still at its default
    assert "Grain.Color → Principled BSDF.Base Color" in text, text
    assert "Principled BSDF.BSDF → Material Output.Surface" in text, text
    assert len(bpy.data.node_groups) == groups_before  # the defaults' scratch tree is gone
    group = targets.new_geometry_group("Scatter")
    group.interface.new_socket("Count", in_out="INPUT", socket_type="NodeSocketInt")
    chair = bpy.data.objects["Chair"]
    modifier = chair.modifiers.new("GeometryNodes", "NODES")
    modifier.node_group = group
    tree = ok("inspect_nodes", owner="Chair/modifiers/GeometryNodes")
    assert "Interface:" in tree and "input Count (NodeSocketInt" in tree, tree
    inputs = ok("inspect", target="Chair", sections=["modifiers"])
    line = next(l for l in inputs.splitlines() if "# Count" in l)  # 5.x RNA path, or 4.x ["Socket_n"]
    values.assign(chair, line.split(" = ")[0], 7)
    assert "= 7  # Count" in ok("inspect", target="Chair", sections=["modifiers"])
    assert "The scene's world has no nodes." == err("inspect_nodes", owner="world")
    assert "There is no material" in err("inspect_nodes", owner="material:Pine")
    assert "not a Geometry Nodes modifier" in err("inspect_nodes", owner="Chair/modifiers/Bevel")


def run_with(tool_name, args):
    """Like agent_common.call, for arguments it cannot take as keywords (find's "name")."""
    box = {}
    ctx = tools.Context({"toolUseId": "toolu_test", "name": tool_name, "input": args, "effect": "read"},
                        lambda result: box.setdefault("result", result))
    tools.run(ctx.call, ctx)
    return box["result"]


def test_find():
    scene()
    named = run_with("find", {"name": "cha"})
    assert not named.error and "Chair (MESH, collection Props)" in named.text, named.text
    assert "Lamp (LIGHT" in ok("find", type="LIGHT")
    assert ok("find", modifier="BEVEL").splitlines()[1].startswith("Chair")
    assert "Chair" in ok("find", material="material:Oak")
    by_collection = ok("find", collection="Props")
    assert "2 objects match" in by_collection, by_collection
    assert "material:Oak" in ok("find", kind="materials")
    assert run_with("find", {"name": "zzz"}).text == "No objects match."
    assert "Did you mean: Props" in err("find", collection="Prop")
    assert "kind is one of" in err("find", kind="sounds")


def test_get_selection():
    scene()
    text = ok("get_selection")
    assert "Mode: OBJECT" in text and "Active: Chair (MESH)" in text, text
    assert "Selected (1): Chair (MESH)" in text and "Active material slot: 0 (material:Oak)" in text, text
    assert "No 3D View is open" in text, text
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    edit = ok("get_selection")
    bpy.ops.object.mode_set(mode="OBJECT")
    assert "Edit Mode Chair: 8 verts, 12 edges, 6 faces selected" in edit, edit


def test_read_text():
    fresh()
    text = bpy.data.texts.new("tools.py")
    text.write("\n".join(f"line {n}" for n in range(1, 3001)))
    first = ok("read_text", target="text:tools.py")
    assert first.splitlines()[0] == "text:tools.py — 3000 lines, showing 1–2000:", first[:80]
    assert "   1  line 1" in first and "more lines: read again with startLine=2001" in first
    part = ok("read_text", target="text:tools.py", startLine=2999)
    assert part.splitlines()[-1] == "3000  line 3000", part
    assert "needs the file saved first" in err("read_text", target="//scripts/rig.py")
    folder, _ = saved_scene()
    assert "3  def build_rig():" in ok("read_text", target="//scripts/rig.py")
    assert "outside the .blend's folder" in err("read_text", target="//../elsewhere.txt")
    assert "There is no file" in err("read_text", target="//missing.py")
    assert "a folder" in err("read_text", target="//scripts")
    assert 'There is no text "tool.py"' in err("read_text", target="text:tool.py")
    assert "past the end" in err("read_text", target="//scripts/rig.py", startLine=50)


def test_search_text():
    folder, _ = saved_scene()
    bpy.data.texts.new("rig.py").write("import bpy\nspine = 'long'\n")
    assert ok("search_text", pattern=r"spine\s=") == "text:rig.py:2: spine = 'long'"
    in_files = ok("search_text", pattern="spine", **{"in": "files"})
    assert "//scripts/rig.py:4:" in in_files and "//notes.txt:1: the spine is long" in in_files, in_files
    only_py = ok("search_text", pattern="spine", glob="//scripts/**/*.py", **{"in": "files"})
    assert "notes.txt" not in only_py and "rig.py" in only_py, only_py
    assert "No match" in ok("search_text", pattern="nothing-here")
    assert "not a valid regular expression" in err("search_text", pattern="(")
    assert "outside the .blend's folder" in err("search_text", pattern="x", glob="//../**/*", **{"in": "files"})


def test_recent_errors():
    errors.record("run_python", "ValueError: boom")
    text = ok("recent_errors", limit=5)
    assert "[run_python," in text and "ValueError: boom" in text, text
    assert "between 1 and 100" in err("recent_errors", limit="many")


def test_api_lookup():
    exact = ok("api_lookup", query="BevelModifier.width")
    assert "property BevelModifier.width: FLOAT" in exact.splitlines()[1], exact
    words = ok("api_lookup", query="bevel width", kind="property", limit=5)
    assert "BevelModifier.width" in words, words
    operator = ok("api_lookup", query="object.shade_auto_smooth", kind="operator", limit=1)
    assert "operator bpy.ops.object.shade_auto_smooth()" in operator and "angle:" in operator, operator
    kind = ok("api_lookup", query="BevelModifier", kind="type", limit=1)
    assert "type bpy.types.BevelModifier (Modifier)" in kind and "segments" in kind, kind
    enum = ok("api_lookup", query="BevelModifier.limit_method", limit=1)
    assert "NONE, ANGLE, WEIGHT" in enum, enum
    assert "query is" in err("api_lookup", query="  ")
    assert "kind is" in err("api_lookup", query="bevel", kind="function")
    assert "Nothing in Blender" in ok("api_lookup", query="qqqzzzxx")


# ─── view.py ─────────────────────────────────────────────────────────────────────────────────────


def test_capture_view_renders_without_a_window():
    scene()
    scenes_before = len(bpy.data.scenes)
    around = call("capture_view", target="Chair", width=256)
    assert not around.error, around.text
    assert len(around.images) == 4 and all(data.startswith(JPEG) for data, _ in around.images), around.text
    assert "front-left, front-right, back, above" in around.text and "Chair" in around.text, around.text
    top = call("capture_view", angles="top", width=256, shading="material")
    assert not top.error and len(top.images) == 1, top.text
    assert len(bpy.data.scenes) == scenes_before and "Chair" in bpy.context.scene.objects  # nothing left over
    assert NO_VIEW_TEXT in err("capture_view")
    assert "no camera" in err("capture_view", view="camera")
    camera = bpy.data.objects.new("Cam", bpy.data.cameras.new("Cam"))
    bpy.context.scene.collection.objects.link(camera)
    camera.location = (0, -8, 1)
    camera.rotation_euler = (1.5708, 0, 0)
    bpy.context.scene.camera = camera
    through = call("capture_view", view="camera", width=320)
    assert not through.error and len(through.images) == 1, through.text
    assert "Through camera Cam" in through.text, through.text
    assert "256 to 1280" in err("capture_view", angles="top", width=100)
    assert "not an object or a collection" in err("capture_view", target="material:Oak")


NO_VIEW_TEXT = "No 3D View is open."


def test_viewport_and_ui_navigate_need_a_window():
    scene()
    assert NO_VIEW_TEXT in err("viewport", action="shading", shading="solid")
    assert "action is" in err("viewport", action="spin")
    assert "No Blender window is open" in err("ui_navigate", workspace="Shading")
    assert "Say where to go" in err("ui_navigate")
    assert "editor is one of" in err("ui_navigate", editor="sculpt")
    assert view._section("add-ons") == "ADDONS" and view._section("Save & Load") == "SAVE_LOAD"
    assert view._engine("BLENDER_EEVEE_NEXT").startswith("BLENDER_EEVEE")


def test_render_preview_and_still():
    folder, _ = saved_scene()
    scene_ = bpy.context.scene
    assert "no camera" in err("render")
    camera = bpy.data.objects.new("Cam", bpy.data.cameras.new("Cam"))
    scene_.collection.objects.link(camera)
    camera.location = (0, -8, 1)
    camera.rotation_euler = (1.5708, 0, 0)
    scene_.camera = camera
    before = (scene_.render.engine, scene_.render.resolution_percentage, scene_.render.filepath,
              scene_.render.image_settings.file_format, scene_.render.image_settings.color_mode)
    preview = call("render", engine="BLENDER_WORKBENCH")
    assert not preview.error, preview.text
    assert len(preview.images) == 1 and preview.images[0][0].startswith(JPEG), preview.text
    assert "BLENDER_WORKBENCH at 499×281" in preview.text, preview.text
    after = (scene_.render.engine, scene_.render.resolution_percentage, scene_.render.filepath,
             scene_.render.image_settings.file_format, scene_.render.image_settings.color_mode)
    assert before == after, (before, after)
    changes.begin_turn("render test")
    still = ok("render", kind="still", engine="BLENDER_WORKBENCH", resolution=[64, 48],
               output="//renders/hero.png")
    assert os.path.isfile(os.path.join(folder, "renders", "hero.png")), still
    assert "saved //renders/hero.png" in still, still
    assert changes.current()["lines"][-1]["label"] == "Rendered //renders/hero.png"
    assert scene_.render.resolution_x == 1920  # put back
    assert "outside the .blend's folder" in err("render", kind="still", output="//../hero.png")
    assert "must end in" in err("render", kind="still", output="//renders/hero.gif")
    assert "no NOPE engine" in err("render", engine="NOPE")


run_checks(globals())
