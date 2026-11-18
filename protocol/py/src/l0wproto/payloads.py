"""Payload builders, validators and typed verifiers for every document type.

Validators check required fields and types and ignore unknown fields (versions.md).
They raise ValidationError. The verify_* functions combine signature, outer checks and validation.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from . import b64
from .consts import MAX_SKEW, VERSION
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


def build_self(address, seq, keys, hostnames, *, protocols=(VERSION,), history=(), own_urls=_OMIT, iat=None, exp=None) -> dict:
    return _p("self", iat, exp, address=address, seq=seq, keys=_kd(keys), history=list(history), hostnames=hostnames,
              own_urls=own_urls, protocols=list(protocols))


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
    if p.get("own_urls") is not None:
        _url_pair(p["own_urls"], "own_urls")
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
              membership=membership or {"invites": True, "signups": "closed", "own_url": True, "require_2fa": True},
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
    for f in ("invites", "own_url", "require_2fa"):
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


def build_registration(hub, name, invite, mode, keys, *, own_urls=None, release="0.9.0", protocols=(VERSION,),
                       reregister=_OMIT, iat=None, exp=None) -> dict:
    return _p("registration", iat, exp, hub=hub, name=name, invite=invite, mode=mode, own_urls=own_urls, keys=_kd(keys),
              instance={"release": release, "protocols": list(protocols)}, reregister=reregister)


def validate_registration(p: dict, hub_domain: str | None = None, now: int | None = None) -> dict:
    _base(p, "registration")
    check_fresh(p["iat"], now)
    hub = _need(p, "hub", str)
    if hub_domain is not None and hub.lower() != hub_domain.lower():
        raise ValidationError("registration is for another hub")
    validate_name_syntax(_need(p, "name", str))
    _need(p, "invite", str)
    mode = p.get("mode")
    if mode == "tunnel":
        if p.get("own_urls") is not None:
            raise ValidationError("own_urls must be null in tunnel mode")
    elif mode == "own-url":
        _url_pair(p.get("own_urls"), "own_urls")
        if not all(p["own_urls"][k].startswith("https://") for k in ("instance", "files")):
            raise ValidationError("own_urls must be https")
    else:
        raise ValidationError("mode must be 'tunnel' or 'own-url'")
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


# --- other bodies ----------------------------------------------------------------------------


def build_invite_response(expires: str, hub_name: str, name_hint: str | None = None) -> dict:
    return {"v": VERSION, "valid": True, "expires": expires, "name_hint": name_hint, "hub": hub_name}


def build_join_response(address: str, hostnames: dict, hub_key: Key, tunnel: str | None) -> dict:
    return {"v": VERSION, "address": address, "hostnames": hostnames, "hub_key": hub_key.to_dict(), "tunnel": tunnel}


__all__ = [n for n in dir() if n.startswith(("build_", "validate_", "verify_"))] + ["split_address", "make_address", "sign_document", "KeyPair"]
