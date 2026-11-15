"""Shared helpers for the agent's headless checks — tests/agent_*.py, each run by `tests/run.sh <script>`.

    from agent_common import call, ok, err, fresh, run_checks, PACKAGE, module

A check script defines test_* functions and ends with run_checks(globals()). Everything runs inside the
installed add-on in background Blender: no window, no network.
"""

import importlib
import os
import sys
import traceback

import bpy

sys.path.insert(0, os.path.dirname(__file__))

PACKAGE = next(name for name in bpy.context.preferences.addons.keys() if name.endswith("zetta3d"))


def module(name):
    """A module of the installed add-on: module("agent.targets")."""
    return importlib.import_module(f"{PACKAGE}.{name}")


tools = module("agent.tools")


def call(name, /, **args):
    """Runs one client tool to the end and returns its Result. A LATER tool must finish by itself here
    (background Blender runs no timers) — test its parts directly instead."""
    box = {}
    ctx = tools.Context({"toolUseId": "toolu_test", "name": name, "input": args, "effect": "write"},
                        lambda result: box.setdefault("result", result))
    tools.run(ctx.call, ctx)
    assert "result" in box, f"{name} did not finish (a LATER tool?)"
    return box["result"]


def ok(name, /, **args):
    """The tool's text, asserting it succeeded."""
    result = call(name, **args)
    assert not result.error, f"{name} failed: {result.text}"
    return result.text


def err(name, /, **args):
    """The tool's error line, asserting it failed."""
    result = call(name, **args)
    assert result.error, f"{name} should have failed, but said: {result.text}"
    return result.text


def fresh():
    """An empty file — no objects, no extra data — keeping the add-on loaded."""
    bpy.ops.wm.read_homefile(use_empty=True)
    for collection in (bpy.data.objects, bpy.data.meshes, bpy.data.materials, bpy.data.node_groups,
                       bpy.data.images, bpy.data.collections, bpy.data.texts, bpy.data.actions,
                       bpy.data.cameras, bpy.data.lights, bpy.data.curves, bpy.data.armatures):
        for block in list(collection):
            collection.remove(block)


def run_checks(namespace):
    failures = []
    print(f"Blender {bpy.app.version_string}")
    for name, fn in list(namespace.items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok   {name}")
            except Exception:  # noqa: BLE001
                failures.append(name)
                print(f"  FAIL {name}")
                traceback.print_exc()
    print("all passed" if not failures else f"{len(failures)} failed")
    sys.exit(1 if failures else 0)
