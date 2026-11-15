"""The scene and node tools: create, change, organise, animate, mesh operations, delete, undo, nodes, materials.

Run: tests/run.sh tests/agent_scene.py
"""

import math
import os
import sys
import tempfile

import bpy

sys.path.insert(0, os.path.dirname(__file__))
from agent_common import fresh, module, run_checks

scene_tools = module("agent.tools.scene")
node_tools = module("agent.tools.nodes")
changes = module("agent.changes")
tools = module("agent.tools")


# agent_common's call/ok/err take the tool as `name`, which collides with the `name` argument of
# create_object, create_material and collection_op — so these take it positionally instead.
def call(tool_name, /, **args):
    box = {}
    ctx = tools.Context({"toolUseId": "toolu_test", "name": tool_name, "input": args, "effect": "write"},
                        lambda result: box.setdefault("result", result))
    tools.run(ctx.call, ctx)
    assert "result" in box, f"{tool_name} did not finish"
    return box["result"]


def ok(tool_name, /, **args):
    result = call(tool_name, **args)
    assert not result.error, f"{tool_name} failed: {result.text}"
    return result.text


def err(tool_name, /, **args):
    result = call(tool_name, **args)
    assert result.error, f"{tool_name} should have failed, but said: {result.text}"
    return result.text


def close(a, b, tolerance=1e-4):
    return abs(a - b) < tolerance


def cube(name="Cube", location=(0, 0, 0)):
    ok("create_object", name=name, kind="cube", location=list(location))
    return bpy.data.objects[name]


# ─── create_object, modify_object ────────────────────────────────────────────────────────────────


def test_create_object():
    fresh()
    bpy.data.materials.new("Oak")
    text = ok("create_object", name="Chair", kind="cube", collection="collection:Props", location=[1, 2, 3],
              rotation=[0, 0, 90], size=1, modifiers=[{"type": "BEVEL", "properties": {"width": 0.05}}],
              material="material:Oak")
    chair = bpy.data.objects["Chair"]
    assert "Chair" in text and "Props" in text, text
    assert chair.type == "MESH" and chair.users_collection[0].name == "Props"
    assert close(chair.location.z, 3) and close(chair.rotation_euler.z, math.radians(90))
    assert close(chair.dimensions.x, 1.0), chair.dimensions
    assert chair.modifiers["Bevel"].type == "BEVEL" and close(chair.modifiers["Bevel"].width, 0.05)
    assert chair.material_slots[0].material.name == "Oak"
    text = ok("create_object", name="Chair", kind="uv_sphere")
    assert '"Chair.001"' in text, text
    ok("create_object", name="Key", kind="light", light="AREA", size=2)
    assert bpy.data.objects["Key"].data.type == "AREA" and close(bpy.data.objects["Key"].data.size, 2)
    ok("create_object", name="Cam", kind="camera", parent="Chair")
    assert bpy.data.objects["Cam"].parent == chair
    for kind in ("empty", "text", "bezier", "armature", "torus", "monkey", "plane", "cylinder"):
        ok("create_object", name=f"K {kind}", kind=kind)
    # Not left in Edit Mode or with the new object selected: the user's state comes back.
    assert bpy.context.mode == "OBJECT"


def test_create_object_errors_leave_nothing():
    fresh()
    count = len(bpy.data.objects)
    assert "kind is one of" in err("create_object", name="Pot", kind="teapot")
    assert "no modifier type" in err("create_object", name="Pot", kind="cube", modifiers=[{"type": "BEVELL"}])
    line = err("create_object", name="Pot", kind="cube", modifiers=[{"type": "BEVEL", "properties": {"widht": 1}}])
    assert "Did you mean: width" in line, line
    assert len(bpy.data.objects) == count, [o.name for o in bpy.data.objects]


