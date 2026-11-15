"""The safety guard for run_python: reads the model's code with `ast` before it runs and refuses what an
assistant has no business doing, whatever the user's autonomy setting (BlenderAgent.md §3.5, decision D2).

WHY A GUARD AND NOT A SANDBOX. Blender's Python can't be sandboxed: bpy reaches the whole program, and the
code must be able to do real work (bmesh edits, drivers, batch renames). So the guard refuses a short list of
things by name — running programs, the network, native code, deleting files, changing Blender itself, the
add-on's private folder, code built from strings, writing outside the .blend's folder — and run_python
backs it up at run time (`agent.tools.python`): the code's own builtins have no exec/eval/compile, and their
`open`, `__import__` and `getattr` apply the same rules to whatever the code works out while it runs.

STRICT, BUT NOT ON ORDINARY CODE. Every rule names something no bpy/bmesh/mathutils script needs. Names that
plain Blender code also uses are refused only where they can't be Blender's: `collection.objects.unlink(obj)`
takes an object, `Path.unlink()` doesn't; `socket` is a node socket unless it was imported.

A refusal is one line — the rule, what was found and where, and what to do instead — which run_python
returns to the model as "Refused by the add-on's safety rules: <that line>", so it can rewrite the code.

Known gaps: code can read files anywhere but the add-on's folder, and a path to that folder worked out at run
time is caught by the run-time `open` but not by pathlib or shutil reads (the keys themselves are in the
system's credential store, which needs a subprocess or ctypes to reach); it can loop forever (the main thread
can't be interrupted from Python); a path in a variable is not checked inside bpy operators that write files
(an exporter given an absolute path); `Path.rename` / `Path.replace` can't be told from Blender's
`ID.rename` and `str.replace`; Python written into a Text data-block (write_text) is reviewed by the server,
not here.
"""

import ast
import os
import re

# rule → (what is not allowed, what to do instead)
RULES = {
    "process": ("running other programs is not allowed",
                "Do it with Python inside Blender, or ask the user to run the program themselves."),
    "network": ("the network is off limits",
                "Ask the user for the file, or use the agent's own tools to fetch things."),
    "native": ("loading native code is not allowed",
               "Use bpy, bmesh, mathutils or numpy."),
    "delete": ("deleting, moving or renaming files is not allowed",
               "Leave files where they are; write new ones under \"//\" (write_text, export_file) and ask the user "
               "to delete or move files."),
    "blender": ("changing Blender itself (quitting, preferences, add-ons, extensions) is not allowed",
                "Ask the user to change it in Edit › Preferences; ui_navigate can show them where."),
    "addon": ("the Zetta 3D add-on's own folder, settings and modules are private",
              "Work with the .blend and the files next to it."),
    "dynamic": ("code built from strings or reached through introspection is not allowed",
                "Write the code out plainly, with literal names and imports."),
    "write": ("writing files outside the .blend's folder is not allowed",
              "Write under the .blend's folder: open(bpy.path.abspath(\"//notes.txt\"), \"w\"), or use write_text."),
    "file": ("opening, saving, reloading or appending .blend files from code is not allowed",
             "Use file_op (save, save_copy, append, link) or open_file."),
    "freeze": ("waiting for console input would freeze Blender",
               "Use the tools' own inputs, or ask the user in the chat."),
}

# Modules refused outright, with every submodule ("urllib" covers "urllib.request").
MODULES = {
    "process": ["subprocess", "_posixsubprocess", "pty", "multiprocessing", "_multiprocessing", "webbrowser",
                "posix", "nt", "_winapi", "msvcrt", "winreg", "_winreg", "pexpect", "sh", "plumbum"],
    "network": ["socket", "_socket", "ssl", "_ssl", "urllib", "urllib2", "urllib3", "http", "httplib", "requests",
                "httpx", "aiohttp", "ftplib", "smtplib", "smtpd", "poplib", "imaplib", "nntplib", "telnetlib",
                "xmlrpc", "socketserver", "asyncio", "websocket", "websockets", "paramiko", "wsgiref", "selectors"],
    "native": ["ctypes", "_ctypes", "cffi", "_cffi_backend"],
    "dynamic": ["importlib", "imp", "runpy", "code", "codeop", "pkgutil", "zipimport", "pickle", "_pickle",
                "cPickle", "marshal", "shelve", "dill", "cloudpickle", "copyreg", "builtins", "__builtin__",
                "_io"],
    "blender": ["addon_utils", "bl_pkg"],
    "addon": ["bl_ext", "keyring", "_keyring"],
}

