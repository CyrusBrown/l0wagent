"""`l0w-updater TAG`: the one-shot container ops-docker starts to update the install.

It runs in the project folder (mounted at the same path as on the host, so compose's relative paths mean the same thing) as
`python3 cli/l0w.py update --version TAG --no-drain`: control drained and waited for idle before the update was asked for.
It lives in its own container so it survives the restart of everything else, and it leaves its result in
`extensions/.l0w-update.json` (the one folder `ops` and control can both read) for control to show after the restart.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .common import TAG_RE

TAIL = 4000
RESULT = Path("extensions") / ".l0w-update.json"
UPDATED = re.compile(r"^Updated: (.*?) \S+ (.*?)\.$", re.M)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def write_result(project: Path, result: dict) -> None:
    path = project / RESULT
    path.parent.mkdir(exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    with os.fdopen(fd, "w") as f:
        json.dump(result, f, indent=2)
        f.write("\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    try:  # this container runs as root; leave the file to whoever owns the folder
        st = path.parent.stat()
        os.chown(path, st.st_uid, st.st_gid)
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or not TAG_RE.match(args[0]):
        print("usage: l0w-updater TAG   (edge, or a release version like 0.9.1)", file=sys.stderr)
        return 2
    tag = args[0]
    project = Path.cwd()
    result = {"tag": tag, "started_at": _now(), "finished_at": None, "ok": None, "before": None, "after": None, "tail": ""}
    write_result(project, result)
    cli = project / "cli" / "l0w.py"
    if not cli.is_file() or not (project / "compose.yaml").is_file():
        result.update(finished_at=_now(), ok=False, tail=f"{project} isn't an l0wagent folder (no cli/l0w.py and compose.yaml)")
        write_result(project, result)
        return 1
    env = {**os.environ, "L0W_IN_UPDATER": "1", "GIT_TERMINAL_PROMPT": "0"}
    out = ""
    try:
        p = subprocess.run([sys.executable, str(cli), "update", "--version", tag, "--no-drain"], cwd=project, env=env, text=True,
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=3600)
        out, ok = p.stdout or "", p.returncode == 0
    except subprocess.TimeoutExpired as e:
        out, ok = (e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")) + "\ntimed out after an hour", False
    except OSError as e:
        out, ok = f"couldn't run the update: {e}", False
    m = UPDATED.search(out)
    result.update(finished_at=_now(), ok=ok, before=m.group(1) if m else None, after=m.group(2) if m else None, tail=out[-TAIL:])
    write_result(project, result)
    print(out)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
