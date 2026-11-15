"""The agent's file tools, run_python and its guard: import_file, export_file, write_text, edit_text, file_op,
open_file, run_python, agent.guard."""

import os
import sys
import tempfile

import bpy

sys.path.insert(0, os.path.dirname(__file__))
from agent_common import call, err, fresh, module, ok, run_checks

files = module("agent.tools.files")
python = module("agent.tools.python")
guard = module("agent.guard")
changes = module("agent.changes")
errors = module("agent.errors")


def saved():
    """An empty file saved into a new temporary folder, so "//" paths work; a new changes turn."""
    fresh()
    folder = tempfile.mkdtemp(prefix="zetta-files-")
    bpy.ops.wm.save_as_mainfile(filepath=os.path.join(folder, "scene.blend"))
    changes.begin_turn("test")
    return folder


def cube(name, location=(0, 0, 0)):
    bpy.ops.mesh.primitive_cube_add(size=1, location=location)
    obj = bpy.context.active_object
    obj.name = name
    return obj


def file_lines():
    return [line for line in changes.current()["lines"] if line.get("kind") == "file"]


# ─── import_file / export_file ───────────────────────────────────────────────────────────────────


def test_glb_round_trip_keeps_selection():
    folder = saved()
    crate, ball = cube("Crate"), cube("Ball", (3, 0, 0))
    for obj in bpy.context.view_layer.objects:
        obj.select_set(obj is crate)
    text = ok("export_file", path="//export/crate", format="glb")
    path = os.path.join(folder, "export", "crate.glb")
    assert os.path.isfile(path) and "Crate" in text and "Ball" not in text and "the selection" in text, text
    assert crate.select_get() and not ball.select_get(), "the selection was not restored"
    assert any(line["path"] == path for line in file_lines()), "the export was not recorded"

    text = ok("import_file", path="//export/crate.glb")
    assert 'collection "crate"' in text and "MESH" in text and "Overall size" in text, text
    collection = bpy.data.collections["crate"]
    assert len(collection.all_objects) >= 1
    assert all(o.name in collection.all_objects for o in bpy.data.objects if o not in (crate, ball))

    text = ok("export_file", path="//export/ball.glb", format="glb", targets=["Ball"])
    assert "Ball" in text and "Crate" not in text, text


def test_every_format_exports_and_imports():
    saved()
    cube("Box")
    for fmt in ("fbx", "obj", "stl", "ply", "usd", "abc"):
        ext = ".usdc" if fmt == "usd" else f".{fmt}"
        ok("export_file", path=f"//out/box{ext}", format=fmt, targets=["Box"])
        before = len(bpy.data.objects)
        text = ok("import_file", path=f"//out/box{ext}", collection=f"from {fmt}")
        assert len(bpy.data.objects) > before, (fmt, text)
        assert bpy.data.collections.get(f"from {fmt}") is not None, (fmt, text)


def test_import_svg():
    saved()
    ok("write_text", target="//art/logo.svg", content=(
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
        '<rect x="1" y="1" width="8" height="8"/></svg>'))
    text = ok("import_file", path="//art/logo.svg")
    assert "CURVE" in text, text


def test_import_and_export_errors():
    folder = saved()
    assert "There is no file" in err("import_file", path="//missing.glb")
    with open(os.path.join(folder, "notes.xyz"), "w") as handle:
        handle.write("x")
    assert "format" in err("import_file", path="//notes.xyz")
    cube("Box")
    assert "has no setting" in err("export_file", path="//a.glb", format="glb", options={"use_selectoin": True})
    assert "ends in" in err("export_file", path="//a.fbx", format="glb")
    assert "There is no object" in err("export_file", path="//a.glb", format="glb", targets=["Nope"])


def test_path_escapes_refused():
    saved()
    cube("Box")
    for tool, args in [
        ("write_text", {"target": "//../escape.txt", "content": "x"}),
        ("write_text", {"target": "/tmp/escape.txt", "content": "x"}),
        ("export_file", {"path": "//../../escape.glb", "format": "glb"}),
        ("export_file", {"path": "/tmp/escape.glb", "format": "glb"}),
        ("import_file", {"path": "//../../../../etc/hosts"}),
        ("file_op", {"action": "save_copy", "path": "/tmp/escape.blend"}),
        ("edit_text", {"target": "//../escape.txt", "edits": [{"find": "a", "replace": "b"}]}),
    ]:
        text = err(tool, **args)
        assert "outside the .blend's folder" in text or "starting \"//\"" in text, (tool, args, text)


