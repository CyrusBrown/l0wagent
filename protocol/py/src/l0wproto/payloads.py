"""Payload builders, validators and typed verifiers for every document type.

Validators check required fields and types and ignore unknown fields (versions.md).
They raise ValidationError. The verify_* functions combine signature, outer checks and validation.
"""

from __future__ import annotations

import hashlib as _hashlib
import json as _json
import os as _os
import re
from collections.abc import Sequence

from . import b64
from .consts import MAX_FILES, MAX_FRIEND_REQUEST_BODY, MAX_SKEW, VERSION
from .docs import parse_payload, sign_document, verify_document, verify_one_of
from .errors import DocumentError, SignatureError, ValidationError
from .keys import Key, KeyPair
from .names import validate_name
from .times import check_fresh, format_time, now as _now, parse_time

_DOMAIN = re.compile(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:\d{1,5})?\Z")


# --- small helpers ---------------------------------------------------------------------------


def _need(p: dict, field: str, typ: type | tuple, what: str = "") -> object:
    v = p.get(field)
    types = typ if isinstance(typ, tuple) else (typ,)
    if not isinstance(v, typ) or (isinstance(v, bool) and bool not in types):
        raise ValidationError(f"field {field!r} missing or not {what or typ}")
    return v


def _str_list(p: dict, field: str) -> list[str]:
    v = _need(p, field, list, "a list")
    if not all(isinstance(x, str) for x in v):
        raise ValidationError(f"field {field!r} must be a list of strings")
    return v


def _base(p: object, type_: str) -> dict:
    if not isinstance(p, dict):
        raise ValidationError("payload must be an object")
    if p.get("v") != VERSION or p.get("type") != type_:
        raise ValidationError(f"payload must have v={VERSION!r} and type={type_!r}")
    parse_time(p.get("iat"))
    if "exp" in p:
        parse_time(p["exp"])
    return p


def split_address(addr: str) -> tuple[str, str]:
    """`name@domain` -> (name, domain), lowercased and validated."""
    if not isinstance(addr, str) or addr.count("@") != 1:
        raise ValidationError("address must be name@domain")
    name, domain = addr.lower().split("@")
    validate_name_syntax(name)
    if not _DOMAIN.match(domain):
        raise ValidationError("bad hub domain in address")
    return name, domain


def validate_name_syntax(name: str) -> None:
    try:
        validate_name(name)
    except Exception as e:
        raise ValidationError(f"bad name: {e}") from e


def make_address(name: str, domain: str) -> str:
    return f"{name}@{domain}".lower()


def _keys(p: dict, field: str = "keys") -> list[Key]:
    raw = _need(p, field, list, "a list")
    try:
        return [Key.from_dict(k) for k in raw]
    except ValidationError:
        raise
    except Exception as e:
        raise ValidationError(f"bad key: {e}") from e


def _one_per_use(keys: Sequence[Key], uses: Sequence[str]) -> dict[str, Key]:
    out = {}
    for u in uses:
        found = [k for k in keys if k.use == u and k.usable]
        if len(found) != 1:
            raise ValidationError(f"exactly one {u!r} key required, found {len(found)}")
        out[u] = found[0]
    return out


def _url_pair(v: object, field: str) -> None:
    if not isinstance(v, dict) or not all(isinstance(v.get(k), str) for k in ("instance", "files")):
        raise ValidationError(f"{field!r} must have string 'instance' and 'files'")


def _p(type_: str, iat: str | int | None, exp: str | int | None, **fields) -> dict:
    out = {"v": VERSION, "type": type_, "iat": iat if isinstance(iat, str) else format_time(_now() if iat is None else iat)}
    if exp is not None:
        out["exp"] = exp if isinstance(exp, str) else format_time(exp)
    out.update({k: v for k, v in fields.items() if v is not _OMIT})
    return out


_OMIT = object()


def _kd(keys) -> list[dict]:
    return [k.to_dict() if isinstance(k, Key) else k for k in keys]


# --- self ------------------------------------------------------------------------------------


def build_self(address, seq, keys, hostnames, *, protocols=(VERSION,), history=(), iat=None, exp=None) -> dict:
    return _p("self", iat, exp, address=address, seq=seq, keys=_kd(keys), history=list(history), hostnames=hostnames,
              protocols=list(protocols))


