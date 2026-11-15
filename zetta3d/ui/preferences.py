"""Preferences › Add-ons › Zetta 3D: account, what the agent runs on (and your own API keys), where models and
pictures go, and the version. The server is not a setting: it is always zetta3d.ai (config.py)."""

import bpy
from bpy.props import BoolProperty, EnumProperty, StringProperty

from .. import auth, config, providers, updates
from ..core import keychain
from ..core.prefs import PACKAGE
from . import panels


def _redraw():
    from ..core import tasks

    tasks.redraw()


def _place_changed(self, context):
    wm = getattr(context, "window_manager", None)
    if wm is not None:
        wm.zetta.place = self.place_in_scene


class ZettaPreferences(bpy.types.AddonPreferences):
    bl_idname = PACKAGE

    agent_account: EnumProperty(
        name="The Zetta Agent runs on",
        items=[(providers.ZETTA, "Zetta account", "Messages use your Zetta 3D plan"),
               (providers.OWN, "Your own API key", "Messages go to the provider you saved a key for, billed by them")],
        default=providers.ZETTA,
    )
    models_folder: StringProperty(
        name="Models folder", default="//zetta3d/models", subtype="DIR_PATH",
        description="Where imported models are downloaded. // means next to the .blend file")
    images_folder: StringProperty(
        name="Images folder", default="//zetta3d/images", subtype="DIR_PATH",
        description="Where saved pictures go. // means next to the .blend file")
    place_in_scene: BoolProperty(
        name="Place imported models at the 3D cursor", default=True, update=_place_changed,
        description="Imported models stand on the ground at the 3D cursor")
    preferred_format: EnumProperty(
        name="Preferred format",
        items=[("glb", "GLB", "glTF binary — materials and textures come with it"),
               ("fbx", "FBX", "Best for rigs going on to other tools")],
        default="glb",
        description="When a result has both, this format is imported")
    agent_keep_copies: BoolProperty(
        name="Keep a copy before risky steps", default=True,
        description="Before the Zetta Agent runs Python, deletes, applies or imports, save a copy of the file "
                    "(files up to 200 MB; 3 kept per file, a week at most) so it can be restored. When the agent "
                    "asks first, you decide on its card")

    agent_classic_panel: BoolProperty(
        name="Classic agent panel", default=False, update=lambda _self, _context: _redraw(),
        description="Show the Zetta Agent with Blender's own buttons and fields: no fixed "
                    "tabs or message box, and @ tags are picked from a list instead of typed")

    def draw(self, context):
        layout = self.layout

        box = layout.box()
        box.label(text="Account", icon="USER")
        panels.draw_account(box)

        box = layout.box()
        box.label(text="Zetta Agent — what it runs on", icon="OUTLINER_DATA_LIGHTPROBE")
        panels.wrap(box, "The Zetta Agent is in the 3D View's sidebar, in the Zetta Agent tab.", "INFO", width=100)
        box.row().prop(self, "agent_account", expand=True)
        box.prop(self, "agent_keep_copies")
        box.prop(self, "agent_classic_panel")
        if self.agent_account == providers.OWN:
            draw_own_keys(box)

        box = layout.box()
        box.label(text="Models and pictures", icon="FILE_FOLDER")
        box.prop(self, "models_folder")
        box.prop(self, "images_folder")
        box.prop(self, "place_in_scene")
        box.prop(self, "preferred_format")

        box = layout.box()
        status = ("checking…" if updates.checking() else
                  f"{updates.known['latest']} is out" if updates.available() else "up to date" if updates.known
                  else "not checked yet")
        box.label(text=f"Zetta 3D for Blender {config.VERSION} — {status}", icon="INFO")
        row = box.row(align=True)
        panels.btn(row, "check_updates", "Check for updates", "FILE_REFRESH")
        if updates.available():
            if updates.can_install():
                panels.btn(row, "install_update", "Update", "IMPORT")
            panels.btn(row, "update_notes", "What's new", "URL")
        if updates.problem:
            panels.wrap(box, updates.problem, "ERROR", alert=True, width=100)

        if not config.is_production():  # never in a release: a build made for a local backend says so
            box = layout.box()
            box.label(text=f"Development build — server: {config.api_base()}", icon="ERROR")


def draw_own_keys(layout):
    found = providers.providers()
    if found is None:
        if auth.signed_in():
            if not providers.loading():
                providers.refresh()
            layout.label(text="Loading the providers…", icon="TIME")
        else:
            layout.label(text="Sign in to see the providers you can use.", icon="LOCKED")
        return
    if not found:
        layout.label(text="Your own API keys are not offered on this server.")
        return
    panels.wrap(layout, f"Keys are kept in {keychain.store_name()}, never in Blender's preferences or your .blend "
                        "files. What the agent uses on your key is billed by that provider; making 3D models and "
                        "pictures still uses Zetta credits.", width=100)
    for provider in found:
        key = provider.get("key")
        box = layout.box()
        row = box.row()
        row.label(text=provider.get("label") or key)
        saved = keychain.has(key)
        if saved:
            row.label(text="Key saved", icon="CHECKMARK")
            row = box.row(align=True)
            panels.btn(row, "provider_check", "Check key", "FILE_REFRESH", arg=key)
            op = row.operator("zetta.provider_key", text="Replace", icon="GREASEPENCIL")
            op.provider = key
            panels.btn(row, "provider_remove", "Remove", "TRASH", arg=key,
                       confirm=f"Remove the {provider.get('label') or key} key?|It is deleted from {keychain.store_name()}.")
        else:
            row = box.row(align=True)
            op = row.operator("zetta.provider_key", text="Paste a key…", icon="KEY_HLT")
            op.provider = key
            if provider.get("keyUrl"):
                panels.btn(row, "open_url", f"Get a {provider.get('label') or key} key", "URL", arg=provider["keyUrl"])
        if key == "glm":
            box.label(text="Use a Standard API Key from Z.ai. A GLM Coding Plan key doesn't work here.")
        check = providers.checks.get(key)
        if check:
            ok, message = check
            panels.wrap(box, message, "CHECKMARK" if ok else "TIME" if ok is None else "ERROR", alert=ok is False,
                        width=100)


def register():
    bpy.utils.register_class(ZettaPreferences)


def unregister():
    bpy.utils.unregister_class(ZettaPreferences)
