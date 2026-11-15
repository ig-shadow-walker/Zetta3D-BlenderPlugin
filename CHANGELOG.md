# Changelog

## [Unreleased]

### Added
- **The Zetta Agent** — an assistant in the 3D View's sidebar (Zetta Agent tab) that works in the open .blend with
  Blender's own tools: objects and collections, any property, modifiers and constraints, mesh clean-up and UVs,
  materials and node trees (shader, world, compositor, Geometry Nodes), keyframes, imports and exports, texts and
  Python, renders — and every Zetta 3D tool, with a price card before anything that spends credits. It looks at
  its own work (viewport captures and preview renders), checks your Blender version's Python API before it writes,
  and opens the right panel when you ask where something is.
- **You stay in charge of it**: every change is a named undo step, a changes card per message (Keep all / Undo
  all), Ask first or Full autonomy, a card before deleting, running Python or opening files, a copy of the file
  before risky steps (Restore the file as it was), and Stop.
- **Python the agent writes is guarded**: shown to you first, and code that would run programs, go online, delete
  files, load native code, change preferences or add-ons, or build code from strings is refused.
- **@ to tag** objects and data-blocks, **Add to Zetta Agent** in the 3D View and Outliner menus (⇧⌘M /
  Ctrl+Shift+M), attachments (a viewport picture, pictures, PDFs, text and other files), **Write more…** for long
  messages, Ask mode, History and Memory per .blend file.
- **Runs on your Zetta account or your own API key** (Gemini, OpenAI, DeepSeek, Claude, GLM, Meta).
- Preferences › **Keep a copy before risky steps**.

## [0.1.0]

The first release: everything the Unity plugin does, in Blender, except the agent.

### Added
- **Sign in with your browser** (zetta3d.ai › Connect Blender). The session is kept in the add-on's private folder,
  never in a .blend.
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
- **Your own API keys** for the coming Zetta Agent, kept in the system key store and checked as you save them.
- **Updates**: a line in the sidebar when a newer release is out, with What's new and Update.