def validate_self(p: dict) -> dict:
    _base(p, "self")
    split_address(_need(p, "address", str))
    seq = _need(p, "seq", int, "an integer")
    if seq < 0:
        raise ValidationError("seq must not be negative")
    _one_per_use(_keys(p), ("sign", "seal", "recover"))
    h = _need(p, "history", list, "a list")
    for d in h:
        if not isinstance(d, dict) or d.get("type") != "rotation":
            raise ValidationError("history entries must be signed rotation documents")
    _url_pair(p.get("hostnames"), "hostnames")
    _str_list(p, "protocols")
    return p


def self_signing_key(p: dict) -> Key:
    return _one_per_use(_keys(p), ("sign",))["sign"]


def verify_self(doc: dict, now: int | None = None) -> dict:
    """Verify a self document (or a directory entry's member half) against the key inside it."""
    p = validate_self(parse_payload(_shape_ok(doc), now))
    k = self_signing_key(p)
    if len(doc.get("sigs") or []) != 1:
        raise SignatureError("a self document carries exactly one signature")
    return validate_self(verify_document(doc, [k], expected_type="self", required=[k.kid], now=now))


def _shape_ok(doc: dict) -> dict:
    if not isinstance(doc, dict) or not isinstance(doc.get("payload"), str) or not isinstance(doc.get("type"), str):
        raise DocumentError("document must have type and payload")
    from .docs import _shape

    return _shape(doc)


# --- rotation --------------------------------------------------------------------------------


def build_rotation(address, old_kid, new_key, reason="scheduled", *, iat=None, exp=None) -> dict:
    return _p("rotation", iat, exp, address=address, old=old_kid, new=new_key.to_dict() if isinstance(new_key, Key) else new_key,
              reason=reason)


def validate_rotation(p: dict) -> dict:
    _base(p, "rotation")
    split_address(_need(p, "address", str))
    _need(p, "old", str)
    try:
        Key.from_dict(_need(p, "new", dict, "a key object"))
    except ValidationError:
        raise
    except Exception as e:
        raise ValidationError(f"bad new key: {e}") from e
    if p.get("reason") not in ("scheduled", "compromise"):
        raise ValidationError("reason must be 'scheduled' or 'compromise'")
    return p


def verify_rotation(doc: dict, old_key: Key, recovery_key: Key | None = None, now: int | None = None) -> dict:
    """Signed by the key it replaces, or by the recovery key (when the old key is lost)."""
    cands = [old_key] + ([recovery_key] if recovery_key else [])
    p, _ = verify_one_of(doc, cands, expected_type="rotation", now=now)
    validate_rotation(p)
    if p["old"] != old_key.kid:
        raise ValidationError("rotation does not replace the expected key")
    return p


# --- revocation ------------------------------------------------------------------------------


def build_revocation(address, kid, reason, *, iat=None, exp=None) -> dict:
    return _p("revocation", iat, exp, address=address, kid=kid, reason=reason)


def validate_revocation(p: dict) -> dict:
    _base(p, "revocation")
    split_address(_need(p, "address", str))
    _need(p, "kid", str)
    _need(p, "reason", str)
    return p


def verify_revocation(doc: dict, allowed: Sequence[Key], now: int | None = None) -> dict:
    """Signed by the member's recovery key, or by the hub (for its own directory)."""
    p, _ = verify_one_of(doc, allowed, expected_type="revocation", now=now)
    return validate_revocation(p)


# --- moved -----------------------------------------------------------------------------------


def build_moved(from_addr, to_addr, *, iat=None, exp=None) -> dict:
    return _p("moved", iat, exp, **{"from": from_addr, "to": to_addr})


def validate_moved(p: dict) -> dict:
    _base(p, "moved")
    split_address(_need(p, "from", str))
    split_address(_need(p, "to", str))
    return p


def verify_moved(doc: dict, signing_key: Key, now: int | None = None) -> dict:
    return validate_moved(verify_document(doc, [signing_key], expected_type="moved", required=[signing_key.kid], now=now))


# --- hub -------------------------------------------------------------------------------------


def build_hub(name, domain, keys, api, tunnel, *, protocols=(VERSION,), min_release="0.8.0", membership=None, hostnames=None,
              policies=_OMIT, limits=None, extras=(), iat=None, exp=None) -> dict:
    return _p("hub", iat, exp, name=name, domain=domain, keys=_kd(keys), protocols=list(protocols),
              instance_releases={"min": min_release}, api=api, tunnel=tunnel,
              membership=membership or {"invites": True, "signups": "closed", "require_2fa": True},
              hostnames=hostnames or {"layout": "two-domains"}, policies=policies,
              limits=limits or {"tunnel_streams": 64, "request_body_bytes": 104857600, "auth_rate_per_minute": 20},
              extras=list(extras))