def test_unsaved_file_has_no_folder():
    fresh()
    assert "saved first" in err("write_text", target="//a.txt", content="x")
    assert "never been saved" in err("file_op", action="save")


# ─── write_text / edit_text ──────────────────────────────────────────────────────────────────────


def test_text_datablock_write_and_edit():
    saved()
    text = ok("write_text", target="text:notes.py", content="a = 1\nb = 2\nb = 2\n")
    assert "Created text:notes.py (3 lines)" in text, text
    assert "already exists" in err("write_text", target="text:notes.py", content="x")
    ok("write_text", target="text:notes.py", content="a = 1\nb = 2\nc = 2\n", overwrite=True)
    assert bpy.data.texts["notes.py"].as_string() == "a = 1\nb = 2\nc = 2\n"

    text = ok("edit_text", target="text:notes.py", edits=[{"find": "a = 1", "replace": "a = 10"}])
    assert "line 1" in text, text
    assert "is not in" in err("edit_text", target="text:notes.py", edits=[{"find": "zzz", "replace": "y"}])
    # The second edit fails, so the first must not apply either.
    assert "appears 2 times" in err("edit_text", target="text:notes.py", edits=[
        {"find": "a = 10", "replace": "a = 11"}, {"find": "= 2", "replace": "= 3"}])
    assert bpy.data.texts["notes.py"].as_string() == "a = 10\nb = 2\nc = 2\n"
    ok("edit_text", target="text:notes.py", edits=[{"find": "= 2", "replace": "= 3", "all": True}])
    assert bpy.data.texts["notes.py"].as_string() == "a = 10\nb = 3\nc = 3\n"
    assert "There is no text:nots.py" in err("edit_text", target="text:nots.py",
                                             edits=[{"find": "a", "replace": "b"}])


def test_file_write_is_recorded_and_undone():
    folder = saved()
    path = os.path.join(folder, "scripts", "tool.py")
    text = ok("write_text", target="//scripts/tool.py", content="print('one')\n")
    assert "Created //scripts/tool.py" in text, text
    assert "already exists" in err("write_text", target="//scripts/tool.py", content="x")

    changes.begin_turn("second message")
    ok("edit_text", target="//scripts/tool.py", edits=[{"find": "one", "replace": "two"}])
    ok("write_text", target="//scripts/tool.py", content="print('three')\n", overwrite=True)
    lines = file_lines()
    assert len(lines) == 1 and lines[0]["path"] == path, lines  # backed up once, before the first change
    with open(lines[0]["backup"]) as handle:
        assert handle.read() == "print('one')\n"
    assert changes.undo_file(lines[0]) is None
    with open(path) as handle:
        assert handle.read() == "print('one')\n"


# ─── file_op / open_file ─────────────────────────────────────────────────────────────────────────


def test_save_and_save_copy():
    folder = saved()
    cube("Box")
    text = ok("file_op", action="save")
    assert "Saved scene.blend" in text, text
    assert any(line.get("kind") == "note" for line in changes.current()["lines"])
    text = ok("file_op", action="save_copy", path="//versions/v2")
    assert os.path.isfile(os.path.join(folder, "versions", "v2.blend")), text
    assert bpy.data.filepath == os.path.join(folder, "scene.blend")
    assert "use save" in err("file_op", action="save_copy", path="//scene.blend")


