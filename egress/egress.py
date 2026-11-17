"""Egress proxy: the sandbox's only route to the outside world. (Carried over from the old assistant.)

- HTTP forward proxy on :3128 (plain HTTP requests + CONNECT tunnels for HTTPS).
- Logs every request (full URL for plain HTTP; host:port for HTTPS, which isn't decrypted).
- Rules: block | allow | allow_ro (read-only: GET/HEAD/OPTIONS over plain HTTP).
- Blocks: user block rules; private/LAN/loopback/link-local addresses unless an allow rule
  matches; and "gated" hosts (model providers) unless control holds an active lease. The
  lease is in memory only, so restarting egress cuts off model access until control renews it.
- Connects to the exact IP it vetted, so DNS rebinding can't sneak past the private-address check.
- Admin API on :8090 for the control plane only (token + refuses requests from the sandbox subnet).

The agent has root in its own container but no route anywhere except through here.
"""
import asyncio
import ipaddress
import logging
import os
import re
import secrets
import socket
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s egress: %(message)s")
log = logging.getLogger("egress")

TOKEN = os.environ.get("EGRESS_TOKEN", "")
SANDBOX_NET = ipaddress.ip_network(os.environ.get("SANDBOX_SUBNET", "172.31.240.0/24"), strict=False)
# The chat-runtime sidecar's network: it may reach only CHAT_HOSTS (Claude's API), whatever the rules
# or the lease say; the chat is the owner's own usage, so it needs no run lease.
CHAT_NET = ipaddress.ip_network(os.environ["CHAT_SUBNET"], strict=False) if os.environ.get("CHAT_SUBNET") else None
CHAT_HOSTS = [h.strip().lower() for h in os.environ.get("CHAT_HOSTS", "api.anthropic.com").split(",") if h.strip()]
GATED_HOSTS = [h.strip().lower() for h in os.environ.get("GATED_HOSTS", "api.anthropic.com,opencode.ai").split(",") if h.strip()]
DEFAULT_POLICY = os.environ.get("DEFAULT_POLICY", "allow").lower()  # allow | deny (allowlist mode)
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "90"))
DATA_DIR = os.environ.get("DATA_DIR", "/data")
BIND = os.environ.get("BIND", "0.0.0.0")
PROXY_PORT = int(os.environ.get("PROXY_PORT", "3128"))
ADMIN_PORT = int(os.environ.get("ADMIN_PORT", "8090"))

# ------------------------------------------------------------------ storage