# Names refused wherever they appear — called, passed or aliased. Exact dotted names after import aliases are
# resolved; an entry also covers everything under it ("bpy.ops.preferences" covers its operators).
NAMES = {
    "process": [
        "os.system", "os.popen", "os.fork", "os.forkpty", "os.posix_spawn", "os.posix_spawnp", "os.startfile",
        "os.kill", "os.killpg", "os.execl", "os.execle", "os.execlp", "os.execlpe", "os.execv", "os.execve",
        "os.execvp", "os.execvpe", "os.spawnl", "os.spawnle", "os.spawnlp", "os.spawnlpe", "os.spawnv",
        "os.spawnve", "os.spawnvp", "os.spawnvpe", "platform.popen", "concurrent.futures.ProcessPoolExecutor",
        "bpy.ops.wm.path_open",
    ],
    "network": ["bpy.ops.wm.url_open", "bpy.ops.wm.url_open_preset", "bpy.ops.wm.doc_view",
                "bpy.ops.wm.doc_view_manual"],
    "delete": ["os.remove", "os.unlink", "os.rmdir", "os.removedirs", "os.rename", "os.renames", "os.replace",
               "os.truncate", "shutil.rmtree", "shutil.move"],
    "blender": [
        "sys.exit", "os._exit", "os.abort", "bpy.ops.wm.quit_blender", "bpy.ops.preferences", "bpy.ops.extensions",
        "bpy.ops.wm.save_userpref", "bpy.ops.wm.read_userpref", "bpy.ops.wm.read_factory_userpref",
        "bpy.ops.wm.read_factory_settings", "bpy.ops.wm.save_homefile", "bpy.ops.script.reload",
    ],
    "file": ["bpy.ops.wm.open_mainfile", "bpy.ops.wm.read_homefile", "bpy.ops.wm.revert_mainfile",
             "bpy.ops.wm.recover_last_session", "bpy.ops.wm.recover_auto_save", "bpy.ops.wm.save_mainfile",
             "bpy.ops.wm.save_as_mainfile", "bpy.ops.wm.link", "bpy.ops.wm.append"],
    "dynamic": [
        "bpy.utils.execfile", "bpy.utils.load_scripts", "bpy.utils.modules_from_path",
        "bpy.ops.script.python_file_run", "bpy.ops.script.execute_preset", "bpy.ops.text.run_script",
        "sys.modules", "sys._getframe", "sys.settrace", "sys.setprofile", "gc.get_objects", "gc.get_referrers",
        "gc.get_referents", "inspect.currentframe", "inspect.stack", "inspect.trace", "inspect.getouterframes",
        "inspect.getinnerframes", "types.FunctionType", "types.LambdaType", "types.CodeType",
    ],
    "addon": ["bpy.utils.extension_path_user", "bpy.utils.user_resource", "bpy.ops.zetta"],
    "write": ["io.open", "io.FileIO", "io.open_code", "os.open", "os.fdopen", "codecs.open",
              "shutil.unpack_archive", "shutil.make_archive"],
}

# Method names refused on any receiver when called: no Blender API has them, so whatever object they are
# reached through (a module passed into a function, an alias the guard can't follow), they are the dangerous
# one.
METHODS = {
    "process": ["system", "popen", "Popen", "check_output", "check_call", "getoutput", "getstatusoutput", "fork",
                "forkpty", "posix_spawn", "posix_spawnp", "startfile", "execl", "execle", "execlp", "execlpe",
                "execv", "execve", "execvp", "execvpe", "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv",
                "spawnve", "spawnvp", "spawnvpe", "path_open"],
    "network": ["url_open", "urlopen"],
    "delete": ["rmtree", "rmdir", "removedirs"],
    "blender": ["quit_blender", "addon_enable", "addon_disable", "addon_install", "addon_remove",
                "save_userpref", "read_userpref", "read_factory_settings", "package_install", "package_uninstall",
                "package_install_files", "save_homefile"],
    "file": ["open_mainfile", "read_homefile", "revert_mainfile", "save_as_mainfile", "save_mainfile"],
    "dynamic": ["as_module", "python_file_run", "run_script", "execute_preset", "execfile"],
}

