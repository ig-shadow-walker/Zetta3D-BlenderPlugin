# Changelog

All notable changes to Zetta 3D for Blender. Versions follow [semantic versioning](https://semver.org); each
release is on the [releases page](https://github.com/ig-shadow-walker/Zetta3D-BlenderPlugin/releases) with its zip.

## [Unreleased]

## [0.1.0] — 2026-11-15

The first release: your Zetta 3D account inside Blender — the 3D Studio, Image Studio, your library, 3D Pipelines
and the Zetta Agent.

### Added
- **Sign in with your browser** (zetta3d.ai › Connect Blender). The session is kept in the add-on's private folder,
  never in a .blend. The add-on talks only to zetta3d.ai.
- **3D Studio** in the 3D View sidebar: Generate 3D (Zetta N1, N2.5) and Smart 3D (Zetta Np1, Np1.5) from a prompt, a
  picture or several views — with Control Mode, Redraw, AI edit and Generate missing views — and the finishing tools:
  Segment and Smart Segment, Retopology, Smart UV, Texture (from a picture, a description or pictures of each side),
  and Auto Rig with body detection. Every option, price and plan limit of the web Studio.
- **Image Studio**: generate, edit and compose pictures; save them as Blender images or send them to Generate 3D.
- **Jobs**: everything you started, followed to the end — across Blender restarts — and imported when it lands, into
  its own collection, standing on the ground at the 3D cursor.
- **My Generations**: your whole Zetta 3D library with thumbnails — import, show, place, send to a tool, fork,
  rename, delete (one or many), pipeline runs as one tile, the 2D library, and uploading Blender objects to it.
- **3D Pipelines**: recipes built from a description or step by step, with every step's settings, prices, checks,
  undo and autosave; new runs on up to 20 inputs (Manual or Auto); and runs you can follow, steer, retry and import
  from.
- **3D Pipelines as nodes**: Edit as nodes opens a pipeline in Blender's Node Editor (Zetta 3D Pipeline) — steps as
  nodes with typed, coloured ports, Shift+A to add, wires checked as you draw them, Ctrl+Z for the pipeline's undo,
  each step's price and problems on its node, and its settings, Describe it, New run and Runs in the editor's
  sidebar. An open run shows on the canvas, each step coloured by its status.
- **The Zetta Agent** — an assistant in the 3D View's sidebar (Zetta Agent tab) that works in the open .blend with
  Blender's own tools: objects and collections, any property, modifiers and constraints, mesh clean-up and UVs,
  materials and node trees (shader, world, compositor, Geometry Nodes), keyframes, imports and exports, texts and
  Python, renders — and every Zetta 3D tool, with a price card before anything that spends credits. It looks at its
  own work (viewport captures and preview renders), checks your Blender version's Python API before it writes, and
  opens the right panel when you ask where something is.
- **You stay in charge of it**: every change is a named undo step; the changes under each reply can be kept or
  undone; Ask first or Full autonomy; a card before deleting, running Python or opening files; a copy of the file
  before risky steps (Restore the file as it was); and Stop. Generating a 3D model always asks, in Full autonomy
  too — and its card offers **Build in Blender**, which has the agent model it with Blender's own tools instead of
  spending credits.
- **Python the agent writes is guarded**: shown to you first, and code that would run programs, go online, delete
  files, load native code, change preferences or add-ons, or build code from strings is refused.
- **A chat that stays in place**: Chat, History and Memory at the top and a multi-line message box at the bottom
  (Enter sends, Shift+Enter starts a new line), with the conversation scrolling between them. Steps and changes fold
  into one line each, a failed step says why, and a bar above the box says when the agent is waiting for you.
- **@ to tag** objects and data-blocks — a list opens as you type. **Add to Zetta Agent** in the 3D View and Outliner
  menus (⇧⌘M / Ctrl+Shift+M); attachments (a viewport picture, pictures, PDFs, text and other files) shown as
  pictures; **Write a long message…**; Ask mode; History and Memory per .blend file.
- **Runs on your Zetta account or your own API key** (Gemini, OpenAI, DeepSeek, Claude, GLM, Meta), kept in the system
  key store and checked as you save it.
- Preferences › **Keep a copy before risky steps**, and **Classic agent panel** for the chat drawn with Blender's
  own widgets.
- **Updates**: a line in the sidebar when a newer release is out, with What's new and Update.

[Unreleased]: https://github.com/ig-shadow-walker/Zetta3D-BlenderPlugin/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/ig-shadow-walker/Zetta3D-BlenderPlugin/releases/tag/v0.1.0
