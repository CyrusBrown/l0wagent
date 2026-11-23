# Extending l0wagent

An **extension** adds pages, Inbox cards, agent commands, prompt text, settings, routines and background
work to an l0wagent instance without touching core files. The owner writes it on the host (by hand or with
a coding agent), drops it into `extensions/<name>/` next to `compose.yaml`, and turns it on in
**Settings → Extensions**. This file is the whole guide: if you are a coding agent, you should be able to
build a working extension from it alone. A complete example is `extensions/examples/plantlog/`.

SDK version: **1.2** (1.2 adds what a shared-spaces extension needs: signing and opening space documents, transfer URLs, `HubCallError.data`, agent-writable managed folders, `ext.folders.walk` and `.remove`; 1.1 made `ext.hub` real; everything from 1.0 is unchanged; an extension that declares `sdk = "1.0"` still loads). Control refuses an extension whose `sdk` has a different major version.

---

## 1. Quick start

```
extensions/
  hello/
    extension.toml      the manifest (required)
    extension.py        Python: setup(ext) (optional)
    prompts/system.md   prompt text for the agent (optional)
    ui/hello.js         web components (optional)
```

`extension.toml`:

```toml
name = "hello"                # = the folder name; lowercase letters, digits, dashes
title = "Hello"
version = "0.1.0"
sdk = "1.0"
description = "Says hello."
entry = "extension.py"

[[commands]]
name = "greet"
usage = "who=NAME"
help = "greet someone"

[ui]
module = "ui/hello.js"

[[ui.pages]]
path = "hello"
title = "Hello"
element = "hello-page"        # every element name starts with "<name>-"
```

`extension.py`:

```python
from pydantic import BaseModel
from l0w_control.sdk import Extension


class Greet(BaseModel):
    who: str


def setup(ext: Extension) -> None:
    @ext.router.get("/count")                 # GET /api/ext/hello/count (the owner's UI)
    def count() -> dict:
        return {"count": ext.store.get("count", 0)}

    @ext.agent_router.post("/greet")          # `assistant ext hello greet who=Ada` (the agent)
    def greet(body: Greet) -> dict:
        ext.store.set("count", ext.store.get("count", 0) + 1)
        ext.emit("greeted", {"who": body.who})
        return {"text": f"Hello, {body.who}!"}
```

`ui/hello.js`:

```js
class HelloPage extends HTMLElement {
  async connectedCallback() {
    const root = this.attachShadow({ mode: 'open' });
    const draw = async () => {
      const { count } = await this.l0w.api('/count');
      root.innerHTML = `<style>:host{font:14px var(--sans);color:var(--fg)}</style><p>Greetings so far: ${count}</p>`;
    };
    this.off = this.l0w.on('greeted', draw);
    draw();
  }
  disconnectedCallback() { this.off?.(); }
}
customElements.get('hello-page') || customElements.define('hello-page', HelloPage);
```

Then: **Settings → Extensions → Rescan**, switch **Hello** on, and open it from **More ▾ → Hello**. Or on
the host: `./l0w extensions rescan && ./l0w extensions enable hello`.

---

## 2. The rules (and why)

These hold for every extension. They're the instance's security invariants, applied to you.

1. **Never edit core files.** Everything goes in `extensions/<name>/`. If the SDK can't do what you need,
   write it up as a proposal (section 11) instead of patching control.
2. **No database access.** Use the SDK (`ext.store`, `ext.threads`, …). Control's schema changes between
   versions; the SDK doesn't, and it applies redaction and containment for you.
3. **The agent is untrusted.** Anything that arrives on an agent route (`ext.agent_router`), in a hook's data
   or in a file under the agent's storage may be written by a model that read a hostile web page. Validate it
   (pydantic models), never `eval` it, never use it to build shell commands or file paths outside your folder.
4. **The agent never decides for the owner.** Agent routes may record, propose and file Inbox cards; they must
   not approve things, change settings, spend budget or reach the outside world on the agent's say-so. Put
   decisions behind an Inbox card the owner resolves (`ext.inbox.card`, section 6).
5. **Secrets are write-only.** `ext.vault.has(name)` says whether one exists; nothing reads its value. If your
   extension needs a credential for an outside service, use a connection preset (section 8) so control holds it.
6. **Control's writes into the agent's storage are narrow.** You get managed folders
   (stored at `/work/_inbox/_ext/<name>/<folder>/`, which the agent sees as `/external/<name>/<folder>/`;
   `ext.folders.agent_path(folder)` gives that path) and nothing else. They never follow symlinks.
