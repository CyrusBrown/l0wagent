# Membership

## Names
- Lowercase ASCII letters and digits, with single dashes between them: `^[a-z0-9]+(-[a-z0-9]+)*$`,
  3 to 32 characters. No `--` (hubs use it in flat hostnames, e.g. `files--mara`).
- **Reserved** names (a hub MUST refuse them, and MAY reserve more): `admin`, `api`, `www`, `hub`,
  `files`, `web`, `mail`, `email`, `board`, `spaces`, `rooms`, `relay`, `status`, `help`, `support`,
  `abuse`, `postmaster`, `root`, `security`, `tunnel`, `directory`, `well-known`, plus the names of
  the hub's services and the hub's own name.
- **Lookalikes**: a hub MUST refuse a name that equals an existing or reserved name after mapping
  `0→o`, `1→l`, `i→l`, `5→s` and removing dashes (answer `409 name_reserved` or `409 name_taken`, whichever it collides with).
- **Never reused.** A name that left stays taken forever (its directory answers `410`).

## Invites
An invite is a string the owner pastes into their instance: `l0w-invite:<hub base URL>#<code>`, e.g.
`l0w-invite:https://hub.example#Zk3…`. The code is opaque, at least 128 bits of randomness, single
use, and expires (the hub decides when; 7 days is RECOMMENDED). The instance fetches the hub's
capabilities from the base URL before showing the owner anything.

`GET {api}invites/{code}` → `{"v", "valid": true, "expires": "…", "name_hint": "mara" | null, "hub": "<hub name>"}`
or `404 {"error": "no_such_invite"}` / `410 {"error": "invite_used" | "invite_expired"}`. It doesn't use the invite.

## Joining (`POST {api}members`)
Before sending anything the instance MUST check: 2FA is on for its owner, a backup has been made
(or the owner has explicitly skipped it), and the hub's capabilities document verifies.

The request body is a signed document of type `registration`, signed by **both** the new signing key and
the recovery key (proving the instance holds both). Payload:

```json
{
  "v": "l0w/0.1", "type": "registration", "iat": "…",
  "hub": "hub.example", "name": "mara", "invite": "<code>",
  "mode": "tunnel",
  "own_urls": null,
  "keys": [<sign key>, <seal key>, <recover key>],
  "instance": {"release": "0.9.0", "protocols": ["l0w/0.1"]}
}
```

- `mode`: `"tunnel"` or `"own-url"`. With `"own-url"`, `own_urls` is `{"instance": "https://…", "files": "https://…"}`;
  the hub MUST check both answer over HTTPS, are different origins, and serve the member's `self`
  document at the instance URL (identity.md) before accepting, and SHOULD warn when both are the same site.
- `hub` MUST equal the hub's own domain (so a registration can't be replayed at another hub).
- `iat` within 300 s.

Response `201`:

```json
{
  "v": "l0w/0.1",
  "address": "mara@hub.example",
  "hostnames": {"instance": "mara.hub.example", "files": "files--mara.hubfiles.example"},
  "hub_key": {"kid": "k1.…", …},
  "tunnel": "wss://tunnel.hub.example/v0/tunnel"
}
```

`hostnames` are authoritative: the instance uses exactly these and never builds one from the name. In
own-url mode, `hostnames` is the member's own URLs' hosts and `tunnel` is `null`.

Errors: `400 bad_registration` (signature, fields), `403 needs_invite`, `409 name_taken`,
`409 name_reserved`, `410 invite_used`/`invite_expired`, `422 own_url_unreachable`.

After `201` the instance publishes its first `self` document (`PUT {api}members/{name}/self`, below),
connects the tunnel (tunnel.md), and checks **from outside** that both hostnames answer and that the
directory serves its key. Until the first `self` is accepted, the directory answers `404` for the name.

## Updating the self-description (`PUT {api}members/{name}/self`)
Body: a `self` document. Authentication: the document itself (signed by the member's current signing
key, `seq` higher than the hub's copy). The hub checks `address`, `hostnames` and keys against its
records (a key change must come with a rotation in `history` signed by the previous key or the
recovery key) and answers `200` with the countersigned directory entry.

## Leaving (`POST {api}members/{name}/leave`)
Body: a signed `leave` document: `{"v", "type": "leave", "iat", "address", "moved_to": "<new address>" | null}`.
The hub removes the hostnames and the tunnel, answers `410` for the name from then on (or `301` with
the member's `moved` document, if `moved_to` is set and the member published one), and never gives
the name to anyone else. Response: `200 {"v", "left": true}`.

## Re-registration when both keys are lost
The owner gets a new invite from the hub's operator (out of band), and registers again with
`"name": "<the same name>"` and `"reregister": true` in the payload. If the operator approves, the hub
issues a `reregistration` document (`{"v", "type": "reregistration", "iat", "address", "old_keys": [kids], "new_keys": [kids]}`,
signed by the hub) and serves it at `GET /.well-known/l0wagent/members/<name>/reregistrations`
(a JSON array of signed documents, oldest first). Friends' instances MUST NOT follow it
automatically: they show their owner a card ("mara's key changed without proof it's the same instance.
Re-verify?").

## Dormant members
A hub MAY take away the tunnel and hostnames of a member whose instance hasn't connected or pushed a
`self` for a long time (at least 90 days), and gives them back when it returns. The name stays reserved.
