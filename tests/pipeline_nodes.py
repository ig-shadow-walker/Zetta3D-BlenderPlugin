"""3D Pipelines as nodes (ui/pipeline_nodes.py): the canvas follows the recipe, and what is done on the canvas
becomes recipe edits checked by the same rules.

    tests/run.sh tests/pipeline_nodes.py

Headless: nothing is saved to zetta3d.ai (saving is switched off), and edits are made on the tree the way the
node editor makes them, then read back as the editor's timer would.
"""

import os
import sys

import bpy

sys.path.insert(0, os.path.dirname(__file__))
from agent_common import module, run_checks  # noqa: E402

g = module("pipelines.graph")
pipes = module("pipelines.state")
tasks = module("core.tasks")
nodes = module("ui.pipeline_nodes")
panels = module("ui.panels")

tasks.run = lambda *args, **kwargs: None  # no requests
pipes.schedule_save = lambda: None


def _open(graph=None):
    if graph is None:
        graph = g.empty()
        graph, seed = g.add_step(graph, "seed_picture")
        graph, gen = g.add_step(graph, "generate_3d", after=seed)
        graph, _retopo = g.add_step(graph, "retopology", after=gen)
    pipes.editor.update(pipeline={"id": 9, "title": "Hero prop"}, graph=graph, selected=None, undo=[], redo=[],
                        dirty=False, error=None)
    pipes.run_view.update(run=None, selected=None)
    nodes.view.update(message=None, pending=False)
    nodes.refresh()
    return nodes.get_tree()


def _by_kind(tree, kind):
    return next(n for n in tree.nodes if getattr(n, "zetta_kind", "") == kind)


def _graph():
    return pipes.editor["graph"]


def test_canvas_shows_the_recipe():
    tree = _open()
    assert tree is not None and tree.bl_idname == nodes.TREE
    assert sorted(n.zetta_kind for n in tree.nodes) == ["generate_3d", "retopology", "seed_picture"]
    assert len(tree.links) == 2
    seed = _by_kind(tree, "seed_picture")
    assert [s.identifier for s in seed.outputs] == ["out"] and not seed.inputs
    gen = _by_kind(tree, "generate_3d")
    assert gen.inputs["Picture"].bl_idname == "ZettaPipeSocket_picture"
    assert gen.outputs["Model"].bl_idname == "ZettaPipeSocket_mesh"
    assert gen.use_custom_color, "steps are coloured by their area"
    n = g.node(_graph(), gen.step_id)
    assert tuple(gen.location) == (n["position"]["x"], -n["position"]["y"])


def test_adding_and_wiring_a_step():
    tree = _open()
    texture = tree.nodes.new("ZettaPipeNode_texture")
    nodes._apply_canvas()
    added = [n for n in _graph()["nodes"] if n["kind"] == "texture"]
    assert len(added) == 1 and texture.step_id == added[0]["id"], "the new node is a new step"
    retopo = _by_kind(tree, "retopology")
    tree.links.new(retopo.outputs[0], texture.inputs[0])
    nodes._apply_canvas()
    assert g.incoming_edge(_graph(), texture.step_id)["source"] == retopo.step_id, "the wire is an edge"
    assert pipes.editor["undo"], "an undoable recipe edit"


def test_a_wrong_wire_is_refused_with_the_reason():
    tree = _open()
    seed, retopo = _by_kind(tree, "seed_picture"), _by_kind(tree, "retopology")
    tree.links.new(seed.outputs[0], retopo.inputs[0])  # a picture into a step that takes a model
    nodes._apply_canvas()
    edge = g.incoming_edge(_graph(), retopo.step_id)
    assert edge["source"] != seed.step_id, "the recipe kept its own wire"
    assert nodes.view["message"], "and says why"
    assert not any(link.from_node == seed and link.to_node == retopo for link in tree.links), "the wire is gone"


def test_one_seed_only():
    tree = _open()
    tree.nodes.new("ZettaPipeNode_seed_model")
    nodes._apply_canvas()
    assert sum(1 for n in _graph()["nodes"] if g.is_seed(n["kind"])) == 1
    assert "one seed" in (nodes.view["message"] or ""), nodes.view["message"]
    assert sum(1 for n in tree.nodes if n.zetta_kind.startswith("seed_")) == 1, "the extra seed is taken away"


