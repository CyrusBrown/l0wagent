# Extensions

Put your extensions here, one folder each (`extensions/<name>/extension.toml`), then turn them on in
**Settings → Extensions** (or `./l0w extensions rescan` and `./l0w extensions enable <name>`). Control mounts
this folder read-only; the assistant can't see or change it.

- How to write one: [docs/EXTENDING.md](../docs/EXTENDING.md) (and [AGENTS.md](AGENTS.md) for coding agents).
- The example: [examples/plantlog/](examples/plantlog/). Copy it to `extensions/plantlog/` to try it.
- Extensions from git: list them in `extensions.toml` next to `compose.yaml` and run `./l0w extensions sync`.

Everything in this folder except this file, AGENTS.md and `examples/` is ignored by git: your extensions are
yours, and `git pull` / `./l0w update` never touch them.
