from __future__ import annotations

import argparse
import asyncio
import sys

from .harness import Net, Report
from .suite import Suite


async def run(hub: str, invite_cmd: str, *, connect: str | None = None, insecure: bool = False, bind_name: str | None = "opsname", verbose: bool = False) -> int:
    report = Report(verbose)
    net = Net(hub, connect, insecure)
    try:
        await Suite(net, invite_cmd, report, bind_name).run()
    finally:
        await net.close()
    print(f"\n{report.summary()}")
    return 1 if report.failed else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="conformance", description="Check a hub against the l0wagent hub protocol (l0w/0.1).")
    ap.add_argument("--hub", required=True, help="the hub's base URL, e.g. https://hub.example")
    ap.add_argument("--invite-cmd", required=True, help="a shell command that prints a fresh l0w-invite: string (run once per registration)")
    ap.add_argument("--connect", metavar="HOST:PORT", help="open every connection to this address, keeping Host and TLS names (for a hub whose names do not resolve)")
    ap.add_argument("--insecure", action="store_true", help="do not verify TLS certificates")
    ap.add_argument("--bind-name", default="opsname", help="a name the hub reserved (its own name or an extra one); the bound-invite checks run the invite command with `--bind NAME` for it and skip when that is unsupported. Default: opsname")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    return asyncio.run(run(a.hub, a.invite_cmd, connect=a.connect, insecure=a.insecure, bind_name=a.bind_name or None, verbose=a.verbose))


if __name__ == "__main__":
    sys.exit(main())
