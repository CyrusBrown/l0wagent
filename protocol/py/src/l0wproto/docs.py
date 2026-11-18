"""Signed documents (signing.md) and directory entries (identity.md)."""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable, Mapping, Sequence

from . import b64
from .consts import MAX_SKEW, SIG_PREFIX, SUPPORTED_VERSIONS, VERSION
from .errors import DocumentError, SignatureError, UnsupportedVersion, ValidationError
from .keys import Key, KeyPair
from .times import now as _now
from .times import parse_time

DIRECTORY_TYPE = "directory-entry"

KeyMap = Mapping[str, Key] | Iterable[Key]


def signing_input(type_: str, payload_b64: str, prefix: str = SIG_PREFIX) -> bytes:
    """`"l0wagent-sig/1\\n" + type + "\\n" + payload`, as ASCII bytes."""
    return f"{prefix}\n{type_}\n{payload_b64}".encode("ascii")


def serialize_payload(payload: dict) -> bytes:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _keymap(keys: KeyMap) -> dict[str, Key]:
    if isinstance(keys, Mapping):
        return dict(keys)
    return {k.kid: k for k in keys}


def sign_document(type_: str, payload: dict | bytes, signers: Sequence[KeyPair], v: str = VERSION) -> dict:
    """Serialize `payload` once and sign it with every signer."""
    raw = payload if isinstance(payload, bytes) else serialize_payload(payload)
    obj = json.loads(raw)
    if not isinstance(obj, dict) or obj.get("v") != v or obj.get("type") != type_ or "iat" not in obj:
        raise DocumentError("payload must be an object with matching v and type, and iat")
    p = b64.encode(raw)
    msg = signing_input(type_, p)
    return {"v": v, "type": type_, "payload": p, "sigs": [{"kid": s.kid, "sig": b64.encode(s.sign(msg))} for s in signers]}


def _check_sig(sig: object, keys: dict[str, Key], msg: bytes) -> str:
    if not isinstance(sig, dict) or not isinstance(sig.get("kid"), str) or not isinstance(sig.get("sig"), str):
        raise SignatureError("malformed signature entry")
    key = keys.get(sig["kid"])
    if key is None or not key.usable:
        raise SignatureError("signature from a kid that cannot be resolved")
    raw = b64.decode(sig["sig"])
    if len(raw) != 64 or not key.verify(raw, msg):
        raise SignatureError("signature does not verify")
    return sig["kid"]


def _shape(doc: object) -> dict:
    if not isinstance(doc, dict):
        raise DocumentError("document must be an object")
    for f in ("v", "type", "payload"):
        if not isinstance(doc.get(f), str):
            raise DocumentError(f"document field {f!r} missing")
    if doc["v"] not in SUPPORTED_VERSIONS:
        raise UnsupportedVersion(f"unsupported version {doc['v']!r}")
    return doc


def parse_payload(doc: dict, now: int | None = None) -> dict:
    """Decode the payload and apply the inner/outer, iat and exp checks (no signature check)."""
    n = _now() if now is None else now
    try:
        obj = json.loads(b64.decode(doc["payload"]).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise ValidationError(f"payload is not valid JSON: {e}") from e
    if not isinstance(obj, dict):
        raise ValidationError("payload must be a JSON object")
    if obj.get("v") != doc["v"] or obj.get("type") != doc["type"]:
        raise ValidationError("inner v/type differ from outer")
    if parse_time(obj.get("iat")) > n + MAX_SKEW:
        raise ValidationError("iat is in the future")
    if "exp" in obj and n > parse_time(obj["exp"]):
        raise ValidationError("document has expired")
    return obj


def verify_document(
    doc: dict,
    keys: KeyMap,
    *,
    expected_type: str | None = None,
    required: Iterable[str] | None = None,
    now: int | None = None,
) -> dict:
    """Verify every signature, require each `required` kid (default: every kid present),
    and return the parsed payload. Any signature that fails or cannot be resolved rejects."""
    _shape(doc)
    if expected_type is not None and doc["type"] != expected_type:
        raise DocumentError(f"expected type {expected_type!r}, got {doc['type']!r}")
    sigs = doc.get("sigs")
    if not isinstance(sigs, list) or not sigs:
        raise SignatureError("no signatures")
    km = _keymap(keys)
    msg = signing_input(doc["type"], doc["payload"])
    seen = [_check_sig(s, km, msg) for s in sigs]
    if len(set(seen)) != len(seen):
        raise SignatureError("duplicate signer")
    need = list(seen) if required is None else list(required)
    if not need:
        raise SignatureError("no required signers")
    missing = [k for k in need if k not in seen]
    if missing:
        raise SignatureError(f"missing required signature from {missing[0]}")
    return parse_payload(doc, now)


def verify_one_of(doc: dict, candidates: KeyMap, *, expected_type: str | None = None, now: int | None = None) -> tuple[dict, str]:
    """For types signed by "A or B" (rotation, revocation): exactly one signature, from one of the
    candidates. Returns (payload, signer kid)."""
    km = _keymap(candidates)
    _shape(doc)
    sigs = doc.get("sigs")
    if not isinstance(sigs, list) or len(sigs) != 1 or not isinstance(sigs[0], dict):
        raise SignatureError("expected exactly one signature")
    kid = sigs[0].get("kid")
    return verify_document(doc, km, expected_type=expected_type, required=[kid], now=now), kid


# --- directory entries -----------------------------------------------------------------------


def countersign_directory(self_doc: dict, hub: KeyPair) -> dict:
    """Add the hub's `hub_sig` over the same payload bytes with type `directory-entry`."""
    _shape(self_doc)
    if self_doc["type"] != "self":
        raise DocumentError("only a self document can be countersigned")
    entry = copy.deepcopy(self_doc)
    entry.pop("hub_sig", None)
    msg = signing_input(DIRECTORY_TYPE, self_doc["payload"])
    entry["hub_sig"] = {"kid": hub.kid, "sig": b64.encode(hub.sign(msg))}
    return entry


def verify_directory_entry(
    entry: dict, hub_key: Key, *, hub_domain: str, name: str | None = None, now: int | None = None
) -> dict:
    """Verify the member's self signature (against the key in the payload), the hub's `hub_sig`
    (against `hub_key`), and that the address's name and hub domain match. Returns the self payload."""
    from .payloads import verify_self, split_address

    payload = verify_self(entry, now=now)
    hs = entry.get("hub_sig")
    if not isinstance(hs, dict) or hs.get("kid") != hub_key.kid:
        raise SignatureError("hub_sig missing or from another key")
    _check_sig(hs, {hub_key.kid: hub_key}, signing_input(DIRECTORY_TYPE, entry["payload"]))
    addr_name, addr_domain = split_address(payload["address"])
    if addr_domain != hub_domain.lower():
        raise ValidationError("address is on another hub")
    if name is not None and addr_name != name.lower():
        raise ValidationError("address does not match the requested name")
    return payload