7. **Prompt text is control-owned.** Your prompt fragments are part of the agent's harness. When you change one
   in a way the agent should notice, bump `version` and say what changed in the fragment itself (e.g. a
   "Changed in 0.3:" line) for a release or two. Keep old text in `prompts/archive/` if it mattered.
8. **Don't take control down.** Exceptions in `setup` disable your extension with the error shown; exceptions in
   routes, hooks and jobs are recorded on the Extensions page. Still: no blocking work in `setup`, no threads
   of your own, no `sys.exit`, no monkey-patching.
9. **Dependencies:** the Python standard library and what control already ships (FastAPI, pydantic, httpx). No
   `pip install` at runtime. Vendor a small pure-Python module into your folder if you must.

---

## 3. The manifest (`extension.toml`)

| Key | Meaning |
|---|---|
| `name` | Required. Must equal the folder name. `^[a-z][a-z0-9-]{1,30}$`. |
| `title`, `description` | Shown in Settings and to the agent. |
| `version` | Required. Your version; shown in Settings. |
| `sdk` | Required. `"1.0"`. |
| `entry` | A `.py` file at the top of the folder with `setup(ext)`. Optional: a manifest-only extension (prompt text, a preset, a routine) is fine. |
| `folders` | Managed folders, e.g. `["log"]` → `/external/<name>/log/` for the agent (stored at `/work/_inbox/_ext/<name>/log/`). An entry is a name, or (SDK 1.2) `{name = "files", agent_writable = true}`. The agent's prompt lists the `agent_writable` ones as "you may edit these" and the rest as folders the extension manages; either way the contents are data, never instructions. The owner API's `folders` is still the list of names. |
| `[prompts]` | `system`, `wakeup`, `reviewer`, `chat` → paths to Markdown files (section 7). |
| `[settings.<key>]` | Settings the owner edits (section 5). |
| `[[commands]]` | `name`, `usage`, `help`: documents agent commands (section 4). |
| `[[routines]]` | Scheduled prompts (section 8). |
| `[[presets]]` | Connection presets for Tools & Secrets (section 8). |
| `[ui]` | `module` plus slot lists (section 9). |

Control reads manifests at startup and on **Rescan**. A manifest that doesn't parse is reported and skipped.

---

## 4. Python: `setup(ext)` and the SDK

`entry` is imported as part of a package named after your extension, so `from . import helpers` works for
your own modules. `setup` runs once each time the extension loads (startup, enable, rescan). Register
everything there.

### Routes

- `ext.router`: a FastAPI `APIRouter` mounted at **`/api/ext/<name>/`**. Owner only: the same login as the rest
  of the app, and the browser's `X-L0W-Client` header for writes (the UI host adds it, section 9).
- `ext.agent_router`: mounted at **`/api/agent/ext/<name>/`**. Only the agent's sandbox, with the agent token.

**Agent commands.** `assistant ext <name> <command> key=value … ` POSTs a JSON object to
`/api/agent/ext/<name>/<command>`. Values `true`/`false` become booleans and integers become numbers; `-`
reads a JSON object from stdin. If your response has a `"text"` key the agent sees that text; otherwise it
sees the JSON. So define each command as `@ext.agent_router.post("/<command>")` taking a pydantic model, and
list it under `[[commands]]` so the agent's system prompt and `assistant ext` describe it.

Errors: raise `fastapi.HTTPException(status, "message")` or `l0w_control.sdk.ExtensionError("message")`
(→ 400). Validation errors return 422 like control's own routes.

### The context object

