import json
import struct

import pytest

from l0wproto import b64, tunnel as T
from l0wproto.errors import FrameError


def rt(f):
    d = T.decode(T.encode(f))
    assert d == f
    return d


def test_layout():
    b = T.encode(T.Frame(T.DATA, 0x01020304, b"xyz"))
    assert b == bytes([3, 0, 1, 2, 3, 4]) + b"xyz"


def test_roundtrips():
    o = rt(T.open_frame(1, "GET", "/a?b=c", "mara.hub.example", [["accept", "*/*"]], "203.0.113.9"))
    assert o.json()["host"] == "mara.hub.example" and o.json()["scheme"] == "https"
    assert rt(T.response_frame(1, 204, [])).json()["status"] == 204
    assert rt(T.data_frame(5, b"")).payload == b""
    rt(T.data_frame(5, b"\x00" * T.MAX_DATA))
    rt(T.end_frame(7))
    assert rt(T.reset_frame(3, "flow_control")).json() == {"error": "flow_control"}
    assert T.credit_amount(rt(T.credit_frame(1, 0xFFFFFFFF))) == 0xFFFFFFFF
    p = rt(T.ping_frame(b"12345678"))
    assert not p.is_pong
    q = rt(T.pong_for(p))
    assert q.is_pong and q.flags == 1 and q.payload == p.payload


def test_data_limit():
    with pytest.raises(FrameError):
        T.encode(T.data_frame(1, b"\x00" * (T.MAX_DATA + 1)))
    with pytest.raises(FrameError):
        T.decode(struct.pack(">BBI", 3, 0, 1) + b"\x00" * (T.MAX_DATA + 1))


def test_unknown_flags_ignored():
    f = T.decode(struct.pack(">BBI", 4, 0xFE, 9))
    assert f.type == T.END and f.flags == 0xFE
    p = T.decode(struct.pack(">BBI", 7, 0xFE, 0) + b"12345678")
    assert not p.is_pong
    assert T.decode(struct.pack(">BBI", 7, 0x03, 0) + b"12345678").is_pong


@pytest.mark.parametrize("raw", [
    b"", b"\x03\x00\x00\x00\x00", b"\x00\x00\x00\x00\x00\x01", b"\x08\x00\x00\x00\x00\x01", b"\xff\x00\x00\x00\x00\x01",
    struct.pack(">BBI", 3, 0, 0) + b"x",          # DATA on stream 0
    struct.pack(">BBI", 4, 0, 1) + b"x",          # END with payload
    struct.pack(">BBI", 6, 0, 1) + b"\x00\x00\x00",  # CREDIT 3 bytes
    struct.pack(">BBI", 6, 0, 1) + b"\x00" * 5,
    struct.pack(">BBI", 7, 0, 1) + b"12345678",   # PING on a stream
    struct.pack(">BBI", 7, 0, 0) + b"1234567",
    struct.pack(">BBI", 2, 0, 1) + b"not json",
    struct.pack(">BBI", 2, 0, 1) + b"[]",
    struct.pack(">BBI", 2, 0, 1) + b'{"status":"200","headers":[]}',
    struct.pack(">BBI", 2, 0, 1) + b'{"status":200}',
    struct.pack(">BBI", 2, 0, 1) + b'{"status":200,"headers":[["Upper","x"]]}',
    struct.pack(">BBI", 2, 0, 1) + b'{"status":200,"headers":[["a"]]}',
    struct.pack(">BBI", 5, 0, 1) + b"{}",
    struct.pack(">BBI", 1, 0, 1) + b'{"method":"GET"}',
    struct.pack(">BBI", 2, 0, 1) + b"\xff\xfe",
])
def test_malformed(raw):
    with pytest.raises(FrameError):
        T.decode(raw)


def test_encode_rejects():
    for f in (T.Frame(0x20, 1), T.Frame(T.END, -1), T.Frame(T.END, 2**32), T.Frame(T.DATA, 0, b"x"), T.Frame(T.PING, 0, b"x"),
              T.Frame(T.CREDIT, 1, b"x")):
        with pytest.raises(FrameError):
            T.encode(f)
    with pytest.raises(FrameError):
        T.open_frame(2, "GET", "/", "h", [], "1.1.1.1")
    with pytest.raises(FrameError):
        T.pong_for(T.pong_for(T.ping_frame(b"12345678")))


def test_handshake_messages():
    c = json.loads(T.challenge_message(b"\x07" * 32, "hub.example"))
    assert c["t"] == "challenge" and len(b64.decode(c["nonce"])) == 32
    assert T.parse_text_message(T.challenge_message(b"\x07" * 32, "h"))["hub"] == "h"
    assert T.parse_text_message(T.ready_message("s1", {"tunnel_streams": 64}))["session"] == "s1"
    for bad in ("nope", "[]", '{"t":"x"}', '{"t":"challenge","nonce":"AA","hub":"h"}', '{"t":"ready"}'):
        with pytest.raises(FrameError):
            T.parse_text_message(bad)
    with pytest.raises(FrameError):
        T.challenge_message(b"x", "h")


def test_filter_headers():
    out = T.filter_request_headers([("Host", "a"), ("Connection", "close"), ("Keep-Alive", "1"), ("Transfer-Encoding", "chunked"),
                                    ("Upgrade", "websocket"), ("TE", "x"), ("Trailer", "x"), ("Proxy-Authorization", "x"),
                                    ("X-L0W-Tunnel-Client", "6.6.6.6"), ("Accept", "*/*")])
    assert out == [["host", "a"], ["accept", "*/*"]]


def test_constants():
    assert (T.MAX_DATA, T.INITIAL_WINDOW, T.SUBPROTOCOL) == (65536, 262144, "l0w-tunnel.0.1")
    assert (T.CLOSE_UNAUTHORIZED, T.CLOSE_REPLACED, T.CLOSE_UPGRADE_REQUIRED) == (4401, 4409, 4426)
