"""Preferences › Add-ons › Zetta 3D: account, what the agent runs on (and your own API keys), where models and
pictures go, the connection, and the version."""

import bpy
from bpy.props import BoolProperty, EnumProperty, StringProperty

from .. import auth, config, providers, updates
from ..core import keychain
from ..core.prefs import PACKAGE
from . import panels


def _connection_changed(self, _context):
    from .. import library
    from ..pipelines import state as pipes
    from ..studio import context as studio

    studio.reset()
    library.reset()
    pipes.reset()
    if auth.current != auth.SIGNED_OUT:
        auth.sign_out("The server changed. Sign in again.")


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
    api_base: StringProperty(name="API", default="", update=_connection_changed,
                             description=f"Empty = production ({config.PRODUCTION_API})")
    site_base: StringProperty(name="Website", default="", update=_connection_changed,
                              description=f"Empty = production ({config.PRODUCTION_SITE})")
    show_developer: BoolProperty(name="Developer", default=False)

    def draw(self, context):
        layout = self.layout

        box = layout.box()
        box.label(text="Account", icon="USER")
        panels.draw_account(box)

        box = layout.box()
        box.label(text="Zetta Agent — what it runs on", icon="OUTLINER_DATA_LIGHTPROBE")
        panels.wrap(box, "The Zetta Agent arrives in an upcoming version of the add-on. Keys you save here are kept "
                         "ready for it.", "INFO", width=100)
        box.row().prop(self, "agent_account", expand=True)
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

        box = layout.box()
        box.prop(self, "show_developer", icon="TRIA_DOWN" if self.show_developer else "TRIA_RIGHT", emboss=False)
        if self.show_developer:
            box.label(text="Only change these if you are developing against your own Zetta 3D server.")
            box.prop(self, "api_base")
            box.prop(self, "site_base")
            box.label(text="Connected to production." if config.is_production() else f"Using {config.api_base()}")
            if not config.is_production():
                panels.btn(box, "reset_connection", "Reset to production", "LOOP_BACK")


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