def test_modify_object():
    fresh()
    table = cube("Table", (2, 0, 0))
    lamp = cube("Lamp", (2, 0, 3))
    ok("collection_op", action="create", collection="collection:Set")
    text = ok("modify_object", target="Lamp", name="Desk Lamp", parent="Table", collection="collection:Set",
              hide_render=True, display_type="WIRE")
    assert lamp.name == "Desk Lamp" and lamp.parent == table and "renamed" in text
    assert close(lamp.matrix_world.translation.z, 3)  # keepTransform: stayed put
    assert [c.name for c in lamp.users_collection] == ["Set"] and lamp.hide_render and lamp.display_type == "WIRE"
    ok("modify_object", target="Desk Lamp", rotation=[90, 0, 0], hide_viewport=True)
    assert close(lamp.rotation_euler.x, math.pi / 2) and lamp.hide_get()
    ok("modify_object", target="Desk Lamp", parent=None, hide_viewport=False)
    assert lamp.parent is None and close(lamp.matrix_world.translation.z, 3) and not lamp.hide_get()
    assert "cannot be its parent" in err("modify_object", target="Table", parent="Table")
    assert "Did you mean: Table" in err("modify_object", target="Tabel", location=[0, 0, 0])


# ─── set_properties ──────────────────────────────────────────────────────────────────────────────


def test_set_properties():
    fresh()
    obj = cube("Box")
    obj.modifiers.new("Bevel", "BEVEL")
    text = ok("set_properties", target="Box",
              properties={'modifiers["Bevel"].width': 0.1, "location": [1, 2, 3], '["note"]': "hi"})
    assert close(obj.modifiers["Bevel"].width, 0.1) and close(obj.location.y, 2) and obj["note"] == "hi"
    assert 'modifiers["Bevel"].width = 0.1' in text, text
    ok("set_properties", target="scene:Scene", properties={"frame_end": 120, "render.resolution_x": 1280})
    assert bpy.context.scene.frame_end == 120 and bpy.context.scene.render.resolution_x == 1280
    ok("set_properties", target="Box/modifiers/Bevel", properties={"segments": 4})
    assert obj.modifiers["Bevel"].segments == 4


def test_set_properties_all_or_nothing():
    fresh()
    obj = cube("Box")
    line = err("set_properties", target="Box",
               properties={"location": [5, 5, 5], "locaton": 1, '["fresh"]': 1, "scale": "big"})
    assert "Nothing was changed" in line and "locaton" in line and "scale" in line, line
    assert close(obj.location.x, 0), obj.location  # the good one was put back
    assert "fresh" not in obj.keys()


# ─── add_modifier, add_constraint ────────────────────────────────────────────────────────────────


def test_add_modifier():
    fresh()
    obj = cube("Box")
    ok("add_modifier", target="Box", type="BEVEL")
    text = ok("add_modifier", target="Box", type="SUBSURF", properties={"levels": 2}, index=0)
    assert [m.type for m in obj.modifiers] == ["SUBSURF", "BEVEL"], text
    assert obj.modifiers[0].levels == 2 and "position 0" in text
    text = ok("add_modifier", target="Box", type="NODES")
    assert "create" in text and obj.modifiers[-1].name == "GeometryNodes"
    ok("add_modifier", target="Box", type="SMOOTH_BY_ANGLE", properties={"angle": 40})
    assert obj.modifiers[-1].type == "NODES" and obj.modifiers[-1].node_group.name.startswith("Smooth by Angle")
    ok("create_object", name="Empty", kind="empty")
    assert "cannot take" in err("add_modifier", target="Empty", type="BEVEL")
    assert "Did you mean: SUBSURF" in err("add_modifier", target="Box", type="SUBSURFF")
    count = len(obj.modifiers)
    err("add_modifier", target="Box", type="ARRAY", properties={"count": -2})
    assert len(obj.modifiers) == count  # the refused one did not stay


def test_add_constraint():
    fresh()
    cam = cube("Cam")
    cube("Goal", (0, 5, 0))
    text = ok("add_constraint", target="Cam", type="TRACK_TO", properties={"target": {"id": "Goal"}})
    assert cam.constraints[0].type == "TRACK_TO" and cam.constraints[0].target.name == "Goal", text
    ok("create_object", name="Rig", kind="armature")
    ok("add_constraint", target="Rig", bone="Bone", type="COPY_ROTATION", name="Follow")
    assert bpy.data.objects["Rig"].pose.bones["Bone"].constraints["Follow"].type == "COPY_ROTATION"
    assert "no constraint type" in err("add_constraint", target="Cam", type="LOOK_AT")
    assert "not an armature" in err("add_constraint", target="Cam", type="IK", bone="spine")