# Attributes that lead from any object to globals, builtins, modules or frames.
DUNDERS = {"__globals__", "__builtins__", "__subclasses__", "__code__", "__closure__", "__mro__", "__bases__",
           "__base__", "__loader__", "__spec__", "__import__", "__getattribute__", "__reduce__", "__reduce_ex__",
           "__self__", "__init_subclass__"}
FRAMES = {"f_globals", "f_locals", "f_builtins", "f_back", "gi_frame", "cr_frame", "ag_frame", "tb_frame",
          "gi_code", "co_code"}

# Builtins the code may not reference (unless it defines the name itself); and ones it may not call.
BUILTINS = {"exec": "dynamic", "eval": "dynamic", "compile": "dynamic", "__import__": "dynamic",
            "globals": "dynamic"}
CALLS = {"input": "freeze", "breakpoint": "freeze", "help": "freeze", "exit": "blender", "quit": "blender"}

# Modules that may be used only as `module.name`: passed around as a value, the guard could not follow what
# is called on them.
SENSITIVE = {"os", "shutil", "sys", "gc", "inspect", "io"}

# Read-only methods on a part of the preferences.
PREFS_READS = {"get", "keys", "values", "items", "find", "path_resolve", "as_pointer", "is_property_set",
               "is_property_readonly", "bl_rna_get_subclass", "bl_rna_get_subclass_py", "path_from_id"}

# What run_python preloads (agent.tools.python): these names are modules or their members without an import.
PRELOADED = {"bpy": "bpy", "bmesh": "bmesh", "mathutils": "mathutils", "math": "math", "random": "random",
             "Vector": "mathutils.Vector", "Matrix": "mathutils.Matrix", "Euler": "mathutils.Euler",
             "Quaternion": "mathutils.Quaternion", "Color": "mathutils.Color", "C": "bpy.context",
             "D": "bpy.data"}

# Archive and file functions whose path (first argument) is written to.
WRITING_OPENERS = {"tarfile.open", "gzip.open", "bz2.open", "lzma.open", "zipfile.ZipFile", "tarfile.TarFile"}

_MODULE_RULE = {name: rule for rule, names in MODULES.items() for name in names}
# Modules other modules carry as attributes (os.path.os, tempfile._os, shutil.os): reaching one that way would
# hide it from the names above.
_CARRIED = SENSITIVE | {name for name in _MODULE_RULE if "." not in name}
_NAME_RULE = {name: rule for rule, names in NAMES.items() for name in names}
_METHOD_RULE = {name: rule for rule, names in METHODS.items() for name in names}
_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
_CLIMB = re.compile(r"(^|[\\/])\.\.([\\/]|$)")
_PRIVATE_MARKERS = ("zetta3d-blender", "/extensions/.user/", "\\extensions\\.user\\", "bl_ext.")
# Path pieces that only lead to extensions' private data (os.path.join(…, ".user", "user_default", …)).
_PRIVATE_PIECES = {".user", "user_default", "zetta3d-blender"}
_MODE = re.compile(r"^[rwxabtU+]{1,4}$")


class Refused(Exception):
    """Raised at run time by run_python's builtins; its text is the full refusal line."""


def message(rule, detail):
    what, instead = RULES[rule]
    return f"{what} ({detail}). {instead}"


def module_rule(name):
    """The rule refusing an import of `name` ("urllib.request"), or None."""
    if not name:
        return None
    parts = name.split(".")
    for i in range(1, len(parts) + 1):
        rule = _MODULE_RULE.get(".".join(parts[:i]))
        if rule:
            return rule
    if "zetta3d" in name.lower():
        return "addon"
    return None


def name_rule(dotted):
    """The rule refusing the dotted name ("os.system", "bpy.ops.preferences.addon_enable"), or None."""
    if not dotted:
        return None
    parts = dotted.split(".")
    for i in range(1, len(parts) + 1):
        rule = _NAME_RULE.get(".".join(parts[:i]))
        if rule:
            return rule
    return None