def test_duplicate_moves_and_delete():
    tree = _open()
    retopo = _by_kind(tree, "retopology")
    pipes.edit(lambda graph: g.patch_settings(graph, retopo.step_id, {"faces": 12000}))
    nodes.refresh()
    copy = tree.nodes.new("ZettaPipeNode_retopology")
    copy.copied_from, copy.step_id = retopo.step_id, ""
    nodes._apply_canvas()
    copies = [n for n in _graph()["nodes"] if n["kind"] == "retopology"]
    assert len(copies) == 2 and all(g.settings_of(n).get("faces") == 12000 for n in copies), copies

    copy.location = (900.0, -420.0)
    nodes._read_positions()
    assert g.node(_graph(), copy.step_id)["position"] == {"x": 900, "y": 420}

    tree.nodes.remove(copy)
    nodes._apply_canvas()
    assert sum(1 for n in _graph()["nodes"] if n["kind"] == "retopology") == 1, "deleting the node deletes the step"


def test_recipe_changes_redraw_the_canvas_and_undo():
    tree = _open()
    gen = _by_kind(tree, "generate_3d")
    pipes.edit(lambda graph: g.add_step(graph, "rigging", after=_by_kind(tree, "retopology").step_id)[0])
    nodes.refresh()
    assert any(n.zetta_kind == "rigging" for n in tree.nodes), "a step added from the sidebar shows up"
    pipes.undo()
    nodes.refresh()
    assert not any(n.zetta_kind == "rigging" for n in tree.nodes), "and Undo takes it away"
    assert gen.as_pointer() == _by_kind(tree, "generate_3d").as_pointer(), "the other nodes stay where they are"


def test_isolate_parts_has_one_output_per_part():
    graph = g.empty()
    graph, seed = g.add_step(graph, "seed_picture")
    graph, iso = g.add_step(graph, "isolate_parts", after=seed)
    graph = g.patch_settings(graph, iso, {"knownParts": [{"key": "sword", "label": "Sword"}]})
    tree = _open(graph)
    node = _by_kind(tree, "isolate_parts")
    assert [s.identifier for s in node.outputs] == ["character", "each", "part:sword"]
    assert node.outputs["Part: Sword"].bl_idname == "ZettaPipeSocket_picture"


def test_a_run_shows_on_the_canvas_and_is_not_edited():
    tree = _open()
    graph = _graph()
    seed, gen, retopo = (n["id"] for n in graph["nodes"])
    run = {"id": 31, "status": "running", "graph": graph, "steps": [
        {"id": 1, "nodeId": gen, "status": "success", "credits": 30},
        {"id": 2, "nodeId": retopo, "status": "failed", "credits": 0, "errorMessage": "The model has no faces."}]}
    pipes.run_view.update(run=run, selected=None)
    nodes.refresh()
    colours = {n.step_id: tuple(round(c, 3) for c in n.color) for n in tree.nodes}
    assert colours[gen] != colours[retopo], "done and failed look different"
    before = len(_graph()["nodes"])
    tree.nodes.new("ZettaPipeNode_rigging")
    nodes._apply_canvas()
    assert len(_graph()["nodes"]) == before and "run" in nodes.view["message"]
    assert not any(n.zetta_kind == "rigging" for n in tree.nodes)
    pipes.run_view.update(run=None, selected=None)


class Layout:
    """Takes any layout call — enough to run the drawing code headless."""

    def __getattr__(self, _name):
        return lambda *args, **kwargs: Layout()

    def __setattr__(self, _name, _value):
        pass


def test_drawing():
    tree = _open()
    icons = set(bpy.types.UILayout.bl_rna.functions["prop"].parameters["icon"].enum_items.keys())
    assert set(nodes.ICONS.values()) <= icons, set(nodes.ICONS.values()) - icons
    pipes.editor["selected"] = _by_kind(tree, "generate_3d").step_id
    for node in tree.nodes:
        node.draw_buttons(bpy.context, Layout())
        assert node.draw_label()
    nodes.draw_step_panel(bpy.context, Layout())
    panel = type("P", (), {"layout": Layout()})()
    nodes.ZETTA_PT_pipe_nodes.draw(panel, bpy.context)
    nodes.ZETTA_PT_pipe_nodes_runs.draw(panel, bpy.context)


def test_the_tree_is_not_saved_in_the_file():
    _open()
    assert nodes.get_tree() is not None
    nodes._save_pre()
    assert nodes.get_tree() is None
    nodes._save_post()
    nodes.refresh()
    assert nodes.get_tree() is not None, "and it comes back after the save"


run_checks(globals())