# ─── keyframes ───────────────────────────────────────────────────────────────────────────────────


def test_keyframes():
    fresh()
    obj = cube("Box")
    text = ok("keyframes", target="Box", data_path="location", index=0,
              keys=[{"frame": 1, "value": 0}, {"frame": 24, "value": 4}], interpolation="LINEAR")
    assert "2 keys" in text, text
    curves = scene_tools.fcurves_of(obj.animation_data)
    curve = next(c for c in curves if c.data_path == "location" and c.array_index == 0)
    assert len(curve.keyframe_points) == 2 and all(p.interpolation == "LINEAR" for p in curve.keyframe_points)
    assert close(curve.evaluate(24), 4)
    ok("keyframes", target="Box", data_path="rotation_euler", keys=[{"frame": 10, "value": [0, 0, 180]}])
    rot = next(c for c in scene_tools.fcurves_of(obj.animation_data)
               if c.data_path == "rotation_euler" and c.array_index == 2)
    assert close(rot.evaluate(10), math.pi), rot.evaluate(10)
    ok("create_object", name="Bulb", kind="light")
    text = ok("keyframes", target="Bulb", data_path="data.energy", keys=[{"frame": 1, "value": 10},
                                                                         {"frame": 5, "value": 500}],
              action="Flicker")
    light = bpy.data.objects["Bulb"].data
    assert light.animation_data.action.name == "Flicker", text
    assert any(c.data_path == "energy" for c in scene_tools.fcurves_of(light.animation_data))
    obj.modifiers.new("Bevel", "BEVEL")
    ok("keyframes", target="Box", data_path='modifiers["Bevel"].width', keys=[{"frame": 1, "value": 0.2}])
    assert "Did you mean: location" in err("keyframes", target="Box", data_path="locaton",
                                           keys=[{"frame": 1, "value": 0}])
    assert "index 3 is not" in err("keyframes", target="Box", data_path="location", index=3,
                                   keys=[{"frame": 1, "value": 0}])


# ─── collection_op ───────────────────────────────────────────────────────────────────────────────


def test_collection_op():
    fresh()
    box = cube("Box")
    ok("collection_op", action="create", collection="collection:Props", objects=["Box"])
    props = bpy.data.collections["Props"]
    assert "Box" in props.objects and len(box.users_collection) == 2
    ok("collection_op", action="create", collection="collection:Small", parent="collection:Props")
    assert "Small" in props.children
    home = [c for c in box.users_collection if c != props][0]
    ok("collection_op", action="unlink", collection=f"collection:{home.name}", objects=["Box"])
    assert box.users_collection[:] == [props] or list(box.users_collection) == [props]
    assert "in no collection" in err("collection_op", action="unlink", collection="collection:Props",
                                     objects=["Box"])
    ok("collection_op", action="rename", collection="collection:Props", name="Furniture")
    assert props.name == "Furniture"
    ok("collection_op", action="hide", collection="collection:Furniture")
    assert scene_tools.layer_collection(props).hide_viewport
    ok("collection_op", action="show", collection="collection:Furniture")
    assert not scene_tools.layer_collection(props).hide_viewport
    ok("collection_op", action="exclude", collection="collection:Furniture")
    assert scene_tools.layer_collection(props).exclude
    ok("collection_op", action="include", collection="collection:Furniture")
    assert "already a collection" in err("collection_op", action="create", collection="collection:Furniture")
    assert "Did you mean: Furniture" in err("collection_op", action="hide", collection="collection:Furnture")


# ─── select, set_mode ────────────────────────────────────────────────────────────────────────────


