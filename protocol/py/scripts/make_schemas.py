#!/usr/bin/env python3
"""Generate protocol/schemas/*.json and protocol/openapi.json. Re-runnable; `--check` fails if stale.

    cd protocol/py && uv run python scripts/make_schemas.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE = "https://schemas.l0wagent.invalid/0.1/"
DRAFT = "https://json-schema.org/draft/2020-12/schema"
V = "l0w/0.1"

TIME = {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", "description": "RFC 3339 UTC, whole seconds"}
B64 = {"type": "string", "pattern": r"^[A-Za-z0-9_-]+$", "description": "base64url, no padding"}
KID = {"type": "string", "pattern": r"^k1\.[A-Za-z0-9_-]{22}$"}
ADDRESS = {"type": "string", "pattern": r"^[a-z0-9]+(-[a-z0-9]+)*@[a-z0-9][a-z0-9.-]*(:[0-9]+)?$"}
NAME = {"type": "string", "minLength": 3, "maxLength": 32, "pattern": r"^[a-z0-9]+(-[a-z0-9]+)*$"}
URLPAIR = {"type": "object", "required": ["instance", "files"],
           "properties": {"instance": {"type": "string"}, "files": {"type": "string"}}}


def ref(name: str) -> dict:
    return {"$ref": name + ".json"}


def payload(type_: str, props: dict, required: list[str], desc: str) -> dict:
    """Payload schemas allow unknown fields (versions.md)."""
    return {
        "description": desc,
        "type": "object",
        "required": ["v", "type", "iat"] + required,
        "properties": {"v": {"const": V}, "type": {"const": type_}, "iat": TIME, "exp": TIME, **props},
        "additionalProperties": True,
    }


def keys_arr(min_items=1) -> dict:
    return {"type": "array", "minItems": min_items, "items": ref("key")}


S: dict[str, dict] = {}

S["key"] = {
    "description": "A public key object (signing.md). kid = 'k1.' + base64url(first 16 bytes of SHA-256(alg + LF + raw pub)); receivers recompute it.",
    "type": "object", "required": ["kid", "alg", "use", "pub", "created"],
    "properties": {"kid": KID, "alg": {"type": "string", "description": "Ed25519 or X25519; others may appear and are unusable"},
                   "use": {"type": "string", "description": "sign, seal or recover"}, "pub": B64, "created": TIME},
    "additionalProperties": True,
}
S["signed-document"] = {
    "description": "The envelope of every signed document. Signing input: 'l0wagent-sig/1' LF type LF payload (the base64url string).",
    "type": "object", "required": ["v", "type", "payload", "sigs"],
    "properties": {
        "v": {"const": V}, "type": {"type": "string"}, "payload": B64,
        "sigs": {"type": "array", "minItems": 1, "items": {
            "type": "object", "required": ["kid", "sig"], "properties": {"kid": KID, "sig": B64}}},
    },
    "additionalProperties": True,
}
S["directory-entry"] = {
    "description": "A member's current self document plus the hub's countersignature over the same payload bytes (signing input type 'directory-entry').",
    "allOf": [ref("signed-document")],
    "type": "object", "required": ["hub_sig"],
    "properties": {"type": {"const": "self"}, "hub_sig": {
        "type": "object", "required": ["kid", "sig"], "properties": {"kid": KID, "sig": B64}}},
}
S["self"] = payload("self", {
    "address": ADDRESS, "seq": {"type": "integer", "minimum": 0},
    "keys": {**keys_arr(3), "description": "Exactly one current key each for use sign, seal and recover"},
    "history": {"type": "array", "items": ref("signed-document"), "description": "Signed rotation documents, oldest first"},
    "hostnames": URLPAIR, "own_urls": {"oneOf": [URLPAIR, {"type": "null"}]},
    "protocols": {"type": "array", "items": {"type": "string"}},
}, ["address", "seq", "keys", "history", "hostnames", "protocols"], "Payload of a self document (identity.md)")
S["rotation"] = payload("rotation", {
    "address": ADDRESS, "old": KID, "new": ref("key"), "reason": {"enum": ["scheduled", "compromise"]},
}, ["address", "old", "new", "reason"], "Payload of a rotation document")
S["revocation"] = payload("revocation", {"address": ADDRESS, "kid": KID, "reason": {"type": "string"}},
                          ["address", "kid", "reason"], "Payload of a revocation document")
S["moved"] = payload("moved", {"from": ADDRESS, "to": ADDRESS}, ["from", "to"], "Payload of a moved document")
S["hub"] = payload("hub", {
    "name": {"type": "string"}, "domain": {"type": "string"}, "keys": keys_arr(),
    "protocols": {"type": "array", "items": {"type": "string"}},
    "instance_releases": {"type": "object", "required": ["min"], "properties": {"min": {"type": "string"}}},
    "api": {"type": "string", "pattern": "/$"}, "tunnel": {"type": "string"},
    "membership": {"type": "object", "required": ["invites", "signups", "own_url", "require_2fa"], "properties": {
        "invites": {"type": "boolean"}, "signups": {"enum": ["closed", "open", "approval"]},
        "own_url": {"type": "boolean"}, "require_2fa": {"type": "boolean"}}},
    "hostnames": {"type": "object", "properties": {"layout": {"type": "string"}, "example": URLPAIR}},
    "policies": {"type": "object", "additionalProperties": {"type": "string"}},
    "limits": {"type": "object", "required": ["tunnel_streams", "request_body_bytes", "auth_rate_per_minute"], "properties": {
        "tunnel_streams": {"type": "integer"}, "request_body_bytes": {"type": "integer"}, "auth_rate_per_minute": {"type": "integer"}}},
    "extras": {"type": "array", "items": {"type": "object", "required": ["kind"], "properties": {"kind": {"type": "string"}}}},
}, ["name", "domain", "keys", "protocols", "instance_releases", "api", "tunnel", "membership", "hostnames", "limits"],
    "Payload of the hub's capabilities document (capabilities.md)")
S["registration"] = payload("registration", {
    "hub": {"type": "string"}, "name": NAME, "invite": {"type": "string"}, "mode": {"enum": ["tunnel", "own-url"]},
    "own_urls": {"oneOf": [URLPAIR, {"type": "null"}]}, "keys": keys_arr(3), "reregister": {"type": "boolean"},
    "instance": {"type": "object", "required": ["release", "protocols"], "properties": {
        "release": {"type": "string"}, "protocols": {"type": "array", "items": {"type": "string"}}}},
}, ["hub", "name", "invite", "mode", "keys", "instance"], "Payload of a registration document; signed by the new signing key AND the recovery key")
S["leave"] = payload("leave", {"address": ADDRESS, "moved_to": {"oneOf": [ADDRESS, {"type": "null"}]}},
                     ["address", "moved_to"], "Payload of a leave document")
S["reregistration"] = payload("reregistration", {
    "address": ADDRESS, "old_keys": {"type": "array", "items": KID}, "new_keys": {"type": "array", "items": KID},
}, ["address", "old_keys", "new_keys"], "Payload of a reregistration document (signed by the hub)")
S["tunnel-hello"] = payload("tunnel-hello", {
    "address": ADDRESS, "nonce": B64, "hub": {"type": "string"}, "release": {"type": "string"},
}, ["address", "nonce", "hub", "release"], "Payload of a tunnel-hello document")
S["accelerator-request"] = payload("accelerator-request", {
    "address": ADDRESS, "hub": {"type": "string"}, "accelerator": {"const": "cloudflared"},
    "action": {"enum": ["enable", "activate", "deactivate", "disable"]},
    "service": {"type": "string", "pattern": r"^http://127\.0\.0\.1:[0-9]{1,5}$",
                "description": "http://127.0.0.1:<port>, port 1-65535 (the pattern does not check the range). Required for `enable`."},
}, ["address", "hub", "accelerator", "action"], "Payload of an accelerator-request document (accelerators.md)")
S["accelerator-state"] = {
    "description": "The `200` answer of POST /v0/accelerators/cloudflared (accelerators.md). `token` only in the answer to `enable`.",
    "type": "object", "required": ["state"],
    "properties": {"state": {"type": "object", "required": ["enabled", "active", "tunnel_id", "hostnames"], "properties": {
        "enabled": {"type": "boolean"}, "active": {"type": "boolean"},
        "tunnel_id": {"oneOf": [{"type": "string"}, {"type": "null"}]},
        "hostnames": {"type": "array", "items": {"type": "string"}},
        "token": {"type": "string", "description": "The tunnel's run token: a secret"}},
        "additionalProperties": True}},
    "additionalProperties": True,
}
S["invite-response"] = {
    "type": "object", "required": ["v", "valid", "expires", "name_hint", "hub"],
    "properties": {"v": {"const": V}, "valid": {"const": True}, "expires": TIME,
                   "name_hint": {"oneOf": [NAME, {"type": "null"}]}, "hub": {"type": "string"}},
    "additionalProperties": True,
}
S["join-response"] = {
    "type": "object", "required": ["v", "address", "hostnames", "hub_key", "tunnel"],
    "properties": {"v": {"const": V}, "address": ADDRESS, "hostnames": URLPAIR, "hub_key": ref("key"),
                   "tunnel": {"oneOf": [{"type": "string"}, {"type": "null"}]}},
    "additionalProperties": True,
}
S["error"] = {
    "description": "Error body (README: v, error, detail). identity.md's examples omit v and detail, so only `error` is required.",
    "type": "object", "required": ["error"],
    "properties": {"v": {"const": V}, "error": {"type": "string"}, "detail": {"type": "string"},
                   "supported": {"type": "array", "items": {"type": "string"}}},
    "additionalProperties": True,
}
S["tunnel-challenge"] = {
    "type": "object", "required": ["t", "nonce", "hub"],
    "properties": {"t": {"const": "challenge"}, "nonce": B64, "hub": {"type": "string"}}, "additionalProperties": True,
}
S["tunnel-ready"] = {
    "type": "object", "required": ["t", "session", "limits"],
    "properties": {"t": {"const": "ready"}, "session": {"type": "string"}, "limits": {"type": "object"}}, "additionalProperties": True,
}

PAYLOAD_TYPES = ["self", "rotation", "revocation", "moved", "hub", "registration", "leave", "reregistration", "tunnel-hello", "accelerator-request"]


def schema_files() -> dict[str, str]:
    out = {}
    for name, body in S.items():
        d = {"$schema": DRAFT, "$id": f"{BASE}{name}.json", "title": name, **body}
        out[f"schemas/{name}.json"] = json.dumps(d, indent=2) + "\n"
    return out


# --- OpenAPI ---------------------------------------------------------------------------------


def sref(name: str) -> dict:
    return {"$ref": f"schemas/{name}.json"}


def err(desc: str, *codes: str) -> dict:
    return {"description": desc + (" Codes: " + ", ".join(f"`{c}`" for c in codes) + "." if codes else ""),
            "content": {"application/json": {"schema": sref("error")}}}


def json_resp(desc: str, schema: dict, **headers) -> dict:
    r = {"description": desc, "content": {"application/json": {"schema": schema}}}
    if headers:
        r["headers"] = headers
    return r


NAME_PARAM = {"name": "name", "in": "path", "required": True, "schema": NAME}
WK = [{"url": "https://{hub}", "variables": {"hub": {"default": "hub.example", "description": "The hub domain"}}}]
UNSUPPORTED = err("The document's `v` is not spoken here (body has `supported`).", "unsupported_version")


def openapi() -> dict:
    return {
        "openapi": "3.1.0",
        "info": {"title": "l0wagent hub protocol, core endpoints", "version": "0.1.0",
                 "description": "Draft 0 of l0w/0.1. Documents are signed documents (signing.md); every body is application/json."},
        "servers": [{"url": "https://{hub}/v0", "description": "The hub's `api` base (capabilities.md), without the trailing slash",
                     "variables": {"hub": {"default": "hub.example"}}}],
        "paths": {
            "/.well-known/l0wagent/hub": {"servers": WK, "get": {
                "operationId": "getCapabilities", "summary": "The hub's capabilities (signed document of type `hub`)",
                "responses": {"200": json_resp("Signed `hub` document; the payload follows schemas/hub.json. `Cache-Control: max-age=300` is RECOMMENDED.",
                                               sref("signed-document"))}}},
            "/.well-known/l0wagent/members/{name}": {"servers": WK, "parameters": [NAME_PARAM], "get": {
                "operationId": "getDirectoryEntry", "summary": "A member's directory entry",
                "responses": {
                    "200": json_resp("The member's current `self` document plus `hub_sig`.", sref("directory-entry")),
                    "301": json_resp("The member left for another hub and published a `moved` document (served for at least a year).",
                                     sref("signed-document")),
                    "404": err("Unknown name, or no `self` accepted yet.", "no_such_member"),
                    "410": err("The member left; names are never reused.", "left")}}},
            "/.well-known/l0wagent/members/{name}/revocations": {"servers": WK, "parameters": [NAME_PARAM], "get": {
                "operationId": "getRevocations", "summary": "Revocations the hub has accepted for the member",
                "responses": {"200": json_resp("An array of signed `revocation` documents.",
                                               {"type": "array", "items": sref("signed-document")}),
                              "404": err("Unknown name.", "no_such_member")}}},
            "/.well-known/l0wagent/members/{name}/reregistrations": {"servers": WK, "parameters": [NAME_PARAM], "get": {
                "operationId": "getReregistrations", "summary": "Re-registrations the hub has issued for the member",
                "responses": {"200": json_resp("An array of signed `reregistration` documents.",
                                               {"type": "array", "items": sref("signed-document")}),
                              "404": err("Unknown name.", "no_such_member")}}},
            "/invites/{code}": {"parameters": [{"name": "code", "in": "path", "required": True, "schema": {"type": "string"}}], "get": {
                "operationId": "getInvite", "summary": "Check an invite without using it",
                "responses": {"200": json_resp("The invite is valid.", sref("invite-response")),
                              "404": err("No such invite.", "no_such_invite"),
                              "410": err("The invite was used or has expired.", "invite_used", "invite_expired")}}},
            "/members": {"post": {
                "operationId": "register", "summary": "Join the hub",
                "description": "Body: a `registration` document signed by the new signing key and the recovery key.",
                "requestBody": {"required": True, "content": {"application/json": {"schema": sref("signed-document")}}},
                "responses": {
                    "201": json_resp("Joined. `hostnames` are authoritative.", sref("join-response")),
                    "400": err("Bad signature or fields, or an unsupported `v`.", "bad_registration", "unsupported_version"),
                    "403": err("An invite is required.", "needs_invite"),
                    "409": err("The name is taken or reserved.", "name_taken", "name_reserved"),
                    "410": err("The invite was used or has expired.", "invite_used", "invite_expired"),
                    "422": err("Own-URL mode: the URLs failed the hub's checks.", "own_url_unreachable"),
                    "426": err("The instance release is older than `instance_releases.min`.", "instance_too_old")}}},
            "/members/{name}/self": {"parameters": [NAME_PARAM], "put": {
                "operationId": "putSelf", "summary": "Publish a new self-description",
                "description": "Body: a `self` document signed by the member's current signing key with a higher `seq`. The document is the authentication.",
                "requestBody": {"required": True, "content": {"application/json": {"schema": sref("signed-document")}}},
                "responses": {
                    "200": json_resp("Accepted; the countersigned directory entry.", sref("directory-entry")),
                    "400": err("Bad signature or fields, records mismatch, or unsupported `v`.", "bad_self", "unsupported_version"),
                    "404": err("Unknown name.", "no_such_member"),
                    "409": err("`seq` is not higher than the hub's copy.", "stale_seq"),
                    "410": err("The member left.", "left")}}},
            "/members/{name}/leave": {"parameters": [NAME_PARAM], "post": {
                "operationId": "leave", "summary": "Leave the hub",
                "description": "Body: a `leave` document signed by the member's signing key. The name answers 410 (or 301 with the `moved` document) from then on and is never reused.",
                "requestBody": {"required": True, "content": {"application/json": {"schema": sref("signed-document")}}},
                "responses": {
                    "200": {"description": "Left; the hostnames and the tunnel are removed."},
                    "400": err("Bad signature or fields, or unsupported `v`.", "bad_leave", "unsupported_version"),
                    "404": err("Unknown name.", "no_such_member"),
                    "410": err("Already left.", "left")}}},
            "/accelerators/cloudflared": {"post": {
                "operationId": "cloudflaredAccelerator", "summary": "Use the `cloudflared` accelerator (accelerators.md)",
                "description": "Body: an `accelerator-request` document signed by the member's current signing key. "
                               "Only advertised when the hub lists the accelerator in its capabilities `extras`.",
                "requestBody": {"required": True, "content": {"application/json": {"schema": sref("signed-document")}}},
                "responses": {
                    "200": json_resp("The member's accelerator state.", sref("accelerator-state")),
                    "400": err("Malformed, unknown action, or bad `service`.", "bad_request"),
                    "401": err("Signature, `iat`, `hub`, `accelerator` or address check failed.", "bad_sig"),
                    "403": err("Not an active tunnel-mode member.", "not_member"),
                    "409": err("`stale` (body has `last_iat`), `not_enabled`, or `not_connected`.", "stale", "not_enabled", "not_connected"),
                    "502": err("The hub's provider failed.", "upstream")}}},
        },
    }


def all_files() -> dict[str, str]:
    f = schema_files()
    f["openapi.json"] = json.dumps(openapi(), indent=2) + "\n"
    return f


def main() -> int:
    files = all_files()
    if "--check" in sys.argv:
        stale = [n for n, c in files.items() if not (ROOT / n).exists() or (ROOT / n).read_text() != c]
        print("stale:", ", ".join(stale)) if stale else None
        return 1 if stale else 0
    (ROOT / "schemas").mkdir(exist_ok=True)
    for n, c in files.items():
        (ROOT / n).write_text(c)
    print(f"wrote {len(files)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
