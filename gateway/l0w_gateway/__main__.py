import os
import sys

import uvicorn

from .app import create_app


def main() -> None:
    control = os.environ.get("GATEWAY_CONTROL_TOKEN", "")
    tunnel = os.environ.get("GATEWAY_TUNNEL_TOKEN", "")
    try:
        app = create_app(os.environ.get("GATEWAY_DATA_DIR", "/data"), control, tunnel,
                         outbox_interval=max(0.05, float(os.environ.get("GATEWAY_OUTBOX_INTERVAL") or 5)),
                         backoff_first=max(1, int(float(os.environ.get("GATEWAY_OUTBOX_MIN_DELAY") or 30))))
    except ValueError as e:  # a missing token, or a setting that isn't a number
        sys.exit(f"gateway: {e}")
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("GATEWAY_PORT", "8780")), access_log=False)


if __name__ == "__main__":
    main()