def test_select_and_mode():
    fresh()
    a, b = cube("A"), cube("B")
    ok("select", targets=["A", "B"], active="B")
    assert a.select_get() and b.select_get() and bpy.context.view_layer.objects.active == b
    ok("select", targets=["A"])
    assert a.select_get() and not b.select_get()
    ok("select", targets=["B"], mode="add")
    assert a.select_get() and b.select_get()
    assert "Nothing is selected" in ok("select", targets=[])
    assert 'There is no object "AA"' in err("select", targets=["AA"])
    text = ok("set_mode", mode="EDIT", target="A")
    assert a.mode == "EDIT", text
    ok("set_mode", mode="OBJECT")
    assert a.mode == "OBJECT"
    assert "Pose Mode is for armature" in err("set_mode", mode="POSE", target="A")


# ─── mesh_op ─────────────────────────────────────────────────────────────────────────────────────


def test_mesh_op_transform_and_origin():
    fresh()
    box = cube("Box", (1, 0, 0))
    child = cube("Child", (1, 0, 5))
    child.parent = box
    child.matrix_parent_inverse = box.matrix_world.inverted()
    box.scale = (2, 2, 2)
    box.rotation_euler = (0, 0, math.radians(45))
    bpy.context.view_layer.update()
    child_world = child.matrix_world.translation.copy()
    ok("mesh_op", targets=["Box"], op="apply_transform", options={"rotation": True, "scale": True})
    bpy.context.view_layer.update()
    assert close(box.scale.x, 1) and close(box.rotation_euler.z, 0)
    assert close(max(abs(v.co.y) for v in box.data.vertices), 2 * math.sqrt(2))  # scaled 2, turned 45°
    assert (child.matrix_world.translation - child_world).length < 1e-4  # the child did not move
    ok("mesh_op", targets=["Box"], op="set_origin", options={"to": "bounds_bottom"})
    assert close(min(v.co.z for v in box.data.vertices), 0) and close(box.location.z, -2)
    ok("create_object", name="Cam", kind="camera")
    assert "geometry" in err("mesh_op", targets=["Cam"], op="apply_transform")


def test_mesh_op_mesh_edits():
    fresh()
    box = cube("Box")
    ok("mesh_op", targets=["Box"], op="shade_smooth")
    assert all(p.use_smooth for p in box.data.polygons)
    ok("mesh_op", targets=["Box"], op="shade_flat")
    assert not any(p.use_smooth for p in box.data.polygons)
    ok("mesh_op", targets=["Box"], op="shade_auto_smooth", options={"angle": 45})
    assert any(m.type == "NODES" for m in box.modifiers)
    box.modifiers.clear()
    ok("mesh_op", targets=["Box"], op="recalculate_normals", options={"inside": True})
    ok("mesh_op", targets=["Box"], op="recalculate_normals")
    ok("mesh_op", targets=["Box"], op="triangulate")
    assert len(box.data.polygons) == 12
    # A second cube in the same place: merging welds it onto the first.
    other = cube("Other")
    ok("mesh_op", targets=["Box", "Other"], op="join")
    assert "Other" not in bpy.data.objects and len(box.data.vertices) == 16
    ok("mesh_op", targets=["Box"], op="merge_by_distance", options={"distance": 0.001})
    assert len(box.data.vertices) == 8, len(box.data.vertices)
    ok("mesh_op", targets=["Box"], op="smart_uv_project")
    assert box.data.uv_layers.active is not None
    assert "at least two" in err("mesh_op", targets=["Box"], op="join")
    del other


