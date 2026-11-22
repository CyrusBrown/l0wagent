"""Run control on 127.0.0.1 and [::1] at once, on one port (uvicorn's own `--host ::` isn't dual-stack on every OS).

    python dual_stack_control.py PORT

Used by `HubStack(tunnel_from_ipv6=True)`: the hub tunnel service reaches control over ::1 and is the only caller `L0W_TUNNEL_SUBNET`
(`::1/128`) names; the tests, the owner's client and the runner use 127.0.0.1 and so are not the tunnel."""

import socket
import sys

import uvicorn


def main() -> None:
    port = int(sys.argv[1])
    socks = []
    for family, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        s = socket.socket(family, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if family == socket.AF_INET6:
            s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        s.bind((host, port))
        s.listen(100)
        s.set_inheritable(True)
        socks.append(s)
    uvicorn.Server(uvicorn.Config("l0w_control.app:app")).run(sockets=socks)


if __name__ == "__main__":
    main()