_lock = threading.RLock()
os.makedirs(DATA_DIR, exist_ok=True)
_db = sqlite3.connect(os.path.join(DATA_DIR, "egress.db"), check_same_thread=False, isolation_level=None)
_db.row_factory = sqlite3.Row
_db.executescript(
    """
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS requests (
        id INTEGER PRIMARY KEY, ts TEXT NOT NULL, client TEXT, method TEXT, host TEXT, port INTEGER,
        url TEXT, action TEXT, reason TEXT, status INTEGER, bytes_up INTEGER, bytes_down INTEGER,
        duration_ms INTEGER
    );
    CREATE INDEX IF NOT EXISTS requests_ts ON requests(ts);
    CREATE INDEX IF NOT EXISTS requests_host ON requests(host);
    CREATE TABLE IF NOT EXISTS rules (
        id INTEGER PRIMARY KEY, pattern TEXT NOT NULL, action TEXT NOT NULL, note TEXT, created_at TEXT
    );
    """
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_exec(sql: str, params=()) -> int:
    with _lock:
        return _db.execute(sql, params).lastrowid


def db_query(sql: str, params=()) -> list[dict]:
    with _lock:
        return [dict(r) for r in _db.execute(sql, params).fetchall()]


# ------------------------------------------------------------------ policy

class Lease:
    until: float = 0.0
    reason: str = ""

    @classmethod
    def active(cls) -> bool:
        return time.time() < cls.until


_tunnels: set[tuple[str, asyncio.StreamWriter, asyncio.StreamWriter]] = set()


def is_gated(host: str) -> bool:
    return any(host == g or host.endswith("." + g) for g in GATED_HOSTS)


def parse_pattern(pattern: str) -> dict:
    p = pattern.strip().lower()
    port = None
    if p.count(":") == 1:
        p, port_s = p.split(":")
        port = int(port_s)
    if p.startswith("*."):
        p = p[2:]
    try:
        return {"net": ipaddress.ip_network(p, strict=False), "domain": None, "port": port}
    except ValueError:
        pass
    domain = p.strip(".")
    if not domain or " " in domain or "/" in domain or not re.search(r"[a-z0-9]", domain):
        raise ValueError(f"invalid pattern: {pattern}")
    return {"net": None, "domain": domain, "port": port}


def _load_rules() -> list[dict]:
    rules = []
    for r in db_query("SELECT * FROM rules ORDER BY id"):
        try:
            rules.append({**r, **parse_pattern(r["pattern"])})
        except ValueError:
            continue
    return rules


def _match(rule: dict, host: str, port: int, ips: list) -> bool:
    if rule["port"] and rule["port"] != port:
        return False
    if rule["domain"]:
        return host == rule["domain"] or host.endswith("." + rule["domain"])
    if rule["net"] is None:
        return False
    return any(ip in rule["net"] for ip in ips)


def _host_ip(host: str):
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


READ_METHODS = ("GET", "HEAD", "OPTIONS")


def from_chat(client: str) -> bool:
    try:
        return CHAT_NET is not None and ipaddress.ip_address(client) in CHAT_NET
    except ValueError:
        return False


async def decide_chat(host: str, port: int) -> tuple[bool, str, str | None, bool]:
    """The chat sidecar: its model host over TLS (CONNECT to 443), nothing else."""
    host = host.lower().rstrip(".")
    if host not in CHAT_HOSTS or port != 443:
        return False, "the chat runtime may only reach the model provider", None, False
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        return False, f"DNS lookup failed: {e}", None, False
    ip = next((info[4][0] for info in infos if ipaddress.ip_address(info[4][0]).is_global), None)
    return (True, "", ip, False) if ip else (False, "the model provider resolved to no public address", None, False)


async def decide(host: str, port: int, method: str = "GET") -> tuple[bool, str, str | None, bool]:
    """Return (allowed, reason, ip_to_connect, read_only).

    Rule actions: block | allow | allow_ro. allow_ro (read-only) permits only GET/HEAD/OPTIONS over
    plain HTTP, which is how LAN services like the *arr apps, qBittorrent or Glances are reached,
    so API keys with write access can be handed over safely."""
    host = host.lower().rstrip(".")
    method = method.upper()
    if is_gated(host) and not Lease.active():
        return False, "model providers are only reachable during runs (no active lease)", None, False
    rules = _load_rules()
    literal = _host_ip(host)
    pre_ips = [literal] if literal else []

    def best_allow(ips):
        matches = [r for r in rules if r["action"] in ("allow", "allow_ro") and _match(r, host, port, ips)]
        return next((r for r in matches if r["action"] == "allow"), matches[0] if matches else None)

    allow = best_allow(pre_ips)
    block = next((r for r in rules if r["action"] == "block" and _match(r, host, port, pre_ips)), None)
    if block and not allow:
        return False, f"blocked by rule '{block['pattern']}'", None, False
    if literal:
        ips = [literal]
    else:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as e:
            return False, f"DNS lookup failed: {e}", None, False
        ips = []
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if ip not in ips:
                ips.append(ip)
    if not ips:
        return False, "no addresses", None, False
    # IP/CIDR rules can match resolved addresses too.
    allow = allow or best_allow(ips)
    block = next((r for r in rules if r["action"] == "block" and _match(r, host, port, ips)), None)
    if block and not allow:
        return False, f"blocked by rule '{block['pattern']}'", None, False
    read_only = bool(allow and allow["action"] == "allow_ro")
    if read_only:
        if method == "CONNECT":
            return False, f"read-only rule '{allow['pattern']}' only permits plain-HTTP reads (no HTTPS tunnels)", None, False
        if method not in READ_METHODS:
            return False, f"read-only rule '{allow['pattern']}': {method} not allowed", None, False
    if not allow:
        private = [ip for ip in ips if not ip.is_global]
        if private:
            return False, f"private/LAN address {private[0]} (add an allow rule to permit)", None, False
        if DEFAULT_POLICY == "deny":
            return False, "not on the allowlist (DEFAULT_POLICY=deny)", None, False
    # Prefer IPv4 (Docker networks are usually v4-only).
    ip = next((ip for ip in ips if ip.version == 4), ips[0])
    reason = f"allowed by {'read-only ' if read_only else ''}rule '{allow['pattern']}'" if allow else ""
    return True, reason, str(ip), read_only


# ------------------------------------------------------------------ proxy

def log_request(**f) -> int:
    return db_exec(
        "INSERT INTO requests(ts, client, method, host, port, url, action, reason, status, bytes_up, bytes_down, duration_ms) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (now_iso(), f.get("client"), f.get("method"), f.get("host"), f.get("port"), f.get("url"), f.get("action"),
         f.get("reason"), f.get("status"), f.get("bytes_up", 0), f.get("bytes_down", 0), f.get("duration_ms")),
    )