def test_mesh_op_modifiers_and_objects():
    fresh()
    box = cube("Box")
    box.modifiers.new("Sub", "SUBSURF")
    ok("mesh_op", targets=["Box"], op="apply_modifier", options={"modifier": "Sub"})
    assert not box.modifiers and len(box.data.vertices) > 8
    assert "There is no modifier on Box" in err("mesh_op", targets=["Box"], op="apply_modifier",
                                                options={"modifier": "Sub"})
    cube("Far", (10, 0, 0))
    ok("mesh_op", targets=["Box", "Far"], op="join")
    before = set(bpy.data.objects.keys())
    text = ok("mesh_op", targets=["Box"], op="separate_loose")
    assert len(bpy.data.objects) == len(before) + 1, text
    ok("create_object", name="Path", kind="bezier")
    ok("mesh_op", targets=["Path"], op="convert_to_mesh")
    assert bpy.data.objects["Path"].type == "MESH"
    twin = bpy.data.objects.new("Twin", box.data)
    bpy.context.collection.objects.link(twin)
    assert "make_single_user first" in err("mesh_op", targets=["Twin"], op="apply_transform")
    ok("mesh_op", targets=["Twin"], op="make_single_user")
    assert twin.data != box.data and box.data.users == 1
    assert "op is one of" in err("mesh_op", targets=["Box"], op="explode")


def test_mesh_op_puts_mode_back():
    fresh()
    box = cube("Box")
    other = cube("Other", (4, 0, 0))
    ok("set_mode", mode="EDIT", target="Box")
    ok("mesh_op", targets=["Box", "Other"], op="shade_smooth")
    assert box.mode == "EDIT" and bpy.context.view_layer.objects.active == box
    ok("set_mode", mode="OBJECT")
    del other


# ─── delete, purge_unused ────────────────────────────────────────────────────────────────────────


def test_delete():
    fresh()
    box = cube("Box")
    box.modifiers.new("Bevel", "BEVEL")
    cube("Parent", (0, 0, 2))
    child = cube("Kid", (3, 0, 0))
    ok("modify_object", target="Kid", parent="Parent")
    ok("collection_op", action="create", collection="collection:Bin", objects=["Box"])
    for other in [c for c in box.users_collection if c.name != "Bin"]:
        other.objects.unlink(box)
    material = bpy.data.materials.new("Old")
    ok("edit_nodes", owner="material:Old", create="shader",
       changes=[{"op": "add", "type": "ShaderNodeMix", "name": "Mix"}])
    text = ok("delete", targets=["Box/modifiers/Bevel", "Parent", "material:Old/nodes/Mix", "collection:Bin"])
    assert not box.modifiers and "Parent" not in bpy.data.objects, text
    assert "Mix" not in material.node_tree.nodes
    assert "Bin" not in bpy.data.collections and box.users_collection  # Box moved up, not lost
    assert close(child.matrix_world.translation.x, 3) and child.parent is None
    ok("delete", targets=["material:Old"])
    assert "Old" not in bpy.data.materials
    assert "Did you mean: Box" in err("delete", targets=["Boxx"])
    assert "Box" in bpy.data.objects  # an unresolved target stops the whole call first


def test_purge_unused():
    fresh()
    bpy.data.meshes.new("Orphan")
    bpy.data.materials.new("Unused")
    cube("Kept")
    text = ok("purge_unused")
    assert "Orphan" not in bpy.data.meshes and "Unused" not in bpy.data.materials, text
    assert "Kept" in bpy.data.objects and "mesh:Orphan" in text
    assert "true or false" in err("purge_unused", recursive="yes")


# ─── undo ────────────────────────────────────────────────────────────────────────────────────────


def test_undo():
    fresh()
    changes.turns.clear()
    assert "nothing of yours" in err("undo")
    changes.begin_turn("test")
    bpy.ops.ed.undo_push(message="before")
    ok("create_object", name="First", kind="cube")
    ok("create_object", name="Second", kind="cube")
    assert changes.current()["steps"] == 2
    text = ok("undo", steps=5)
    assert "Second" not in bpy.data.objects and "First" not in bpy.data.objects, text
    assert "Only 2" in text and changes.current()["steps"] == 0
    assert "nothing of yours" in err("undo")
    changes.turns.clear()


# ─── edit_nodes ──────────────────────────────────────────────────────────────────────────────────


