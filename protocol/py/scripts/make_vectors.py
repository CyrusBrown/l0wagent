#!/usr/bin/env python3
"""Generate protocol/vectors/*.json from fixed seeds. Ed25519 is deterministic: re-running reproduces the files.

    cd protocol/py && uv run python scripts/make_vectors.py          # write
    cd protocol/py && uv run python scripts/make_vectors.py --check  # fail if files differ
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from l0wproto import b64, docs, hopsig, keys, payloads, tunnel
from l0wproto.times import parse_time

OUT = Path(__file__).resolve().parents[2] / "vectors"
IAT = "2026-11-17T21:30:00Z"
NOW = parse_time(IAT)
HUB = "hub.example"
ADDR = f"mara@{HUB}"


def seed(label: str) -> bytes:
    return hashlib.sha256(b"l0wagent-vector-seed/" + label.encode()).digest()


LABELS = {  # label -> (use)
    "member-sign": "sign", "member-seal": "seal", "member-recover": "recover",
    "member-sign-2": "sign", "hub-sign": "sign", "hop-sign": "sign",
}
KP = {label: keys.from_seed(seed(label), use, IAT) for label, use in LABELS.items()}
MEMBER, SEAL, RECOVER, MEMBER2, HUBKEY = (KP[k] for k in ("member-sign", "member-seal", "member-recover", "member-sign-2", "hub-sign"))


def dump(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def doc_vector(description: str, type_: str, payload: dict, signers: list[keys.KeyPair], verify_keys: list[keys.KeyPair],
               **extra) -> dict:
    raw = docs.serialize_payload(payload)
    doc = docs.sign_document(type_, raw, signers)
    return {
        "description": description,
        "type": type_,
        "now": IAT,
        "payload_json": raw.decode("utf-8"),
        "payload_b64": doc["payload"],
        "signing_input": docs.signing_input(type_, doc["payload"]).decode("ascii"),
        "signatures": doc["sigs"],
        "verify_keys": [k.key.to_dict() for k in verify_keys],
        "required": [s.kid for s in signers],
        "document": doc,
        **extra,
    }


def build() -> dict[str, str]:
    files: dict[str, str] = {}
    files["keys.json"] = dump({
        "note": "Seeds are SHA-256('l0wagent-vector-seed/' + label). The seed is the 32-byte Ed25519/X25519 private key.",
        "keys": {label: {"seed": kp.private.hex(), "key": kp.key.to_dict()} for label, kp in KP.items()},
    })
    files["base64url.json"] = dump({
        "valid": [{"bytes_hex": h, "b64": b64.encode(bytes.fromhex(h))} for h in ("", "00", "fb", "fffe", "000102", "deadbeef")],
        "invalid": [{"b64": s, "why": w} for s, w in (("AA==", "padding"), ("AA=", "padding"), ("+/8", "standard alphabet"),
                    ("AB", "non-canonical trailing bits"), ("A", "impossible length"), ("AA AA", "whitespace"))],
    })
    files["names.json"] = dump({
        "valid": ["mara", "a1b", "my-name", "abc", "a" * 32],
        "invalid": ["ab", "a" * 33, "-abc", "abc-", "a--b", "Mara", "ma_ra", "mara!", "ma ra", "mará"],
        "reserved": ["admin", "api", "files", "well-known", "tunnel"],
        "lookalike": [{"candidate": "adm-in", "existing_or_reserved": "admin"}, {"candidate": "ma-ra", "existing_or_reserved": "mara"},
                      {"candidate": "m4ra", "existing_or_reserved": None}, {"candidate": "5tatus", "existing_or_reserved": "status"},
                      {"candidate": "mari", "existing_or_reserved": "marl"}],
    })
    files["invites.json"] = dump({
        "valid": [{"string": "l0w-invite:https://hub.example#Zk3abc", "base_url": "https://hub.example", "code": "Zk3abc"}],
        "invalid": ["https://hub.example#x", "l0w-invite:https://hub.example", "l0w-invite:https://hub.example#",
                    "l0w-invite:http://hub.example#x", "l0w-invite:https://hub.example/path#x"],
    })

    member_keys = [MEMBER.key, SEAL.key, RECOVER.key]
    hostnames = {"instance": "mara.hub.example", "files": "files--mara.hubfiles.example"}

    rot = doc_vector("Scheduled rotation of the signing key, signed by the old key", "rotation",
                     payloads.build_rotation(ADDR, MEMBER.kid, MEMBER2.key, "scheduled", iat=IAT), [MEMBER], [MEMBER])
    files["rotation.json"] = dump(rot)
    rot_rec = doc_vector("Compromise rotation signed by the recovery key", "rotation",
                         payloads.build_rotation(ADDR, MEMBER.kid, MEMBER2.key, "compromise", iat=IAT), [RECOVER], [RECOVER])
    files["rotation-recovery.json"] = dump(rot_rec)

    self_p = payloads.build_self(ADDR, 3, member_keys, hostnames, history=[], iat=IAT,
                                 own_urls={"instance": "https://box.mara.example", "files": "https://files.mara.example"})
    self_v = doc_vector("A self-description, signed by the current signing key", "self", self_p, [MEMBER], [MEMBER])
    files["self.json"] = dump(self_v)
    self_h = payloads.build_self(ADDR, 4, [MEMBER2.key, SEAL.key, RECOVER.key], hostnames, history=[rot["document"]], iat=IAT)
    files["self-after-rotation.json"] = dump(
        doc_vector("A self-description after a rotation, with history", "self", self_h, [MEMBER2], [MEMBER2]))

    entry = docs.countersign_directory(self_v["document"], HUBKEY)
    files["directory-entry.json"] = dump({
        "description": "The directory entry for the self vector: the same document plus hub_sig",
        "now": IAT, "hub_domain": HUB, "name": "mara",
        "hub_key": HUBKEY.key.to_dict(),
        "hub_signing_input": docs.signing_input("directory-entry", self_v["payload_b64"]).decode("ascii"),
        "hub_sig": entry["hub_sig"],
        "entry": entry,
    })

    files["revocation.json"] = dump(doc_vector(
        "The owner revokes a stolen key with the recovery key", "revocation",
        payloads.build_revocation(ADDR, MEMBER.kid, "stolen", iat=IAT), [RECOVER], [RECOVER]))
    files["moved.json"] = dump(doc_vector(
        "A member moves to another hub", "moved", payloads.build_moved(ADDR, "mara@other.example", iat=IAT), [MEMBER], [MEMBER]))
    files["hub.json"] = dump(doc_vector(
        "The hub's capabilities document", "hub",
        payloads.build_hub("Example hub", HUB, [HUBKEY.key], f"https://{HUB}/v0/", "wss://tunnel.hub.example/v0/tunnel",
                           hostnames={"layout": "two-domains", "example": hostnames},
                           policies={"terms": "https://hub.example/terms", "privacy": "https://hub.example/privacy"},
                           extras=[{"kind": "accelerator", "id": "cloudflared", "doc": "https://hub.example/docs/cloudflared",
                                     "api": f"https://{HUB}/v0/accelerators/cloudflared"}],
                           iat=IAT), [HUBKEY], [HUBKEY]))
    files["registration.json"] = dump(doc_vector(
        "Join request, signed by the new signing key and the recovery key", "registration",
        payloads.build_registration(HUB, "mara", "Zk3abc", "tunnel", member_keys, iat=IAT), [MEMBER, RECOVER], [MEMBER, RECOVER]))
    files["leave.json"] = dump(doc_vector(
        "Leaving the hub for another", "leave", payloads.build_leave(ADDR, "mara@other.example", iat=IAT), [MEMBER], [MEMBER]))
    files["reregistration.json"] = dump(doc_vector(
        "The hub accepts a re-registration after both keys were lost", "reregistration",
        payloads.build_reregistration(ADDR, [MEMBER.kid, RECOVER.kid], [MEMBER2.kid], iat=IAT), [HUBKEY], [HUBKEY]))
    nonce = hashlib.sha256(b"l0wagent-vector-nonce").digest()
    files["tunnel-hello.json"] = dump(doc_vector(
        "Answer to the hub's challenge", "tunnel-hello",
        payloads.build_tunnel_hello(ADDR, b64.encode(nonce), HUB, "0.9.0", iat=IAT), [MEMBER], [MEMBER],
        challenge=json.loads(tunnel.challenge_message(nonce, HUB))))
    files["accelerator-request.json"] = dump(doc_vector(
        "Enable the cloudflared accelerator", "accelerator-request",
        payloads.build_accelerator_request(ADDR, HUB, "enable", "http://127.0.0.1:8790", iat=IAT), [MEMBER], [MEMBER]))

    files["hop-auth.json"] = dump(hop_vectors())
    files["tunnel-frames.json"] = dump(frame_vectors())
    return files


def hop_vectors() -> dict:
    kp = KP["hop-sign"]
    created, nonce = NOW, b64.encode(hashlib.sha256(b"l0wagent-vector-hop-nonce").digest()[:16])
    cases = []
    for label, method, uri, body, ctype in (
        ("POST with a JSON body", "POST", "https://hub.example/v0/members/mara/leave", b'{"hello":"world"}', "application/json"),
        ("GET without a body", "GET", "https://hub.example/.well-known/l0wagent/members/mara?x=1", b"", None),
    ):
        sr = hopsig.sign_request(method, uri, body, ctype, kp, ADDR, created=created, nonce=nonce)
        cases.append({
            "description": label,
            "request": {"method": method, "target_uri": uri, "body": body.decode(), "content_type": ctype},
            "signature_base": sr.signature_base,
            "signature_params": sr.params,
            "headers": sr.headers,
        })
    return {"now": IAT, "created": created, "expires": created + 300, "signer": ADDR, "key": {"seed": kp.private.hex(), "key": kp.key.to_dict()},
            "nonce": nonce, "cases": cases}


def frame_vectors() -> dict:
    F = tunnel
    valid = []

    def add(desc, f):
        valid.append({"description": desc, "type": f.type, "flags": f.flags, "stream_id": f.stream_id,
                      "payload_hex": f.payload.hex(), "frame_hex": tunnel.encode(f).hex()})

    add("OPEN", F.open_frame(1, "GET", "/api/health?x=1", "mara.hub.example", [["accept", "application/json"]], "203.0.113.9"))
    add("RESPONSE", F.response_frame(1, 200, [["content-type", "application/json"]]))
    add("DATA", F.data_frame(1, b"hello"))
    add("DATA at the 65536 limit", F.data_frame(3, b"\x00" * F.MAX_DATA))
    add("END", F.end_frame(1))
    add("RESET", F.reset_frame(3, "flow_control"))
    add("CREDIT 262144", F.credit_frame(1, 262144))
    add("PING", F.ping_frame(bytes(range(8))))
    add("PONG", F.pong_for(F.ping_frame(bytes(range(8)))))
    for v in valid:  # the 64 KiB frame is too long to print
        if len(v["frame_hex"]) > 4096:
            v["payload_hex"] = f"00 x {F.MAX_DATA}"
            v["frame_hex"] = f"{v['type']:02x}{v['flags']:02x}{v['stream_id']:08x} + payload"
            v["elided"] = True
    bad = [
        ("too short", "01000000"),
        ("unknown type", "09000000000100"),
        ("DATA with stream id 0", "030000000000" + "00"),
        ("DATA over 65536 bytes", "03000000000" "1" + "00" * 65537),
        ("END with a payload", "0400000000010" "0"),
        ("CREDIT with 3 bytes", "060000000001000000"),
        ("PING with stream id 1", "0700000000010001020304050607"),
        ("PING with 7 bytes", "07000000000000010203040506"),
        ("RESPONSE not JSON", "0200000000017b"),
        ("RESET without error", "0500000000017b7d"),
    ]
    out_bad = []
    for why, h in bad:
        try:
            tunnel.decode(bytes.fromhex(h))
        except tunnel.FrameError:
            pass
        else:
            raise SystemExit(f"bad frame vector {why!r} did not fail")
        out_bad.append({"why": why, "frame_hex": h if len(h) < 400 else f"{h[:12]}... ({len(h) // 2} bytes)",
                        **({"frame_hex_elided": True} if len(h) >= 400 else {})})
    return {"valid": valid, "malformed": out_bad}


def main() -> int:
    files = build()
    OUT.mkdir(exist_ok=True)
    if "--check" in sys.argv:
        stale = [n for n, c in files.items() if not (OUT / n).exists() or (OUT / n).read_text() != c]
        if stale:
            print("stale vectors:", ", ".join(stale))
            return 1
        return 0
    for n, c in files.items():
        (OUT / n).write_text(c)
    print(f"wrote {len(files)} files to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
