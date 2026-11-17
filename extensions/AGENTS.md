# For coding agents building an extension

You're writing an l0wagent extension for the owner of this instance. Read **[docs/EXTENDING.md](../docs/EXTENDING.md)**
first: it is the complete guide and SDK reference. A working example is [examples/plantlog/](examples/plantlog/).

The short version:

- Work only in `extensions/<name>/`. **Never edit core files** (`control/`, `web/`, `agent/`, compose files).
  If the SDK can't do something, stop and write a proposal for the owner instead.
- Use only `l0w_control.sdk` from Python, never the database or other `l0w_control` modules.
- Treat everything from the agent (agent routes, hook data, files in its storage) as untrusted input. The agent
  never decides for the owner: put decisions behind an Inbox card.
- No secret values: `ext.vault.has()` only. Outside credentials go through a connection preset.
- Prompt fragments are part of the agent's harness: keep them short, and say in them what changed when you change them.
- Escape everything you put into HTML in the UI module; style with the app's CSS variables.
- Test with `control/scripts/dev_stack.py --extensions ../extensions` and the web dev server (EXTENDING.md §10).
- When you're done, tell the owner: Settings → Extensions → Rescan, then switch it on.
