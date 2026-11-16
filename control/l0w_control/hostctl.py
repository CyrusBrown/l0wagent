"""Host-side commands for `./l0w`, run inside the control container:

    docker compose exec control python -m l0w_control.hostctl drain on [MINUTES] | off
    docker compose exec control python -m l0w_control.hostctl idle      → JSON {idle, waiting_for}
    docker compose exec control python -m l0w_control.hostctl version
    docker compose exec -T control python -m l0w_control.hostctl import-secrets   < {"secrets": [...]} on stdin
    docker compose exec -T control python -m l0w_control.hostctl import-legacy    (reads /work/_legacy/control-export)

Anyone who can run `docker compose exec` already controls the host, so these need no token.
They change only the drain flag the running control reads before starting a run, and (once, for
`./l0w migrate`) import the old assistant: its secrets, archived threads and egress rules (legacy.py).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from . import __version__
from .db import Database
from .updates import blockers, draining, set_drain


def main(argv: list[str]) -> int:
    db = Database(str(Path(os.environ.get("L0W_DATA_DIR", "/data")) / "control.db"))
    cmd = argv[0] if argv else "help"
    if cmd == "drain" and len(argv) >= 2 and argv[1] in ("on", "off"):
        set_drain(db, argv[1] == "on", float(argv[2]) if len(argv) > 2 else 120)
        until = draining(db)
        print(json.dumps({"draining_until": until.isoformat() if until else None}))
        return 0
    if cmd == "idle":
        waiting = blockers(db)
        print(json.dumps({"idle": not waiting, "waiting_for": waiting}))
        return 0
    if cmd == "version":
        print(__version__)
        return 0
    if cmd in ("import-secrets", "import-legacy"):
        from . import legacy
        from .config import Env
        from .vault import Vault

        env = Env()
        vault = Vault(db, env.data_dir)
        if cmd == "import-secrets":
            print(json.dumps(legacy.import_secrets(db, vault, json.loads(sys.stdin.read() or "{}"))))
            return 0
        path = Path(argv[1] if len(argv) > 1 else "/work/_legacy/control-export/export.json")
        if not path.is_file():
            print(json.dumps({"error": f"{path} not found: run the export first"}))
            return 1
        export = json.loads(path.read_text())
        out = {"threads": legacy.import_legacy(db, vault, export)}
        from .runs.clients import EgressClient

        out["egress"] = legacy.import_egress(db, export.get("egress_rules") or [], EgressClient(env).add_rule)
        print(json.dumps(out))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
