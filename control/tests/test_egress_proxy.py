"""Egress: the decision table, the TLS server-name parser and the tunnel gate."""

import asyncio
import importlib.util
import ipaddress
import ssl
from pathlib import Path

import pytest


def _load(monkeypatch, tmp_path, name="egress_proxy_under_test"):
    monkeypatch.delenv("CHAT_SUBNET", raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[2] / "egress" / "egress.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture
def eg(monkeypatch, tmp_path):
    return _load(monkeypatch, tmp_path)


def client_hello(server_name: str | None) -> bytes:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    obj = ctx.wrap_bio(incoming, outgoing, server_hostname=server_name)
    try:
        obj.do_handshake()
    except ssl.SSLWantReadError:
        pass
    return outgoing.read()


def _fake_dns(monkeypatch, mapping):
    async def getaddrinfo(self, host, port, **kw):
        if host not in mapping:
            import socket

            raise socket.gaierror("nope")
        return [(2, 1, 6, "", (ip, port)) for ip in mapping[host]]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", getaddrinfo)


# ------------------------------------------------------------------ parser

@pytest.mark.parametrize("name", ["api.anthropic.com", "Example.COM", "a.b.c.example.org"])
def test_parse_sni_real_hello(eg, name):
    assert eg.parse_sni(client_hello(name)) == name.lower()


def test_parse_sni_no_server_name(eg):
    assert eg.parse_sni(client_hello(None)) is None


def test_parse_sni_split_and_garbage(eg):
    hello = client_hello("api.anthropic.com")
    for cut in (1, 4, 5, 6, 50, len(hello) - 1):
        assert eg.parse_sni(hello[:cut]) is None  # incomplete is never a guess
    assert eg.parse_sni(b"") is None
    assert eg.parse_sni(b"GET / HTTP/1.1\r\n\r\n") is None
    assert eg.parse_sni(b"SSH-2.0-OpenSSH_9.6\r\n") is None
    assert eg.parse_sni(b"\x16\x03\x01\x00\x05\x01\x00\x00\x01\x00") is None
    for i in range(0, len(hello), 7):  # corrupt a byte anywhere: never raises
        bad = bytearray(hello)
        bad[i] ^= 0xFF
        eg.parse_sni(bytes(bad))


def test_parse_sni_hello_split_across_records(eg):
    hello = client_hello("api.anthropic.com")
    body = hello[5:]
    cut = 40
    two = b"\x16\x03\x01" + cut.to_bytes(2, "big") + body[:cut] + b"\x16\x03\x01" + (len(body) - cut).to_bytes(2, "big") + body[cut:]
    assert eg.parse_sni(two) == "api.anthropic.com"


def test_read_client_hello_reassembles_reads(eg):
    hello = client_hello("example.com")

    async def go(chunks, hold=False):
        r = asyncio.StreamReader()
        for c in chunks:
            r.feed_data(c)
        if not hold:
            r.feed_eof()
        return await eg.read_client_hello(r)

    async def run():
        r = asyncio.StreamReader()

        async def feed():
            for i in range(0, len(hello), 100):
                await asyncio.sleep(0.01)
                r.feed_data(hello[i:i + 100])

        t = asyncio.create_task(feed())
        got = await eg.read_client_hello(r)
        await t
        assert got == hello
        assert await go([b"SSH-2.0-x\r\n"], hold=True) == b"SSH-2.0-x\r\n"  # not TLS: returned at once
        eg.HELLO_TIMEOUT = 0.1
        assert await go([hello[:20]], hold=True) == hello[:20]  # stalls: what we have comes back

    asyncio.run(run())


# ------------------------------------------------------------------ patterns

@pytest.mark.parametrize("p", [".", "..", "*.", "*..", ":443", "-", "a b"])
def test_parse_pattern_rejects_empty_domains(eg, p):
    with pytest.raises(ValueError):
        eg.parse_pattern(p)


def test_parse_pattern_accepts_and_match_needs_domain_or_net(eg):
    assert eg.parse_pattern("*.Example.com.")["domain"] == "example.com"
    assert eg.parse_pattern("10.0.0.0/8")["net"] == ipaddress.ip_network("10.0.0.0/8")
    assert eg._match({"port": None, "domain": None, "net": None}, "x.com", 80, [ipaddress.ip_address("1.1.1.1")]) is False


# ------------------------------------------------------------------ decide

def _rule(eg, pattern, action):
    eg.db_exec("INSERT INTO rules(pattern, action) VALUES(?,?)", (pattern, action))


def test_decide_table(eg, monkeypatch):
    _fake_dns(monkeypatch, {
        "public.test": ["93.184.216.34"],
        "lan.test": ["192.168.1.5"],
        "rebind.test": ["93.184.216.34", "10.0.0.7"],
        "api.anthropic.com": ["160.79.104.10"],
        "blocked.test": ["93.184.216.35"],
    })
    d = lambda *a: asyncio.run(eg.decide(*a))  # noqa: E731

    ok, _, ip, ro = d("public.test", 443, "CONNECT")
    assert (ok, ip, ro) == (True, "93.184.216.34", False)
    # private addresses, literal or resolved, are refused without a rule
    for host in ("192.168.1.9", "127.0.0.1", "169.254.169.254", "[::1]", "lan.test"):
        ok, why, ip, _ = d(host, 80, "GET")
        assert not ok and "private" in why and ip is None, host
    # DNS rebinding: one private answer among public ones is still refused
    assert not d("rebind.test", 80, "GET")[0]
    assert not d("nx.test", 80, "GET")[0]

    _rule(eg, "192.168.0.0/16", "allow_ro")
    ok, _, ip, ro = d("lan.test", 80, "GET")
    assert (ok, ip, ro) == (True, "192.168.1.5", True)
    assert not d("lan.test", 80, "POST")[0]
    assert not d("lan.test", 443, "CONNECT")[0]

    _rule(eg, "blocked.test", "block")
    assert "blocked by rule" in d("blocked.test", 80, "GET")[1]
    assert "blocked by rule" in d("sub.blocked.test", 80, "GET")[1]
    _rule(eg, "blocked.test", "allow")
    assert d("blocked.test", 80, "GET")[0]  # an allow rule outranks a block
    _rule(eg, "public.test:8080", "block")
    assert d("public.test", 443, "CONNECT")[0] and not d("public.test", 8080, "GET")[0]


def test_decide_lease(eg, monkeypatch):
    _fake_dns(monkeypatch, {"api.anthropic.com": ["160.79.104.10"]})
    assert "no active lease" in asyncio.run(eg.decide("api.anthropic.com", 443, "CONNECT"))[1]
    eg.Lease.until = 1e18
    try:
        assert asyncio.run(eg.decide("api.anthropic.com", 443, "CONNECT"))[0]
    finally:
        eg.Lease.until = 0


def test_sni_verdict(eg):
    assert "no active lease" in eg.sni_verdict("api.anthropic.com", 443, False)
    assert eg.sni_verdict("example.com", 443, False) is None
    _rule(eg, "evil.test", "block")
    assert "blocked by rule" in eg.sni_verdict("x.evil.test", 443, False)
    eg.Lease.until = 1e18
    try:
        assert eg.sni_verdict("opencode.ai", 443, False) is None
    finally:
        eg.Lease.until = 0
    assert eg.sni_verdict("api.anthropic.com", 443, True) is None
    assert eg.sni_verdict("example.com", 443, True)


# ------------------------------------------------------------------ the tunnel

def test_tunnel_with_a_gated_server_name_is_closed_without_a_lease(eg, monkeypatch):
    """CONNECT to a harmless host, TLS server name api.anthropic.com: no lease, so the tunnel dies."""
    hello = client_hello("api.anthropic.com")

    async def run():
        got = []

        async def upstream(r, w):
            got.append(await r.read(65536))
            w.close()

        up = await asyncio.start_server(upstream, "127.0.0.1", 0)
        up_port = up.sockets[0].getsockname()[1]
        eg.decide = lambda host, port, method="GET": _async((True, "", "127.0.0.1", False))
        proxy = await asyncio.start_server(eg.handle_client, "127.0.0.1", 0)
        port = proxy.sockets[0].getsockname()[1]

        async def connect(hello_bytes):
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.write(f"CONNECT 127.0.0.1:{up_port} HTTP/1.1\r\n\r\n".encode())
            assert (await r.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200")
            w.write(hello_bytes)
            await w.drain()
            data = await asyncio.wait_for(r.read(), 5)
            w.close()
            return data

        await connect(hello)
        await asyncio.sleep(0.1)
        assert not any(got)  # nothing reached upstream
        rows = eg.db_query("SELECT * FROM requests WHERE action='blocked'")
        assert rows and rows[0]["host"] == "api.anthropic.com"

        got.clear()
        fine = client_hello("example.com")
        await connect(fine)
        await asyncio.sleep(0.1)
        assert got == [fine]  # forwarded unchanged
        got.clear()
        await connect(b"SSH-2.0-x\r\n")
        await asyncio.sleep(0.1)
        assert got == [b"SSH-2.0-x\r\n"]

        # A hello that stalls before its server name is readable is refused, not forwarded unvetted.
        got.clear()
        monkeypatch.setattr(eg, "HELLO_TIMEOUT", 0.3)
        await connect(hello[:8])
        await asyncio.sleep(0.1)
        assert not any(got)
        proxy.close()
        up.close()

    asyncio.run(run())


async def _async(v):
    return v


def test_agent_side_pattern_check_rejects_dots():
    from l0w_control.conv import BadRequest, _check_action

    for bad in (".", "..", "*.", ":443", "-"):
        with pytest.raises(BadRequest):
            _check_action({"kind": "egress_rule", "pattern": bad})
    assert _check_action({"kind": "egress_rule", "pattern": "*.example.org:443"})[1]["mode"] == "allow_ro"


def test_read_only_request_drops_method_override_headers(eg, monkeypatch):
    seen = []

    async def run():
        async def upstream(r, w):
            seen.append(await r.readuntil(b"\r\n\r\n"))
            w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
            await w.drain()
            w.close()

        up = await asyncio.start_server(upstream, "127.0.0.1", 0)
        up_port = up.sockets[0].getsockname()[1]
        eg.decide = lambda host, port, method="GET": _async((True, "", "127.0.0.1", True))
        proxy = await asyncio.start_server(eg.handle_client, "127.0.0.1", 0)
        r, w = await asyncio.open_connection("127.0.0.1", proxy.sockets[0].getsockname()[1])
        w.write((f"GET http://lan.test:{up_port}/x HTTP/1.1\r\nHost: lan.test\r\nX-HTTP-Method-Override: DELETE\r\n"
                 "x-method-override: PUT\r\nX-HTTP-Method: POST\r\nX-Keep: 1\r\n\r\n").encode())
        await asyncio.wait_for(r.read(), 5)
        proxy.close()
        up.close()

    asyncio.run(run())
    head = seen[0].lower()
    assert b"x-keep: 1" in head and b"override" not in head and b"x-http-method" not in head
