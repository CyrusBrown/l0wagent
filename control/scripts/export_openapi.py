"""Write the OpenAPI spec to control/openapi.json (the contract the web app's types come from).

    uv run python scripts/export_openapi.py          write it
    uv run python scripts/export_openapi.py --check  fail if it's out of date (CI)
"""

import json
import sys
from pathlib import Path

from l0w_control.app import create_app
from l0w_control.backends.base import Backend

OUT = Path(__file__).resolve().parent.parent / "openapi.json"


def render() -> str:
    return json.dumps(create_app(Backend()).openapi(), indent=2, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    spec = render()
    if "--check" in sys.argv:
        if not OUT.exists() or OUT.read_text() != spec:
            sys.exit("control/openapi.json is out of date: run `uv run python scripts/export_openapi.py`")
        print("openapi.json is up to date")
    else:
        OUT.write_text(spec)
        print(f"wrote {OUT}")
