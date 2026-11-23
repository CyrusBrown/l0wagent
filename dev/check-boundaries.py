#!/usr/bin/env python3
"""The import boundary (docs/plans/hub-development.md, "Repos and layout"): the instance and `hub/`
depend only on `protocol/`, never on each other, and the instance never ships the fake hub.

- Instance code (control, gateway, tunnel, ops, agent, egress, chat-runtime, cli) must not import `fakehub`
  or anything from `hub/` (tests may use the fake hub: it's how the instance is tested).
- `hub/src` must not import from outside `hub/` (it may read `protocol/` files in its tests).
- Path dependencies in the instance's pyproject.toml files point only into `protocol/` (dev groups
  may also point at `dev/hub`).
Exit 1 with a list of violations."""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTANCE = ["control/l0w_control", "gateway/l0w_gateway", "tunnel/l0w_tunnel", "ops/l0w_ops", "agent", "egress", "chat-runtime", "cli"]
PY_IMPORT = re.compile(r"^\s*(?:from|import)\s+(fakehub|hub)\b", re.M)
TS_IMPORT = re.compile(r"""(?:from\s+|import\s*\(\s*|require\s*\(\s*)["']([^"']+)["']""")


def main() -> int:
    bad: list[str] = []
    for d in INSTANCE:
        for f in (ROOT / d).rglob("*.py"):
            if ".venv" in f.parts or "tests" in f.parts or f.name.startswith("test_"):
                continue
            for m in PY_IMPORT.finditer(f.read_text(errors="replace")):
                bad.append(f"{f.relative_to(ROOT)}: imports {m.group(1)}")
    hub_src = ROOT / "hub" / "src"
    if hub_src.exists():
        for f in list(hub_src.rglob("*.ts")) + list(hub_src.rglob("*.js")) + list(hub_src.rglob("*.mjs")):
            for m in TS_IMPORT.finditer(f.read_text(errors="replace")):
                spec = m.group(1)
                if spec.startswith("."):
                    target = (f.parent / spec).resolve()
                    if not target.is_relative_to(ROOT / "hub"):
                        bad.append(f"{f.relative_to(ROOT)}: imports {spec} (outside hub/)")
    for proj in ("control", "gateway", "tunnel", "ops"):
        pp = ROOT / proj / "pyproject.toml"
        if not pp.exists():
            continue
        data = tomllib.loads(pp.read_text())
        dev_deps = {str(d).split(">")[0].split("=")[0].strip() for g in data.get("dependency-groups", {}).values() for d in g if isinstance(d, str)}
        for name, src in data.get("tool", {}).get("uv", {}).get("sources", {}).items():
            path = src.get("path") if isinstance(src, dict) else None
            if not path:
                continue
            target = (pp.parent / path).resolve()
            if target.is_relative_to(ROOT / "protocol"):
                continue
            if name in dev_deps and target.is_relative_to(ROOT / "dev" / "hub"):
                continue
            bad.append(f"{proj}/pyproject.toml: path dependency {name} -> {path} (only protocol/ is allowed)")
    for b in bad:
        print("boundary:", b)
    print("import boundaries: " + ("OK" if not bad else f"{len(bad)} violation(s)"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
