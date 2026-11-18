import os
import sys

import uvicorn

from .app import create_app


def main() -> None:
    control = os.environ.get("GATEWAY_CONTROL_TOKEN", "")
    tunnel = os.environ.get("GATEWAY_TUNNEL_TOKEN", "")
    try:
        app = create_app(os.environ.get("GATEWAY_DATA_DIR", "/data"), control, tunnel)
    except ValueError as e:
        sys.exit(f"gateway: {e}")
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("GATEWAY_PORT", "8780")), access_log=False)


if __name__ == "__main__":
    main()