def test_append_and_link():
    folder = saved()
    chair = cube("Chair")
    oak = bpy.data.materials.new("Oak")
    oak.use_fake_user = True
    props = bpy.data.collections.new("Props")
    bpy.context.scene.collection.children.link(props)
    box = cube("Box")
    for owner in list(box.users_collection):
        owner.objects.unlink(box)
    props.objects.link(box)
    ok("file_op", action="save_copy", path="//library.blend")
    for obj in (chair, box):
        bpy.data.objects.remove(obj)
    bpy.data.collections.remove(props)
    bpy.data.materials.remove(oak)

    text = ok("file_op", action="append", path="//library.blend",
              items=["object:Chair", "material:Oak", "collection:Props"])
    assert "Appended from //library.blend" in text, text
    scene_objects = set(bpy.context.scene.objects.keys())
    assert {"Chair", "Box"} <= scene_objects, scene_objects
    assert bpy.data.materials["Oak"].library is None

    text = err("file_op", action="link", path="//library.blend", items=["material:Oka", "object:Chiar"])
    assert "did you mean Oak" in text and "did you mean Chair" in text and "Nothing was linked" in text, text
    text = ok("file_op", action="link", path="//library.blend", items=["material:Oak"])
    assert any(m.library is not None for m in bpy.data.materials), text
    assert "can't be edited here" in text


def test_open_file_revert_new_open():
    folder = saved()
    cube("Kept")
    bpy.ops.wm.save_mainfile()
    cube("Lost")
    text = ok("open_file", action="revert")
    assert "Kept" in bpy.data.objects and "Lost" not in bpy.data.objects
    assert "turn ends" in text, text
    path = bpy.data.filepath
    ok("open_file", action="new")
    assert bpy.data.filepath == ""
    bpy.ops.wm.save_as_mainfile(filepath=os.path.join(folder, "other.blend"))
    text = ok("open_file", action="open", path="//scene.blend")
    assert bpy.data.filepath == path and "Kept" in bpy.data.objects, text


# ─── The guard ───────────────────────────────────────────────────────────────────────────────────

