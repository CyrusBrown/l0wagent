# Shared spaces (`kind: "spaces"`)

**Status: draft (owner's go-ahead 2026-11-22: the full design, end to end encrypted), for review.** Part of
`l0w/0.1`. A well-known kind of hub service (services.md); calls, replies and events are `service-*`
envelopes. The design is [docs/plans/hub.md](../docs/plans/hub.md#shared-space-hub-service).

A **space** is a synced folder shared by members of one hub. The service stores and orders it but
**can't read it** (files, paths, commit messages and lock reasons are encrypted to the members) and
**can't forge it** (every version is signed by the instance that made it). It can withhold or delay.

## Keys
- Each space has **epochs**, numbered from 1. Each epoch has a **space key** `K` (32 random bytes), made
  by the space's creator. Removing a member starts a new epoch; nothing else does. Members keep old
  epochs' keys: what a removed member already had, they keep.
- From `K`, by HKDF-SHA256 (RFC 5869, empty salt, 32-byte outputs) with these ASCII `info` strings:
  `K_id` = `"l0wagent-space/1 blob-id"`, `K_blob` = `"l0wagent-space/1 blob-key"`, `K_meta` = `"l0wagent-space/1 meta"`, `K_tag` = `"l0wagent-space/1 lock-tag"`.
- **Space-key documents** (`type: "space-key"`), signed by the creator's instance, carry `K` sealed to one member:
  ```json
  {"v": "l0w/0.1", "type": "space-key", "iat": "…", "space": "<space id>", "epoch": 1,
   "member": "mara@hub.example", "member_kid": "<mara's seal key id>", "granter": "<creator address>",
   "granter_kid": "<creator's signing key id>",
   "sealed": {"alg": "HPKE-v1-X25519-SHA256-ChaCha20Poly1305", "enc": "…", "ct": "…"}}
  ```
  `sealed` is HPKE (messages.md's suite, base mode) of the 32 bytes of `K` to `member_kid`, with
  `info` = `"l0wagent-space-key/1\n" + space + "\n" + epoch + "\n" + member + "\n" + member_kid + "\n" + granter + "\n" + granter_kid`
  and empty `aad`. A member checks the signature (the granter's directory entry), that the granter is the
  space's creator, and that it is the member named. When a member rotates its seal key, the creator
  re-grants (event `rekey`, below). Members verify a space-key document against the granter's **current** signing key; when the
  creator rotates its signing key it re-grants every member's keys (`keys.put`), and until then a member keeps the keys it already opened.
- On an instance, only the gateway opens a space-key document (it holds the seal key). It builds `info`
  from the document's own fields, so it can't be used to open anything else, and returns `K` to control.

## Blobs
- A file's contents are one **blob** (at most the service's `max_file`, default 95 MiB).
- **Id**: `"e" + epoch + "." + base64url(HMAC-SHA256(K_id, contents))`. The same file in the same epoch is the same blob.
- **Encryption**: `key` = HMAC-SHA256(`K_blob`, id as ASCII); ciphertext = ChaCha20Poly1305(`key`, nonce 12 zero bytes,
  contents, aad = `space + "\n" + id`). The key is unique per contents, so the fixed nonce is never reused with a key.
  Stored and transferred as `0x01` followed by the ciphertext (tag included).
- A reader recomputes the id from the decrypted contents and rejects a blob whose id doesn't match.

## Versions (`type: "space-version"`)
Every change is a version, signed by the instance of the member who made it:
```json
{"v": "l0w/0.1", "type": "space-version", "iat": "…", "space": "<space id>", "seq": 7,
 "parent": "<version id of seq 6>" or null, "epoch": 2, "author": "mara@hub.example",
 "blobs": ["e1.…", "e2.…"], "meta": {"nonce": "<base64url, 12 bytes>", "ct": "<base64url>"}}
```
- **Version id**: `"v." + base64url(first 16 bytes of SHA-256(the document's `payload` string, as ASCII))`. The payload is carried as exact
  bytes (signing.md), so the id survives the document being re-serialized inside call arguments.
- `blobs`: every blob the version's files use (sorted ascending by code point, distinct), so the service can count quota and keep blobs alive.
- `meta`: ChaCha20Poly1305(`K_meta` of `epoch`, random `nonce`, the manifest as UTF-8 JSON, aad = `space + "\n" + seq + "\n" + author`):
  ```json
  {"message": "Added motor mounts", "files": {"cad/frame.FCStd": {"blob": "e2.…", "size": 81234, "exec": false}},
   "conflicts": ["notes.md"]}
  ```
  Paths are relative, `/`-separated, at most 1024 bytes, without empty, `.` or `..` segments; `conflicts`
  lists files this version left in conflict (below). A manifest is the whole tree, not a diff.
- The service checks the signature against the author's directory entry, that the author is a member,
  that `epoch` is the space's current epoch, that every blob is stored, that `seq` is the head's `seq + 1`
  and `parent` the head's id. Otherwise it answers `conflict` with the head. **One head per space**:
  a push succeeds only if the head hasn't moved.
- Readers verify every version they apply, including that `parent` and `seq` chain to the version they
  had, and refuse a manifest whose blob ids don't appear in `blobs`.

## Locks
For files that can't merge (CAD and other binaries). A lock names a file by **tag**: base64url of the
first 16 bytes of HMAC-SHA256(`K_tag` of the current epoch, path). Its path and reason travel sealed
with `K_meta` (`{nonce, ct}`, aad = `space + "\n" + tag`); the plaintext is the UTF-8 JSON `{"path": …, "reason": …}`, and a reader checks that the path gives the tag. Locks expire (`ttl_hours`, at most 72; default 24),
the holder releases them, the creator can break them, and a new epoch clears them.

## Methods
| Method | Args | Result / notes |
|---|---|---|
| `spaces.list` | `{}` | `{spaces: [Space]}`: spaces the member belongs to or is invited to |
| `space.create` | `{name, keys: [space-key doc]}` | `{space: Space}`; `name` like a topic slug, unique per creator; `keys` holds the creator's own epoch-1 key |
| `space.get` | `{space}` | `{space: Space}` |
| `members.invite` | `{space, member, keys: [space-key doc for every epoch 1..current]}` | creator only; the invitee gets `invited`. Every epoch, because the head's files may use older epochs' blobs |
| `space.accept` / `space.decline` | `{space}` | the invitee (its owner decides) |
| `members.remove` | `{space, member, keys: [space-key doc for each remaining member, new epoch]}` | creator only; the epoch goes up by one |
| `keys.put` | `{space, keys: [space-key doc]}` | creator only: re-grants after a member's key rotation |
| `space.leave` | `{space}` | a member (not the creator) |
| `space.delete` | `{space}` | creator only; everything is dropped after 7 days, `deleted` is sent |
| `keys.get` | `{space}` | `{keys: [space-key doc]}`: the caller's keys for every epoch it was in |
| `head` | `{space}` | `{seq, version}` (`seq` 0 and `version` null for an empty space) |
| `versions.list` | `{space, after?: seq, limit?}` | `{versions: [space-version doc], next}` in `seq` order |
| `blobs.missing` | `{space, ids: [id]}` | `{missing: [id]}` (at most 500 ids) |
| `blob.upload` | `{space, id, size}` | `{url, expires}`: `PUT` the stored form there |
| `blob.download` | `{space, id}` | `{url, expires}`: `GET` the stored form |
| `version.push` | `{space, version: space-version doc}` | `{seq, version}`, or error `conflict` with `{head: {seq, version}}` |
| `locks.list` | `{space}` | `{locks: [Lock]}` |
| `lock.take` | `{space, tag, sealed, ttl_hours?}` | `{lock: Lock}`, or error `locked` with `{lock}` |
| `lock.release` | `{space, tag}` | the holder |
| `lock.break` | `{space, tag}` | the creator |

Space ids are chosen by the creator (random, e.g. 16 bytes base64url; the creator's key document names it before the service sees it): 1 to 64 characters of `[A-Za-z0-9_-]`; the service refuses one in use (`exists`).
`Space` is `{id, name, creator, members: [{address, state: "member"|"invited", since}], epoch, seq, version, size, quota, max_file}`.
`Lock` is `{tag, holder, sealed, taken, expires}`.

**Transfer URLs** are on the service's own instance hostname (`https://spaces.hub.example/v0/blob/…`), single-use per
blob and direction, valid 15 minutes. An instance fetches only URLs on that hostname. `PUT` takes the stored form
(`0x01` + ciphertext) with `Content-Length` equal to `size`; the service checks the size, not the contents (it can't).

## Errors
services.md's codes, plus: `not_found` (no such space, or the caller isn't in it), `not_member` (invited, not yet accepted),
`not_allowed` (creator- or holder-only), `exists`, `conflict` `{head}`, `locked` `{lock}`, `missing_blobs` `{missing}`,
`missing_keys` `{members}` or `{epochs}`, `bad_epoch`, `bad_signature`, `bad_document`, `unknown_member`, `quota_exceeded`, `limit_reached`.
Extra fields are part of the error object.

## Events
| Event | Data |
|---|---|
| `head` | `{space, seq, version, author}` (someone pushed) |
| `invited` | `{space, name, creator}` |
| `members` | `{space, members}` (someone joined, left or was removed) |
| `epoch` | `{space, epoch}` (fetch `keys.get`) |
| `rekey` | `{space, member}` (to the creator: a member's seal key changed; re-grant with `keys.put`) |
| `lock` | `{space, lock, state: "taken"|"released"|"broken"|"expired"}` |
| `removed` / `deleted` | `{space}` |

## Limits
The service's settings: `quota` per space (default 1 GiB, counted over distinct live blobs), a total for the
whole service (what it may store, all spaces together; an upload past it is `quota_exceeded` with `{scope: "service", quota}`), `max_file`
(default 95 MiB), spaces per creator (default 10), members per space (default 20). Members are members of
this hub; cross-hub members are later. Old versions are kept (history and restore); a blob no version of
the last 90 days uses may be dropped.

## What an instance does (not normative)
Control syncs, never the agent. A space appears to the agent at `/external/spaces/<name>/` (the extension's
managed folder, which the agent may edit). Control won't push symlinks, special files, files over `max_file`,
files matched by `.l0wignore`, or files containing a vault value. Different files merge; a text file changed
on both sides keeps both (`name (conflict mara 2026-11-22).ext`) and lists it in `conflicts`; an edit to a file
someone else has locked is saved as a copy. Files from other members are untrusted, code included.