def validate_hub(p: dict) -> dict:
    _base(p, "hub")
    _need(p, "name", str)
    if not _DOMAIN.match(_need(p, "domain", str)):
        raise ValidationError("bad hub domain")
    ks = _keys(p)
    if not any(k.use == "sign" and k.alg == "Ed25519" for k in ks):
        raise ValidationError("hub needs an Ed25519 signing key")
    _str_list(p, "protocols")
    _need(_need(p, "instance_releases", dict), "min", str)
    if not _need(p, "api", str).endswith("/"):
        raise ValidationError("api must end with a slash")
    _need(p, "tunnel", str)
    m = _need(p, "membership", dict)
    if m.get("signups") not in ("closed", "open", "approval"):
        raise ValidationError("membership.signups must be closed, open or approval")
    for f in ("invites", "require_2fa"):
        _need(m, f, bool, "a boolean")
    _need(p, "hostnames", dict)
    lim = _need(p, "limits", dict)
    for f in ("tunnel_streams", "request_body_bytes", "auth_rate_per_minute"):
        _need(lim, f, int, "an integer")
    for e in p.get("extras", []):
        if not isinstance(e, dict) or not isinstance(e.get("kind"), str):
            raise ValidationError("extras entries need a 'kind'")
    return p


def verify_hub(doc: dict, pinned: Key | None = None, now: int | None = None) -> dict:
    """Verify a capabilities document. Signed by one of its own signing keys; if `pinned` is given,
    that key must be the signer or still listed (otherwise the caller should raise the key-change alarm).
    Also enforces require_2fa (instances check it even though it's always true)."""
    p = validate_hub(parse_payload(_shape_ok(doc), now))
    ks = [k for k in _keys(p) if k.use == "sign" and k.alg == "Ed25519"]
    if len(doc.get("sigs") or []) != 1:
        raise SignatureError("a hub document carries exactly one signature")
    p = validate_hub(verify_document(doc, ks, expected_type="hub", now=now))
    if pinned is not None and pinned.kid not in {k.kid for k in _keys(p)}:
        raise SignatureError("hub key does not match the pinned key")
    if p["membership"]["require_2fa"] is not True:
        raise ValidationError("hub does not require 2FA")
    return p


# --- registration ----------------------------------------------------------------------------


def build_registration(hub, name, invite, keys, *, release="0.9.0", protocols=(VERSION,),
                       reregister=_OMIT, iat=None, exp=None) -> dict:
    return _p("registration", iat, exp, hub=hub, name=name, invite=invite, keys=_kd(keys),
              instance={"release": release, "protocols": list(protocols)}, reregister=reregister)


def validate_registration(p: dict, hub_domain: str | None = None, now: int | None = None) -> dict:
    _base(p, "registration")
    check_fresh(p["iat"], now)
    hub = _need(p, "hub", str)
    if hub_domain is not None and hub.lower() != hub_domain.lower():
        raise ValidationError("registration is for another hub")
    validate_name_syntax(_need(p, "name", str))
    _need(p, "invite", str)
    _one_per_use(_keys(p), ("sign", "seal", "recover"))
    inst = _need(p, "instance", dict)
    _need(inst, "release", str)
    _str_list(inst, "protocols")
    if "reregister" in p:
        _need(p, "reregister", bool, "a boolean")
    return p


def verify_registration(doc: dict, hub_domain: str, now: int | None = None) -> dict:
    """Signed by BOTH the new signing key and the recovery key listed in the payload."""
    p = validate_registration(parse_payload(_shape_ok(doc), now), hub_domain, now)
    ks = _one_per_use(_keys(p), ("sign", "recover"))
    req = [ks["sign"].kid, ks["recover"].kid]
    if len(doc.get("sigs") or []) != 2:
        raise SignatureError("a registration carries exactly two signatures")
    verify_document(doc, list(ks.values()), expected_type="registration", required=req, now=now)
    return p


# --- leave -----------------------------------------------------------------------------------


def build_leave(address, moved_to=None, *, iat=None, exp=None) -> dict:
    return _p("leave", iat, exp, address=address, moved_to=moved_to)


def validate_leave(p: dict) -> dict:
    _base(p, "leave")
    split_address(_need(p, "address", str))
    if "moved_to" not in p:
        raise ValidationError("moved_to is required (null if none)")
    if p["moved_to"] is not None:
        split_address(_need(p, "moved_to", str))
    return p


def verify_leave(doc: dict, signing_key: Key, now: int | None = None) -> dict:
    return validate_leave(verify_document(doc, [signing_key], expected_type="leave", required=[signing_key.kid], now=now))


