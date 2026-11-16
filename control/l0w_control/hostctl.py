"""Host-side commands for `./l0w`, run inside the control container:

    docker compose exec control python -m l0w_control.hostctl drain on [MINUTES] | off
    docker compose exec control python -m l0w_control.hostctl idle      → JSON {idle, waiting_for}
    docker compose exec control python -m l0w_control.hostctl version

Anyone who can run `docker compose exec` already controls the host, so these need no token.
They change only the drain flag the running control reads before starting a run.
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
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
