"""The agent's foundation: targets, values, the registry, the changes record."""

import os
import sys

import bpy

sys.path.insert(0, os.path.dirname(__file__))
from agent_common import call, fresh, module, run_checks

targets = module("agent.targets")
values = module("agent.values")
tools = module("agent.tools")
ToolError = tools.ToolError


def test_targets_resolve_and_suggest():
    fresh()
    bpy.ops.mesh.primitive_cube_add()
    cube = bpy.context.active_object
    cube.name = "Chair"
    cube.modifiers.new("Bevel", "BEVEL")
    material = bpy.data.materials.new("Oak")
    assert targets.datablock("Chair") is cube
    assert targets.datablock("material:Oak") is material
    owner, bevel = targets.part("Chair/modifiers/Bevel")
    assert owner is cube and bevel.type == "BEVEL"
    assert targets.name_of(material) == "material:Oak"
    try:
        targets.datablock("Chiar")
    except ToolError as error:
        assert "Did you mean: Chair" in str(error), error
    else:
        raise AssertionError("a wrong name should fail")


def test_values_checked_against_rna():
    fresh()
    bpy.ops.mesh.primitive_cube_add()
    cube = bpy.context.active_object
    cube.modifiers.new("Bevel", "BEVEL")
    assert abs(values.assign(cube, 'modifiers["Bevel"].width', 0.05) - 0.05) < 1e-6
    assert values.assign(cube, 'modifiers["Bevel"].limit_method', "angle") == "ANGLE"
    values.assign(cube, "location[2]", 1.5)
    assert abs(cube.location.z - 1.5) < 1e-6
    values.assign(cube, '["note"]', "hello")
    assert cube["note"] == "hello"
    light = bpy.data.lights.new("Key", "POINT")
    rgb = values.assign(light, "color", "#ff8000")
    assert abs(rgb[0] - 1.0) < 1e-4 and 0.2 < rgb[1] < 0.25, list(rgb)  # linear from sRGB
    for path, value, words in [
        ('modifiers["Bevel"].segments', -3, "between"),
        ('modifiers["Bevel"].limit_method', "SIDEWAYS", "SIDEWAYS"),  # Blender's own error names it
        ("locaton", 1, "Did you mean: location"),
    ]:
        try:
            values.assign(cube, path, value)
        except ToolError as error:
            assert words in str(error), (path, error)
        else:
            raise AssertionError(f"{path} = {value} should fail")


def test_dynamic_enums_take_their_runtime_values():
    """render.engine lists only its fixed items in RNA; CYCLES is still a value Blender takes."""
    fresh()
    assert values.assign(bpy.context.scene, "render.engine", "CYCLES") == "CYCLES"


def test_paths_stay_in_the_blend_folder_even_through_a_symlink():
    import tempfile

    fresh()
    folder = tempfile.mkdtemp()
    outside = tempfile.mkdtemp()
    bpy.ops.wm.save_as_mainfile(filepath=os.path.join(folder, "t.blend"))
    os.symlink(outside, os.path.join(folder, "escape"))
    assert targets.file_path("//textures/a.png").startswith(os.path.realpath(folder)) or \
        targets.file_path("//textures/a.png").startswith(folder)
    for bad in ("//../x.png", "//escape/x.png", "/etc/hosts"):
        try:
            targets.file_path(bad)
        except ToolError:
            continue
        raise AssertionError(f"{bad} should be refused")


# Every client tool the server offers Blender (BlenderAgentProtocol.md v1.0, blender-agent-tools.ts).
PROTOCOL_TOOLS = """
blender_info scene_outline inspect inspect_nodes find get_selection read_text search_text recent_errors
capture_view api_lookup viewport ui_navigate ask_user list_generations list_images list_pipelines get_pipeline
pipeline_run_status create_object modify_object set_properties add_modifier add_constraint edit_nodes
create_material assign_material keyframes collection_op select set_mode mesh_op import_file export_file
write_text edit_text render file_op undo save_image import_generation build_pipeline edit_pipeline
manage_pipeline_run import_pipeline_results generate_image_with_references edit_image compose_images delete
purge_unused open_file run_python generate_3d_model process_model run_pipeline
""".split()


def test_every_protocol_tool_is_implemented():
    module("agent.runtime")  # ask_user lives with the conversation
    missing = [name for name in PROTOCOL_TOOLS if tools.spec(name) is None]
    assert not missing, f"the server offers tools the add-on does not have: {missing}"
    assert len(PROTOCOL_TOOLS) == 55


def test_unknown_tool_says_update():
    result = call("no_such_tool")
    assert result.error and "update the add-on" in result.text


run_checks(globals())