# --- reregistration --------------------------------------------------------------------------


def build_reregistration(address, old_keys, new_keys, *, iat=None, exp=None) -> dict:
    return _p("reregistration", iat, exp, address=address, old_keys=list(old_keys), new_keys=list(new_keys))


def validate_reregistration(p: dict) -> dict:
    _base(p, "reregistration")
    split_address(_need(p, "address", str))
    _str_list(p, "old_keys")
    _str_list(p, "new_keys")
    return p


def verify_reregistration(doc: dict, hub_key: Key, now: int | None = None) -> dict:
    return validate_reregistration(
        verify_document(doc, [hub_key], expected_type="reregistration", required=[hub_key.kid], now=now))


# --- tunnel-hello ----------------------------------------------------------------------------


def build_tunnel_hello(address, nonce, hub, release="0.9.0", *, iat=None, exp=None) -> dict:
    return _p("tunnel-hello", iat, exp, address=address, nonce=nonce, hub=hub, release=release)


def validate_tunnel_hello(p: dict) -> dict:
    _base(p, "tunnel-hello")
    split_address(_need(p, "address", str))
    n = _need(p, "nonce", str)
    if len(b64.decode(n)) != 32:
        raise ValidationError("nonce must be 32 bytes")
    _need(p, "hub", str)
    _need(p, "release", str)
    return p


def verify_tunnel_hello(doc: dict, signing_key: Key, *, nonce: str, hub: str, now: int | None = None) -> dict:
    """The hub's check at step 4 of the handshake (nonce single-use/30 s age is the hub's own state)."""
    p = validate_tunnel_hello(
        verify_document(doc, [signing_key], expected_type="tunnel-hello", required=[signing_key.kid], now=now))
    if p["nonce"] != nonce:
        raise ValidationError("nonce mismatch")
    if p["hub"].lower() != hub.lower():
        raise ValidationError("hello is for another hub")
    check_fresh(p["iat"], now)
    return p


# --- accelerator-request ---------------------------------------------------------------------

ACCELERATOR_ACTIONS = ("enable", "activate", "deactivate", "disable")
ACCELERATOR_MAX_AGE = 120  # seconds an accelerator-request's iat may lie in the past
_SERVICE = re.compile(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})\Z")


def valid_service(service: object) -> bool:
    """`http://127.0.0.1:<port>`, port 1-65535, nothing else (accelerators.md)."""
    m = _SERVICE.match(service) if isinstance(service, str) else None
    return bool(m) and int(m.group(1)) <= 65535


def build_accelerator_request(address, hub, action, service=_OMIT, *, accelerator="cloudflared", iat=None, exp=None) -> dict:
    return _p("accelerator-request", iat, exp, address=address, hub=hub, accelerator=accelerator, action=action, service=service)


def validate_accelerator_request(p: dict) -> dict:
    """Shape rules: raises ValidationError (hub answers 400 bad_request)."""
    _base(p, "accelerator-request")
    split_address(_need(p, "address", str))
    _need(p, "hub", str)
    _need(p, "accelerator", str)
    action = _need(p, "action", str)
    if action not in ACCELERATOR_ACTIONS:
        raise ValidationError(f"unknown action {action!r}", "bad_request")
    if action == "enable" and not valid_service(p.get("service")):
        raise ValidationError("service must be http://127.0.0.1:<port> with a port from 1 to 65535", "bad_request")
    return p


def verify_accelerator_request(doc: dict, signing_key: Key, *, hub: str, address: str | None = None,
                               accelerator: str = "cloudflared", now: int | None = None) -> dict:
    """The hub's check of an accelerator-request. Raises SignatureError (code `bad_sig`: signature, iat,
    hub, accelerator or address) or ValidationError (code `bad_request`: malformed, action, service).
    Strictly-increasing iat per member (`stale`) is the hub's own state."""
    n = _now() if now is None else now
    try:
        p = verify_document(doc, [signing_key], expected_type="accelerator-request", required=[signing_key.kid], now=n)
    except SignatureError as e:
        e.code = "bad_sig"
        raise
    except DocumentError as e:
        if isinstance(e, ValidationError) and ("iat" in str(e) or "expired" in str(e)):
            raise SignatureError(str(e), "bad_sig") from e
        raise
    try:
        validate_accelerator_request(p)
    except ValidationError as e:
        e.code = "bad_request"
        raise
    if parse_time(p["iat"]) < n - ACCELERATOR_MAX_AGE:
        raise SignatureError("iat is more than 120 s in the past", "bad_sig")
    if p["hub"].lower() != hub.lower():
        raise SignatureError("request is for another hub", "bad_sig")
    if p["accelerator"] != accelerator:
        raise SignatureError("request is for another accelerator", "bad_sig")
    if address is not None and p["address"].lower() != address.lower():
        raise SignatureError("request is for another address", "bad_sig")
    return p


