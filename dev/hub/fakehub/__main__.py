"""`python -m fakehub serve` and `python -m fakehub invite`."""

from __future__ import annotations

import argparse
import sys

from l0wproto.invite import format_invite

from .config import Config
from .store import Store


def build_config(a: argparse.Namespace) -> Config:
    return Config.from_env(base_url=a.base_url, domain=a.domain, files_domain=a.files_domain, state_path=a.state,
                           seed_path=a.key, reserved=[n.strip().lower() for n in a.reserved.split(",") if n.strip()] if a.reserved else None, listen_host=getattr(a, "listen", None), listen_port=getattr(a, "port", None))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fakehub", description="A fake l0wagent hub for development.")
    ap.add_argument("--base-url", help="default http://hub.localhost:8700")
    ap.add_argument("--domain", help="the hub domain in addresses (default: the host of the base URL)")
    ap.add_argument("--files-domain", help="parent domain of the files hostnames (default hubfiles.localhost)")
    ap.add_argument("--state", help="state file (default ./fakehub-state.json)")
    ap.add_argument("--reserved", help="comma-separated extra reserved names (besides the hub's own)")
    ap.add_argument("--key", help="hub key seed file (default: next to the state file)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sv = sub.add_parser("serve", help="run the hub")
    sv.add_argument("--listen", help="address to listen on (default 127.0.0.1)")
    sv.add_argument("--port", type=int, help="port (default: the base URL's)")
    iv = sub.add_parser("invite", help="print a new invite string")
    iv.add_argument("--name-hint")
    iv.add_argument("--reregister", metavar="NAME", help="an invite that approves re-registering NAME (both keys lost)")
    iv.add_argument("--bind", metavar="NAME", help="an invite that registers exactly NAME (it may be a name the hub reserved)")
    iv.add_argument("--days", type=int, default=7)
    a = ap.parse_args(argv)
    cfg = build_config(a)
    if a.cmd == "invite":
        try:
            code = Store(cfg.state_path).make_invite(days=a.days, name_hint=a.name_hint, reregister=a.reregister,
                                                     bind=a.bind, reserved_extra=cfg.reserved_extra())
        except ValueError as e:
            print(f"fakehub: {e}", file=sys.stderr)
            return 1
        print(format_invite(cfg.base_url, code))
        return 0
    import uvicorn

    from .hub import create_app

    app = create_app(cfg)
    print(f"fakehub: {cfg.base_url}  (listening on {cfg.listen_host}:{cfg.listen_port}, state {cfg.state_path})", file=sys.stderr)
    uvicorn.run(app, host=cfg.listen_host, port=cfg.listen_port, log_level="warning", ws_ping_interval=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