def test_edit_nodes_shader():
    fresh()
    material = bpy.data.materials.new("Oak")
    text = ok("edit_nodes", owner="material:Oak", create="shader", changes=[
        {"op": "add", "type": "ShaderNodeTexNoise", "name": "Grain"},
        {"op": "add", "type": "ShaderNodeMix", "name": "Tint", "properties": {"data_type": "RGBA"},
         "inputs": {"Factor": 0.25, "A": "#ff0000"}},
        {"op": "link", "from": "Grain.Color", "to": "Tint.B"},
        {"op": "link", "from": "Tint.Result", "to": "Principled BSDF.Base Color"},
        {"op": "set", "node": "Principled BSDF", "inputs": {"Roughness": 0.3}},
    ])
    tree = material.node_tree
    tint = tree.nodes["Tint"]
    assert tint.data_type == "RGBA" and close(tint.inputs[0].default_value, 0.25), text
    assert close(node_tools.find_socket(tint, "A", False).default_value[0], 1.0)
    assert tree.nodes["Principled BSDF"].inputs["Base Color"].links[0].from_node == tint
    assert close(tree.nodes["Principled BSDF"].inputs["Roughness"].default_value, 0.3)
    assert "Tint.Result → Principled BSDF.Base Color" in text
    xs = {n.name: n.location.x for n in tree.nodes}
    assert xs["Grain"] < xs["Tint"] < xs["Principled BSDF"] < xs["Material Output"], xs  # arranged
    # By index among the shown sockets, and node names with dots in them.
    ok("edit_nodes", owner="material:Oak", changes=[
        {"op": "add", "type": "ShaderNodeRGB", "name": "Base.Paint"},
        {"op": "link", "from": "Base.Paint.Color", "to": "Tint.1"},
        {"op": "unlink", "to": "Tint.B"},
        {"op": "remove", "node": "Grain"},
        {"op": "add", "type": "ShaderNodeTexNoise", "name": "Grain"},
    ], arrange=False)
    assert node_tools.find_socket(tint, "A", False).links[0].from_node.name == "Base.Paint"
    assert not node_tools.find_socket(tint, "B", False).is_linked
    assert tree.nodes["Grain"].bl_idname == "ShaderNodeTexNoise" and len(tree.nodes) == 5


def test_edit_nodes_fails_whole():
    fresh()
    material = bpy.data.materials.new("Oak")
    ok("edit_nodes", owner="material:Oak", create="shader",
       changes=[{"op": "set", "node": "Principled BSDF", "inputs": {"Metallic": 0.5}}])
    tree = material.node_tree
    before = sorted(tree.nodes.keys())
    links = len(tree.links)
    line = err("edit_nodes", owner="material:Oak", changes=[
        {"op": "add", "type": "ShaderNodeTexImage", "name": "Img"},
        {"op": "set", "node": "Principled BSDF", "inputs": {"Metallic": 1.0}},
        {"op": "remove", "node": "Material Output"},
        {"op": "link", "from": "Img.Colour", "to": "Principled BSDF.Base Color"},
    ])
    assert "Change 4" in line and "Its outputs: 0 Color, 1 Alpha" in line and "Nothing was changed" in line, line
    assert sorted(tree.nodes.keys()) == before and len(tree.links) == links
    assert close(tree.nodes["Principled BSDF"].inputs["Metallic"].default_value, 0.5)
    assert "Did you mean: ShaderNodeTexImage" in err("edit_nodes", owner="material:Oak", changes=[
        {"op": "add", "type": "ShaderNodeTexImag"}])
    assert "cannot go in" in err("edit_nodes", owner="material:Oak", changes=[
        {"op": "add", "type": "GeometryNodeMeshCube"}])


def test_edit_nodes_geometry():
    fresh()
    cube("Rocks")
    ok("add_modifier", target="Rocks", type="NODES")
    text = ok("edit_nodes", owner="Rocks/modifiers/GeometryNodes", create="geometry", changes=[
        {"op": "add", "type": "GeometryNodeSetPosition", "name": "Lift", "inputs": {"Offset": [0, 0, 1]}},
        {"op": "link", "from": "Group Input.Geometry", "to": "Lift.Geometry"},
        {"op": "link", "from": "Lift.Geometry", "to": "Group Output.Geometry"},
    ])
    group = bpy.data.objects["Rocks"].modifiers["GeometryNodes"].node_group
    assert group is not None and "replacing Group Input.Geometry" in text, text
    xs = {n.name: n.location.x for n in group.nodes}
    assert xs["Group Input"] < xs["Lift"] < xs["Group Output"], xs
    bpy.context.view_layer.update()
    evaluated = bpy.data.objects["Rocks"].evaluated_get(bpy.context.evaluated_depsgraph_get())
    assert close(max(v.co.z for v in evaluated.data.vertices), 2.0)