REFUSED = [  # (code, words the refusal must contain)
    ("import subprocess\nsubprocess.run(['ls'])", 'programs'),
    ("import os\nos.system('ls')", 'programs'),
    ("import os as o\no.system('ls')", 'programs'),
    ("from os import system as s\ns('ls')", 'programs'),
    ("from subprocess import *\nrun(['ls'])", 'programs'),
    ("from os import *\nsystem('ls')", 'built from strings'),
    ("import os\nos.popen('ls').read()", 'programs'),
    ("import os\nos.execv('/bin/sh', ['sh'])", 'programs'),
    ("import os\nos.spawnl(os.P_WAIT, '/bin/ls')", 'programs'),
    ('import os\npid = os.fork()', 'programs'),
    ('import pty', 'programs'),
    ('import multiprocessing', 'programs'),
    ("import os\no = os\no.system('x')", 'programs'),
    ("import os\ndef f(m):\n    return m.remove('x')\nf(os)", 'built from strings'),
    ("def f(m):\n    m.system('x')", 'programs'),
    ('import socket', 'network'),
    ("import urllib.request\nurllib.request.urlopen('http://x')", 'network'),
    ('from urllib import request', 'network'),
    ('import http.client', 'network'),
    ('import ssl', 'network'),
    ('import requests', 'network'),
    ('import ftplib', 'network'),
    ('import smtplib', 'network'),
    ('import asyncio', 'network'),
    ("bpy.ops.wm.url_open(url='http://x')", 'network'),
    ('import ctypes', 'native'),
    ('from ctypes import CDLL', 'native'),
    ('import cffi', 'native'),
    ("import shutil\nshutil.rmtree(bpy.path.abspath('//x'))", 'deleting'),
    ("import shutil\nshutil.move('a', 'b')", 'deleting'),
    ("import os\nos.remove('x')", 'deleting'),
    ("import os\nos.unlink('x')", 'deleting'),
    ("import os\nos.rmdir('x')", 'deleting'),
    ("import os\nos.removedirs('x')", 'deleting'),
    ("from pathlib import Path\nPath('//x').unlink()", 'deleting'),
    ("from pathlib import Path\nPath('//x').unlink(missing_ok=True)", 'deleting'),
    ("from pathlib import Path\nPath('//x').rmdir()", 'deleting'),
    ("import os\nos.rename('a', 'b')", 'deleting'),
    ('bpy.ops.wm.quit_blender()', 'Blender itself'),
    ('ops = bpy.ops\nops.wm.quit_blender()', 'Blender itself'),
    ("import addon_utils\naddon_utils.enable('x')", 'Blender itself'),
    ("bpy.ops.preferences.addon_enable(module='x')", 'Blender itself'),
    ('bpy.ops.extensions.package_install()', 'Blender itself'),
    ('bpy.context.preferences.view.ui_scale = 2', 'Blender itself'),
    ('C.preferences.view.ui_scale = 2', 'Blender itself'),
    ('prefs = bpy.context.preferences\nprefs.filepaths.use_scripts_auto_execute = True', 'Blender itself'),
    ('p = bpy.context.preferences.view\np.ui_scale += 1', 'Blender itself'),
    ("setattr(bpy.context.preferences.view, 'ui_scale', 2)", 'Blender itself'),
    ('def execute(self, context):\n    context.preferences.system.memory_cache_limit = 1', 'Blender itself'),
    ('import sys\nsys.exit()', 'Blender itself'),
    ('exit()', 'Blender itself'),
    ('bpy.ops.wm.save_userpref()', 'Blender itself'),
    ("x = bpy.context.preferences.addons['bl_ext.user_default.zetta3d'].preferences", 'private'),
    ("open('/Users/me/.config/zetta3d-blender/session.json').read()", 'private'),
    ("p = bpy.utils.extension_path_user('bl_ext.user_default.zetta3d')", 'private'),
    ("import os\np = os.path.join(bpy.utils.resource_path('USER'), 'extensions', '.user', 'user_default')", 'private'),
    ('from bl_ext.user_default.zetta3d.core import state', 'private'),
    ('import keyring', 'private'),
    ('bpy.ops.zetta.sign_out()', 'private'),
    ("exec('print(1)')", 'built from strings'),
    ("eval('1+1')", 'built from strings'),
    ("compile('1', 'x', 'eval')", 'built from strings'),
    ("__import__('os').system('ls')", 'not allowed'),
    ("__import__('o' + 's')", 'built from strings'),
    ("import importlib\nimportlib.import_module('os')", 'built from strings'),
    ("import os\ngetattr(os, 'sys' + 'tem')('ls')", 'built from strings'),
    ("import os\ngetattr(os, 'system')('ls')", 'programs'),
    ('import os\nf = getattr(os, name)', 'built from strings'),
    ("__builtins__['eval']('1')", 'built from strings'),
    ("globals()['__builtins__']", 'built from strings'),
    ('g = globals()', 'built from strings'),
    ("import os\nos.__dict__['system']('ls')", 'built from strings'),
    ("import os\nvars(os)['system']('ls')", 'built from strings'),
    ('().__class__.__base__.__subclasses__()', 'built from strings'),
    ('f = lambda: 0\nf.__globals__', 'built from strings'),
    ("import sys\nm = sys.modules['os']", 'built from strings'),
    ('import sys\nsys._getframe().f_globals', 'built from strings'),
    ('import pickle', 'built from strings'),
    ('import marshal', 'built from strings'),
    ('import runpy', 'built from strings'),
    ("import builtins\nbuiltins.exec('1')", 'built from strings'),
    ("bpy.data.texts['x'].as_module()", 'built from strings'),
    ('bpy.ops.text.run_script()', 'built from strings'),
    ('print.__self__', 'built from strings'),
    ("'{0.__globals__}'.format(f)", 'built from strings'),
    ('import gc\ngc.get_objects()', 'built from strings'),
    ('fc = obj.driver_add(\'location\', 0)\nfc.driver.expression = "__import__(\'os\').system(\'x\')"', 'driver expression'),
    ("open('/tmp/x.txt', 'w').write('x')", 'writing files'),
    ("open('notes.txt', 'w')", 'writing files'),
    ("open(bpy.path.abspath('//../x.txt'), 'w')", 'writing files'),
    ("with open('~/x', 'a') as f:\n    pass", 'writing files'),
    ("open('C:\\\\x.txt', mode='w')", 'writing files'),
    ("from pathlib import Path\nPath('/tmp/x').write_text('x')", 'writing files'),
    ("from pathlib import Path\np = Path(somewhere)\np.write_text('x')", 'writing files'),
    ("import shutil\nshutil.copy(bpy.path.abspath('//a'), '/tmp/b')", 'writing files'),
    ("import io\nio.open('/tmp/x', 'w')", 'writing files'),
    ("import os\nfd = os.open('/tmp/x', os.O_WRONLY)", 'writing files'),
    ("bpy.ops.export_scene.gltf(filepath='/tmp/x.glb')", 'writing files'),
    ("bpy.context.scene.render.filepath = '/tmp/render'", 'writing files'),
    ("import zipfile\nzipfile.ZipFile('/tmp/x.zip', 'w')", 'writing files'),
    ("import tarfile\ntarfile.open(bpy.path.abspath('//x.tar')).extractall()", 'writing files'),
    ("bpy.ops.wm.open_mainfile(filepath='//x.blend')", 'save'),
    ("bpy.ops.wm.save_as_mainfile(filepath='//x.blend')", 'save'),
    ("bpy.ops.wm.append(filepath='x')", 'save'),
    ("input('name?')", 'freeze'),
    ('breakpoint()', 'freeze'),
    ('o = __import__', 'built from strings'),
    ('import os\nf = os.system', 'programs'),
    ("getattr(__builtins__, 'eval')", 'built from strings'),
    ('[].__class__.__mro__[-1].__subclasses__()', 'built from strings'),
    ('import os\nt = type(os)', 'built from strings'),
    ("x = bpy.ops\ngetattr(x.wm, 'quit_' + 'blender')()", 'built from strings'),
    ('q = bpy.ops.wm.quit_blender', 'Blender itself'),
    ("import os.path as p\np.os.remove('x')", 'built from strings'),
    ("import tempfile\ntempfile._os.remove('x')", 'built from strings'),
    ("import shutil\nshutil.os.unlink('x')", 'built from strings'),
    ('from bpy import ops\nops.wm.quit_blender()', 'Blender itself'),
    ('from bpy.ops import preferences', 'Blender itself'),
    ("import os\nfor f in [os.remove]:\n    f('x')", 'deleting'),
    ("img = bpy.data.images['x']\nbpy.data.images['x'].save_render(filepath='/tmp/x.png')", 'writing files'),
    ("import operator\noperator.attrgetter('__globals__')", 'built from strings'),
    ("import sys as s\ns.modules['os']", 'built from strings'),
    ('import os\nos.kill(os.getpid(), 9)', 'programs'),
    ('import webbrowser', 'programs'),
    ('exec_ = exec', 'built from strings'),
]

