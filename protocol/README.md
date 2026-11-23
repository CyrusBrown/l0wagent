# The l0wagent hub protocol

**Status: draft 0 (H2), for the owner's review.** Version string: `l0w/0.1`.

This folder is the spec: what a hub must do and what an instance must do to join one. A hub is any
program that passes the conformance suite here; nothing in the spec names a particular hub, domain or
hosting provider. Your hub on Cloudflare (`hub/`) and the fake hub (`dev/hub/`) are two implementations.

The spec covers the **core**: membership, routing (the tunnel), identity (keys, the self-description,
the signed directory), capabilities and hop authentication; and, from H5, **messages between instances**
(envelopes, delivery tokens, friends; messages.md) and, from H6, **hub services** (services.md). Published services come in H7.
Hub-to-hub linking and streams are not part of it.

| Document | What it defines |
|---|---|
| [signing.md](signing.md) | Keys, key ids, signed documents, base64url, timestamps |
| [identity.md](identity.md) | The self-description, the directory entry, rotation, recovery, revocation, "moved to" |
| [capabilities.md](capabilities.md) | The hub's capabilities document |
| [membership.md](membership.md) | Names, invites, registration, hostnames, leaving, re-registration |
| [tunnel.md](tunnel.md) | The tunnel: handshake, framing, flow control, reconnecting, offline answers |
| [accelerators.md](accelerators.md) | Optional faster ways to connect: the `cloudflared` accelerator and its signed request |
| [messages.md](messages.md) | Envelopes, sealing (HPKE), delivery tokens, friend codes and requests, presence, waking |
| [services.md](services.md) | Hub services: the hub-signed service document, connecting, calls, events, role grants |
| [board.md](board.md) | The board service kind: topics, posts, votes, discussions, moderation |
| [spaces.md](spaces.md) | The shared-spaces service kind: encrypted, signed, synced folders with locks |
| [hop-auth.md](hop-auth.md) | HTTP Message Signatures between instances, hubs and gateways |
| [versions.md](versions.md) | Version strings, unknown fields, what a hub must support |
| `openapi.json` | The hub's core HTTP endpoints |
| `schemas/` | JSON Schemas for every document |
| `vectors/` | Test vectors: fixed keys, the exact bytes signed, the expected signatures |
| `py/`, `ts/` | The reference libraries: Python (`l0wproto`) and TypeScript (`@l0w/proto`, for hub services on Workers); both run every vector |
| `conformance/` | The conformance suite: `python -m conformance --hub URL` |

## Words
- **Hub**: a service that gives members hostnames, a way to be reached, and a signed directory.
- **Member**: one instance registered with a hub under a **name**. An owner with two instances is two members.
- **Instance hostname** / **files hostname**: the two hostnames every member gets (see membership.md).
  They must be different origins and should be different sites.
- **Gateway**: the instance's container that holds its private keys and signs for it. Nothing else in
  the instance sees a private key.
- MUST, SHOULD, MAY: as in RFC 2119.

## Rules that apply everywhere
- All hub endpoints are HTTPS. JSON bodies are UTF-8, `Content-Type: application/json`.
- Every document has a `"v"` field (the protocol version, versions.md). Receivers MUST ignore fields
  they don't know.
- Times are RFC 3339 UTC strings with a `Z` and whole seconds (`2026-11-17T21:30:00Z`). Clocks may
  differ by up to 300 seconds; checks allow that much skew and no more.
- Errors from a hub are `{"v": "l0w/0.1", "error": "<code>", "detail": "<human text>"}` with a 4xx or
  5xx status. Codes are listed per endpoint in `openapi.json`.
