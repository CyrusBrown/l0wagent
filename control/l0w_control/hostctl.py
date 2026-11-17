"""Host-side commands for `./l0w`, run inside the control container:

    docker compose exec control python -m l0w_control.hostctl drain on [MINUTES] [BY] | renew [MINUTES] | off
    docker compose exec control python -m l0w_control.hostctl idle      → JSON {idle, waiting_for}
    docker compose exec control python -m l0w_control.hostctl version
    docker compose exec control python -m l0w_control.hostctl 2fa-reset   → JSON {totp_was_enabled, recovery_codes_deleted}
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
    if cmd == "drain" and len(argv) >= 2 and argv[1] in ("on", "off", "renew"):
        if argv[1] == "renew" and not draining(db):  # nothing to renew: don't revive a cleared drain
            print(json.dumps({"draining_until": None}))
            return 0
        set_drain(db, argv[1] != "off", float(argv[2]) if len(argv) > 2 else 120, argv[3] if len(argv) > 3 else None)
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
    if cmd == "2fa-reset":
        # Lost the authenticator and the recovery codes: whoever can exec into the container may turn 2FA off.
        was = bool((db.q1("SELECT totp_secret FROM users WHERE id=1") or {}).get("totp_secret"))
        codes = db.q1("SELECT COUNT(*) AS n FROM recovery_codes")["n"]
        db.ex("UPDATE users SET totp_secret=NULL WHERE id=1")
        db.ex("DELETE FROM recovery_codes")
        db.ex("DELETE FROM kv WHERE key IN ('totp_pending', 'totp_last_step')")
        print(json.dumps({"totp_was_enabled": was, "recovery_codes_deleted": codes}))
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