def finish_request(row_id: int, **f) -> None:
    cols = ", ".join(f"{k}=?" for k in f)
    db_exec(f"UPDATE requests SET {cols} WHERE id=?", (*f.values(), row_id))


async def _respond(writer: asyncio.StreamWriter, code: int, msg: str, body: str) -> None:
    payload = body.encode()
    writer.write(
        f"HTTP/1.1 {code} {msg}\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: {len(payload)}\r\n"
        f"X-Egress-Proxy: blocked\r\nConnection: close\r\n\r\n".encode() + payload
    )
    try:
        await writer.drain()
    except Exception:  # noqa: BLE001
        pass
    writer.close()


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, counter: dict, key: str, first: list | None = None):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            if first is not None and not first:
                first.append(data[:64])
            counter[key] += len(data)
            writer.write(data)
            await writer.drain()
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


# ------------------------------------------------------------------ TLS server name (SNI)

HELLO_MAX = 16384
HELLO_TIMEOUT = 5.0


def _handshake_bytes(data: bytes) -> tuple[bytes | None, bool]:
    """Join the handshake records at the start of `data`. Returns (payload, complete): payload is None
    when `data` doesn't start with a TLS handshake record; complete says the first handshake message
    is all there (a hello may be split over several records or reads)."""
    if len(data) < 1 or data[0] != 0x16:
        return None, False
    payload, pos = b"", 0
    while pos + 5 <= len(data) and data[pos] == 0x16:
        n = int.from_bytes(data[pos + 3:pos + 5], "big")
        payload += data[pos + 5:pos + 5 + n]
        pos += 5 + n
        if len(payload) >= 4 and len(payload) >= 4 + int.from_bytes(payload[1:4], "big"):
            return payload, True
    return payload, False


def parse_sni(data: bytes) -> str | None:
    """The server_name from a TLS ClientHello at the start of `data`, or None (not TLS, no SNI, malformed)."""
    try:
        payload, complete = _handshake_bytes(data)
        if not payload or not complete or payload[0] != 1:
            return None
        end = 4 + int.from_bytes(payload[1:4], "big")
        i = 4 + 2 + 32  # version, random
        i += 1 + payload[i]  # session id
        i += 2 + int.from_bytes(payload[i:i + 2], "big")  # cipher suites
        i += 1 + payload[i]  # compression
        if i + 2 > end:
            return None
        ext_end = i + 2 + int.from_bytes(payload[i:i + 2], "big")
        i += 2
        ext_end = min(ext_end, end)
        while i + 4 <= ext_end:
            etype = int.from_bytes(payload[i:i + 2], "big")
            elen = int.from_bytes(payload[i + 2:i + 4], "big")
            body = payload[i + 4:i + 4 + elen]
            i += 4 + elen
            if etype != 0:
                continue
            # server_name_list: u16 length, then (u8 type, u16 length, name)
            j = 2
            while j + 3 <= len(body):
                ntype, nlen = body[j], int.from_bytes(body[j + 1:j + 3], "big")
                name = body[j + 3:j + 3 + nlen]
                j += 3 + nlen
                if ntype == 0 and name:
                    return name.decode("ascii").lower().rstrip(".") or None
            return None
    except (IndexError, ValueError, UnicodeDecodeError):
        return None
    return None