# --- envelope, delivery-token, same-owner (messages.md) -------------------------------------

SERVICE_KINDS = ("service-hello", "service-welcome", "service-bye", "service-call", "service-reply", "service-event")
KINDS = ("friend-request", "friend-accept", "friend-decline", "unfriend", "message", "presence", "wake-request",
         "same-owner", "wake-reply") + SERVICE_KINDS
TOKENLESS_KINDS = ("friend-request", "service-hello")  # envelope kinds that carry no delivery token
PRESENCE_STATES = ("asleep", "working", "idle")
WAKE_OUTCOMES = ("allowed", "denied", "later")
BY_VALUES = ("owner", "assistant")


def new_id() -> str:
    """16 random bytes, base64url (envelope and token ids)."""
    return b64.encode(_os.urandom(16))


def _id_field(p: dict, field: str = "id") -> str:
    v = _need(p, field, str)
    try:
        ok = len(b64.decode(v)) == 16
    except Exception:
        ok = False
    if not ok:
        raise ValidationError(f"field {field!r} must be the base64url of 16 bytes")
    return v


def _kid(p: dict, field: str) -> str:
    v = _need(p, field, str)
    if not re.fullmatch(r"k1\.[A-Za-z0-9_-]{22}", v):
        raise ValidationError(f"field {field!r} is not a key id")
    return v


def build_delivery_token(issuer, issuer_kid, holder, holder_kid, *, id=None, iat=None, exp=None) -> dict:
    return _p("delivery-token", iat, exp, id=id or new_id(), issuer=issuer, issuer_kid=issuer_kid, holder=holder,
              holder_kid=holder_kid)


def validate_delivery_token(p: dict) -> dict:
    _base(p, "delivery-token")
    _id_field(p)
    split_address(_need(p, "issuer", str))
    split_address(_need(p, "holder", str))
    _kid(p, "issuer_kid")
    _kid(p, "holder_kid")
    return p


def build_same_owner(a, a_kid, b, b_kid, *, iat=None, exp=None) -> dict:
    return _p("same-owner", iat, exp, a=a, a_kid=a_kid, b=b, b_kid=b_kid)


def validate_same_owner(p: dict) -> dict:
    _base(p, "same-owner")
    a = split_address(_need(p, "a", str))
    b = split_address(_need(p, "b", str))
    ka, kb = _kid(p, "a_kid"), _kid(p, "b_kid")
    if a == b or ka == kb:
        raise ValidationError("a same-owner statement needs two different instances and keys")
    return p


def verify_same_owner(doc: dict, key_a: Key, key_b: Key, now: int | None = None) -> dict:
    """Both signatures must verify, each against the key named in the payload (`a_kid`, `b_kid`)."""
    p = validate_same_owner(
        verify_document(doc, [key_a, key_b], expected_type="same-owner", required=[key_a.kid, key_b.kid], now=now))
    if (p["a_kid"], p["b_kid"]) != (key_a.kid, key_b.kid):
        raise SignatureError("keys do not match the statement")
    return p


def build_envelope(from_addr, from_kid, to_addr, to_kid, env_id, seq, kind, sealed, token=None, *, iat=None) -> dict:
    """The payload. `token` (a signed delivery-token document) goes in unless it is None."""
    return _p("envelope", iat, None, id=env_id, **{"from": from_addr}, from_kid=from_kid, to=to_addr, to_kid=to_kid, seq=seq,
              kind=kind, **({"token": token} if token is not None else {}), sealed=sealed)


def validate_envelope(p: dict) -> dict:
    """Shape rules. An unknown `kind` is not a shape error (the receiver answers 422 unknown_kind)."""
    _base(p, "envelope")
    _id_field(p)
    split_address(_need(p, "from", str))
    split_address(_need(p, "to", str))
    _kid(p, "from_kid")
    _kid(p, "to_kid")
    seq = _need(p, "seq", int, "an integer")
    if seq < 1:
        raise ValidationError("seq starts at 1")
    kind = _need(p, "kind", str)
    if kind in TOKENLESS_KINDS:
        if "token" in p:
            raise ValidationError(f"a {kind} carries no token")
    else:
        tok = p.get("token")
        if not isinstance(tok, dict):
            raise ValidationError("token required")
        validate_token_document(tok)
    s = _need(p, "sealed", dict, "an object")
    for f in ("alg", "enc", "ct"):
        _need(s, f, str)
    return p


