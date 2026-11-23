"""Shared spaces (spaces.md): the crypto and the two signed document types, as pure functions.

Subkeys come from a space key `K` by HKDF-SHA256 (empty salt). A blob's id is an HMAC of its contents, its
key another HMAC of the id, so the fixed nonce is never reused with a key. Manifests and lock reasons are
sealed with `K_meta`. A space key travels to a member as HPKE (seal.py) inside a `space-key` document; a
version is a `space-version` document signed by the author's instance. Nothing here touches the network or
a private signing key; callers sign with docs.sign_document.

Choices where spaces.md is silent (also noted in the vectors' README text):
  * a version document's "bytes as sent" are `docs.serialize_payload(doc)`: compact JSON, keys in the order
    the document has them (sign_document builds v, type, payload, sigs);
  * a lock's sealed plaintext is the JSON object {"path": ..., "reason": ...};
  * `blobs` must be distinct; this library sorts what it builds and does not insist on an order when reading.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from collections.abc import Sequence

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import b64, seal as _seal
from .docs import parse_payload, serialize_payload, verify_document
from .errors import EncodingError, SealError, SignatureError, ValidationError
from .keys import Key
from .payloads import _base, _kid, _need, _p, split_address

INFO_ID = b"l0wagent-space/1 blob-id"
INFO_BLOB = b"l0wagent-space/1 blob-key"
INFO_META = b"l0wagent-space/1 meta"
INFO_TAG = b"l0wagent-space/1 lock-tag"
KEY_INFO_PREFIX = "l0wagent-space-key/1"

STORED_VERSION = b"\x01"
MAX_PATH_BYTES = 1024
MAX_LOCK_TTL_HOURS = 72
DEFAULT_LOCK_TTL_HOURS = 24
DEFAULT_MAX_FILE = 95 * 1024 * 1024
_ZERO_NONCE = b"\x00" * 12

_BLOB_ID = re.compile(r"e([1-9][0-9]*)\.([A-Za-z0-9_-]{43})\Z")
_VERSION_ID = re.compile(r"v\.[A-Za-z0-9_-]{22}\Z")


# --- subkeys -----------------------------------------------------------------------------------


def _check_key(k: object) -> bytes:
    if not isinstance(k, (bytes, bytearray)) or len(k) != 32:
        raise ValidationError("a space key is 32 bytes")
    return bytes(k)


def hkdf(k: bytes, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(_check_key(k))


def k_id(k: bytes) -> bytes:
    return hkdf(k, INFO_ID)


def k_blob(k: bytes) -> bytes:
    return hkdf(k, INFO_BLOB)


def k_meta(k: bytes) -> bytes:
    return hkdf(k, INFO_META)


def k_tag(k: bytes) -> bytes:
    return hkdf(k, INFO_TAG)


def _hmac(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def _ascii(*parts: object) -> bytes:
    try:
        return "\n".join(str(p) for p in parts).encode("ascii")
    except UnicodeEncodeError as e:
        raise ValidationError("must be ASCII") from e


def _check_space(space: object) -> str:
    if not isinstance(space, str) or not space or len(space) > 128 or any(ord(c) < 0x20 or ord(c) == 0x7F for c in space):
        raise ValidationError("a space id is a short string without control characters")
    return space


def _check_epoch(epoch: object) -> int:
    if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 1:
        raise ValidationError("an epoch is an integer from 1")
    return epoch


# --- paths -------------------------------------------------------------------------------------


def valid_path(path: object) -> bool:
    """spaces.md: relative, `/`-separated, at most 1024 bytes, no empty, `.` or `..` segments. (Also: no NUL,
    and it must encode as UTF-8.)"""
    if not isinstance(path, str) or not path or "\x00" in path:
        return False
    try:
        raw = path.encode("utf-8")
    except UnicodeEncodeError:
        return False
    if len(raw) > MAX_PATH_BYTES:
        return False
    return all(seg not in ("", ".", "..") for seg in path.split("/"))


def check_path(path: object) -> str:
    if not valid_path(path):
        raise ValidationError("not a valid space path")
    return path  # type: ignore[return-value]


# --- blobs -------------------------------------------------------------------------------------


def blob_id(k: bytes, epoch: int, contents: bytes) -> str:
    """`"e" + epoch + "." + base64url(HMAC-SHA256(K_id, contents))`."""
    _check_epoch(epoch)
    return f"e{epoch}.{b64.encode(_hmac(k_id(k), contents))}"


def parse_blob_id(bid: object) -> tuple[int, str]:
    """-> (epoch, the base64url part); ValidationError if it isn't a blob id."""
    m = _BLOB_ID.match(bid) if isinstance(bid, str) else None
    if not m:
        raise ValidationError("not a blob id")
    return int(m.group(1)), m.group(2)