def attribute_refusal(obj, name):
    """Run time: why getattr(obj, name) is refused, or None. Used by run_python's own getattr."""
    import types

    if name in DUNDERS or name in FRAMES:
        return message("dynamic", name)
    if isinstance(obj, types.ModuleType):
        dotted = f"{obj.__name__}.{name}"
        if name == "__dict__":
            return message("dynamic", f"{obj.__name__}.__dict__")
        if name.lstrip("_") in _CARRIED:
            return message("dynamic", f"{dotted}; import {name.lstrip('_')} directly")
        rule = name_rule(dotted) or module_rule(dotted)
        if rule:
            return message(rule, dotted)
    return None


def private_folders():
    """The add-on's private folders (session, keys, file copies, attachments) — never for user code."""
    folders = []
    try:
        from ..core import state

        folders.append(state.folder())
    except Exception:  # noqa: BLE001 — outside Blender (or before the add-on is set up) there is none yet
        pass
    folders.append(os.path.join(os.path.expanduser("~"), ".config", "zetta3d-blender"))
    return [os.path.realpath(folder) for folder in folders]


def check(code):
    """The refusal line for this code, or None when it may run."""
    if not isinstance(code, str):
        return None
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        return None  # it can't run either; run_python reports the syntax error itself
    try:
        _Checker(tree).run()
    except _Refusal as refusal:
        return str(refusal)
    return None


class _Refusal(Exception):
    pass


def _refuse(rule, detail, node=None):
    line = getattr(node, "lineno", None)
    raise _Refusal(message(rule, f"{detail}, line {line}" if line else detail))


def _literals(node):
    """Every string constant inside node (f-string parts included)."""
    found = []
    for each in ast.walk(node):
        if isinstance(each, ast.Constant) and isinstance(each.value, str):
            found.append(each.value)
    return found