def validate_token_document(doc: object) -> dict:
    """A delivery-token as a signed document: shape and payload shape only (no signature check)."""
    if not isinstance(doc, dict) or doc.get("type") != "delivery-token" or doc.get("v") != VERSION \
            or not isinstance(doc.get("payload"), str) or not isinstance(doc.get("sigs"), list):
        raise ValidationError("not a delivery-token document")
    try:
        inner = _json.loads(b64.decode(doc["payload"]).decode("utf-8"))
    except Exception as e:
        raise ValidationError("delivery-token payload is not valid") from e
    return validate_delivery_token(inner)


def validate_statement_document(doc: object) -> dict:
    if not isinstance(doc, dict) or doc.get("type") != "same-owner" or doc.get("v") != VERSION \
            or not isinstance(doc.get("payload"), str) or not isinstance(doc.get("sigs"), list):
        raise ValidationError("not a same-owner document")
    try:
        inner = _json.loads(b64.decode(doc["payload"]).decode("utf-8"))
    except Exception as e:
        raise ValidationError("same-owner payload is not valid") from e
    return validate_same_owner(inner)


def validate_file(f: object) -> dict:
    if not isinstance(f, dict):
        raise ValidationError("a file must be an object")
    name = _need(f, "name", str)
    if not name or "/" in name or "\\" in name or "\x00" in name or name in (".", ".."):
        raise ValidationError("file name must be a single path component")
    _need(f, "type", str)
    size = _need(f, "size", int, "an integer")
    sha = _need(f, "sha256", str)
    if size < 0 or not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise ValidationError("bad file size or sha256")
    try:
        raw = b64.decode(_need(f, "data", str))
    except Exception as e:
        raise ValidationError("file data is not base64url") from e
    if len(raw) != size:
        raise ValidationError("file size does not match its data")
    if _hashlib.sha256(raw).hexdigest() != sha:
        raise ValidationError("file sha256 does not match its data")
    return f


def build_file(name: str, media_type: str, data: bytes) -> dict:
    return {"name": name, "type": media_type, "size": len(data), "sha256": _hashlib.sha256(data).hexdigest(), "data": b64.encode(data)}


def _opt_str(b: dict, field: str) -> None:
    if b.get(field) is not None and not isinstance(b[field], str):
        raise ValidationError(f"field {field!r} must be a string or null")


def validate_body(kind: str, body: object) -> dict:
    """The body rules per kind. Raises ValidationError with code `bad_body`; unknown kinds raise code `unknown_kind`."""
    try:
        return _validate_body(kind, body)
    except ValidationError as e:
        if e.code == "bad_document":
            e.code = "bad_body"
        raise