def _blob_key(k: bytes, bid: str) -> bytes:
    return _hmac(k_blob(k), bid.encode("ascii"))


def blob_encrypt(k: bytes, space: str, epoch: int, contents: bytes) -> tuple[str, bytes]:
    """-> (id, stored form `0x01 + ciphertext`). `k` is the key of `epoch`."""
    _check_space(space)
    bid = blob_id(k, epoch, contents)
    ct = ChaCha20Poly1305(_blob_key(k, bid)).encrypt(_ZERO_NONCE, contents, f"{space}\n{bid}".encode())
    return bid, STORED_VERSION + ct


def blob_decrypt(k: bytes, space: str, bid: str, stored: bytes) -> bytes:
    """The contents; SealError if the stored form is wrong, tampered, or its contents don't hash to `bid`.
    `k` is the key of the epoch in the id (a caller with the wrong epoch's key just fails)."""
    _check_space(space)
    epoch, _ = parse_blob_id(bid)
    if not stored or stored[:1] != STORED_VERSION:
        raise SealError("cannot open")
    try:
        contents = ChaCha20Poly1305(_blob_key(k, bid)).decrypt(_ZERO_NONCE, stored[1:], f"{space}\n{bid}".encode())
    except Exception as e:
        raise SealError("cannot open") from e
    if not hmac.compare_digest(blob_id(k, epoch, contents), bid):
        raise SealError("blob id does not match its contents")
    return contents


# --- sealed JSON (manifests, lock details) -------------------------------------------------------


def _aead_seal(key: bytes, plaintext: bytes, aad: bytes, nonce: bytes | None) -> dict:
    nonce = os.urandom(12) if nonce is None else nonce
    if len(nonce) != 12:
        raise ValidationError("a nonce is 12 bytes")
    return {"nonce": b64.encode(nonce), "ct": b64.encode(ChaCha20Poly1305(key).encrypt(nonce, plaintext, aad))}


def _aead_open(key: bytes, sealed: object, aad: bytes) -> bytes:
    try:
        if not isinstance(sealed, dict):
            raise SealError("cannot open")
        nonce, ct = b64.decode(sealed.get("nonce")), b64.decode(sealed.get("ct"))
        if len(nonce) != 12:
            raise SealError("cannot open")
        return ChaCha20Poly1305(key).decrypt(nonce, ct, aad)
    except SealError:
        raise
    except Exception as e:
        raise SealError("cannot open") from e


def validate_manifest(m: object) -> dict:
    """`{message?, files: {path: {blob, size, exec}}, conflicts?: [path]}`; unknown fields are ignored."""
    if not isinstance(m, dict):
        raise ValidationError("a manifest is an object")
    if "message" in m and not isinstance(m["message"], str):
        raise ValidationError("message must be a string")
    files = _need(m, "files", dict, "an object")
    for path, f in files.items():
        check_path(path)
        if not isinstance(f, dict):
            raise ValidationError("a file entry is an object")
        parse_blob_id(f.get("blob"))
        size = f.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ValidationError("size must be a non-negative integer")
        if not isinstance(f.get("exec"), bool):
            raise ValidationError("exec must be a boolean")
    if "conflicts" in m:
        c = _need(m, "conflicts", list, "a list")
        for path in c:
            check_path(path)
    return m


def manifest_blobs(manifest: dict) -> list[str]:
    """The `blobs` list a version needs: every blob the manifest uses, sorted, distinct."""
    return sorted({f["blob"] for f in validate_manifest(manifest)["files"].values()})


def meta_seal(k: bytes, space: str, seq: int, author: str, manifest: dict, *, nonce: bytes | None = None) -> dict:
    """-> {"nonce", "ct"}: the manifest as compact UTF-8 JSON under K_meta; aad = `space\\nseq\\nauthor`.
    `nonce` is random unless fixed (test vectors only)."""
    _check_space(space)
    validate_manifest(manifest)
    return _aead_seal(k_meta(k), serialize_payload(manifest), f"{space}\n{seq}\n{author}".encode(), nonce)


def meta_open(k: bytes, space: str, seq: int, author: str, meta: object) -> dict:
    """The manifest (validated); SealError on any failure of the AEAD, ValidationError on a bad manifest."""
    _check_space(space)
    raw = _aead_open(k_meta(k), meta, f"{space}\n{seq}\n{author}".encode())
    try:
        m = json.loads(raw.decode("utf-8"))
    except ValueError as e:
        raise ValidationError("manifest is not valid JSON") from e
    return validate_manifest(m)


# --- locks -------------------------------------------------------------------------------------


def lock_tag(k: bytes, path: str) -> str:
    """base64url of the first 16 bytes of HMAC-SHA256(K_tag, path)."""
    return b64.encode(_hmac(k_tag(k), check_path(path).encode("utf-8"))[:16])