ALLOWED = [
    'bpy.ops.mesh.primitive_cube_add(size=2, location=(0, 0, 1))',
    'obj = bpy.context.active_object\nobj.location.z += 1\nobj.rotation_euler = Euler((0, 0, math.radians(45)))',
    "for obj in bpy.data.objects:\n    if obj.type == 'MESH':\n        obj.name = obj.name.replace('Cube', 'Box')",
    'me = bpy.context.object.data\nbm = bmesh.new()\nbm.from_mesh(me)\nbmesh.ops.subdivide_edges(bm, edges=bm.edges, cuts=1)\nbm.to_mesh(me)\nbm.free()',
    'bm = bmesh.from_edit_mesh(C.object.data)\nfor v in bm.verts:\n    v.co *= 1.1\nbmesh.update_edit_mesh(C.object.data)',
    "coll = bpy.data.collections.new('Rocks')\nbpy.context.scene.collection.children.link(coll)\nfor o in list(coll.objects):\n    coll.objects.unlink(o)",
    "bpy.data.objects.remove(bpy.data.objects['Cube'], do_unlink=True)",
    "mat = bpy.data.materials.new('Oak')\nmat.use_nodes = True\nbsdf = mat.node_tree.nodes['Principled BSDF']\nbsdf.inputs['Base Color'].default_value = (0.5, 0.3, 0.1, 1)",
    'for socket in node.inputs:\n    print(socket.name, socket.default_value)',
    "import os\nfolder = bpy.path.abspath('//textures')\nfor name in sorted(os.listdir(folder)):\n    print(os.path.join(folder, name))",
    "import os\nprint(os.path.exists(bpy.path.abspath('//x.png')))",
    "with open(bpy.path.abspath('//notes.txt'), 'w') as f:\n    f.write('hello')",
    "with open('//notes.txt', 'w') as f:\n    f.write('hello')",
    "data = open(bpy.path.abspath('//data.json')).read()",
    "import json\nprint(json.dumps({'a': 1}))",
    'from mathutils import Vector, Matrix\nm = Matrix.Translation(Vector((1, 2, 3)))\nm @ Vector((0, 0, 0))',
    'import random\nrandom.seed(4)\n[random.random() for _ in range(3)]',
    "fc = obj.driver_add('location', 2)\nfc.driver.type = 'SCRIPTED'\nfc.driver.expression = 'frame * 0.1'",
    'def handler(scene):\n    print(scene.frame_current)\nbpy.app.handlers.frame_change_pre.append(handler)',
    "class OBJECT_OT_hello(bpy.types.Operator):\n    bl_idname = 'object.hello'\n    bl_label = 'Hello'\n    def execute(self, context):\n        self.report({'INFO'}, 'hi')\n        return {'FINISHED'}\nbpy.utils.register_class(OBJECT_OT_hello)",
    "getattr(obj, 'location')",
    "for prop in ('location', 'scale'):\n    print(getattr(obj, prop))",
    'setattr(mod, name, value)',
    'print(bpy.context.preferences.view.ui_scale)',
    "C.scene.render.engine = 'CYCLES'\nC.scene.cycles.samples = 64",
    "bpy.context.scene.render.filepath = '//renders/shot_'",
    "bpy.ops.export_scene.gltf(filepath=bpy.path.abspath('//export/a.glb'), use_selection=True)",
    'import numpy as np\nprint(np.zeros(3))',
    'import sys\nprint(sys.version)',
    "text = bpy.data.texts.new('notes')\ntext.write('hello')",
    "s = 'a_b'.replace('_', ' ')",
    "obj.rename('Box')",
    'print(type(obj).__name__, obj.__class__.__name__)',
    "import re\npattern = re.compile(r'\\\\d+')\nprint(pattern.findall('a1b22'))",
    "with bpy.context.temp_override(active_object=obj):\n    bpy.ops.object.modifier_apply(modifier='Bevel')",
    'from collections import defaultdict\nd = defaultdict(list)',
    "import os\np = os.environ.get('HOME')",
    'import tempfile\nprint(tempfile.gettempdir())',
    "img = bpy.data.images.load(bpy.path.abspath('//tex.png'))\nimg.filepath_raw = '//tex2.png'\nimg.save()",
    "if __name__ == '__main__':\n    print('main')",
    'for input in node.inputs:\n    print(input.name)',
    'vars(obj)',
    'vars()',
    "import os.path\nos.path.basename('x')",
    'items = [o.name for o in bpy.context.selected_objects]\nitems',
    "from pathlib import Path\nprint(Path(bpy.path.abspath('//')).name)\nPath(bpy.path.abspath('//out.txt')).write_text('x')",
    "import shutil\nshutil.copy(bpy.path.abspath('//a.png'), bpy.path.abspath('//b.png'))",
    "mod = obj.modifiers.new('Bevel', 'BEVEL')\nmod.width = 0.02",
    'obj.select_set(True)\nbpy.context.view_layer.objects.active = obj',
    'import bpy\nimport bmesh\nfrom bpy.props import FloatProperty',
    "import os\nprint(os.path.join('a', 'b'), os.sep, os.getcwd())",
    'import tempfile\nprint(tempfile.gettempdir())',
    "obj.data.vertices.foreach_get('co', buf)",
]


