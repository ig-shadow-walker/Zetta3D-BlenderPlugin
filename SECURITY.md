# Security

## Reporting a vulnerability

Please don't open a public issue for a security problem. Report it privately instead:

- on GitHub: **Security › Report a vulnerability** on this repository, or
- by email: hello@zetta3d.ai, with "Security" in the subject.

Say what you found, how to reproduce it, and which version of the add-on and of Blender you used. We'll answer
within a few working days and tell you when it's fixed. Please give us a reasonable time to release a fix before
you talk about it publicly.

Only the latest release is supported: fixes ship as a new release, which the add-on offers to install.

## What the add-on does with your data

- **Your sign-in.** You sign in on zetta3d.ai in your browser; Blender never sees your password. The session the
  add-on receives is kept in its own folder on your computer — never in a .blend file, so it doesn't travel with
  your scenes.
- **Your own API keys** (for the Zetta Agent) are kept in your system's key store — macOS Keychain, Windows
  Credential Manager or the Linux keyring — never in Blender's preferences or your files. They are sent only with
  your agent's requests to zetta3d.ai, which passes them to the provider you chose.
- **Where it connects.** The add-on talks only to zetta3d.ai (`https://api.zetta3d.ai`), over HTTPS, and to
  GitHub to check for and download updates. It needs Blender's **Allow Online Access** to do either.
- **What the Zetta Agent sends.** With each message: what you wrote and attached, and a summary of where you are in
  Blender — its version, the open file's path, the mode, what's selected, recent Python errors, an outline of the
  scene, the render and unit settings, your GPU and the add-ons you have enabled. When it captures the viewport or
  renders a preview, that picture is sent too, and so is what its tools read in your file. Its file tools only
  reach the folder your .blend is in (and files you attach); nothing else on your computer.
- **Python the agent writes** runs inside Blender, which has no sandbox. The add-on shows you the code first and
  refuses code that would run programs, go online, delete files, load native code, change your preferences or
  add-ons, or build code from strings. It's a guard, not a cage: read the code on the card before you allow it.

The Zetta 3D [Privacy Policy](https://zetta3d.ai/privacy) and [Terms of Service](https://zetta3d.ai/terms) cover
what happens on zetta3d.ai.
