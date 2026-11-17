# Extending l0wagent

An **extension** adds pages, Inbox cards, agent commands, prompt text, settings, routines and background
work to an l0wagent instance without touching core files. The owner writes it on the host (by hand or with
a coding agent), drops it into `extensions/<name>/` next to `compose.yaml`, and turns it on in
**Settings → Extensions**. This file is the whole guide: if you are a coding agent, you should be able to
build a working extension from it alone. A complete example is `extensions/examples/plantlog/`.

SDK version: **1.0**. Control refuses an extension whose `sdk` has a different major version.

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
   (`/work/_inbox/_ext/<name>/<folder>/`) and nothing else. They never follow symlinks.
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
| `folders` | Managed folder names, e.g. `["log"]` → `/work/_inbox/_ext/<name>/log/`. |
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
| `ext.on(event, handler)` | Event hooks (below). |
| `ext.every(seconds, job)` | A background job every ≥ 30 s, in a worker thread, while enabled. |
| `ext.notify(title, body="", url="/")` | A notification to the owner (push/ntfy, as they've set up). |
| `ext.emit(type, data)` | A live event to your UI (`this.l0w.on(type, fn)`). |
| `ext.hub.available()`, `ext.hub.call(service, method, payload)` | Hub services through the gateway. No hub exists yet: `available()` is False and `call` raises `HubUnavailable`. |
| `ext.log(text)`, `ext.file(rel)` | Log a line; read a text file from your folder. |

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