async def read_client_hello(reader: asyncio.StreamReader) -> bytes:
    """The client's first bytes after the tunnel opens: up to a whole ClientHello (HELLO_MAX bytes,
    HELLO_TIMEOUT seconds). Anything that doesn't start like TLS is returned as soon as it arrives."""
    data = b""
    deadline = time.monotonic() + HELLO_TIMEOUT
    while len(data) < HELLO_MAX:
        left = deadline - time.monotonic()
        if left <= 0:
            break
        try:
            chunk = await asyncio.wait_for(reader.read(HELLO_MAX - len(data)), timeout=left)
        except TimeoutError:
            break
        if not chunk:
            break
        data += chunk
        payload, complete = _handshake_bytes(data)
        if payload is None or complete:
            break
    return data


def sni_verdict(sni: str, port: int, chat: bool) -> str | None:
    """A reason to refuse the tunnel because of the TLS server name, or None."""
    if chat:
        return None if sni in CHAT_HOSTS else "the chat runtime may only reach the model provider"
    if is_gated(sni) and not Lease.active():
        return "model providers are only reachable during runs (no active lease)"
    rules = _load_rules()
    allowed = any(r["action"] in ("allow", "allow_ro") and _match(r, sni, port, []) for r in rules)
    block = next((r for r in rules if r["action"] == "block" and _match(r, sni, port, [])), None)
    if block and not allowed:
        return f"blocked by rule '{block['pattern']}'"
    return None


