"""The checks. Each section is a coroutine; sections depend on the member they register, and say so
when an earlier failure means they cannot run."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import shlex
import time

import httpx
import websockets
from l0wproto import VERSION, docs, keys, payloads, tunnel as lt
from l0wproto.invite import parse_invite
from l0wproto.keys import Key
from l0wproto.times import format_time, now

from .harness import (Failure, Inst, Net, Peer, Report, connect_tunnel, expect, expect_error, new_name, open_peer, sha,
                      stale)


class Suite:
    def __init__(self, net: Net, invite_cmd: str, report: Report, bind_name: str | None = "opsname"):
        self.net, self.cmd, self.r = net, invite_cmd, report
        self.bind_name = bind_name
        self.caps: dict = {}
        self.hub_key: Key | None = None
        self.api = ""
        self.domain = ""

    # --- helpers --------------------------------------------------------------------------

    async def invite(self, bind: str | None = None) -> str:
        """A fresh invite's code. `bind`: run the command with `--bind NAME` appended."""
        cmd = self.cmd if bind is None else f"{self.cmd} --bind {shlex.quote(bind)}"
        p = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(p.communicate(), 60)
        expect(p.returncode == 0, f"invite command failed ({p.returncode}): {err.decode()[:200]}")
        lines = [ln.strip() for ln in out.decode().splitlines() if "l0w-invite:" in ln]
        expect(lines, "invite command printed no l0w-invite: line")
        s = lines[-1][lines[-1].index("l0w-invite:"):]
        return parse_invite(s, allow_insecure=True).code

    def url(self, path: str) -> str:
        return f"{self.net.base}{path}"

    async def post_reg(self, doc) -> httpx.Response:
        return await self.net.http.post(self.api + "members", json=doc)

    async def put_self(self, name: str, doc) -> httpx.Response:
        return await self.net.http.put(self.api + f"members/{name}/self", json=doc)

    async def entry(self, name: str) -> httpx.Response:
        return await self.net.http.get(self.url(f"/.well-known/l0wagent/members/{name}"))

    def verify_entry(self, r: httpx.Response, name: str) -> dict:
        expect(r.status_code == 200, f"expected 200, got {r.status_code} {r.text[:200]}")
        return docs.verify_directory_entry(r.json(), self.hub_key, hub_domain=self.domain, name=name)

    async def refused(self, label: str, status: int, code: str, build) -> None:
        """Register with a fresh invite; `build(invite)` returns the request document."""
        async def go():
            doc = build(await self.invite())
            expect_error(await self.post_reg(doc), status, code)
        await self.r.step(f"registration refused: {label} -> {status} {code}", go)

    # --- 1. capabilities and invites ------------------------------------------------------

    async def capabilities(self) -> bool:
        async def go():
            r = await self.net.http.get(self.url("/.well-known/l0wagent/hub"))
            expect(r.status_code == 200, f"status {r.status_code}")
            expect("json" in r.headers.get("content-type", ""), "content-type is not JSON")
            p = payloads.verify_hub(r.json())
            expect(VERSION in p["protocols"], "hub does not list l0w/0.1")
            expect(p["membership"]["require_2fa"] is True, "require_2fa is not true")
            expect(p["api"].endswith("/") and p["tunnel"], "api/tunnel missing")
            for k in p["keys"]:
                expect(Key.from_dict(k).kid == k["kid"], "bad kid")
            self.caps = p
            self.hub_key = next(Key.from_dict(k) for k in p["keys"] if k["use"] == "sign")
            self.api, self.domain = p["api"], p["domain"].lower()
            ex = p["hostnames"].get("example")
            if ex:
                expect(ex["instance"] != ex["files"], "example hostnames are the same")
        ok = await self.r.step("capabilities: signed hub document verifies", go)
        return bool(ok)

    async def invites(self) -> None:
        code = await self.r.step("invite command prints an l0w-invite string", self.invite)
        if not code:
            return

        async def lookup_twice():
            for i in range(2):
                r = await self.net.http.get(self.api + f"invites/{code}")
                expect(r.status_code == 200, f"lookup {i + 1}: status {r.status_code} {r.text[:120]}")
                b = r.json()
                expect(b.get("valid") is True and isinstance(b.get("expires"), str) and "name_hint" in b and b.get("name_bound") is False and isinstance(b.get("hub"), str),
                       f"lookup {i + 1}: bad body {b}")
        await self.r.step("invite lookup is valid and does not consume the invite", lookup_twice)

        async def unknown():
            expect_error(await self.net.http.get(self.api + "invites/no-such-invite-0123456789"), 404, "no_such_invite")
        await self.r.step("unknown invite -> 404 no_such_invite", unknown)

    async def bound_invites(self) -> None:
        """Bound invites (membership.md): the command is run with `--bind NAME` for a name the hub reserved itself
        (its own name or one of its extra reserved names; `--bind-name`, default `opsname`)."""
        name = self.bind_name
        if not name:
            self.r.add("SKIP", "bound invite checks", "no --bind-name given")
            return
        try:
            probe = await self.invite(bind=name)
        except Failure as e:
            self.r.add("SKIP", "bound invite checks", f"the invite command cannot bind {name!r} (unsupported, or the hub does not reserve it): {e}")
            return

        async def lookup():
            r = await self.net.http.get(self.api + f"invites/{probe}")
            expect(r.status_code == 200, f"status {r.status_code} {r.text[:150]}")
            b = r.json()
            expect(b.get("name_bound") is True and b.get("name_hint") == name, f"lookup of a bound invite: {b}")
        await self.r.step("bound invite: lookup shows name_bound true and the name as name_hint", lookup)

        async def unbound():
            expect_error(await self.post_reg(Inst(self.domain, name).registration(await self.invite())), 409, "name_reserved")
        await self.r.step(f"a reserved name ({name!r}) with an unbound invite -> 409 name_reserved", unbound)

        async def other():
            expect_error(await self.post_reg(Inst(self.domain, new_name()).registration(probe)), 409, "name_bound")
        await self.r.step("a bound invite used with another name -> 409 name_bound", other)

        async def fixed():
            try:
                await self.invite(bind="admin")
            except Failure:
                return
            raise Failure("an invite bound to the spec's reserved name 'admin' was created")
        await self.r.step("binding a name from the spec's reserved list ('admin') is refused at creation", fixed)

        async def register():
            r = await self.post_reg(Inst(self.domain, name).registration(await self.invite(bind=name)))
            if r.status_code == 409 and r.json().get("error") == "name_taken":
                return "taken"
            expect(r.status_code == 201, f"status {r.status_code} {r.text[:200]}")
        res = await self.r.step(f"a bound invite registers the reserved name {name!r} -> 201", register)
        if res == "taken":
            self.r.add("SKIP", f"bound invite registers {name!r}", "the name is already taken (this hub was used before); use a fresh hub or --bind-name")
        elif res:
            async def taken():
                try:
                    await self.invite(bind=name)
                except Failure:
                    return
                raise Failure("an invite bound to a taken name was created")
            await self.r.step("binding a taken name is refused at creation", taken)

    # --- 2. registration ------------------------------------------------------------------

    async def register(self, label: str) -> Inst | None:
        inst = Inst(self.domain, new_name())
        out: dict = {}

        async def go():
            r = await self.post_reg(inst.registration(await self.invite()))
            expect(r.status_code == 201, f"status {r.status_code} {r.text[:200]}")
            b = r.json()
            expect(b.get("address") == inst.address, f"address {b.get('address')!r}")
            hn = b.get("hostnames") or {}
            expect(isinstance(hn.get("instance"), str) and isinstance(hn.get("files"), str), "hostnames missing")
            expect(hn["instance"].lower() != hn["files"].lower(), "instance and files hostnames are the same origin")
            expect(Key.from_dict(b["hub_key"]).kid == self.hub_key.kid, "hub_key is not the capabilities key")
            expect(isinstance(b.get("tunnel"), str), "tunnel mode answer has no tunnel URL")
            site = lambda h: ".".join(h.split(":")[0].split(".")[-2:])  # noqa: E731
            if site(hn["instance"]) == site(hn["files"]):
                self.r.add("WARN", f"{label}: instance and files hostnames are the same site (SHOULD differ)")
            inst.hostnames, inst.tunnel_url = hn, b["tunnel"]
            out["ok"] = True
        await self.r.step(f"registration ({label}): 201 with address, two different hostnames, hub key, tunnel", go)
        return inst if out else None

    async def after_join(self, m: Inst) -> bool:
        async def dir404():
            expect_error(await self.entry(m.name), 404, "no_such_member")
        await self.r.step("directory answers 404 until the first self is accepted", dir404)

        async def first_self():
            doc = m.self_doc(0)
            r = await self.put_self(m.name, doc)
            expect(r.status_code == 200, f"status {r.status_code} {r.text[:200]}")
            p = docs.verify_directory_entry(r.json(), self.hub_key, hub_domain=self.domain, name=m.name)
            expect(p["seq"] == 0 and p["address"] == m.address, "countersigned entry is not our self document")
        ok = await self.r.step("first self accepted: 200 with a valid countersigned entry", first_self)

        async def dir200():
            p = self.verify_entry(await self.entry(m.name), m.name)
            expect(p["hostnames"] == m.hostnames, "hostnames differ")
        await self.r.step("directory entry verifies (member and hub signatures, address)", dir200)

        async def lists():
            for kind in ("revocations", "reregistrations"):
                r = await self.net.http.get(self.url(f"/.well-known/l0wagent/members/{m.name}/{kind}"))
                expect(r.status_code == 200 and isinstance(r.json(), list), f"{kind}: {r.status_code} {r.text[:100]}")
                expect_error(await self.net.http.get(self.url(f"/.well-known/l0wagent/members/zz{m.name}/{kind}")), 404, "no_such_member")
        await self.r.step("revocations and reregistrations are JSON arrays; unknown name 404", lists)

        async def unknown_entry():
            expect_error(await self.entry("nobodyherexyz"), 404, "no_such_member")
        await self.r.step("directory: unknown name -> 404 no_such_member", unknown_entry)
        return bool(ok)

    async def used_invite(self) -> None:
        async def go():
            code = await self.invite()
            a = Inst(self.domain, new_name())
            r = await self.post_reg(a.registration(code))
            expect(r.status_code == 201, f"first use: {r.status_code} {r.text[:150]}")
            expect_error(await self.net.http.get(self.api + f"invites/{code}"), 410, "invite_used")
            b = Inst(self.domain, new_name())
            expect_error(await self.post_reg(b.registration(code)), 410, "invite_used")
        await self.r.step("an invite works once: lookup and registration then answer 410 invite_used", go)

    async def refusals(self, taken: Inst) -> None:
        reg = lambda name=None, **kw: (lambda code: Inst(self.domain, name or new_name()).registration(code, **kw))  # noqa: E731

        def tampered(code):
            i = Inst(self.domain, new_name())
            d = i.registration(code)
            other = json.loads(__import__("l0wproto").b64.decode(d["payload"]))
            other["name"] = new_name()
            d["payload"] = __import__("l0wproto").b64.encode(json.dumps(other, separators=(",", ":")).encode())
            return d

        def one_sig(code):
            i = Inst(self.domain, new_name())
            return i.registration(code, signers=[i.sign])

        def wrong_second(code):
            i = Inst(self.domain, new_name())
            return i.registration(code, signers=[i.sign, keys.generate("recover")])

        def unsupported(code):
            i = Inst(self.domain, new_name())
            return i.registration(code, v="l0w/9.9")

        await self.refused("tampered payload", 400, "bad_registration", tampered)
        await self.refused("only the signing key signed (no recovery signature)", 400, "bad_registration", one_sig)
        await self.refused("recovery signature from a key not in the document", 400, "bad_registration", wrong_second)
        await self.refused("registration for another hub", 400, "bad_registration", reg(hub="other.example"))
        await self.refused("stale iat", 400, "bad_registration", reg(iat=stale(1000)))
        await self.refused("unsupported version", 400, "unsupported_version", unsupported)
        for bad in ("ab", "Mara99", "a--b", "-abc", "x" * 33):
            await self.refused(f"bad name {bad!r}", 400, "bad_registration", reg(name=bad))
        for n in ("admin", "api", "www", "tunnel", "well-known"):
            await self.refused(f"reserved name {n!r}", 409, "name_reserved", reg(name=n))
        await self.refused("lookalike of a reserved name (adm1n)", 409, "name_reserved", reg(name="adm1n"))
        await self.refused("lookalike of a reserved name (mai1: i -> l)", 409, "name_reserved", reg(name="mai1"))
        await self.refused("lookalike of a reserved name (dashes: we-b)", 409, "name_reserved", reg(name="we-b"))
        await self.refused("taken name", 409, "name_taken", reg(name=taken.name))
        dashed = taken.name[:3] + "-" + taken.name[3:]
        await self.refused("lookalike of a taken name (dash inserted)", 409, "name_taken", reg(name=dashed))
        await self.refused("unknown invite code", 403, "needs_invite", lambda code: Inst(self.domain, new_name()).registration("not-a-real-invite-0123456789"))
        min_rel = self.caps["instance_releases"]["min"]
        if min_rel not in ("0", "0.0", "0.0.0"):
            await self.refused("instance release older than instance_releases.min", 426, "instance_too_old", reg(release="0.0.1"))

        async def own_url():
            if not self.caps["membership"].get("own_url"):
                return
            code = await self.invite()
            i = Inst(self.domain, new_name())
            urls = {"instance": "https://own-url-conformance.invalid", "files": "https://own-url-files-conformance.invalid"}
            expect_error(await self.post_reg(i.registration(code, mode="own-url", own_urls=urls)), 422, "own_url_unreachable")
            same = {"instance": "https://own-url-conformance.invalid", "files": "https://own-url-conformance.invalid"}
            expect_error(await self.post_reg(i.registration(await self.invite(), mode="own-url", own_urls=same)), 422, "own_url_unreachable")
        if self.caps["membership"].get("own_url"):
            await self.r.step("own-url mode: unreachable URLs and same-origin URLs -> 422 own_url_unreachable", own_url)

    # --- 3. self updates and rotation -----------------------------------------------------

    async def self_updates(self, m: Inst) -> None:
        async def advance():
            r = await self.put_self(m.name, m.self_doc(1))
            expect(r.status_code == 200, f"{r.status_code} {r.text[:200]}")
            p = self.verify_entry(await self.entry(m.name), m.name)
            expect(p["seq"] == 1, f"directory still at seq {p['seq']}")
        await self.r.step("self update with a higher seq is accepted and served", advance)

        async def rollback():
            expect_error(await self.put_self(m.name, m.self_doc(1)), 409, "stale_seq")
            expect_error(await self.put_self(m.name, m.self_doc(0)), 409, "stale_seq")
            expect(self.verify_entry(await self.entry(m.name), m.name)["seq"] == 1, "directory went backwards")
        await self.r.step("self update with seq equal or lower is refused 409 stale_seq", rollback)

        async def forged():
            other = keys.generate("sign")
            ks = [other.key, m.seal.key, m.recover.key]
            expect_error(await self.put_self(m.name, m.self_doc(5, keys_=ks, signer=other)), 400, "bad_self")
            expect_error(await self.put_self(m.name, m.self_doc(5, signer=other)), 400, "bad_self")
        await self.r.step("self signed by a key the hub does not know (no rotation) -> 400 bad_self", forged)

        async def wrong_records():
            expect_error(await self.put_self(m.name, m.self_doc(5, hostnames={"instance": "evil.example", "files": "evil2.example"})), 400, "bad_self")
            expect_error(await self.put_self(m.name, m.self_doc(5, address=f"someone@{self.domain}")), 400, "bad_self")
        await self.r.step("self with other hostnames or another address -> 400 bad_self", wrong_records)

        async def unknown():
            expect_error(await self.put_self("nobodyherexyz", m.self_doc(9)), 404, "no_such_member")
        await self.r.step("self for an unknown member -> 404 no_such_member", unknown)

    async def rotation(self, m: Inst) -> None:
        seq = 10

        async def no_chain():
            new = keys.generate("sign")
            d = m.self_doc(seq, keys_=[new.key, m.seal.key, m.recover.key], signer=new)
            expect_error(await self.put_self(m.name, d), 400, "bad_self")
        await self.r.step("new signing key without a rotation -> 400 bad_self", no_chain)

        async def stranger_rotation():
            stranger = keys.generate("sign")
            new, rot = m.rotate_sign(signer=stranger)
            d = m.self_doc(seq, keys_=[new.key, m.seal.key, m.recover.key], history=[rot], signer=new)
            expect_error(await self.put_self(m.name, d), 400, "bad_self")
        await self.r.step("rotation signed by neither the old key nor the recovery key -> 400 bad_self", stranger_rotation)

        async def wrong_old():
            new, rot = m.rotate_sign()
            p = payloads.build_rotation(m.address, keys.generate("sign").kid, new.key, "scheduled")
            rot = docs.sign_document("rotation", p, [m.sign])
            d = m.self_doc(seq, keys_=[new.key, m.seal.key, m.recover.key], history=[rot], signer=new)
            expect_error(await self.put_self(m.name, d), 400, "bad_self")
        await self.r.step("rotation that replaces a key the member does not have -> 400 bad_self", wrong_old)

        async def mismatch():
            new, rot = m.rotate_sign()
            other = keys.generate("sign")
            d = m.self_doc(seq, keys_=[other.key, m.seal.key, m.recover.key], history=[rot], signer=other)
            expect_error(await self.put_self(m.name, d), 400, "bad_self")
        await self.r.step("self whose key is not the one the rotation names -> 400 bad_self", mismatch)

        async def good():
            nonlocal seq
            new, rot = m.rotate_sign()
            hist = [*m.history, rot]
            d = m.self_doc(seq, keys_=[new.key, m.seal.key, m.recover.key], history=hist, signer=new)
            r = await self.put_self(m.name, d)
            expect(r.status_code == 200, f"{r.status_code} {r.text[:200]}")
            m.sign, m.history, seq = new, hist, seq + 1
            p = self.verify_entry(await self.entry(m.name), m.name)
            expect(payloads.self_signing_key(p).kid == new.kid, "directory does not show the new key")
        await self.r.step("rotation signed by the old key is accepted; the directory shows the new key", good)

        async def old_key_dead():
            gone = keys.generate("sign")  # a stand-in for the now-replaced key: it must not sign selfs any more
            expect_error(await self.put_self(m.name, m.self_doc(seq, signer=gone)), 400, "bad_self")
        await self.r.step("the replaced key can no longer publish", old_key_dead)

        async def by_recovery():
            nonlocal seq
            new, rot = m.rotate_sign(signer=m.recover, reason="compromise")
            hist = [*m.history, rot]
            d = m.self_doc(seq, keys_=[new.key, m.seal.key, m.recover.key], history=hist, signer=new)
            r = await self.put_self(m.name, d)
            expect(r.status_code == 200, f"{r.status_code} {r.text[:200]}")
            m.sign, m.history, seq = new, hist, seq + 1
        await self.r.step("rotation signed by the recovery key (old key lost) is accepted", by_recovery)

    # --- 4. the tunnel --------------------------------------------------------------------

    async def tunnel(self, m: Inst) -> Peer | None:
        url = m.tunnel_url

        async def refused_hello(label: str, hello_for, code=4401):
            async def go():
                ws, _ = await connect_tunnel(self.net, url, hello_for)
                try:
                    msg = await asyncio.wait_for(ws.recv(), 10)
                    raise Failure(f"expected close {code}, got a message {str(msg)[:80]!r}")
                except websockets.ConnectionClosed as e:
                    expect(e.rcvd is not None and e.rcvd.code == code, f"closed with {e.rcvd.code if e.rcvd else None}, expected {code}")
            await self.r.step(f"tunnel handshake refused ({label}) -> close {code}", go)

        other = keys.generate("sign")
        await refused_hello("wrong nonce", lambda c: m.hello(c, nonce=__import__("l0wproto").b64.encode(os.urandom(32))))
        await refused_hello("signed by a key that is not the member's", lambda c: m.hello(c, signer=other))
        await refused_hello("hello for another hub", lambda c: m.hello(c, hub="other.example"))
        await refused_hello("stale iat", lambda c: m.hello(c, iat=stale(1000)))
        await refused_hello("unknown address", lambda c: m.hello(c, address=f"zzunknown{new_name()}@{self.domain}", signer=other))
        await refused_hello("not a signed document", lambda c: "this is not json")

        async def subproto():
            try:
                ws = await self.net.ws(url, subprotocols=["not-l0w-tunnel"])
            except Exception:
                return  # refused during the handshake
            try:
                msg = await asyncio.wait_for(ws.recv(), 10)
            except websockets.ConnectionClosed:
                return
            raise Failure(f"hub spoke to a client that offered another subprotocol: {str(msg)[:80]!r}")
        await self.r.step("tunnel: another WebSocket subprotocol is refused", subproto)

        a = None

        async def ready():
            nonlocal a
            a = await open_peer(self.net, url, m)
            expect(a.limits == self.caps["limits"], f"ready limits {a.limits} differ from capabilities {self.caps['limits']}")
            expect(isinstance(a.session, str) and a.session, "no session id")
        if not await self.r.step("tunnel handshake: valid hello -> ready with session and limits", ready):
            return None

        async def ping():
            payload = os.urandom(8)
            await a.send(lt.ping_frame(payload))
            f = await a.next(5, (lt.PING,))
            expect(f.is_pong and f.payload == payload and f.stream_id == 0, "PING was not answered with a PONG echoing the payload")
        await self.r.step("tunnel: PING is answered with a PONG echoing the payload", ping)

        b = None

        async def replace():
            nonlocal b
            b = await open_peer(self.net, url, m)
            code = await a.closed_code(40)
            expect(code == 4409, f"replaced connection closed with {code}, expected 4409")
        await self.r.step("tunnel: a second verified connection replaces the first, which closes 4409", replace)
        return b

    async def traffic(self, m: Inst, p: Peer) -> None:
        hn = m.hostnames
        get = lambda host, path="/", **kw: self.net.http.get(self.net.url_for_host(host, path), **kw)  # noqa: E731

        async def served(kind: str):
            async def go():
                task = asyncio.create_task(get(hn[kind], "/probe?a=1&b=%20x", headers={
                    "X-L0W-Tunnel-Client": "6.6.6.6", "x-l0w-tunnel-extra": "1", "Proxy-Authorization": "Basic eA==",
                    "Keep-Alive": "timeout=5", "TE": "trailers", "X-Custom": "ok"}))
                sid, o, body = await p.read_request()
                try:
                    expect(o["method"] == "GET" and o["target"] == "/probe?a=1&b=%20x", f"method/target {o['method']} {o['target']!r}")
                    expect(o["host"].lower() == hn[kind].lower(), f"host {o['host']!r} is not the {kind} hostname {hn[kind]!r}")
                    try:
                        ip = ipaddress.ip_address(o["client"])
                    except ValueError:
                        raise Failure(f"client {o['client']!r} is not an IP address") from None
                    expect(str(ip) != "6.6.6.6", "client field was taken from a visitor header")
                    expect(o["scheme"] in ("http", "https"), f"scheme {o['scheme']!r}")
                    names = [h[0] for h in o["headers"]]
                    bad = [n for n in names if n in lt.DROP_HEADERS or n.startswith(("proxy-", "x-l0w-tunnel-"))]
                    expect(not bad, f"headers that must be stripped reached the instance: {bad}")
                    expect(("x-custom", "ok") in [tuple(h) for h in o["headers"]], "ordinary headers were not forwarded")
                    expect(body == b"", "GET carried a body")
                except Failure:
                    task.cancel()
                    raise
                await p.answer(sid, 200, [["content-type", "text/plain"], ["x-instance", "yes"]], b"hello from the instance")
                r = await task
                expect(r.status_code == 200 and r.content == b"hello from the instance" and r.headers.get("x-instance") == "yes",
                       f"visitor saw {r.status_code} {r.content[:60]!r}")
            await self.r.step(f"tunnel: GET by the {kind} hostname arrives (client set, hop-by-hop and x-l0w-tunnel-* stripped) and the answer returns", go)

        await served("instance")
        await served("files")

        async def upload():
            total, chunk = 600_000, 50_000
            data = os.urandom(total)

            async def gen():
                for i in range(0, total, chunk):
                    yield data[i:i + chunk]
            task = asyncio.create_task(self.net.http.post(self.net.url_for_host(hn["instance"], "/upload"), content=gen(),
                                                          headers={"content-type": "application/octet-stream"}))
            o = await p.next(10, (lt.OPEN,))
            sid, got = o.stream_id, 0
            body = bytearray()
            expect(o.json()["method"] == "POST", "not a POST")
            while True:  # without sending credit, the hub may send at most the initial window
                try:
                    f = await p.next(1.0, (lt.DATA, lt.END))
                except Failure:
                    break
                expect(f.type == lt.DATA, "request ended before the window filled (body is larger)")
                expect(len(f.payload) <= lt.MAX_DATA, "DATA frame over 65536 bytes")
                got += len(f.payload)
                body += f.payload
            expect(0 < got <= lt.INITIAL_WINDOW, f"hub sent {got} bytes before any credit; window is {lt.INITIAL_WINDOW}")
            await p.send(lt.credit_frame(sid, got))
            received = got
            while True:
                f = await p.next(10, (lt.DATA, lt.END, lt.RESET))
                if f.type == lt.END:
                    break
                expect(f.type == lt.DATA, "hub reset the upload")
                body += f.payload
                received += len(f.payload)
                await p.send(lt.credit_frame(sid, len(f.payload)))
            expect(received == total and sha(bytes(body)) == sha(data), f"instance received {received} of {total} bytes (or they differ)")
            reply = os.urandom(600_000)
            await p.answer(sid, 201, [["content-type", "application/octet-stream"]], reply)
            r = await task
            expect(r.status_code == 201 and sha(r.content) == sha(reply), f"response {r.status_code}, {len(r.content)} bytes; body differs")
        await self.r.step("tunnel: a streamed POST (600 kB) and a 600 kB response flow within the windows, with credit", upload, timeout=60)

        async def ws_upgrade():
            try:
                await self.net.ws(f"{'wss' if self.net.scheme == 'https' else 'ws'}://{hn['instance']}/socket")
            except websockets.InvalidStatus as e:
                expect(e.response.status_code == 501, f"WebSocket upgrade answered {e.response.status_code}, expected 501")
            else:
                raise Failure("WebSocket upgrade was accepted")
            r = await get(hn["instance"], "/socket", headers={"Connection": "Upgrade", "Upgrade": "websocket",
                                                               "Sec-WebSocket-Version": "13", "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ=="})
            expect(r.status_code == 501, f"plain HTTP upgrade request answered {r.status_code}, expected 501")
            expect(not [f for f in await p.drain(0.3) if getattr(f, "type", None) == lt.OPEN], "the upgrade request reached the instance")
        await self.r.step("tunnel: WebSocket upgrade -> 501, never reaches the instance", ws_upgrade)

        async def reset_before_response():
            task = asyncio.create_task(get(hn["instance"], "/reset"))
            o = await p.next(10, (lt.OPEN,))
            await p.send(lt.reset_frame(o.stream_id, "refused"))
            r = await task
            expect(r.status_code == 502 and r.headers.get("retry-after") == "30", f"{r.status_code} retry-after={r.headers.get('retry-after')}")
        await self.r.step("tunnel: stream reset before a RESPONSE -> 502 with Retry-After: 30", reset_before_response)

        async def too_large():
            limit = self.caps["limits"]["request_body_bytes"]
            host = hn["instance"]
            head = (f"POST /big HTTP/1.1\r\nHost: {host}\r\nContent-Type: application/octet-stream\r\n"
                    f"Content-Length: {limit + 1}\r\nConnection: close\r\n\r\n").encode()
            try:
                resp = await self.net.raw(host, head, 8.0)
            except TimeoutError:
                raise Failure("no answer within 8 s to a request declaring a body over the limit") from None
            status = resp.split(b"\r\n", 1)[0]
            expect(b" 413" in status, f"expected 413, got {status!r}")
        await self.r.step("tunnel: request body over limits.request_body_bytes -> 413", too_large)

        async def rate():
            limit = self.caps["limits"]["auth_rate_per_minute"]
            if limit > 500:
                self.r.add("SKIP", "tunnel: /api/auth/* rate limit -> 429", f"limit {limit} is too high to exercise")
                return
            opens = 0
            stop = asyncio.Event()

            async def responder():
                nonlocal opens
                while not stop.is_set():
                    try:
                        f = await p.next(0.2, (lt.OPEN,))
                    except Failure:
                        continue
                    opens += 1
                    await p.answer(f.stream_id, 200, [["content-type", "text/plain"]], b"ok")
            rt = asyncio.create_task(responder())
            try:
                statuses = []
                for _ in range(2 * limit + 2):
                    r = await get(hn["instance"], "/api/auth/login")
                    statuses.append(r.status_code)
                    if r.status_code == 429:
                        break
                expect(429 in statuses, f"no 429 after {len(statuses)} requests (limit {limit}/min)")
                expect(statuses.count(200) >= 1 and set(statuses) <= {200, 429}, f"unexpected statuses {set(statuses)}")
                await asyncio.sleep(0.3)
            finally:
                stop.set()
                await rt
            expect(opens == statuses.count(200), f"{opens} requests reached the instance but {statuses.count(200)} were answered 200: 429s must not reach it")
        await self.r.step("tunnel: more than auth_rate_per_minute requests to /api/auth/* -> 429, not forwarded", rate, timeout=90)

    async def offline(self, m: Inst, p: Peer) -> None:
        await p.close()

        async def go():
            end = time.monotonic() + 15
            r = None
            while time.monotonic() < end:
                r = await self.net.http.get(self.net.url_for_host(m.hostnames["instance"], "/x"), headers={"Accept": "application/json"})
                if r.status_code == 502:
                    break
                await asyncio.sleep(0.2)
            expect(r is not None and r.status_code == 502, f"expected 502 once disconnected, got {r.status_code if r else None}")
            expect(r.headers.get("retry-after") == "30", f"Retry-After is {r.headers.get('retry-after')!r}")
            expect("json" in r.headers.get("content-type", "") and r.json().get("error") == "instance_offline", f"body {r.text[:120]!r}")
        await self.r.step("offline: 502 instance_offline (JSON) with Retry-After: 30", go)

        async def html():
            r = await self.net.http.get(self.net.url_for_host(m.hostnames["files"], "/x"), headers={"Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"})
            expect(r.status_code == 502 and r.headers.get("retry-after") == "30", f"{r.status_code} {r.headers.get('retry-after')}")
            expect(r.headers.get("content-type", "").startswith("text/html"), f"content-type {r.headers.get('content-type')!r}")
        await self.r.step("offline: 502 with an HTML page when the browser prefers HTML (files hostname)", html)

    # --- 5. leaving -----------------------------------------------------------------------

    async def leaving(self, m: Inst) -> None:
        c = None

        async def connect_again():
            nonlocal c
            c = await open_peer(self.net, m.tunnel_url, m)
        await self.r.step("tunnel: reconnect after being offline", connect_again)

        async def bad_leave():
            other = keys.generate("sign")
            expect_error(await self.net.http.post(self.api + f"members/{m.name}/leave", json=m.leave_doc(signer=other)), 400, "bad_leave")
            expect_error(await self.net.http.post(self.api + f"members/{m.name}/leave", json=m.leave_doc(address=f"someone@{self.domain}")), 400, "bad_leave")
            expect_error(await self.net.http.post(self.api + "members/nobodyherexyz/leave", json=m.leave_doc()), 404, "no_such_member")
        await self.r.step("leave: wrong signer or another address -> 400 bad_leave; unknown name -> 404", bad_leave)

        async def leave():
            r = await self.net.http.post(self.api + f"members/{m.name}/leave", json=m.leave_doc())
            expect(r.status_code == 200 and r.json().get("left") is True and r.json().get("v") == VERSION, f"{r.status_code} {r.text[:150]}")
        if not await self.r.step("leave: signed leave -> 200 {v, left: true}", leave):
            return

        async def gone():
            expect_error(await self.entry(m.name), 410, "left")
        await self.r.step("after leaving: the directory answers 410 left", gone)

        async def again():
            expect_error(await self.net.http.post(self.api + f"members/{m.name}/leave", json=m.leave_doc()), 410, "left")
            expect_error(await self.put_self(m.name, m.self_doc(99)), 410, "left")
        await self.r.step("after leaving: leave again and self updates answer 410 left", again)

        async def never_reused():
            expect_error(await self.post_reg(Inst(self.domain, m.name).registration(await self.invite())), 409, "name_taken")
        await self.r.step("after leaving: the name cannot be registered again (409 name_taken)", never_reused)

        async def tunnel_closed():
            if c is not None:
                code = await c.closed_code(10)
                expect(code is not None, "tunnel still open")
            ws, _ = await connect_tunnel(self.net, m.tunnel_url, m.hello)
            try:
                msg = await asyncio.wait_for(ws.recv(), 10)
                raise Failure(f"hub accepted a hello from a member that left: {str(msg)[:60]!r}")
            except websockets.ConnectionClosed as e:
                expect(e.rcvd is not None and e.rcvd.code == 4401, f"closed with {e.rcvd.code if e.rcvd else None}, expected 4401")
        await self.r.step("after leaving: the open tunnel is closed and a new hello is refused (4401)", tunnel_closed)

        async def host_gone():
            for kind in ("instance", "files"):
                r = await self.net.http.get(self.net.url_for_host(m.hostnames[kind], "/"))
                expect(r.status_code >= 400, f"{kind} hostname still answers {r.status_code}")
        await self.r.step("after leaving: the hostnames no longer serve", host_gone)

    # --- accelerators ---------------------------------------------------------------------

    def accelerator_api(self) -> str | None:
        for e in self.caps.get("extras", []):
            if e.get("kind") == "accelerator" and e.get("id") == "cloudflared" and isinstance(e.get("api"), str):
                return e["api"]
        return None

    async def cloudflared(self, m: Inst, api: str) -> None:
        """The `cloudflared` accelerator (accelerators.md). The hub under test must report the tunnel as
        connected (a real hub is pointed at a provider that does)."""
        n = [0]

        def when() -> str:  # strictly increasing iat, within the 300 s future allowance
            n[0] += 1
            return format_time(now() + n[0])

        async def post(doc):
            return await self.net.http.post(api, json=doc)

        async def ok(doc) -> dict:
            r = await post(doc)
            expect(r.status_code == 200, f"expected 200, got {r.status_code} {r.text[:200]}")
            st = r.json().get("state")
            expect(isinstance(st, dict), "answer has no state object")
            return st

        async def not_enabled():
            expect_error(await post(m.accel("activate", iat=when())), 409, "not_enabled")
            expect_error(await post(m.accel("deactivate", iat=when())), 409, "not_enabled")
        await self.r.step("cloudflared: activate/deactivate before enable -> 409 not_enabled", not_enabled)

        async def refused():
            expect_error(await post(m.accel("enable", iat=when(), signer=keys.generate("sign"), service="http://127.0.0.1:8790")), 401, "bad_sig")
            expect_error(await post(m.accel("enable", "http://127.0.0.1:8790", iat=when(), hub="other.example")), 401, "bad_sig")
            expect_error(await post(m.accel("enable", "http://127.0.0.1:8790", iat=when(), accelerator="other")), 401, "bad_sig")
            expect_error(await post(m.accel("enable", "http://127.0.0.1:8790", iat=format_time(now() - 200))), 401, "bad_sig")
            expect_error(await post(m.accel("enable", "http://127.0.0.1:8790", iat=format_time(now() + 4000))), 401, "bad_sig")
        await self.r.step("cloudflared: bad signature, wrong hub or accelerator, old or future iat -> 401 bad_sig", refused)

        async def bad_requests():
            for svc in ("http://10.0.0.1:80", "https://127.0.0.1:1", "http://127.0.0.1:0", "http://127.0.0.1:65536",
                        "http://localhost:80", None):
                expect_error(await post(m.accel("enable", svc, iat=when())), 400, "bad_request")
            expect_error(await post(m.accel("explode", "http://127.0.0.1:8790", iat=when())), 400, "bad_request")
            r = await self.net.http.post(api, content=b"not json", headers={"content-type": "application/json"})
            expect_error(r, 400, "bad_request")
        await self.r.step("cloudflared: bad service, unknown action or body -> 400 bad_request", bad_requests)

        out: dict = {}

        async def enable():
            st = await ok(m.accel("enable", "http://127.0.0.1:8790", iat=when()))
            expect(st.get("enabled") is True and st.get("active") is False, f"state {st}")
            expect(isinstance(st.get("token"), str) and st["token"], "enable answer has no token")
            expect(isinstance(st.get("tunnel_id"), str) and st["tunnel_id"], "no tunnel_id")
            expect(sorted(h.lower() for h in st.get("hostnames", [])) == sorted(h.lower() for h in m.hostnames.values()),
                   f"hostnames {st.get('hostnames')} are not the member's {m.hostnames}")
            out["tunnel_id"] = st["tunnel_id"]
        if not await self.r.step("cloudflared: enable -> token, tunnel_id, both hostnames, enabled and not active", enable):
            return

        async def enable_again():
            st = await ok(m.accel("enable", "http://127.0.0.1:8791", iat=when()))
            expect(st.get("tunnel_id") == out["tunnel_id"], "a second enable made another tunnel")
            expect(st.get("enabled") is True and st.get("active") is False, f"state {st}")
        await self.r.step("cloudflared: enable again -> the same tunnel_id", enable_again)

        async def replay():
            doc = m.accel("activate", iat=when())
            st = await ok(doc)
            expect(st.get("active") is True, f"state {st}")
            body = expect_error(await post(doc), 409, "stale")
            expect("last_iat" in body, "stale answer has no last_iat")
            expect_error(await post(m.accel("deactivate", iat=format_time(now() + 1))), 409, "stale")
        await self.r.step("cloudflared: activate -> active true; replaying the document, or an older iat -> 409 stale with last_iat", replay)

        async def state_changes():
            st = await ok(m.accel("deactivate", iat=when()))
            expect(st.get("active") is False and st.get("enabled") is True, f"state {st}")
            st = await ok(m.accel("activate", iat=when()))
            expect(st.get("active") is True, f"state {st}")
            st = await ok(m.accel("disable", iat=when()))
            expect(st.get("enabled") is False and st.get("active") is False and st.get("tunnel_id") is None, f"state {st}")
            expect("token" not in st, "disable answer carries a token")
            expect_error(await post(m.accel("activate", iat=when())), 409, "not_enabled")
        await self.r.step("cloudflared: deactivate, activate, disable (enabled false, tunnel_id null), then not_enabled", state_changes)

        async def after_leave():
            await ok(m.accel("enable", "http://127.0.0.1:8790", iat=when()))
            r = await self.net.http.post(self.api + f"members/{m.name}/leave", json=m.leave_doc())
            expect(r.status_code == 200, f"leave answered {r.status_code}")
            r = await post(m.accel("enable", "http://127.0.0.1:8790", iat=when()))
            expect(r.status_code in (401, 403), f"a member that left got {r.status_code}")
        await self.r.step("cloudflared: after leaving the member cannot use it (401 or 403)", after_leave)

    # --- everything -----------------------------------------------------------------------

    async def run(self) -> None:
        if not await self.capabilities():
            self.r.add("FAIL", "the remaining checks need a verified capabilities document", "not run")
            return
        await self.invites()
        await self.bound_invites()
        m1 = await self.register("member 1")
        if m1 is None or not await self.after_join(m1):
            self.r.add("FAIL", "checks on a registered member", "not run: registration or the first self failed")
        else:
            await self.used_invite()
            await self.refusals(m1)
            await self.self_updates(m1)
            await self.rotation(m1)
        api = self.accelerator_api()
        if api is None:
            self.r.add("SKIP", "cloudflared accelerator checks", "the hub does not advertise the cloudflared accelerator")
        else:
            m3 = await self.register("member 3 (accelerator)")
            if m3 is None or not await self.after_join(m3):
                self.r.add("FAIL", "cloudflared accelerator checks", "not run: registration or the first self failed")
            else:
                await self.cloudflared(m3, api)
        m2 = await self.register("member 2")
        if m2 is None or not await self.after_join(m2):
            self.r.add("FAIL", "tunnel and leave checks", "not run: registration or the first self failed")
            return
        peer = await self.tunnel(m2)
        if peer is None:
            self.r.add("FAIL", "tunnel traffic checks", "not run: no tunnel connection")
        else:
            await self.traffic(m2, peer)
            await self.offline(m2, peer)
        await self.leaving(m2)

