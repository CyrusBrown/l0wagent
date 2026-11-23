# Published services: the assistant's apps, and l0w.page

Status: plan (owner, 2026-11-23). It replaces the details of H7 in [hub-development.md](hub-development.md) and the
"Published services" section of [hub.md](hub.md); where they disagree, this file wins.

## Decisions (owner, 2026-11-23)
- **The assistant can host things again.** Whatever it builds (a game, a dashboard, an API, a static site) runs as an
  **app** in a separate **`apps` container**, never in the agent's own container. This brings back the assistant's own
  page that H1 removed, now safe to publish.
- **The `apps` container ships with every install**, hub or not. Without a hub, apps are reachable locally only, with
  nothing else to set up.
- **Many apps, one container (option A):** each app runs as its own Linux user with its own folders and limits. Apps are
  *separated* from each other, not fully *isolated* (see "What separates apps"). How many apps may run is a setting.
- **On a hub, the hub is a reverse proxy and a name registry**, nothing more: it gives an app a hostname on the hub's
  pages domain (`l0w.page` on L0w) and routes visitors down the member's connection. Who may see an app, and everything
  about running it, stays on the instance.
- **The hub is trusted like an ISP.** It can see traffic it carries (Cloudflare terminates TLS); nothing here tries to hide
  traffic from the operator.
- **`l0w.page` is not on the Public Suffix List** and won't be for a while. Everything below is built to be safe without it.
  Nothing is served on bare `l0w.page`, except, at most, a static list of public apps (the operator's choice).

## What an app is
A folder the agent writes under `/work/apps/<name>/` with an `app.toml`:
```toml
name = "games"                  # lowercase letters, digits, single dashes; becomes part of the hostname
kind = "process"                # or "static": just files, served by the front, no process
start = "node server.js"        # process apps: started with $PORT set; listen on 127.0.0.1:$PORT
static = "public"               # static apps (and optional for process apps): a folder served as-is
cross_site_writes = false       # see "Without the Public Suffix List"
```
- **Publishing snapshots the folder.** Control copies it (through `Storage`, symlink-safe) into the apps volume, read-only.
  The agent keeps editing `/work/apps/<name>/`; nothing goes live until the next publish. Each app also gets a writable
  data folder that survives restarts and republishes.
- **The image is the agent's own**, so anything that runs in the agent's container runs as an app, with nothing extra to pull.

## The `apps` container
- **Networks:** only `appsnet` (internal: control and apps). Not the sandbox network, `hubnet`, the tunnel's network or
  the internet. Internet access for apps is an owner setting, off by default, for the whole container (see "Open questions").
- **No secrets, no tokens, no Docker socket.** It can't call `/api/agent/*`, egress or the gateway. Read-only root
  filesystem, tmpfs for `/tmp`, every capability dropped except `SETUID`/`SETGID` (to start each app as its own user).
- **`appsd`, the supervisor:** a small process of ours in the container. Control calls it over `appsnet` with its own token
  (start, stop, restart, status, logs). It never calls control. It starts each app as its own user, restarts a crashed app
  with backoff, keeps the last logs, and starts published apps again after a reboot.
- **Footprint:** idle, `appsd` alone, a few tens of MB. Container-wide memory and CPU limits are settings.

### What separates apps
| | |
|---|---|
| **Separate** | a Linux user each; code read-only and data folder `0700`, owned by that user; its own `/tmp`; per-app process, memory and open-file limits (`prlimit`) |
| **Shared** | the container's network (one app can connect to another's port on 127.0.0.1); CPU; the kernel |
| **Never reachable from any app** | the agent's container and its files, tokens and logins; control's API; the gateway and its keys; egress; the owner's vault |

So a bug in one app can expose that app's data, or let it poke at another app's port. It can't reach the assistant, the
owner's files or any credential. Apps that hold anything private should check who's calling, even from 127.0.0.1.
Full per-app containers (option B: a narrow Docker helper like `ops-docker`) stay a later option.