# ─── create_material, assign_material ────────────────────────────────────────────────────────────


def _saved_file_with_textures():
    folder = tempfile.mkdtemp(prefix="zetta-agent-")
    bpy.ops.wm.save_as_mainfile(filepath=os.path.join(folder, "scene.blend"))
    os.makedirs(os.path.join(folder, "textures"))
    for name in ("oak.png", "oak_normal.png"):
        image = bpy.data.images.new(name, 4, 4)
        image.filepath_raw = os.path.join(folder, "textures", name)
        image.file_format = "PNG"
        image.save()
        bpy.data.images.remove(image)
    return folder


def test_create_material():
    fresh()
    box = cube("Box")
    _saved_file_with_textures()
    text = ok("create_material", name="Oak", base_color="#8b5a2b", roughness=0.6, emission="#ff0000",
              transmission=0.2, textures={"base_color": "//textures/oak.png", "normal": "//textures/oak_normal.png"},
              assign_to=["Box"])
    material = bpy.data.materials["Oak"]
    tree = material.node_tree
    bsdf = next(n for n in tree.nodes if n.type == "BSDF_PRINCIPLED")
    assert close(bsdf.inputs["Roughness"].default_value, 0.6) and close(bsdf.inputs["Transmission Weight"].default_value, 0.2)
    assert close(bsdf.inputs["Emission Strength"].default_value, 1.0) and close(bsdf.inputs["Emission Color"].default_value[0], 1)
    base = bsdf.inputs["Base Color"].links[0].from_node
    assert base.image.colorspace_settings.name == "sRGB", text
    normal_map = bsdf.inputs["Normal"].links[0].from_node
    assert normal_map.type == "NORMAL_MAP"
    assert normal_map.inputs["Color"].links[0].from_node.image.colorspace_settings.name == "Non-Color"
    assert box.material_slots[0].material == material
    count = len(bpy.data.materials)
    assert "There is no file" in err("create_material", name="Pine", textures={"roughness": "//textures/nope.png"})
    assert "not a colour" in err("create_material", name="Pine", base_color="brown")
    assert len(bpy.data.materials) == count


def test_assign_material():
    fresh()
    box = cube("Box")
    red = bpy.data.materials.new("Red")
    blue = bpy.data.materials.new("Blue")
    ok("assign_material", target="Box", material="material:Red")
    assert [s.material for s in box.material_slots] == [red]
    ok("assign_material", target="Box", material="material:Blue", slot=1)
    assert [s.material for s in box.material_slots] == [red, blue]
    assert "selected faces" in err("assign_material", target="Box", material="material:Blue", all_faces=False)
    ok("set_mode", mode="EDIT", target="Box")
    bpy.ops.mesh.select_all(action="DESELECT")
    import bmesh

    bm = bmesh.from_edit_mesh(box.data)
    bm.faces.ensure_lookup_table()
    bm.faces[0].select = True
    bmesh.update_edit_mesh(box.data)
    text = ok("assign_material", target="Box", material="material:Blue", all_faces=False)
    assert "1 selected face" in text, text
    ok("set_mode", mode="OBJECT")
    assert sorted(p.material_index for p in box.data.polygons) == [0, 0, 0, 0, 0, 1]
    ok("assign_material", target="Box", material="material:Blue")
    assert box.material_slots[0].material == blue and all(p.material_index == 0 for p in box.data.polygons)
    assert "Did you mean: Red" in err("assign_material", target="Box", material="material:Rde")


run_checks(globals())
