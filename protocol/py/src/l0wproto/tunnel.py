"""Tunnel framing and handshake messages (tunnel.md)."""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass

from . import b64
from .errors import FrameError

SUBPROTOCOL = "l0w-tunnel.0.1"
MAX_DATA = 65536
INITIAL_WINDOW = 262144
HEADER_LEN = 6
PONG_FLAG = 0x01

OPEN, RESPONSE, DATA, END, RESET, CREDIT, PING = 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07
NAMES = {OPEN: "OPEN", RESPONSE: "RESPONSE", DATA: "DATA", END: "END", RESET: "RESET", CREDIT: "CREDIT", PING: "PING"}

# WebSocket close codes
CLOSE_UNAUTHORIZED, CLOSE_REPLACED, CLOSE_UPGRADE_REQUIRED = 4401, 4409, 4426

_JSON_TYPES = (OPEN, RESPONSE, RESET)


@dataclass(frozen=True)
class Frame:
    type: int
    stream_id: int
    payload: bytes = b""
    flags: int = 0

    @property
    def is_pong(self) -> bool:
        return self.type == PING and bool(self.flags & PONG_FLAG)

    def json(self) -> dict:
        """The payload of an OPEN, RESPONSE or RESET frame, validated."""
        return _json_payload(self.type, self.payload)


def _json_payload(type_: int, payload: bytes) -> dict:
    try:
        obj = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise FrameError(f"{NAMES[type_]} payload is not JSON") from e
    if not isinstance(obj, dict):
        raise FrameError(f"{NAMES[type_]} payload must be an object")
    if type_ == OPEN:
        for f in ("method", "target", "host", "client", "scheme"):
            if not isinstance(obj.get(f), str):
                raise FrameError(f"OPEN needs string {f!r}")
        _check_headers(obj.get("headers"))
    elif type_ == RESPONSE:
        if not isinstance(obj.get("status"), int) or isinstance(obj["status"], bool) or not 100 <= obj["status"] <= 599:
            raise FrameError("RESPONSE needs an integer status")
        _check_headers(obj.get("headers"))
    elif type_ == RESET and not isinstance(obj.get("error"), str):
        raise FrameError("RESET needs a string 'error'")
    return obj


def _check_headers(h: object) -> None:
    if not isinstance(h, list) or not all(
        isinstance(p, list) and len(p) == 2 and all(isinstance(x, str) for x in p) for p in h
    ):
        raise FrameError("headers must be a list of [name, value] pairs")
    if any(p[0] != p[0].lower() for p in h):
        raise FrameError("header names must be lowercase")


def encode(f: Frame) -> bytes:
    if f.type not in NAMES:
        raise FrameError(f"unknown frame type {f.type:#x}")
    if not 0 <= f.stream_id <= 0xFFFFFFFF or not 0 <= f.flags <= 0xFF:
        raise FrameError("stream id or flags out of range")
    _validate(f)
    return struct.pack(">BBI", f.type, f.flags, f.stream_id) + f.payload


def decode(data: bytes) -> Frame:
    """Parse one binary WebSocket message. Unknown flags are ignored; unknown types are rejected."""
    if len(data) < HEADER_LEN:
        raise FrameError("frame shorter than its header")
    type_, flags, sid = struct.unpack(">BBI", data[:HEADER_LEN])
    if type_ not in NAMES:
        raise FrameError(f"unknown frame type {type_:#x}")
    f = Frame(type_, sid, bytes(data[HEADER_LEN:]), flags)
    _validate(f)
    return f


def _validate(f: Frame) -> None:
    t, n = f.type, len(f.payload)
    if t == PING:
        if f.stream_id != 0:
            raise FrameError("PING/PONG uses stream id 0")
        if n != 8:
            raise FrameError("PING/PONG payload must be 8 bytes")
        return
    if f.stream_id == 0:
        raise FrameError(f"{NAMES[t]} needs a nonzero stream id")
    if t == DATA and n > MAX_DATA:
        raise FrameError("DATA frame over 65536 bytes")
    elif t == END and n:
        raise FrameError("END has no payload")
    elif t == CREDIT and n != 4:
        raise FrameError("CREDIT payload must be 4 bytes")
    elif t in _JSON_TYPES:
        _json_payload(t, f.payload)


def _j(o: dict) -> bytes:
    return json.dumps(o, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def open_frame(stream_id: int, method: str, target: str, host: str, headers: list[list[str]], client: str, scheme: str = "https") -> Frame:
    if stream_id % 2 == 0:
        raise FrameError("only the hub opens streams, with odd ids")
    return Frame(OPEN, stream_id, _j({"method": method, "target": target, "host": host, "headers": headers,
                                      "client": client, "scheme": scheme}))


def response_frame(stream_id: int, status: int, headers: list[list[str]]) -> Frame:
    return Frame(RESPONSE, stream_id, _j({"status": status, "headers": headers}))


def data_frame(stream_id: int, body: bytes) -> Frame:
    return Frame(DATA, stream_id, body)


def end_frame(stream_id: int) -> Frame:
    return Frame(END, stream_id)


def reset_frame(stream_id: int, error: str) -> Frame:
    return Frame(RESET, stream_id, _j({"error": error}))


def credit_frame(stream_id: int, n: int) -> Frame:
    if not 0 <= n <= 0xFFFFFFFF:
        raise FrameError("credit out of range")
    return Frame(CREDIT, stream_id, struct.pack(">I", n))


def credit_amount(f: Frame) -> int:
    if f.type != CREDIT or len(f.payload) != 4:
        raise FrameError("not a CREDIT frame")
    return struct.unpack(">I", f.payload)[0]


def ping_frame(payload: bytes) -> Frame:
    return Frame(PING, 0, payload)


def pong_for(ping: Frame) -> Frame:
    if ping.type != PING or ping.is_pong:
        raise FrameError("not a PING")
    return Frame(PING, 0, ping.payload, PONG_FLAG)


# --- text messages of the handshake ----------------------------------------------------------


def challenge_message(nonce: bytes, hub: str) -> str:
    if len(nonce) != 32:
        raise FrameError("nonce must be 32 bytes")
    return json.dumps({"t": "challenge", "nonce": b64.encode(nonce), "hub": hub}, separators=(",", ":"))


def ready_message(session: str, limits: dict) -> str:
    return json.dumps({"t": "ready", "session": session, "limits": limits}, separators=(",", ":"))


def parse_text_message(s: str) -> dict:
    try:
        o = json.loads(s)
    except ValueError as e:
        raise FrameError("handshake message is not JSON") from e
    if not isinstance(o, dict) or o.get("t") not in ("challenge", "ready"):
        raise FrameError("unknown handshake message")
    if o["t"] == "challenge":
        if not isinstance(o.get("hub"), str) or len(b64.decode(o.get("nonce") or "")) != 32:
            raise FrameError("bad challenge")
    elif not isinstance(o.get("session"), str) or not isinstance(o.get("limits"), dict):
        raise FrameError("bad ready")
    return o


DROP_HEADERS = frozenset({"connection", "keep-alive", "transfer-encoding", "upgrade", "te", "trailer"})


def filter_request_headers(headers: list[tuple[str, str]]) -> list[list[str]]:
    """What the hub MUST do before OPEN: lowercase names; drop hop-by-hop, proxy-* and x-l0w-tunnel-*."""
    out = []
    for k, v in headers:
        k = k.lower()
        if k in DROP_HEADERS or k.startswith(("proxy-", "x-l0w-tunnel-")):
            continue
        out.append([k, v])
    return out
