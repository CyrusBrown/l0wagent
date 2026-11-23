import os
import sys

import uvicorn

from .app import create_app
from .config import from_env


def main() -> None:
    os.umask(0o022)  # extensions are read by control, which runs as another user
    s = from_env()
    try:
        app = create_app(s)
    except ValueError as e:
        sys.exit(f"ops: {e}")
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("OPS_PORT", "8795")), access_log=False)


if __name__ == "__main__":
    main()