def _split_hostport(target: str, default_port: int) -> tuple[str, int]:
    if target.startswith("["):
        host, _, rest = target[1:].partition("]")
        return host, int(rest[1:]) if rest.startswith(":") else default_port
    if target.count(":") == 1:
        host, port = target.rsplit(":", 1)
        return host, int(port)
    return target, default_port


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    client = (writer.get_extra_info("peername") or ["?"])[0]
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=30)
    except Exception:  # noqa: BLE001
        writer.close()
        return
    lines = head.decode("latin-1").split("\r\n")
    try:
        method, target, version = lines[0].split(" ", 2)
    except ValueError:
        await _respond(writer, 400, "Bad Request", "malformed request line")
        return
    started = time.monotonic()
    counter = {"up": 0, "down": 0}

    if method.upper() == "CONNECT":
        try:
            host, port = _split_hostport(target, 443)
        except ValueError:
            await _respond(writer, 400, "Bad Request", "bad CONNECT target")
            return
        url = None
    else:
        parts = urlsplit(target)
        if parts.scheme != "http" or not parts.hostname:
            await _respond(writer, 400, "Bad Request", "only absolute http:// URLs or CONNECT are supported")
            return
        host, port = parts.hostname, parts.port or 80
        url = target

    host = host.lower()
    if from_chat(client):
        allowed, reason, ip, read_only = await decide_chat(host, port)
    else:
        allowed, reason, ip, read_only = await decide(host, port, method)
    if not allowed:
        log_request(client=client, method=method, host=host, port=port, url=url, action="blocked", reason=reason, status=403)
        await _respond(writer, 403, "Forbidden", f"Blocked by egress policy: {reason}\n")
        return

    try:
        up_reader, up_writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout=20)
    except Exception as e:  # noqa: BLE001
        log_request(client=client, method=method, host=host, port=port, url=url, action="error", reason=f"connect failed: {e}", status=502)
        await _respond(writer, 502, "Bad Gateway", f"egress could not connect to {host}:{port}: {e}\n")
        return

    row = log_request(client=client, method=method, host=host, port=port, url=url, action="allowed", reason=reason or None)
    first: list = []
    if method.upper() == "CONNECT":
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        status = 200
    else:
        parts = urlsplit(target)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        drop = ("proxy-", "connection:", "keep-alive:")
        if read_only:  # a read-only rule must not let a header turn the GET into a write upstream
            drop += ("x-http-method-override:", "x-method-override:", "x-http-method:")
        headers = [h for h in lines[1:] if h and not h.lower().startswith(drop)]
        up_writer.write((f"{method} {path} {version}\r\n" + "\r\n".join(headers + ["Connection: close"]) + "\r\n\r\n").encode("latin-1"))
        await up_writer.drain()
        status = None

    entry = (host, writer, up_writer)
    _tunnels.add(entry)
    sni_entry = None
    pre_down = None
    try:
        if status == 200:
            # The CONNECT line can name a harmless host while the TLS server name says otherwise, so
            # read the ClientHello and vet that name too. Start the server->client pipe first so
            # protocols where the server speaks first don't stall.
            pre_down = asyncio.create_task(_pipe(up_reader, writer, counter, "down"))
            hello = await read_client_hello(reader)
            sni = parse_sni(hello)
            started_tls, complete = _handshake_bytes(hello)
            if started_tls is not None and not complete:
                # A handshake that stalls or overruns before its server name can be read would
                # otherwise go through unvetted (send 5 bytes, wait out the timeout, send the rest).
                sni, why = None, "incomplete TLS ClientHello"
            else:
                why = sni_verdict(sni, port, from_chat(client)) if sni else None
            if why:
                log_request(client=client, method="CONNECT", host=sni or host, port=port, url=None, action="blocked",
                            reason=f"TLS server name {sni or '?'} (tunnel to {host}): {why}", status=403)
                pre_down.cancel()
                for w in (writer, up_writer):
                    w.close()
                return
            if sni:
                if sni != host:
                    sni_entry = (sni, writer, up_writer)
                    _tunnels.add(sni_entry)
            if hello:
                counter["up"] += len(hello)
                up_writer.write(hello)
                await up_writer.drain()
        if read_only:
            # Forward only the vetted request; never relay further client bytes (no smuggled
            # pipelined writes on the same connection). Reads carry no body.
            await _pipe(up_reader, writer, counter, "down", first)
            up_writer.close()
        else:
            await asyncio.gather(
                _pipe(reader, up_writer, counter, "up"),
                pre_down or _pipe(up_reader, writer, counter, "down", first if status is None else None),
            )
    finally:
        _tunnels.discard(entry)
        _tunnels.discard(sni_entry)
        if status is None and first:
            try:
                status = int(first[0].split(b" ", 2)[1])
            except (IndexError, ValueError):
                status = None
        finish_request(row, status=status, bytes_up=counter["up"], bytes_down=counter["down"],
                       duration_ms=int((time.monotonic() - started) * 1000))


def close_gated_tunnels() -> int:
    closed = set()
    for host, cw, uw in list(_tunnels):
        if is_gated(host):
            for w in (cw, uw):
                try:
                    w.close()
                except Exception:  # noqa: BLE001
                    pass
            closed.add(id(cw))
    return len(closed)


# ------------------------------------------------------------------ admin API

admin = FastAPI(docs_url=None, redoc_url=None)


@admin.middleware("http")
async def admin_guard(request: Request, call_next):
    try:
        if ipaddress.ip_address(request.client.host) in SANDBOX_NET or from_chat(request.client.host):
            return JSONResponse({"error": "forbidden"}, status_code=403)
    except (ValueError, AttributeError):
        pass
    if not TOKEN or not secrets.compare_digest(request.headers.get("x-egress-token", ""), TOKEN):
        return JSONResponse({"error": "bad token"}, status_code=401)
    return await call_next(request)


class LeaseIn(BaseModel):
    active: bool
    ttl_seconds: int = 300
    reason: str = ""


class RuleIn(BaseModel):
    pattern: str
    action: str = "block"
    note: str = ""