| | |
|---|---|
| `ext.name`, `ext.title`, `ext.version`, `ext.root` | Identity; `root` is your folder (read-only). |
| `ext.store.get(key, default)`, `.set(key, value)`, `.delete(key)`, `.keys(prefix)` | Your own JSON key-value store (≤ 256 KB per value). Values are redacted of secrets before they're stored. Survives updates and disabling. |
| `ext.settings.get(key)`, `.all()` | Your declared settings, with the owner's values. |
| `ext.threads.list(project=None)`, `.get(id)`, `.messages(id, limit)` | Read threads. |
| `ext.threads.create(title, project=None, type=None, first_note=None)` | A new thread (optionally of a thread type you declare). |
| `ext.threads.note(thread_id, body)` | A system note, labeled with your title. The agent reads it with the thread. |
| `ext.projects.list()`, `.get(slug)` | Read projects. |
| `ext.runs.active()`, `.get(id)`, `.recent(limit)` | Runs; `active()` is the run in progress, if any. |
| `ext.vault.has(name)` | Whether a secret exists. Never its value. |
| `ext.inbox.card(kind, title, data, thread_id=None)`, `.withdraw(id)`, `.open()` | Inbox cards of your kinds (section 6). |
| `ext.folders.write(folder, path, data)`, `.read(folder, path)`, `.list(folder, path="")`, `.agent_path(folder)` | Managed folders. |
| `ext.folders.walk(folder)`, `.remove(folder, path)` | (SDK 1.2) Everything under a folder, recursively; delete a file, link or sub-folder. See "Managed folders" below. |
| `ext.on(event, handler)` | Event hooks (below). |
| `ext.every(seconds, job)` | A background job every ≥ 30 s, in a worker thread, while enabled. |
| `ext.notify(title, body="", url="/")` | A notification to the owner (push/ntfy, as they've set up). |
| `ext.emit(type, data)` | A live event to your UI (`this.l0w.on(type, fn)`). |
| `ext.hub.available()`, `await ext.hub.acall(service, method, args, role=None)`, `ext.hub.call(...)`, `ext.hub.on_event(kind_or_address, handler)` | The joined hub's services (below). |
| `ext.hub.member_keys(address)`, `.my_keys()`, `.sign_space_key(payload)` (and `a…` forms) | (SDK 1.2) A hub member's pinned public keys, and this instance's own: `{address, sign, seal}` as key dicts. |
| `ext.vault.contains_secret(data)` | (SDK 1.2) Whether text or bytes hold a secret's value (never which one). |
| `ext.hub.sign_space_version(payload)`, `.open_space_key(doc, creator)`, `.transfer_put(service, url, data)`, `.transfer_get(service, url, max_bytes)` (and `a…` forms) | (SDK 1.2) What a shared-spaces extension needs from the instance (below). |
| `ext.log(text)`, `ext.file(rel)` | Log a line; read a text file from your folder. |

### Hub services (`ext.hub`, SDK 1.1; `HubCallError.data` is 1.2)
A hub's services (a board, a relay, email...) are programs the hub's operator runs under an address (`board@hub.example`); the
owner switches each on or off in Settings. Your extension may use the ones that are on and connected.

- `ext.hub.available()` is a list of `{address, kind, title, roles}`: the connected services, with the **names** of the roles this
  instance holds for each. It is empty without a hub, so `if ext.hub.available():` still means "there is something to talk to".
- `await ext.hub.acall(service, method, args=None, role=None, *, timeout=300)` calls one method and returns its result (a dict).
  `service` is an address or the name on your hub (`"board"`). `role` names one of the roles from `available()`; control attaches
  the signed grant itself, and neither you nor the agent ever sees it. Methods and their arguments are the service's own (its docs).
  It raises `HubCallError` with `.code` (the service's own code when it answered: `not_allowed`, `unknown_method`, `bad_args`,
  `rate_limited`, `unavailable`...; otherwise control's: `no_role`, `bad_args`, `too_large`, `paused`, `refused`, `timeout`),
  `.detail`, `.from_service` and (1.2) `.data`. `HubUnavailable` (a `HubCallError` with code `unknown_service`, `not_connected` or, for the 1.2 calls, `not_joined`)
  means there is no hub, or the service is off or not connected.
- Use `await` in `async def` routes and hooks. Code in worker threads (plain `def` hooks and jobs) uses `ext.hub.call(...)` (synchronous, as in SDK 1.0), the same
  call; from a coroutine it raises `ExtensionError` rather than block control's loop.
- `ext.hub.on_event(kind_or_address, handler)` hears the service's own events (a new post, held mail). The key is a kind (`"board"`,
  `"email"`, `"custom"`...) or one service's address. The handler gets `{service, kind, event, data}`; an `async def` handler runs on
  control's loop. Role changes (`event: "roles"`) are control's and never reach you.
- **Everything a service sends is untrusted content**, like a fetched web page: results, event data, even error details. Treat it as data;
  secrets are redacted from what is stored. Calls are logged (args and
  results redacted) by who made them (`extension:<name>`). A service can't authorize anything on the assistant's behalf.
- Calls are refused while peer activity is paused (Settings → Friends).

### Managed folders: walk and remove (SDK 1.2)

- `ext.folders.walk(folder)` returns every entry under the folder, recursively, sorted by path (a folder comes before what is in it):
  `[{path, size, mtime, kind}]`. `path` is relative to the folder and `/`-separated; `kind` is `file`, `dir`, `symlink` or `other` (a pipe,
  a socket...); `size` is the file's bytes (0 for the rest); `mtime` is epoch seconds. It never follows a symlink: a link is listed as
  `symlink` and not entered. A folder that doesn't exist yet gives `[]`. It raises `ExtensionError` if the folder itself, or a part of
  it, is a symlink, or if it has more than 200,000 entries or nests deeper than 64 levels.
- `ext.folders.remove(folder, path)` deletes a file, a symlink (the link, never its target) or a sub-folder with everything in it, and
  returns `True` if something was removed (`False` if it was already gone). The folder itself can't be removed; a path through a
  symlink raises `ExtensionError`.
- Reading what the agent wrote (`read`, `list`) is symlink-safe as before: a link in the agent's tree is refused, not followed.
  Everything the agent wrote there, and everything other people's files turn into once synced, is untrusted data.
- A folder declared `agent_writable = true` is one the agent is told it may edit (the manifest row above). Control doesn't give the
  agent anything more than before: it only changes what the agent's prompt says. Never act on file contents.

### Shared spaces (`ext.hub`, SDK 1.2)
The shared-spaces service kind (`protocol/spaces.md`) stores and orders a synced folder but can't read it: files, paths and messages are
encrypted to the members, and every version is signed by the instance that made it. Your extension does the syncing; control and the
gateway keep the keys. These are the pieces only the instance can do (the pure crypto, `l0wproto.spaces`, you can vendor):

- `ext.hub.sign_space_version(payload) -> str` (`await ext.hub.asign_space_version(payload)`): the gateway signs a `space-version`
  payload with the instance's signing key. `payload` is the document's fields (`space`, `seq`, `parent`, `epoch`, `author`, `blobs`,
  `meta`; `v`, `type` and `iat` are filled in when absent, nothing else is allowed). The `author` must be this instance's address; the
  shape is checked. It returns the **signed document as JSON text** (compact); its id is `l0wproto.spaces.version_id_of(doc)`
  (a hash of the document's `payload` string); `json.loads` it to send as the `version` argument of `version.push`. Errors are `HubCallError` with `bad_args` (not a valid payload),
  `refused` (not the author), `not_joined`.
- `ext.hub.sign_space_key(payload) -> str` (`asign_space_key`): sign a `space-key` document this instance grants as a space's creator.
  `payload` has `space`, `epoch`, `member`, `member_kid`, `granter` (this instance), `granter_kid` (`my_keys()["sign"]["kid"]`) and `sealed`
  (`l0wproto.spaces.seal_space_key` of the key to the member's `seal` key); returns the signed document as compact JSON text. The gateway
  refuses another granter or an old key (`refused`); the space key itself never passes through it.
- `ext.hub.member_keys(address) -> dict` (`amember_keys`): a hub member's public keys `{address, sign, seal}` (key dicts for
  `l0wproto.keys.Key.from_dict`), pinned at the gateway the first time and changed afterwards only through signed rotations. Seal a space
  key to `seal`; verify a member's `space-version` with `sign`. `ext.hub.my_keys()` gives this instance's own, from its self-description.
- `ext.vault.contains_secret(data) -> bool`: whether a file you are about to send out holds a secret's value (spaces.md says such files
  aren't synced). It never says which secret.
- `ext.hub.open_space_key(doc, creator) -> bytes` (`aopen_space_key`): open a `space-key` document (a dict or its JSON text) sent to this
  instance and return the 32-byte space key. Control first checks the document's signature against `creator`'s directory entry (the
  key pinned at the gateway the first time; a changed key with no rotation is refused), that its `granter` is `creator`, and that it names this
  instance as `member`; only then does the gateway open it with the seal key (current, or one rotated out in the last 8 days). Errors:
  `bad_signature`, `bad_args`, `not_for_us`, `key_changed`, `unknown_member`, `bad_key` (it was sealed to some other key), `not_joined`.
  The key is a secret: keep it out of what the agent and the owner's UI can see.
- `ext.hub.transfer_put(service, url, data)` and `ext.hub.transfer_get(service, url, max_bytes) -> bytes` (`atransfer_put`,
  `atransfer_get`): upload or download through a transfer URL a service gave you (`blob.upload` / `blob.download`). Only an `https` URL whose
  host is that **connected** service's own instance hostname (from its directory entry) is used; a redirect is an error, nothing of the
  instance's (no token, no cookie) is sent, and outside development the connection goes only to a public address. A download longer than
  `max_bytes` stops with `too_large`; an upload is at most 128 MiB. Other errors: `bad_url`, `not_connected`, `paused`, `transfer_failed`
  (the service answered something other than 2xx, or couldn't be reached). Development: `L0W_HUB_ALLOW_HTTP=1` also accepts `http`, and
  `L0W_HUB_CONNECT_TO` sends the request to the fake hub, as it does for the membership code.
- `HubCallError.data` (a dict, `{}` when there is none): the extra fields of a service's error object, so spaces' `conflict` gives
  `e.data["head"]` (`{seq, version}`) and `locked` gives `e.data["lock"]`. Like `detail` it is untrusted and redacted of secrets; one over 64 KB is dropped.
- The plain forms are for worker threads (`def` routes, hooks, jobs); in a coroutine they raise `ExtensionError`, so use the `a` forms.

### Events (`ext.on`)

| Event | Data |
|---|---|
| `run_started` | `run_id, kind, why, thread_id, provider` |
| `run_ended` | `run_id, status, result` |
| `wakeup_planned` | `wakeup_id, at, budget, tier, reason, by` |
| `message_posted` | `message_id, thread_id, sender, body` |
| `ask_decided` | `kind` (question, permission, task, suggestion, promote), `id, decision` |
| `file_shelved` | `shelf_item_id, project, volume, path` |
| `extension_card_resolved` | `card_id, kind, action, data` (the owner's), `card` (the card's own data); only your cards |

Handlers run in a worker pool, off the request path, after the change is committed. Keep them short;
exceptions are recorded, not raised. `message_posted` bodies come from the owner, the assistant or a chat:
treat them as data (rule 3).

---

## 5. Settings

```toml
[settings.water_every_days]
kind = "int"            # int | float | bool | str | choice
label = "Water every (days)"
help = "Shown under the field."
default = 3
min = 1                 # int/float
max = 60
# choices = ["a", "b"]  # choice
```

Settings → Extensions shows a form for them automatically, saving on change. Read them with
`ext.settings.get("water_every_days")`. For anything richer, add a settings panel element (section 9).

---

## 6. Inbox cards and thread types

Declare each card kind in the manifest and give it an element:

```toml
[[ui.inbox_cards]]
kind = "water"
element = "plantlog-water-card"
```

File one with `ext.inbox.card("water", "Water the fern", {"plant": "Fern"})`. It appears in the owner's Inbox
(and the counts and notifications). Your element renders it; the owner's choice calls
`context.resolve(action, data)`, and you hear it as `extension_card_resolved`. Use cards for everything the
owner must decide: the agent can ask you to file one, never resolve one.

Thread types work the same way: declare `[[ui.thread_types]] type, title, element`, create threads with
`ext.threads.create(..., type="...")`, and your element renders above that thread's messages.

---

## 7. Prompt fragments

```toml
[prompts]
system = "prompts/system.md"    # every session's system prompt
wakeup = "prompts/wakeup.md"    # the brief of every wakeup (not Reply-now runs)
reviewer = "prompts/reviewer.md"
chat = "prompts/chat.md"        # the owner's 1:1 chats
```

Each is capped at 6,000 characters and appears under "## Extensions → ### The <title> extension". Control
adds your `[[commands]]` and managed folders to the system fragment for you, so don't repeat them. Write for
the agent: what the extension is for, when to use its commands, what not to do.

---

## 8. Routines and connection presets

```toml
[[routines]]
key = "weekly"
name = "Plant check"
prompt = "…"                 # or prompt_file = "prompts/weekly.md"
schedule = { kind = "weekly", day = 6, time = "10:00" }   # day: Monday = 0; also {kind="daily", time="HH:MM"}
budget = 5                   # points
enabled = false              # the owner turns it on in Calendar → Routines
# tier = "sonnet/medium"     # default: the worker's tier
```

Routines appear in the owner's routine list, run on the worker's provider, and follow your extension on and
off (keeping the owner's on/off choice).

```toml
[[presets]]
id = "tracker"
title = "Issue tracker"
url = "https://mcp.example.com/"        # a remote MCP server (streamable HTTP)
auth_secret = "TRACKER_TOKEN"
token_url = "https://example.com/settings/tokens"
guidance = "Create a read-only token…"
read_only_tools = ["list_issues", "get_issue"]
```

Presets show up in Tools & Secrets as `<name>-<id>`; control brokers the calls, so the credential never
enters the sandbox.

---

## 9. UI: web components in slots

`[ui] module` is an ES module (no build step needed; plain JS is fine). The web app imports it once, then for
each slot creates your element, sets **`el.l0w`** and **`el.context`**, and attaches it.

```toml
[ui]
module = "ui/plantlog.js"
[[ui.pages]]          # path, title, element → /x/<name>/<path>, listed in More ▾
[[ui.sidebar]]        # title, element → a section in the conversation sidebar
[[ui.project_tabs]]   # id, title, element → a tab on every project
[[ui.run_widgets]]    # title, element → below a run's summary
[[ui.inbox_cards]]    # kind, element
[[ui.previewers]]     # extensions = [".plant"], title, element → file previews
[[ui.thread_types]]   # type, title, element
[ui.settings_panel]   # element → in Settings → Extensions
```

**`el.l0w`** (the host):

| | |
|---|---|
| `ext` | Your name. |
| `api(path, {method, body})` | `fetch` to `/api/ext/<name><path>` with the owner's credentials and headers; JSON in and out. Throws an `Error` (with `.status`) whose message is the server's `detail`. |
| `on(type, fn)` → `off()` | Your live events (`ext.emit`). Call `off` in `disconnectedCallback`. |
| `navigate(to)` | Go to an app route, e.g. `/t/12` or `/x/<name>/plants`. |
| `theme` | `'light'` or `'dark'`. |

**`el.context`** per slot: page `{ path }`; sidebar `{}`; project tab `{ project }` (the slug); run widget
`{ run_id }`; Inbox card `{ card, resolve(action, data) }`; previewer `{ volume, path, url }` (`url` serves the
file's bytes); thread type `{ thread_id }`; settings panel `{}`. The app may set `context` again on a live
element when it changes: define a setter if you care.

**Look and feel.** Use shadow DOM and the app's CSS variables, which inherit into it, so light and dark both
work: `--bg --fg --fg2 --muted --faint --card --line --line2 --soft --hover --accent --accentInk --accentSoft
--okInk --okSoft --warnInk --warnSoft --badInk --badSoft --infoInk --infoSoft --sans --serif --mono`. Keep to
the app's quiet style (14px text, serif headings, hairline borders, no shadows on inline content).

**Security.** Your UI runs in the app with the owner's session, so treat it like core code: escape everything
you insert as HTML (the example's `esc`), never render agent-written HTML, and load nothing from other origins.
Agent-made files must stay in the sandboxed files origin: a previewer gets text from `context.url`; don't
`innerHTML` it.

---

## 10. Testing

Control's tests show how: `control/tests/test_extensions.py` copies the example into a temporary
`extensions/` folder and drives it through the real app with FastAPI's `TestClient`. For your extension:

1. Unit-test your own logic as plain Python.
2. Run a local stack with your extension and the fake runtime (no login, no Docker):
   ```
   cd control && uv run python scripts/dev_stack.py --port 8000 --extensions ../extensions
   cd web && pnpm dev            # http://localhost:5173
   ```
   The fake runtime (`agent/runner/fake_claude.py`) plays scripted agent sessions; call your agent routes
   directly to test them: `curl -X POST localhost:8000/api/agent/ext/<name>/<cmd> -H 'X-Agent-Token: …' -d '{…}'`
   (the dev stack prints the token's location; tests use `L0W_AGENT_API_ANY_ADDRESS=1`).
3. Check: it enables cleanly, its errors show on the Extensions page, it survives disable → enable and a
   restart with a bumped `version` (its store and settings persist), and the agent's system prompt shows your
   fragment (`ext.runs` … or open a run's brief in the UI).

---

## 11. Sharing and proposing upstream

- An extension is a git repo. Others install it by listing it in `extensions.toml` next to `compose.yaml`:
  ```toml
  [extension.plantlog]
  git = "https://github.com/someone/plantlog.git"
  commit = "0123abcd…"     # pinned: an update never changes code you haven't reviewed
  ```
  and running `./l0w extensions sync`, then enabling it. Installing is always the owner's action, with the
  source shown in Settings.
- To propose an extension (or an SDK addition) upstream into core: open a PR or issue on the main repo with
  what it does, why it belongs in core, and which invariants it touches. SDK additions need a test in
  `control/tests/test_extensions.py` and a line here.

## 12. Updates

Your folder lives outside the images, so `./l0w update` doesn't touch it. After an update control reloads
enabled extensions; one built for an older SDK minor keeps working, and a new major disables it with a
message until you update it. Your store, settings, cards and routines are kept across updates, disabling and
removal of the folder (re-adding it brings them back).
