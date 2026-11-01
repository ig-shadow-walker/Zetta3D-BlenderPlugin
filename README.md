# Zetta 3D for Blender

**Zetta 3D for Blender** puts your [Zetta 3D](https://zetta3d.ai) account inside Blender. Use it to:

- **make 3D models** from text, a picture or several views, and finish them: retopology, segmentation, UVs,
  texturing and rigging;
- **make pictures** — textures, references, concept art — and turn any of them into a model;
- **bring them into your scene** in one click: downloaded, imported into its own collection, and standing on the
  ground at the 3D cursor;
- **chain the tools into pipelines** that run over many pictures or models at once.

Everything you make is also in your library on zetta3d.ai, and everything you made there (or in Unity) shows up in
Blender.

> **Coming next: the Zetta Agent** — a full assistant for Blender. Preferences already lets you save your own
> Gemini, OpenAI, DeepSeek, Claude, GLM or Meta API key for it.

## Install

**You need:** Blender **4.2 LTS** or newer and a [Zetta 3D](https://zetta3d.ai) account.

1. Download `zetta3d-<version>.zip` from the [latest release](https://github.com/ig-shadow-walker/Zetta3D---BlenderPlugin/releases/latest).
   Don't unzip it.
2. In Blender, open **Edit › Preferences › Get Extensions**, click **⌄** (top right) › **Install from Disk…**, and
   pick the zip.
3. Make sure **Preferences › System › Network › Allow Online Access** is on — the add-on talks to zetta3d.ai.

The add-on lives in the 3D View's sidebar: press **N** and open the **Zetta 3D** tab.

**Update.** The add-on checks for a newer release when Blender starts and every 6 hours. When one is out, a line at
the top of the Zetta 3D tab says so, with **What's new** and **Update** (it downloads the release and installs it over
this copy; restart Blender to finish). You can also check from **Preferences › Add-ons › Zetta 3D › Check for
updates**.

**Remove.** Preferences › Add-ons › Zetta 3D › **Uninstall**. Models you imported stay in your files.

## Sign in

In the Zetta 3D tab, click **Sign in with your browser**, sign in on zetta3d.ai and click **Connect Blender**. Blender
receives its own session and never sees your password. The session is kept in the add-on's private folder on your
computer — never in a .blend file, so it never travels with your scenes.

## 3D Studio

The **3D Studio** panel holds every tool of the web Studio. Pick one from the menu at its top.

| Tool | What it does |
|---|---|
| **Generate 3D** | Text, a picture or several views → a 3D model, on Zetta N1 or N2.5, with every option the web Studio has |
| **Smart 3D** | Clean, game-ready topology from the start, on Zetta Np1 or Np1.5 |
| **Segment** | Split a model into named parts (or let Smart Segment decide them) — each imports as its own object |
| **Retopology** | Rebuild a mesh at a polygon budget, as triangles or quads |
| **Smart UV** | Lay a model's UVs out again as a clean layout, keeping its textures, rig and animation |
| **Texture** | Re-texture a model from a description, a reference picture or pictures of each side, up to 8K |
| **Auto Rig** | Rig humanoids, animals and other creatures — Standard, Mixamo, Unity Humanoid, UE5, VRM or ActorCore bone names; GLB or FBX |
| **Image Studio** | Generate, edit and compose pictures, save them as Blender images, or send them to Generate 3D |

- **Pictures** come from a file, or from any image already in your .blend (the image button next to each picker).
  Image Editor › **Image › Generate 3D from this image** does the same from there.
- **Control Mode** (on by default) draws a picture from your prompt first and stops, so you can check it — or
  **Redraw** or **AI edit** it — before any 3D credits are spent.
- **Multi-View**: drop the views you have; **Generate missing views** draws the rest from your reference.
- **Source model** for the finishing tools: **Pick from library**, or select objects in the 3D View and press
  **Use selection**. A model imported from Zetta 3D is used directly; anything else is exported as GLB and uploaded to
  your library first (free). Right-click › **Zetta 3D › …** in the 3D View does the same.
- **Auto Rig › Detect the body** renders the imported model from four sides and tells you what kind of body it has.

Every panel shows the price before you start and your balance. Finished models import into
`//zetta3d/models/<id>-<name>/` next to your .blend (change it in Preferences) and, with **Place at the 3D cursor**
on, stand on the ground at the cursor, in their own collection under **Zetta 3D**.

### Jobs

The **Jobs** panel follows everything you started: queue position, progress, and the import when it lands. Jobs run on
the server — close Blender and they keep going; the next time you open it they are picked up and imported.

## My Generations

Everything you've made on Zetta 3D, on the web, in Unity or in Blender, with thumbnails.

- **Import**, **Show** (select it in the scene) or **Place** another copy at the 3D cursor; **⋯** for **Import as
  child of the active object**, **Send to** a finishing tool, **Fork** a generation's settings back into Generate,
  **Rename**, **Delete**, and the UV layout picture of a Smart UV result.
- Search, filter by kind and status, page through, and select several to delete together.
- A pipeline run is one tile: open it to see its models in the order the run made them, with **Import all**, **Delete
  all** and **Open run**.
- **Images** shows your 2D library: save a picture into your images folder, use it for 3D, or edit it.
- **Upload selection to library** sends the selected objects to your library as a GLB (free).

Gaussian splats and worlds made on zetta3d.ai are listed too, but Blender can't import them here; they open on the web.

## 3D Pipelines

Chain the Studio's tools into a reusable recipe — the same pipelines as on
[zetta3d.ai/pipelines](https://zetta3d.ai/pipelines).

- **Describe it** — *"a game-ready character from a photo, rigged"* — and the recipe is built for you. With a recipe
  there, describe a change instead (*"add AI texturing at the end"*); **Undo** brings back what was there.
- Or build it step by step: **Add step** after the selected one (wired up for you), pick each step's **Input from**,
  and set it up below it. Seeds, Remove background, AI edit, Draw views, Isolate parts, Generate 3D / Smart 3D (and
  their Multi-view versions), Retopology, Segmentation, AI texturing, Smart UV and Auto rig — every option the web has.
- The recipe checks itself as you build, shows its price per run, saves as you go, and has undo and redo.
- **New run…** on up to 20 pictures, models or sets of pictures at once — **Manual** (it waits for you before each
  step) or **Auto** — with **Import results when done**.
- **Runs** shows each run's steps and results: run waiting steps, choose the parts Isolate parts found, retry a failed
  step (with new settings if you like), pause, resume or cancel, and import any model a step made. Runs keep going on
  the server even if Blender is closed.

## Settings

**Preferences › Add-ons › Zetta 3D**: your account, what the Zetta Agent will run on and
[your own API keys](#your-own-api-keys), the models and images folders, placing imports at the 3D cursor, the
preferred mesh format (GLB or FBX), the version and updates, and (under **Developer**) the server to talk to.

### Your own API keys

Under **Zetta Agent — what it runs on**, choose **Your own API key**, then **Paste a key…** next to a provider. The
list of providers comes from Zetta 3D, so new ones appear without an update. Each key is checked when you save it.

- **Your key stays yours.** It is kept in your system's key store — macOS Keychain, Windows Credential Manager or the
  Linux keyring — never in Blender's preferences or your .blend files.
- **GLM keys.** Use a **Standard API Key** from Z.ai's API Keys page. A GLM Coding Plan key doesn't work here.

## Development

```
zetta3d/            the extension (what ships)
  api/              the Zetta 3D API: routes, request builders, the model catalogue
  core/             HTTP, background tasks, the session, the key store, preferences
  studio/           the 3D Studio tools and Image Studio
  pipelines/        the recipe graph (pure Python), its routes, and the editor/runs state
  ui/               panels, operators and preferences
tests/              headless checks run inside Blender
tools/build.sh      builds dist/zetta3d-<version>.zip
release/update.json a required minimum version and a message for the update line
```

Run the tests (they install the add-on into a throwaway Blender user folder, so your own setup is untouched):

```
tests/run.sh
```

Build the zip:

```
tools/build.sh
```

**Releasing.** Bump `version` in `zetta3d/blender_manifest.toml` and `VERSION` in `zetta3d/config.py`, add the notes
to `CHANGELOG.md`, build, and publish a GitHub release tagged `v<version>` with `dist/zetta3d-<version>.zip` attached
under exactly that name — the in-app **Update** downloads
`releases/download/v<version>/zetta3d-<version>.zip`. Installed copies learn of a release from `main`'s manifest,
through the API's `/blender-plugin/version`. To make an update required, set `minimum` in `release/update.json`. To
correct it without a release, set `BLENDER_PLUGIN_LATEST_VERSION`, `BLENDER_PLUGIN_MIN_VERSION` or
`BLENDER_PLUGIN_UPDATE_MESSAGE` on the API server.

## License

Source-available, not open source — © 2026 Zetta 3D. See [LICENSE.md](LICENSE.md).