@admin.get("/status")
async def status():
    return {
        "lease_active": Lease.active(),
        "lease_until": datetime.fromtimestamp(Lease.until, timezone.utc).isoformat() if Lease.active() else None,
        "lease_reason": Lease.reason,
        "open_tunnels": len(_tunnels),
        "gated_hosts": GATED_HOSTS,
        "default_policy": DEFAULT_POLICY,
    }


@admin.post("/lease")
async def set_lease(body: LeaseIn):
    if body.active:
        Lease.until = time.time() + max(10, min(body.ttl_seconds, 3600))
        Lease.reason = body.reason
        closed = 0
    else:
        Lease.until = 0
        Lease.reason = body.reason
        closed = close_gated_tunnels()
    return {"lease_active": Lease.active(), "closed_tunnels": closed}


@admin.get("/requests")
async def list_requests(limit: int = 200, offset: int = 0, host: str = "", action: str = "", method: str = "", since: str = ""):
    where, params = [], []
    if method:
        where.append("method = ?")
        params.append(method.upper())
    if since:
        where.append("ts >= ?")
        params.append(since)
    if host:
        where.append("host = ?")
        params.append(host.lower())
    if action:
        where.append("action = ?")
        params.append(action)
    w = ("WHERE " + " AND ".join(where)) if where else ""
    rows = db_query(f"SELECT * FROM requests {w} ORDER BY id DESC LIMIT ? OFFSET ?", (*params, min(limit, 1000), offset))
    total = db_query(f"SELECT COUNT(*) AS n FROM requests {w}", params)[0]["n"]
    return {"requests": rows, "total": total}


@admin.get("/stats/hosts")
async def host_stats(days: float = 7):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    return db_query(
        "SELECT host, COUNT(*) AS n, SUM(action='blocked') AS blocked, SUM(bytes_down) AS bytes_down "
        "FROM requests WHERE ts >= ? GROUP BY host ORDER BY n DESC LIMIT 50",
        (since,),
    )


@admin.get("/rules")
async def list_rules():
    return db_query("SELECT * FROM rules ORDER BY id")


@admin.post("/rules")
async def add_rule(body: RuleIn):
    if body.action not in ("block", "allow", "allow_ro"):
        raise HTTPException(400, "action must be block, allow or allow_ro")
    try:
        parsed = parse_pattern(body.pattern)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if body.action == "block" and parsed["domain"] and any(
        g == parsed["domain"] or g.endswith("." + parsed["domain"]) for g in GATED_HOSTS
    ):
        raise HTTPException(400, "that would block a model provider the assistant runs on; pause runs instead")
    rid = db_exec("INSERT INTO rules(pattern, action, note, created_at) VALUES(?,?,?,?)",
                  (body.pattern.strip(), body.action, body.note, now_iso()))
    return {"id": rid}


@admin.delete("/rules/{rule_id}")
async def delete_rule(rule_id: int):
    db_exec("DELETE FROM rules WHERE id=?", (rule_id,))
    return {"ok": True}


# ------------------------------------------------------------------ main

async def prune_loop() -> None:
    while True:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).isoformat(timespec="seconds")
        db_exec("DELETE FROM requests WHERE ts < ?", (cutoff,))
        await asyncio.sleep(6 * 3600)


async def main() -> None:
    if not TOKEN:
        raise SystemExit("EGRESS_TOKEN is required")
    proxy = await asyncio.start_server(handle_client, BIND, PROXY_PORT, limit=1 << 20)
    log.info("proxy on :%s, admin on :%s, gated=%s, default=%s", PROXY_PORT, ADMIN_PORT, GATED_HOSTS, DEFAULT_POLICY)
    server = uvicorn.Server(uvicorn.Config(admin, host=BIND, port=ADMIN_PORT, log_level="warning"))
    asyncio.create_task(prune_loop())
    async with proxy:
        await asyncio.gather(proxy.serve_forever(), server.serve())


if __name__ == "__main__":
    asyncio.run(main())