def valid_tag(tag: object) -> bool:
    try:
        return isinstance(tag, str) and len(b64.decode(tag)) == 16
    except EncodingError:
        return False


def lock_seal(k: bytes, space: str, tag: str, path: str, reason: str = "", *, nonce: bytes | None = None) -> dict:
    """-> {"nonce", "ct"}: {"path", "reason"} under K_meta; aad = `space\\ntag`. `k` is the current epoch's key."""
    _check_space(space)
    if not valid_tag(tag):
        raise ValidationError("not a lock tag")
    if not isinstance(reason, str):
        raise ValidationError("reason must be a string")
    body = serialize_payload({"path": check_path(path), "reason": reason})
    return _aead_seal(k_meta(k), body, f"{space}\n{tag}".encode(), nonce)


def lock_open(k: bytes, space: str, tag: str, sealed: object) -> dict:
    """-> {"path", "reason"}, and the path must hash to `tag`."""
    _check_space(space)
    raw = _aead_open(k_meta(k), sealed, f"{space}\n{tag}".encode())
    try:
        d = json.loads(raw.decode("utf-8"))
        path, reason = d["path"], d.get("reason", "")
        if not isinstance(reason, str):
            raise ValueError
    except (ValueError, KeyError, TypeError) as e:
        raise ValidationError("lock details are not valid") from e
    if not valid_path(path) or not hmac.compare_digest(lock_tag(k, path), tag):
        raise ValidationError("lock path does not match its tag")
    return {"path": path, "reason": reason}


# --- space-key documents -----------------------------------------------------------------------


def space_key_info(space: str, epoch: int, member: str, member_kid: str, granter: str, granter_kid: str) -> bytes:
    """`"l0wagent-space-key/1\\n" + space + "\\n" + epoch + "\\n" + member + "\\n" + member_kid + "\\n" + granter + "\\n" + granter_kid`."""
    _check_space(space)
    _check_epoch(epoch)
    return _ascii(KEY_INFO_PREFIX, space, epoch, member, member_kid, granter, granter_kid)


def seal_space_key(k: bytes, member_seal_pub: bytes | Key, *, space: str, epoch: int, member: str, member_kid: str,
                   granter: str, granter_kid: str, eph_priv: bytes | None = None) -> dict:
    """K sealed to the member's seal key -> {"alg", "enc", "ct"}. `eph_priv` is for test vectors only."""
    pub = member_seal_pub.pub if isinstance(member_seal_pub, Key) else member_seal_pub
    info = space_key_info(space, epoch, member, member_kid, granter, granter_kid)
    return _seal.seal(pub, _check_key(k), info, eph_priv=eph_priv)


def build_space_key(space: str, epoch: int, member: str, member_kid: str, granter: str, granter_kid: str, sealed: dict, *,
                    iat=None, exp=None) -> dict:
    return _p("space-key", iat, exp, space=space, epoch=epoch, member=member, member_kid=member_kid, granter=granter,
              granter_kid=granter_kid, sealed=sealed)


def validate_space_key(p: dict) -> dict:
    _base(p, "space-key")
    _check_space(_need(p, "space", str))
    _check_epoch(_need(p, "epoch", int, "an integer"))
    split_address(_need(p, "member", str))
    split_address(_need(p, "granter", str))
    _kid(p, "member_kid")
    _kid(p, "granter_kid")
    s = _need(p, "sealed", dict, "an object")
    if s.get("alg") != _seal.ALG or not isinstance(s.get("enc"), str) or not isinstance(s.get("ct"), str):
        raise ValidationError("sealed is not an HPKE seal")
    return p


def space_key_info_of(p: dict) -> bytes:
    """The `info` for a space-key payload, from its own fields."""
    return space_key_info(p["space"], p["epoch"], p["member"], p["member_kid"], p["granter"], p["granter_kid"])


def space_key_payload(doc: object, now: int | None = None) -> dict:
    """A space-key document's payload, validated; the signature is NOT checked here."""
    if not isinstance(doc, dict) or doc.get("type") != "space-key" or not isinstance(doc.get("v"), str) \
            or not isinstance(doc.get("payload"), str):
        raise ValidationError("not a space-key document")
    return validate_space_key(parse_payload(doc, now))


def open_space_key_payload(private_key: bytes, p: dict) -> bytes:
    """Open an already-validated space-key payload with the member's seal private key -> K (32 bytes)."""
    k = _seal.open_sealed(private_key, p["sealed"], space_key_info_of(validate_space_key(p)))
    if len(k) != 32:
        raise SealError("cannot open")
    return k