## The front (in control)
Every request for an app goes through a front end in control's container, the same trusted code that serves the files origin.
`appsd` and the apps never see a request the front hasn't checked.
- **Access**, per app: `owner` (default), `lan`, `friends`, `hub`, `public`.
  - `owner`, `friends`, `hub`: a **visit token**. The viewer's own instance mints it: signed, naming the app, valid one minute,
    usable once. The front checks it against the access rule and the signer's pinned key, sets a session cookie for that one
    app, and redirects to drop the token. The owner's "Open" button mints one for the owner. The reserved path is `/__l0w/` on every app.
  - `lan`: anyone who can reach the port (no hub involved). `public`: anyone (hub only, needs the owner's approval).
- **Cookies** (see the next section): the front owns the cookie namespace.
- **Headers on every response**: `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`,
  a `Permissions-Policy` that denies camera, microphone and location unless the app's manifest asks, and the app's own
  headers otherwise passed through (minus the ones below).
- **Limits**: request size, requests per visitor per minute, concurrent connections per app. WebSockets are passed through.
- **Logs**: every visit (app, access level, who, when) is in control's log, as the plan for published services always said.

### Without the Public Suffix List
Every app on `l0w.page` is the **same site** as every other: browsers let one subdomain set cookies for all of `l0w.page`,
and `SameSite` doesn't separate them. The front closes that on the server side:
1. **App cookies are namespaced and `__Host-`.** The front rewrites every `Set-Cookie` an app sends to
   `__Host-a.<name>` form (`Secure`, `Path=/`, no `Domain`). A `__Host-` cookie can only have been set by that exact
   hostname, so on the way in the front forwards **only** that app's own `__Host-a.<name>` cookies, unprefixed, and drops
   every other cookie. A cookie tossed from another `l0w.page` subdomain never reaches an app. (Cookies an app sets from
   JavaScript don't get through; apps keep client state in `localStorage`, which is per origin.)
2. **Our own cookies** (the visit session) are `__Host-` too, so nothing can overwrite them.
3. **Cross-page writes are refused.** A non-GET request whose `Sec-Fetch-Site` is `same-site` or `cross-site` gets 403,
   unless the app's manifest sets `cross_site_writes = true` (public APIs). This is the CSRF protection `SameSite` can't give
   without the Public Suffix List.
4. **Nothing is served on bare `l0w.page`** with cookies or scripts that could matter. At most, a static list.
5. **Shared reputation** is the one thing the server can't fix: if one app is flagged for phishing, browsers may warn on all
   of `l0w.page`. The operator's takedown (below) and owner approval for public apps are the answer.

The same rewriting protects local use, where apps sit on ports of the owner's own host: browsers send a host's cookies to
every port, so the front never forwards control's cookies to an app and never lets an app set a cookie named like control's.

## Without a hub: the assistant's own page
- Apps are reachable on the instance's LAN address, **one port per app** from a range (`L0W_APPS_PORTS`, default
  `8790-8799`, published by compose; a different port is a different origin, so apps don't share `localStorage`).
  Access `owner` (default) or `lan`.
- The UI gets an **Apps** page: each app, its state, port, access and logs, with Open, Restart, Unpublish.
- Reaching it from outside the LAN is the owner's own business (Tailscale, their own tunnel), as for the UI today.

## On a hub
### What the hub does
1. **Hostnames on its pages domain.** On the instance's signed request it creates `<app>--<member>.l0w.page` and removes it
   on unpublish; a per-member cap (capabilities say how many). The operator can hand out short names (`games.l0w.page`)
   through the admin API.
2. **Routing.** Requests for those hostnames go down the member's tunnel like the instance's own; the tunnel sends them to
   control's front by `Host`, a third destination beside control and the files app. With the member's `cloudflared`
   accelerator, the hostnames are added to that tunnel's ingress, so traffic skips the hub Worker.
3. **Operator controls.** Takedown per app and per member (the hub stops routing the hostname), a report link, rate limits,
   and optionally the static list on the bare domain.

The capabilities advertise it: `pages: {domain, pattern, max_per_member, public: true|false, needs_accelerator: true|false}`.

### Limits to plan around
- **Worker path**: the hub's tunnel answers WebSocket upgrades `501`, and every request counts toward the Worker's free
  100k a day, shared by everyone. Fine for `friends`/`hub` apps with light traffic.
- **Accelerator path**: no Worker cost, WebSockets work. On L0w, **`public` apps need the accelerator**
  (`needs_accelerator: true`), so one popular page can't use up the hub's requests.
- **DNS records**: each hostname with the accelerator is a record; the per-member cap keeps the zone's count bounded.

### Agent-to-agent calls (from H7, unchanged)
An assistant calls another's app as an API through `/s/<name>/`: its gateway signs the request, the target's gateway checks
the signature and the access rule. The agent never holds keys; what comes back is untrusted content.

## Publishing and the owner
- Commands: `assistant app ls | publish <name> [--access …] | logs <name> | restart <name> | unpublish <name>`.
- **`owner` access: the agent publishes and updates on its own**, no approval. It's the assistant's own page.
- **Widening access** (`lan`, `friends`, `hub`, `public`) is a card the owner approves, once per app and level. Narrowing
  never needs approval. The owner can unpublish anything with one click and is told about every new or widened app.
- Updates to an app that's already wider than `owner`: a setting, per access level (default: go live without asking, except
  `public`, which asks).
- Settings (Advanced, and the chat can propose them): max apps, container memory and CPU, the port range, internet for apps,
  update approval per level. Harness news announces the commands when they land.

## Phases
1. **Local apps.** The `apps` container and `appsd`, the front (cookies, headers, limits, visit tokens for `owner`), the port
   range, `assistant app …`, the Apps page, settings, tests (including an app that tries to reach the agent, control,
   another app's data folder and the internet). Works with no hub.
2. **On a hub, for friends and the hub.** The spec (pages hostnames in the capabilities, the signed hostname request,
   visit tokens), the hub core and the fake hub, the tunnel's third destination, `/s/<name>/` signed calls.
3. **Public.** Owner approval for `public`, accelerator ingress for app hostnames, the operator's takedown, report link and
   short names, the optional list on the bare domain, and `l0w.run`'s terms updated for hosted content.

**Done when** (all three): an app the assistant built is open on your LAN with no hub; a friend opens it at
`<app>--low.l0w.page` with a visit token and someone outside the hub can't; a `public` app is reachable through the
accelerator with WebSockets; a cookie set for `l0w.page` by another app never reaches it; and unpublishing removes it and its
hostname.

## Security invariants to add when this is built
- Agent-built apps run only in the `apps` container: on `appsnet` only, no secrets, no Docker socket, no sandbox network,
  no internet unless the owner turns it on. Control's front is the only way in; `appsd` never calls control.
- Every request to an app passes the front: access check, cookie rewriting (only the app's own `__Host-a.<name>` cookies in,
  never control's), cross-site write refusal, size and rate limits.
- Widening an app's access past `owner` needs the owner; the agent can't approve it.
- Hub hostnames for apps are created only on the instance's signed request and removed on unpublish.

## Open questions
- **Internet for apps** is all-or-nothing for the container in option A (apps share its network). Per-app internet would
  need a proxy that tells apps apart, or option B.
- **Static apps** could skip the container entirely (the front serves them). Worth it for phase 1, or one path for both?
- **Port range vs. names locally**: ports are simple and give each app its own origin; names (`games.localhost`) only work on the host itself.
- Moving members' **files hostnames to `l0w.page`** too (`files--low.l0w.page`), as hub.md first planned, would take agent
  files off `l0w.run`'s site; it's a separate change.