def test_guard_refuses():
    for code, words in REFUSED:
        reason = guard.check(code)
        assert reason and words in reason, (code, reason)
        assert len(reason.splitlines()) == 1, reason


def test_guard_allows_ordinary_code():
    for code in ALLOWED:
        assert guard.check(code) is None, (code, guard.check(code))


def test_guard_refuses_the_addons_folder():
    from importlib import import_module

    state = import_module(f"{guard.__name__.rsplit('.', 2)[0]}.core.state")
    reason = guard.check(f"print(open({os.path.join(state.folder(), 'session.json')!r}).read())")
    assert reason and "private" in reason, reason


# ─── run_python ──────────────────────────────────────────────────────────────────────────────────


def test_run_python_output_value_and_changes():
    saved()
    terrain = cube("Terrain")
    text = ok("run_python", purpose="Scatter two rocks", code=(
        "for i in range(2):\n"
        "    bpy.ops.mesh.primitive_ico_sphere_add(location=(i, 0, 0))\n"
        "    C.active_object.name = 'Rock'\n"
        "print('placed', len(D.objects))\n"
        "T = D.objects['Terrain']\n"
        "T.location.z = 2\n"
        "T.modifiers.new('Bevel', 'BEVEL')\n"
        "[o.name for o in D.objects if o.name.startswith('Rock')]"))
    assert "placed 3" in text, text
    assert "Value: ['Rock', 'Rock.001']" in text, text
    assert "2 objects added (Rock, Rock.001)" in text, text
    assert "1 edited (Terrain: transform, modifiers)" in text, text
    assert terrain.location.z == 2
    result = call("run_python", code="1 + 1", purpose="Add numbers.")
    assert result.label == "Add numbers" and "Value: 2" in result.text and "no objects" in result.text, result.text