def _validate_body(kind: str, b: object) -> dict:
    if kind not in KINDS:
        raise ValidationError(f"unknown kind {kind!r}", "unknown_kind")
    if not isinstance(b, dict):
        raise ValidationError("body must be an object")
    if kind == "friend-request":
        _opt_str(b, "code")
        if "code" not in b:
            raise ValidationError("field 'code' missing (use null)")
        _need(b, "note", str)
        if _need(b, "by", str) not in BY_VALUES:
            raise ValidationError("'by' must be owner or assistant")
        validate_token_document(b.get("token"))
        if len(_json.dumps(b, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) > MAX_FRIEND_REQUEST_BODY:
            raise ValidationError("friend request body is over 4 KiB")
    elif kind == "friend-accept":
        _need(b, "request", str)
        validate_token_document(b.get("token"))
    elif kind == "friend-decline":
        _need(b, "request", str)
    elif kind == "message":
        _need(b, "thread", str)
        _opt_str(b, "reply_to")
        _need(b, "text", str)
        files = _need(b, "files", list, "a list")
        total = 0
        for f in files:
            validate_file(f)
            total += f["size"]
        if total > MAX_FILES:
            raise ValidationError("files total more than 5 MiB")
    elif kind == "presence":
        if _need(b, "state", str) not in PRESENCE_STATES:
            raise ValidationError("bad presence state")
        nxt = b.get("next")
        if nxt is not None and nxt != "tonight":
            parse_time(nxt)
        _opt_str(b, "now")
    elif kind == "wake-request":
        _need(b, "reason", str)
    elif kind == "same-owner":
        validate_statement_document(b.get("statement"))
    elif kind == "wake-reply":
        _need(b, "request", str)
        if _need(b, "outcome", str) not in WAKE_OUTCOMES:
            raise ValidationError("bad outcome")
    elif kind in SERVICE_KINDS:
        _validate_service_body(kind, b)
    return b  # unfriend, service-bye: any object


# --- other bodies ----------------------------------------------------------------------------


def build_invite_response(expires: str, hub_name: str, name_hint: str | None = None, name_bound: bool = False) -> dict:
    return {"v": VERSION, "valid": True, "expires": expires, "name_hint": name_hint, "name_bound": name_bound, "hub": hub_name}


def build_join_response(address: str, hostnames: dict, hub_key: Key, tunnel: str | None) -> dict:
    return {"v": VERSION, "address": address, "hostnames": hostnames, "hub_key": hub_key.to_dict(), "tunnel": tunnel}


# --- hub services (services.md) --------------------------------------------------------------

SERVICE_WELL_KNOWN = ("relay", "rooms", "board", "email", "spaces", "directory", "admin")
SERVICE_DEFAULTS = ("on", "off")
MAX_SERVICE_BODY = 1024 * 1024  # bytes, the serialized body of a service-call or service-reply
_ROLE = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
_HEX_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_HTTPS_URL = re.compile(r"https://[^\s/?#][^\s]*\Z")


def valid_role(role: object) -> bool:
    return isinstance(role, str) and bool(_ROLE.match(role))


def _https_url(v: object, field: str) -> str:
    if not isinstance(v, str) or not _HTTPS_URL.match(v):
        raise ValidationError(f"field {field!r} must be an https URL")
    return v


def build_service(address, kind, roles, *, title=None, description=None, docs=None, extension=None, default="on",
                  iat=None, exp=None) -> dict:
    return _p("service", iat, exp, address=address, kind=kind,
              title=title if title is not None else address.split("@")[0], description=description if description is not None else _OMIT,
              docs=docs if docs is not None else _OMIT, roles=list(roles), extension=extension if extension is not None else _OMIT,
              default=default)


def validate_service(p: dict, hub_domain: str | None = None) -> dict:
    _base(p, "service")
    _name, domain = split_address(_need(p, "address", str))
    if hub_domain is not None and domain != hub_domain.lower():
        raise ValidationError("service address is not on this hub")
    kind = _need(p, "kind", str)
    if kind != "custom" and kind not in SERVICE_WELL_KNOWN:
        raise ValidationError(f"kind {kind!r} is neither well-known nor 'custom'")
    _need(p, "title", str)
    for f in ("description",):
        if f in p and not isinstance(p[f], str):
            raise ValidationError(f"field {f!r} must be a string")
    if "docs" in p:
        _https_url(p["docs"], "docs")
    roles = _need(p, "roles", list, "a list")
    if not all(valid_role(r) for r in roles) or len(set(roles)) != len(roles):
        raise ValidationError("roles must be a list of distinct short names")
    if "extension" in p:
        ext = _need(p, "extension", dict, "an object")
        _https_url(ext.get("git"), "extension.git")
        if not isinstance(ext.get("commit"), str) or not _HEX_COMMIT.match(ext["commit"]):
            raise ValidationError("extension.commit must be 40 or 64 lowercase hex digits")
        path = ext.get("path")
        if not isinstance(path, str) or not path or path.startswith("/") or "\\" in path or "\x00" in path \
                or any(seg in ("", "..") for seg in path.split("/")):
            raise ValidationError("extension.path must be a relative path without '..' segments")
    if _need(p, "default", str) not in SERVICE_DEFAULTS:
        raise ValidationError("default must be 'on' or 'off'")
    return p


def verify_service(doc: dict, hub_key: Key, *, hub_domain: str | None = None, now: int | None = None) -> dict:
    """A service document signed (only) by the hub's signing key. Its directory entry is the caller's to check."""
    if isinstance(doc, dict) and len(doc.get("sigs") or []) != 1:
        raise SignatureError("a service document carries exactly one signature")
    return validate_service(
        verify_document(doc, [hub_key], expected_type="service", required=[hub_key.kid], now=now), hub_domain)


def services_in(capabilities: dict, hub_key: Key | None = None, *, now: int | None = None) -> tuple[list[dict], list[str]]:
    """The verified service payloads in a (verified) capabilities payload, and a reason for each entry skipped.
    Each `extras` entry of kind `service` is checked against the hub's signing key (`hub_key`, else the one in the
    payload) and the hub's domain; bad and duplicate entries are skipped, never raised."""
    out: list[dict] = []
    skipped: list[str] = []
    domain = capabilities.get("domain")
    keys_ = [hub_key] if hub_key is not None else [k for k in _keys(capabilities) if k.use == "sign" and k.alg == "Ed25519"]
    seen: set[str] = set()
    for i, e in enumerate(capabilities.get("extras") or []):
        if not isinstance(e, dict) or e.get("kind") != "service":
            continue
        try:
            doc = e.get("doc")
            if not isinstance(doc, dict):
                raise ValidationError("no doc")
            last: Exception | None = None
            for k in keys_:
                try:
                    p = verify_service(doc, k, hub_domain=domain if isinstance(domain, str) else None, now=now)
                    break
                except SignatureError as ex:
                    last = ex
            else:
                raise last or SignatureError("no hub key")
            if p["address"].lower() in seen:
                raise ValidationError("listed twice")
            seen.add(p["address"].lower())
            out.append(p)
        except Exception as ex:  # a bad entry never spoils the rest
            skipped.append(f"extras[{i}]: {ex}")
    return out, skipped


def build_role_grant(service, holder, holder_kid, role, *, iat=None, exp=None) -> dict:
    return _p("role-grant", iat, exp, service=service, holder=holder, holder_kid=holder_kid, role=role)


def validate_role_grant(p: dict) -> dict:
    _base(p, "role-grant")
    split_address(_need(p, "service", str))
    split_address(_need(p, "holder", str))
    _kid(p, "holder_kid")
    if not valid_role(p.get("role")):
        raise ValidationError("role must be a short name")
    return p


def verify_role_grant(doc: dict, service_key: Key, *, service: str | None = None, holder: str | None = None,
                      holder_kid: str | None = None, now: int | None = None) -> dict:
    """Signed by the service's key; `service`, `holder` and `holder_kid` (the envelope's sender, as rotation
    resolved it) are compared when given. `exp` is checked against `now`."""
    if isinstance(doc, dict) and len(doc.get("sigs") or []) != 1:
        raise SignatureError("a role grant carries exactly one signature")
    p = validate_role_grant(verify_document(doc, [service_key], expected_type="role-grant", required=[service_key.kid], now=now))
    for name, want in (("service", service), ("holder", holder)):
        if want is not None and p[name].lower() != want.lower():
            raise SignatureError(f"role grant is for another {name}")
    if holder_kid is not None and p["holder_kid"] != holder_kid:
        raise SignatureError("role grant is for another key")
    return p


def validate_role_grant_document(doc: object) -> dict:
    """A role grant as a signed document: shape and payload shape only (no signature check)."""
    if not isinstance(doc, dict) or doc.get("type") != "role-grant" or doc.get("v") != VERSION \
            or not isinstance(doc.get("payload"), str) or not isinstance(doc.get("sigs"), list):
        raise ValidationError("not a role-grant document")
    try:
        inner = _json.loads(b64.decode(doc["payload"]).decode("utf-8"))
    except Exception as e:
        raise ValidationError("role-grant payload is not valid") from e
    return validate_role_grant(inner)


def _validate_service_body(kind: str, b: dict) -> None:
    if kind == "service-hello":
        validate_token_document(b.get("token"))
        _need(b, "release", str)
    elif kind == "service-welcome":
        validate_token_document(b.get("token"))
        for g in _need(b, "roles", list, "a list"):
            validate_role_grant_document(g)
    elif kind in ("service-call", "service-reply"):
        if len(_json.dumps(b, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) > MAX_SERVICE_BODY:
            raise ValidationError(f"{kind} body is over 1 MiB", "too_large")
        _id_field(b, "call")
        if kind == "service-call":
            if not _need(b, "method", str):
                raise ValidationError("method must not be empty")
            _need(b, "args", dict, "an object")
            if b.get("role") is not None:
                validate_role_grant_document(b["role"])
        else:
            ok = _need(b, "ok", bool, "a boolean")
            if ok:
                _need(b, "result", dict, "an object")
            else:
                err = _need(b, "error", dict, "an object")
                if not isinstance(err.get("code"), str) or not err["code"]:
                    raise ValidationError("error.code missing")
                if "detail" in err and not isinstance(err["detail"], str):
                    raise ValidationError("error.detail must be a string")
    elif kind == "service-event":
        event = _need(b, "event", str)
        data = _need(b, "data", dict, "an object")
        if event == "roles":
            for g in _need(data, "grants", list, "a list"):
                validate_role_grant_document(g)


__all__ = [n for n in dir() if n.startswith(("build_", "validate_", "verify_"))] + ["split_address", "make_address", "sign_document", "KeyPair"]