def open_space_key_with(private_key: bytes, doc: dict, now: int | None = None) -> bytes:
    """Open a space-key document with the member's seal private key -> K. Does not check the granter's signature
    (the caller does, against the granter's directory entry) or that this key is the member's."""
    return open_space_key_payload(private_key, space_key_payload(doc, now))


def verify_space_key(doc: dict, granter_key: Key, *, space: str | None = None, member: str | None = None,
                     member_kid: str | None = None, granter: str | None = None, epoch: int | None = None,
                     now: int | None = None) -> dict:
    """Signed by exactly the granter's signing key; `granter_kid` must be that key. Named fields are compared."""
    if isinstance(doc, dict) and len(doc.get("sigs") or []) != 1:
        raise SignatureError("a space key carries exactly one signature")
    p = validate_space_key(verify_document(doc, [granter_key], expected_type="space-key", required=[granter_key.kid], now=now))
    if p["granter_kid"] != granter_key.kid:
        raise SignatureError("granter_kid is not the signing key")
    for name, want in (("space", space), ("member", member), ("member_kid", member_kid), ("granter", granter), ("epoch", epoch)):
        if want is None:
            continue
        got = p[name]
        if name in ("member", "granter"):
            got, want = got.lower(), want.lower()
        if got != want:
            raise SignatureError(f"space key is for another {name}")
    return p


# --- space-version documents --------------------------------------------------------------------


def valid_version_id(v: object) -> bool:
    return isinstance(v, str) and bool(_VERSION_ID.match(v))


def version_id(payload: bytes | str) -> str:
    """`"v." + base64url(first 16 bytes of SHA-256(the document's `payload` string, as ASCII))` (spaces.md)."""
    if isinstance(payload, str):
        payload = payload.encode("ascii")
    return "v." + b64.encode(hashlib.sha256(payload).digest()[:16])


def version_doc_bytes(doc: dict) -> bytes:
    """A space-version document as compact JSON text (what `sign_space_version` returns)."""
    return serialize_payload(doc)


def version_id_of(doc: dict) -> str:
    return version_id(doc["payload"])


def build_space_version(space: str, seq: int, parent: str | None, epoch: int, author: str, blobs: Sequence[str], meta: dict, *,
                        iat=None, exp=None) -> dict:
    return _p("space-version", iat, exp, space=space, seq=seq, parent=parent, epoch=epoch, author=author,
              blobs=sorted(set(blobs)), meta=meta)


def validate_space_version(p: dict) -> dict:
    _base(p, "space-version")
    _check_space(_need(p, "space", str))
    seq = _need(p, "seq", int, "an integer")
    if seq < 1:
        raise ValidationError("seq starts at 1")
    parent = p.get("parent", ...)
    if seq == 1:
        if parent is not None:
            raise ValidationError("the first version has no parent")
    elif not valid_version_id(parent):
        raise ValidationError("parent must be a version id")
    _check_epoch(_need(p, "epoch", int, "an integer"))
    split_address(_need(p, "author", str))
    blobs = _need(p, "blobs", list, "a list")
    for b in blobs:
        parse_blob_id(b)
    if len(set(blobs)) != len(blobs):
        raise ValidationError("blobs must be distinct")
    meta = _need(p, "meta", dict, "an object")
    try:
        if len(b64.decode(meta.get("nonce"))) != 12:
            raise ValidationError("meta.nonce is 12 bytes")
        b64.decode(meta.get("ct"))
    except EncodingError as e:
        raise ValidationError("meta is not base64url") from e
    return p


def verify_space_version(doc: dict, author_key: Key, *, space: str | None = None, author: str | None = None,
                         now: int | None = None) -> dict:
    """Signed by exactly the author's signing key. (That the author is a member and the chain is right is checked
    by the service and by readers: `check_manifest_blobs`, `check_chain`.)"""
    if isinstance(doc, dict) and len(doc.get("sigs") or []) != 1:
        raise SignatureError("a space version carries exactly one signature")
    p = validate_space_version(verify_document(doc, [author_key], expected_type="space-version", required=[author_key.kid], now=now))
    if space is not None and p["space"] != space:
        raise SignatureError("version is for another space")
    if author is not None and p["author"].lower() != author.lower():
        raise SignatureError("version is by another author")
    return p


def check_manifest_blobs(manifest: dict, blobs: Sequence[str]) -> None:
    """A reader refuses a manifest whose blob ids don't appear in the version's `blobs`."""
    listed = set(blobs)
    for f in validate_manifest(manifest)["files"].values():
        if f["blob"] not in listed:
            raise ValidationError("manifest uses a blob the version does not list")


def check_chain(p: dict, head_seq: int, head_id: str | None) -> None:
    """A reader applying version payload `p` on top of what it had: `seq` and `parent` must chain."""
    if p["seq"] != head_seq + 1 or p["parent"] != head_id:
        raise ValidationError("version does not chain to the one before")