def test_run_python_traceback_shows_only_the_code():
    saved()
    result = call("run_python", purpose="Fail", code="x = 1\nbpy.data.objects.new('Half', None)\ny = missing + 1\n")
    assert result.error, result.text
    assert 'line 3' in result.text and "NameError" in result.text, result.text
    assert "zetta3d" not in result.text and "python.py" not in result.text, result.text
    assert "1 object added (Half)" in result.text and "before the error" in result.text, result.text
    assert errors.recent(1)[0]["source"] == "run_python"
    result = call("run_python", purpose="Bad syntax", code="def (:\n")
    assert result.error and "SyntaxError" in result.text, result.text
    result = call("run_python", purpose="Quit", code="raise SystemExit(1)")
    assert result.error and "SystemExit" in result.text, result.text


def test_run_python_refuses():
    saved()
    text = err("run_python", purpose="List files", code="import subprocess\nsubprocess.run(['ls'])")
    assert text.startswith("Refused by the add-on's safety rules: running other programs"), text


def test_run_python_runtime_guards():
    folder = saved()
    # Paths the guard can't read in the code are checked when the code opens them.
    result = call("run_python", purpose="Write outside", code=(
        "import os, tempfile\n"
        "with open(os.path.join(tempfile.gettempdir(), 'zetta-outside.txt'), 'w') as f:\n"
        "    f.write('x')\n"))
    assert result.error and "Refused by the add-on's safety rules: writing files" in result.text, result.text
    assert "guard.Refused" not in result.text, result.text
    # The code's own builtins: no exec/eval, and imports and getattr checked by name while it runs.
    safe = python._builtins()
    assert "exec" not in safe and "eval" not in safe and "compile" not in safe and "input" not in safe
    for attempt in (lambda: safe["__import__"]("subprocess"), lambda: safe["__import__"]("os", None, None, ["system"]),
                    lambda: safe["getattr"](os, "system"), lambda: safe["getattr"](os.path, "os"),
                    lambda: safe["getattr"](test_run_python_runtime_guards, "__globals__"),
                    lambda: safe["vars"](os), lambda: safe["setattr"](bpy.context.preferences.view, "ui_scale", 2)):
        try:
            attempt()
        except guard.Refused as refusal:
            assert str(refusal).startswith("Refused by the add-on's safety rules:"), refusal
        else:
            raise AssertionError("a run-time check let it through")
    assert safe["__import__"]("json").dumps([1]) == "[1]"

    text = ok("run_python", purpose="Write notes", code=(
        "import os\n"
        "os.makedirs(bpy.path.abspath('//notes'), exist_ok=True)\n"
        "with open('//notes/today.txt', 'w') as f:\n"
        "    f.write('hello')\n"
        "open(bpy.path.abspath('//notes/today.txt')).read()"))
    assert "Value: 'hello'" in text, text
    assert any(line["path"] == os.path.join(folder, "notes", "today.txt") for line in file_lines())


def test_run_python_in_edit_mode():
    saved()
    obj = cube("Box")
    bpy.ops.object.mode_set(mode="EDIT")
    text = ok("run_python", purpose="Subdivide", code=(
        "bm = bmesh.from_edit_mesh(C.object.data)\n"
        "bmesh.ops.subdivide_edges(bm, edges=bm.edges[:], cuts=1, use_grid_fill=True)\n"
        "len(C.object.data.vertices)"))
    assert "vertices 8→26" in text, text
    assert bpy.context.mode == "EDIT_MESH"
    bpy.ops.object.mode_set(mode="OBJECT")
    assert len(obj.data.vertices) == 26


run_checks(globals())