def _literal(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _absolute(text):
    return ((text.startswith("/") and not text.startswith("//")) or text.startswith("~")
            or bool(_DRIVE.match(text)) or text.startswith("\\\\"))


class _Checker:
    def __init__(self, tree):
        self.tree = tree
        self.parents = {}
        self.aliases = dict(PRELOADED)  # name → dotted path it stands for
        self.imported = set()  # names an import bound
        self.bound = set()  # every name the code binds itself
        self.prefs = set()  # names holding a part of the preferences
        self.private = None

    # ─── Setting up ─────────────────────────────────────────────────────────────────────────────────

    def run(self):
        for node in ast.walk(self.tree):
            for child in ast.iter_child_nodes(node):
                self.parents[child] = node
            self._collect_bound(node)
        # Aliases can build on each other (`import os as o; p = o.path`): settle them in a few passes.
        for _ in range(3):
            for node in ast.walk(self.tree):
                self._collect_alias(node)
        for node in ast.walk(self.tree):
            self._visit(node)

    def _collect_bound(self, node):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            self.bound.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            self.bound.add(node.name)
        elif isinstance(node, ast.arg):
            self.bound.add(node.arg)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                self.bound.add((alias.asname or alias.name).split(".")[0])

    def _collect_alias(self, node):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    self.aliases[alias.asname] = alias.name
                    self.imported.add(alias.asname)
                else:
                    top = alias.name.split(".")[0]
                    self.aliases[top] = top
                    self.imported.add(top)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                if alias.name != "*":
                    local = alias.asname or alias.name
                    self.aliases[local] = f"{node.module}.{alias.name}"
                    self.imported.add(local)
        elif isinstance(node, (ast.Assign, ast.NamedExpr, ast.For, ast.comprehension, ast.withitem)):
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.NamedExpr):
                targets, value = [node.target], node.value
            elif isinstance(node, ast.withitem):
                targets, value = [node.optional_vars], node.context_expr
            else:
                targets, value = [node.target], node.iter
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            if not names:
                return
            parts = self._resolved(value)
            if "preferences" in parts or (parts and parts[0] in self.prefs):
                self.prefs.update(names)
            # `o = os`, `C = bpy.context`, `wm = bpy.ops.wm`: a plain alias of something the guard knows.
            original = self._chain(value)
            if (isinstance(node, (ast.Assign, ast.NamedExpr)) and original and original[0] in self.aliases
                    and self._clean(parts) == parts):
                for name in names:
                    if name not in self.imported and name not in PRELOADED:
                        self.aliases[name] = ".".join(parts)

    # ─── Names and chains ───────────────────────────────────────────────────────────────────────────

    def _chain(self, node):
        """["bpy", "context", "preferences", "view"] for bpy.context.preferences.view; "[]" for a subscript,
        "()" for a call, "?" for anything else at the root."""
        parts = []
        while True:
            if isinstance(node, ast.Attribute):
                parts.append(node.attr)
                node = node.value
            elif isinstance(node, ast.Name):
                parts.append(node.id)
                break
            elif isinstance(node, ast.Call) and self._getattr_literal(node):
                parts.append(node.args[1].value)
                node = node.args[0]
            elif isinstance(node, ast.Subscript):
                parts.append("[]")
                node = node.value
            elif isinstance(node, ast.Call):
                parts.append("()")
                node = node.func
            else:
                parts.append("?")
                break
        return list(reversed(parts))

    def _resolved(self, node):
        parts = self._chain(node)
        if parts and parts[0] in self.aliases:
            parts = self.aliases[parts[0]].split(".") + parts[1:]
        return parts

    @staticmethod
    def _clean(parts):
        """The leading plain names of a chain: ["bpy", "data", "objects"] for bpy.data.objects["x"].name."""
        clean = []
        for part in parts:
            if part in ("[]", "()", "?"):
                break
            clean.append(part)
        return clean

    def _getattr_literal(self, node):
        return (isinstance(node.func, ast.Name) and node.func.id == "getattr" and "getattr" not in self.bound
                and len(node.args) >= 2 and _literal(node.args[1]) is not None)

    def _module_like(self, node):
        """True when node is a module (or bpy.ops and the like) rather than Blender data or a plain value."""
        parts = self._chain(node)
        if not parts or parts[0] not in self.aliases:
            return False
        clean = self._clean(self._resolved(node))
        if not clean:
            return False
        return not (clean[0] == "bpy" and len(clean) > 1 and clean[1] in ("data", "context"))

    def _has_prefs(self, parts):
        return "preferences" in parts or (bool(parts) and parts[0] in self.prefs)

    def _builtin(self, node, name):
        return isinstance(node, ast.Name) and node.id == name and name not in self.bound

    # ─── The rules ──────────────────────────────────────────────────────────────────────────────────

    def _visit(self, node):
        if isinstance(node, ast.Import):
            for alias in node.names:
                self._check_module(alias.name, node)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                _refuse("dynamic", "a relative import", node)
            self._check_module(node.module or "", node)
            for alias in node.names:
                if alias.name == "*":
                    if (node.module or "").split(".")[0] in SENSITIVE | {"bpy"}:
                        _refuse("dynamic", f"from {node.module} import *", node)
                    continue
                dotted = f"{node.module}.{alias.name}"
                rule = name_rule(dotted) or module_rule(dotted)
                if rule:
                    _refuse(rule, dotted, node)
        elif isinstance(node, ast.Attribute):
            self._check_attribute(node)
        elif isinstance(node, ast.Name):
            self._check_name(node)
        elif isinstance(node, ast.Call):
            self._check_call(node)
        elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Delete)):
            self._check_store(node)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            self._check_string(node)

    def _check_module(self, name, node):
        rule = module_rule(name)
        if rule:
            _refuse(rule, f"import {name}", node)

    def _check_attribute(self, node):
        if node.attr in DUNDERS or node.attr in FRAMES:
            _refuse("dynamic", f".{node.attr}", node)
        parts = self._resolved(node)
        clean = self._clean(parts)
        if clean == parts:
            rule = name_rule(".".join(clean))
            if rule:
                _refuse(rule, ".".join(clean), node)
        if node.attr.lstrip("_") in _CARRIED and self._module_like(node.value):
            _refuse("dynamic", f"{'.'.join(self._clean(parts))}; import {node.attr.lstrip('_')} directly", node)
        if node.attr == "__dict__" and self._module_like(node.value):
            _refuse("dynamic", f"{'.'.join(self._clean(self._resolved(node.value)))}.__dict__", node)
        if node.attr == "addons" and self._has_prefs(parts[:-1]):
            _refuse("addon", "preferences.addons", node)

    def _check_name(self, node):
        if node.id in ("__builtins__", "__loader__", "__spec__"):
            _refuse("dynamic", node.id, node)
        if node.id in BUILTINS and node.id not in self.bound:
            _refuse(BUILTINS[node.id], f"{node.id}()", node)
        if node.id in self.aliases and node.id not in PRELOADED:
            rule = name_rule(self.aliases[node.id])
            if rule:
                _refuse(rule, self.aliases[node.id], node)
        # A sensitive module may only be used as `module.name`, or aliased plainly (`o = os`, tracked above).
        if (node.id not in PRELOADED and self.aliases.get(node.id) in SENSITIVE
                and isinstance(node.ctx, ast.Load)):
            parent = self.parents.get(node)
            plain_use = isinstance(parent, ast.Attribute) and parent.value is node
            alias = isinstance(parent, ast.Assign) and parent.value is node and all(
                isinstance(t, ast.Name) for t in parent.targets)
            if not plain_use and not alias:
                _refuse("dynamic", f"{node.id} passed around as a value; call {node.id}.<name> directly", node)

    def _check_call(self, node):
        func = node.func
        if isinstance(func, ast.Name) and func.id in CALLS and func.id not in self.bound:
            _refuse(CALLS[func.id], f"{func.id}()", node)
        if isinstance(func, ast.Attribute):
            rule = _METHOD_RULE.get(func.attr)
            if rule:
                parts = self._resolved(func)
                named = ".".join(parts) if self._clean(parts) == parts else f".{func.attr}"
                _refuse(rule, f"{named}()", node)
            # Path.unlink() takes no object; Blender's collection.objects.unlink(obj) always does.
            if func.attr == "unlink" and not node.args:
                _refuse("delete", ".unlink()", node)
            receiver = self._resolved(func.value)
            if self._has_prefs(receiver) and func.attr not in PREFS_READS:
                _refuse("blender", f"preferences ….{func.attr}()", node)
        for name in ("getattr", "setattr", "delattr"):
            if self._builtin(func, name) and len(node.args) >= 2:
                self._check_getattr(name, node)
        if self._builtin(func, "vars") and node.args and self._module_like(node.args[0]):
            _refuse("dynamic", "vars() of a module", node)
        self._check_writes(node)

    def _check_getattr(self, name, node):
        receiver, attribute = node.args[0], _literal(node.args[1])
        receiver_parts = self._resolved(receiver)
        writes = name in ("setattr", "delattr")
        if attribute is None:
            if self._module_like(receiver):
                _refuse("dynamic", f"{name}() with a computed name on a module", node)
            if writes and self._has_prefs(receiver_parts):
                _refuse("blender", f"{name}() on the preferences", node)
            return
        if attribute in DUNDERS or attribute in FRAMES:
            _refuse("dynamic", f'{name}(…, "{attribute}")', node)
        clean = self._clean(receiver_parts)
        if clean == receiver_parts:
            dotted = ".".join(clean + [attribute])
            rule = name_rule(dotted)
            if rule:
                _refuse(rule, dotted, node)
        if attribute == "__dict__" and self._module_like(receiver):
            _refuse("dynamic", "a module's __dict__", node)
        if writes and (self._has_prefs(receiver_parts) or attribute == "preferences"):
            _refuse("blender", f"{name}() on the preferences", node)

    def _check_store(self, node):
        targets = node.targets if isinstance(node, (ast.Assign, ast.Delete)) else [node.target]
        flat = []
        for target in targets:
            flat.extend(target.elts if isinstance(target, (ast.Tuple, ast.List)) else [target])
        value = getattr(node, "value", None)
        for target in flat:
            if isinstance(target, ast.Name):
                if target.id in ("__builtins__",):
                    _refuse("dynamic", target.id, node)
                continue
            parts = self._resolved(target)
            if self._has_prefs(parts):
                _refuse("blender", "a write to the preferences", node)
            if parts[:1] == ["sys"] and len(parts) > 1 and parts[1] in ("modules", "meta_path", "path_hooks"):
                _refuse("dynamic", f"sys.{parts[1]}", node)
            if value is None:
                continue
            last = parts[-1] if parts else ""
            # A driver's expression is Python Blender runs later: it must pass the same rules.
            if last == "expression":
                self._check_expression(value, node)
            # Where a render, an image or a File Output node will be written.
            if (parts[-2:] == ["render", "filepath"] or last in ("filepath_raw", "base_path")) and value is not None:
                self._check_path(value, node, strict=False, bare_ok=True)

    def _check_expression(self, value, node):
        text = _literal(value)
        if text is not None:
            reason = check(text)
            if reason:
                raise _Refusal(f"a driver expression: {reason}")
            return
        if any("__" in piece for piece in _literals(value)):
            _refuse("dynamic", "a driver expression with dunder names", node)

    def _check_string(self, node):
        text = node.value
        for dunder in DUNDERS:
            if dunder in text:
                _refuse("dynamic", f'"{dunder}" in a string', node)
        lowered = text.replace("\\", "/")
        if text in _PRIVATE_PIECES or any(marker.replace("\\", "/") in lowered for marker in _PRIVATE_MARKERS):
            _refuse("addon", "a path or name inside the add-on's folder", node)
        if self.private is None:
            self.private = [p.replace("\\", "/") for p in private_folders()]
        if len(text) >= 4 and any(p and p in lowered for p in self.private):
            _refuse("addon", "a path inside the add-on's folder", node)

    # ─── Writing files ──────────────────────────────────────────────────────────────────────────────

    def _check_writes(self, node):
        func = node.func
        parts = self._resolved(func)
        resolved = ".".join(parts) if self._clean(parts) == parts else ""
        if self._builtin(func, "open"):
            if self._writing_mode(node, 1):
                path = node.args[0] if node.args else self._keyword(node, "file")
                if path is not None:
                    self._check_path(path, node, strict=False)
            return
        if resolved in WRITING_OPENERS:
            if self._writing_mode(node, 1):
                path = node.args[0] if node.args else self._keyword(node, "name") or self._keyword(node, "file")
                if path is not None:
                    self._check_path(path, node, strict=True)
            return
        if resolved in ("shutil.copy", "shutil.copy2", "shutil.copyfile", "shutil.copytree"):
            dst = node.args[1] if len(node.args) > 1 else self._keyword(node, "dst")
            if dst is not None:
                self._check_path(dst, node, strict=True)
            return
        if not isinstance(func, ast.Attribute):
            return
        if func.attr in ("write_text", "write_bytes", "touch", "symlink_to", "hardlink_to"):
            self._check_path(func.value, node, strict=True)
        elif func.attr == "open" and self._writing_mode(node, 0, default_read=True):
            self._check_path(func.value, node, strict=True)
        elif func.attr in ("extractall", "extract"):
            index = 0 if func.attr == "extractall" else 1
            path = node.args[index] if len(node.args) > index else self._keyword(node, "path")
            if path is None:
                _refuse("write", f".{func.attr}() into Blender's working folder", node)
            self._check_path(path, node, strict=True)
        elif any(word in f"{'.'.join(self._clean(parts))}.{func.attr}" for word in ("export", "save", "write")):
            for keyword in node.keywords:
                if keyword.arg in ("filepath", "filename", "directory", "output_path"):
                    self._check_path(keyword.value, node, strict=False, bare_ok=True)
            if func.attr == "save_render" and node.args:
                self._check_path(node.args[0], node, strict=False, bare_ok=True)

    @staticmethod
    def _keyword(node, name):
        for keyword in node.keywords:
            if keyword.arg == name:
                return keyword.value
        return None

    def _writing_mode(self, node, index, default_read=True):
        mode = node.args[index] if len(node.args) > index else self._keyword(node, "mode")
        if mode is None:
            return not default_read
        text = _literal(mode)
        if text is None:
            return True  # a mode worked out at run time: treat it as a write
        if not _MODE.match(text):
            return False  # not a mode at all (ZipFile.open's member name)
        return any(flag in text for flag in "wax+")

    def _check_path(self, path, node, strict, bare_ok=False):
        """Refuses a write whose path is plainly outside the .blend's folder. strict: the path must be built from
        a "//" path, because nothing checks it at run time (pathlib, shutil, archives)."""
        pieces = _literals(path)
        for piece in pieces:
            if _absolute(piece):
                _refuse("write", f'the absolute path "{piece[:80]}"', node)
            if _CLIMB.search(piece):
                _refuse("write", f'a path that climbs out with ".." ("{piece[:80]}")', node)
        text = _literal(path)
        if text is not None and not text.startswith("//") and not bare_ok:
            _refuse("write", f'"{text[:80]}", relative to Blender\'s working folder rather than the .blend', node)
        if strict and not any(piece.startswith("//") for piece in pieces):
            _refuse("write", "a path the add-on can't check (build it from bpy.path.abspath(\"//…\"))", node)
