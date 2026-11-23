#!/usr/bin/env python3
"""l0w: install, update and talk to your l0wagent server, from its host (or any machine).

Install and update (run in the folder with compose.yaml):
  l0w install [--channel stable|edge] [--port 8000] [--yes]
                         check Docker, pick a sandbox subnet, write .env (fresh tokens) and
                         compose.override.yaml, pull the images, start, print the address
  l0w update [--timeout 120] [--force] [--version X.Y.Z] [--no-drain]
                         pull, wait until the assistant is idle (no run, no don't-interrupt
                         jobs; no new runs start meanwhile), recreate, check it's healthy
                         (--no-drain: skip the wait; the web UI's updater uses it after control drained)
  l0w ops enable-updates|disable-updates
                         let the web UI update this install (starts ops-docker, the one container with
                         the Docker socket) or take that away again; asked once at install
  l0w up | down | restart | ps | logs [SERVICE]
  l0w claude-login       log the assistant's computer in to your Claude subscription (interactive)
  l0w migrate --from OLD_DIR [--old-project assistant] [--stop-old] [--force] [--no-claude-login]
                         move the old assistant into this instance (old volumes are never deleted)
  l0w backup [-o FILE]   an encrypted backup of everything (database, keys, the assistant's storage,
                         .env, extensions) in one file; asks for a passphrase. Keep the passphrase.
  l0w restore FILE [--yes]   put a backup back (stops the stack, replaces what's there, starts it)
  l0w version

Talk to it (log in once; the device token is kept in ~/.config/l0w/config.json, mode 600):
  l0w login [URL]        username, password (and 2FA code) → a revocable device token
  l0w logout
  l0w status             the assistant now, budget, what's waiting
  l0w inbox [--history]  everything waiting on you (questions, permissions, tasks, …)
  l0w answer ID VALUE… [--note …]          a question: yes/no, an option, several options, or text
  l0w approve ID [--scope once|project|always] [--note …]   ·   l0w deny ID [--note …]
  l0w done ID [--note …]  ·  l0w decline ID [--note …]      tasks for you
  l0w promote ID approve|deny               a reply asking to become a full run
  l0w threads [--project P]  ·  l0w read THREAD [-n 20]
  l0w send THREAD "text" [--now]           (- reads stdin); --now presses Reply now
  l0w send --new "Title" [--project P] "text"
  l0w runs [-n 15]  ·  l0w run ID [--detailed]  ·  l0w wake [--budget N] [--tier T]  ·  l0w stop RUN
  l0w jobs  ·  l0w budget  ·  l0w level dormant|frugal|steady|busy|max  ·  l0w boost LEVEL [--hours 24 | --until-reset]
  l0w calendar           this week's planned wakeups
  l0w secrets  ·  l0w secret set NAME [VALUE|@file|-] [--desc …]  ·  l0w secret rm NAME   (write-only)
  l0w selftest  ·  l0w update-check
  l0w extensions list|enable NAME|disable NAME|rescan|sync   (sync: git sources from extensions.toml and extensions/.ops.toml)

L0W_URL and L0W_TOKEN override the saved login (for scripts and coding agents).
"""

from __future__ import annotations

import argparse
import getpass
import ipaddress
import json
import os
import platform
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "l0w" / "config.json"
SUBNETS = [f"172.31.{n}.0/24" for n in range(240, 255)] + [f"10.231.{n}.0/24" for n in range(0, 16)]
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def die(msg: str, code: int = 1):
    print(msg, file=sys.stderr)
    sys.exit(code)


# ── the API ────────────────────────────────────────────────────────────────
def config() -> dict:
    try:
        return json.loads(CONFIG.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_config(c: dict) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(c, indent=2))
    CONFIG.chmod(0o600)


def server() -> tuple[str, str | None]:
    c = config()
    url = os.environ.get("L0W_URL") or c.get("url") or "http://localhost:8000"
    return url.rstrip("/"), os.environ.get("L0W_TOKEN") or c.get("token")


def call(method: str, path: str, body=None, auth: bool = True, url: str | None = None, timeout: float = 30):
    base, token = server()
    base = url or base
    headers = {"Content-Type": "application/json", "X-L0W-Client": "l0w-cli"}
    if auth:
        if not token:
            die("Not logged in: run `l0w login`.")
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{base}/api{path}", data=json.dumps(body).encode() if body is not None else None, method=method, headers=headers)
    try:
        with _opener.open(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read())
        except json.JSONDecodeError:
            err = {}
        if e.code == 401 and auth:
            die("The server refused the saved token (revoked or expired): run `l0w login`.")
        die(f"{e.code}: {err.get('detail') or err.get('error') or e.reason}")
    except urllib.error.URLError as e:
        die(f"Can't reach {base}: {e.reason}")


def key() -> str:
    return str(uuid.uuid4())


def text_arg(parts: list[str]) -> str:
    joined = " ".join(parts)
    return sys.stdin.read() if joined == "-" else joined


def when(iso: str | None) -> str:
    if not iso:
        return "–"
    from datetime import datetime
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().strftime("%a %d %b %H:%M")


def period_word(payload: dict | None) -> str:
    period = (payload or {}).get("period")
    return period if period in ("week", "month") else "period"


def worker_provider(a) -> str:
    if getattr(a, "provider", None):
        return a.provider
    providers = call("GET", "/budget").get("providers") or []
    return providers[0]["provider"] if providers else "claude-subscription"


# ── host helpers ───────────────────────────────────────────────────────────
def compose(*args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    cmd = ["docker", "compose", *args]
    return subprocess.run(cmd, check=check, text=True, capture_output=capture)


def service_images() -> dict[str, str]:
    """Each compose service's image (compose.yaml plus the override), from `docker compose config`."""
    r = compose("config", "--format", "json", capture=True, check=False)
    if r.returncode != 0:
        die(f"`docker compose config` failed: {r.stderr.strip()}")
    return {name: s["image"] for name, s in json.loads(r.stdout).get("services", {}).items() if s.get("image")}


def have_image(image: str) -> bool:
    return subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode == 0


def pull_images() -> None:
    """Pull each service's image. One the registry doesn't have is fine if it's already on this
    machine (a release that's missing an image shouldn't block the update); only a service with
    neither stops here, before anything is touched."""
    missing: list[str] = []
    for name, image in service_images().items():
        r = compose("pull", "--quiet", name, capture=True, check=False)
        if r.returncode == 0:
            print(f"  {name}: pulled")
            continue
        reason = (r.stderr.strip().splitlines() or ["pull failed"])[-1]
        if have_image(image):
            print(f"  {name}: couldn't pull {image} ({reason}); keeping the copy already on this machine")
        else:
            print(f"  {name}: couldn't pull {image} ({reason})")
            missing.append(image)
    if missing:
        die("These images aren't on the registry or on this machine:\n  " + "\n  ".join(missing)
            + "\nNothing was changed. If your checkout is newer than the release (see above), check out the release tag; "
            "or pin a version with `./l0w update --version X.Y.Z`.")


def git(*args: str) -> str | None:
    if not Path(".git").exists() or not shutil.which("git"):
        return None
    r = subprocess.run(["git", *args], capture_output=True, text=True, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    return r.stdout.strip() if r.returncode == 0 else None


def check_checkout(version: str) -> None:
    """Warn when this checkout's compose.yaml and l0w aren't from the release the images come from.
    A checkout of main ahead of the last tag can name images (or settings) that release doesn't have."""
    if version == "edge" or git("rev-parse", "HEAD") is None:
        return
    git("fetch", "--quiet", "--tags", "origin")
    if version == "stable":
        tags = (git("tag", "--list", "v*", "--sort=-v:refname") or "").split()
        if not tags:
            return
        want = tags[0]
    else:
        want = "v" + version
        if git("rev-parse", "--verify", "--quiet", want + "^{commit}") is None:
            return
    if git("rev-parse", "HEAD") == git("rev-parse", want + "^{commit}"):
        return
    here = git("describe", "--tags", "--always") or "this checkout"
    print(f"Note: this folder is at {here}, but the {version} images are {want}. Its compose.yaml and ./l0w may expect "
          f"images or settings {want} doesn't have. To match the release: git checkout {want}  (then run ./l0w update again).")


def need_docker() -> None:
    if not shutil.which("docker"):
        die("Docker isn't installed. See docs/install.md.")
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        die("Docker isn't running, or this user can't use it (try sudo, or add yourself to the docker group).")
    if subprocess.run(["docker", "compose", "version"], capture_output=True).returncode != 0:
        die("Docker Compose v2 is missing (`docker compose`). See docs/install.md.")
    if not Path("compose.yaml").is_file():
        die("Run this in the folder with compose.yaml (the l0wagent checkout or release).")
    ensure_install_files()


def env_file() -> dict[str, str]:
    out = {}
    if Path(".env").is_file():
        for line in Path(".env").read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    return out


def write_env(env: dict[str, str]) -> None:
    lines = ["# Written by `l0w install`. Keep it private: it holds the tokens control, the runner and egress use.", ""]
    lines += [f"{k}={v}" for k, v in env.items()]
    Path(".env").write_text("\n".join(lines) + "\n")
    Path(".env").chmod(0o600)


def used_subnets() -> list[ipaddress.IPv4Network]:
    ids = subprocess.run(["docker", "network", "ls", "-q"], capture_output=True, text=True).stdout.split()
    if not ids:
        return []
    data = json.loads(subprocess.run(["docker", "network", "inspect", *ids], capture_output=True, text=True).stdout or "[]")
    nets = []
    for n in data:
        for c in (n.get("IPAM") or {}).get("Config") or []:
            try:
                nets.append(ipaddress.ip_network(c.get("Subnet", ""), strict=False))
            except ValueError:
                pass
    return nets


def pick_subnet(name: str = "SANDBOX_SUBNET", taken: tuple[str, ...] = ()) -> str:
    used = used_subnets() + [ipaddress.ip_network(t) for t in taken if t]
    for cand in SUBNETS:
        net = ipaddress.ip_network(cand)
        if not any(net.overlaps(u) for u in used if u.version == 4):
            return cand
    die(f"No free subnet for the {name.split('_')[0].lower()} network; set {name} in .env by hand.")
    return ""


def add_chat_settings(env: dict[str, str]) -> bool:
    """The chat runtime's token and network, for .env files that predate it. True if anything was added."""
    before = dict(env)
    if not env.get("L0W_CHAT_RUNTIME_TOKEN"):
        env["L0W_CHAT_RUNTIME_TOKEN"] = secrets.token_urlsafe(32)
    if not env.get("CHAT_SUBNET"):
        env["CHAT_SUBNET"] = pick_subnet("CHAT_SUBNET", (env.get("SANDBOX_SUBNET", ""),))
    return env != before


def add_hub_settings(env: dict[str, str]) -> bool:
    """The hub gateway's two tokens and the hubnet subnet, for .env files that predate the hub. True if anything was added."""
    before = dict(env)
    for k in ("L0W_GATEWAY_TOKEN", "L0W_GATEWAY_TUNNEL_TOKEN"):
        if not env.get(k):
            env[k] = secrets.token_urlsafe(32)
    if not env.get("TUNNEL_SUBNET"):
        env["TUNNEL_SUBNET"] = pick_subnet("TUNNEL_SUBNET", (env.get("SANDBOX_SUBNET", ""), env.get("CHAT_SUBNET", "")))
    return env != before


def add_ops_settings(env: dict[str, str]) -> bool:
    """The ops containers' two tokens and the host user they write extensions as, for .env files that predate them. True if anything was added."""
    before = dict(env)
    for k in ("L0W_OPS_TOKEN", "L0W_OPS_DOCKER_TOKEN"):
        if not env.get(k):
            env[k] = secrets.token_urlsafe(32)
    if "L0W_UID" not in env:  # the ops container writes extensions/ as whoever owns this folder
        st = Path.cwd().stat()
        env["L0W_UID"], env["L0W_GID"] = str(st.st_uid), str(st.st_gid)
    return env != before


def profiles(env: dict[str, str]) -> list[str]:
    return [p.strip() for p in env.get("COMPOSE_PROFILES", "").split(",") if p.strip()]


def set_updates(env: dict[str, str], on: bool) -> None:
    """Add or drop the ops-docker compose profile in .env's COMPOSE_PROFILES (other profiles are kept)."""
    rest = [p for p in profiles(env) if p != OPS_DOCKER]
    new = rest + ([OPS_DOCKER] if on else [])
    if new:
        env["COMPOSE_PROFILES"] = ",".join(new)
    else:
        env.pop("COMPOSE_PROFILES", None)


OPS_DOCKER = "ops-docker"
UPDATES_ASK = "Let the web UI update this install? It gives a small container access to Docker (root-equivalent on this machine; it can only pull this project's images and start the updater)"
EXTENSIONS_TOML_HEADER = ("# The extensions to fetch from git: `./l0w extensions sync` clones each at its pinned commit.\n"
                          "# [extension.NAME]\n# git = \"https://github.com/you/your-extension.git\"\n# commit = \"<full commit id>\"\n"
                          "# Extensions installed from the web UI are recorded in extensions/.ops.toml instead.\n")


def ensure_install_files() -> None:
    """The folder and file the ops container mounts. Docker makes a folder of any bind-mounted path that doesn't exist, and
    ./extensions.toml is mounted as a single file, so both must exist before `compose up`."""
    made = []
    if not Path("extensions").exists():
        Path("extensions").mkdir()
        made.append(Path("extensions"))
    toml = Path("extensions.toml")
    if toml.is_dir():
        try:
            toml.rmdir()  # Docker made it (an empty folder); a real folder with files in it is left for the owner to sort out
        except OSError:
            die("extensions.toml is a folder (Docker makes one when the file is missing). Move its contents out, remove it and run this again.")
    if not toml.exists():
        toml.write_text(EXTENSIONS_TOML_HEADER)
        made.append(toml)
    if made and hasattr(os, "geteuid") and os.geteuid() == 0:  # the web UI's updater runs as root: leave these to the folder's owner
        st = Path.cwd().stat()
        for p in made:
            os.chown(p, st.st_uid, st.st_gid)


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))  # no packet is sent; this just picks the outbound interface
        return s.getsockname()[0]
    except OSError:
        return "localhost"
    finally:
        s.close()


def wait_healthy(port: str, timeout: float = 120) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.environ.get("L0W_IN_UPDATER"):
            # Run by the web UI's updater container: the host's ports aren't this container's. Ask control from inside its own container.
            r = compose("exec", "-T", "control", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/meta', timeout=3)", capture=True, check=False)
            if r.returncode == 0:
                return True
            time.sleep(2)
            continue
        try:
            with _opener.open(f"http://127.0.0.1:{port}/api/meta", timeout=3) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(2)
    return False


def hostctl(*args: str) -> dict:
    r = compose("exec", "-T", "control", "python", "-m", "l0w_control.hostctl", *args, capture=True, check=False)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip())
    return json.loads(r.stdout)


def ask(prompt: str, default: str, yes: bool) -> str:
    if yes:
        return default
    got = input(f"{prompt} [{default}]: ").strip()
    return got or default


# ── commands: host ─────────────────────────────────────────────────────────
def cmd_install(a) -> None:
    need_docker()
    env = env_file()
    fresh = not env.get("L0W_AGENT_TOKEN")
    if not fresh:
        print("Found .env: keeping its tokens and subnet.")
    else:
        env.update({
            "L0W_AGENT_TOKEN": secrets.token_urlsafe(32), "L0W_RUNNER_TOKEN": secrets.token_urlsafe(32), "L0W_EGRESS_TOKEN": secrets.token_urlsafe(32),
            "SANDBOX_SUBNET": pick_subnet(),
        })
    add_chat_settings(env)
    add_hub_settings(env)
    add_ops_settings(env)
    if fresh and OPS_DOCKER not in profiles(env):
        if a.web_updates or ask(UPDATES_ASK + "? (y/n)", "n", a.yes).lower().startswith("y"):
            set_updates(env, True)
    if "L0W_SETUP_CODE" not in env:
        env["L0W_SETUP_CODE"] = secrets.token_urlsafe(12)  # one-time: the first owner setup asks for it
    env.setdefault("L0W_CHANNEL", a.channel)
    env.setdefault("L0W_VERSION", a.channel)
    env.setdefault("L0W_PORT", str(a.port))
    env.setdefault("L0W_INSTANCE_NAME", ask("A name for this assistant", platform.node().split(".")[0] or "l0wagent", a.yes))
    env.setdefault("TZ", os.environ.get("TZ") or _host_tz())
    write_env(env)
    print(f"Wrote .env (sandbox subnet {env['SANDBOX_SUBNET']})." + (" The web UI can update this install." if OPS_DOCKER in profiles(env) else " Updates stay on this machine (./l0w update); ./l0w ops enable-updates changes that."))

    if not Path("compose.override.yaml").exists():
        gpu = shutil.which("nvidia-smi") is not None and "nvidia" in subprocess.run(["docker", "info", "--format", "{{json .Runtimes}}"], capture_output=True, text=True).stdout
        mem = ask("Memory for the assistant's computer (e.g. 8g)", "8g", a.yes)
        use_gpu = gpu and ask("Give it the NVIDIA GPU? (y/n)", "y", a.yes).lower().startswith("y")
        out = ["# Written by `l0w install`; see compose.override.example.yaml. ./l0w update keeps it.", "services:", "  agent:", "    deploy:", "      resources:",
               f"        limits: {{ memory: {mem} }}"]
        if use_gpu:
            out += ["        reservations:", "          devices: [{ driver: nvidia, count: all, capabilities: [gpu] }]"]
        Path("compose.override.yaml").write_text("\n".join(out) + "\n")
        print("Wrote compose.override.yaml" + (" (with the GPU)." if use_gpu else "."))

    check_checkout(env["L0W_VERSION"])
    print("Pulling the images…")
    pull_images()
    compose("up", "-d")
    port = env["L0W_PORT"]
    if not wait_healthy(port):
        die("Control didn't come up within 2 minutes: `./l0w logs control`.")
    ip = lan_ip()
    print(f"\nl0wagent is running: open http://{ip}:{port} (or http://localhost:{port} on this machine).")
    if env.get("L0W_SETUP_CODE"):
        print(f"Setup code (the wizard asks for it once): {env['L0W_SETUP_CODE']}")
    print("The setup wizard creates your account, then logs the assistant in to Claude and runs the isolation self-test.")
    print("Phones need HTTPS for notifications and installing the app: see docs/install.md (Tailscale HTTPS).")


def _host_tz() -> str:
    try:
        link = os.readlink("/etc/localtime")
        return link.split("zoneinfo/", 1)[1]
    except (OSError, IndexError):
        return "UTC"


def cmd_update(a) -> None:
    if a.cancel:
        return cmd_drain(a)
    need_docker()
    env = env_file()
    port = env.get("L0W_PORT", "8000")
    if add_chat_settings(env):
        write_env(env)
        print("Added the chat runtime's token and network to .env.")
    if add_hub_settings(env):
        write_env(env)
        print("Added the hub gateway's tokens and network to .env.")
    if add_ops_settings(env):
        write_env(env)
        print("Added the ops container's tokens to .env. (The web UI can't update this install until you run ./l0w ops enable-updates.)")
    if a.version:
        env["L0W_VERSION"] = a.version.lstrip("v")
        write_env(env)
        print(f"Pinned to {env['L0W_VERSION']} in .env.")
    try:
        before = compose("exec", "-T", "control", "python", "-m", "l0w_control.hostctl", "version", capture=True, check=False).stdout.strip()
    except OSError:
        before = "?"
    check_checkout(env.get("L0W_VERSION", "stable"))
    print("Pulling the new images (the assistant keeps working meanwhile)…")
    pull_images()
    if not getattr(a, "no_drain", False):
        print("Waiting until the assistant is idle (no new runs start meanwhile)…")
    # The drain is short and renewed while we work, and cleared on any way out (Ctrl-C, kill, an error),
    # so an update that dies can't leave the assistant refusing runs.
    restore = _signals_raise()
    try:
        _drain_and_restart(a, port)
    finally:
        try:
            hostctl("drain", "off")
        except (RuntimeError, OSError, ValueError):
            print("Couldn't clear the update hold on new runs (control is down?). It clears itself within 10 minutes, or run `./l0w drain off`.", file=sys.stderr)
        restore()
    after = compose("exec", "-T", "control", "python", "-m", "l0w_control.hostctl", "version", capture=True, check=False).stdout.strip()
    print(f"Updated: {before or '?'} → {after or '?'}.")
    subprocess.run(["docker", "image", "prune", "-f", "--filter", "label=org.opencontainers.image.source=https://github.com/CyrusBrown/l0wagent"], capture_output=True)


DRAIN_MINUTES = 15  # renewed every RENEW_EVERY seconds while updating; control drops one not renewed for 10 min
RENEW_EVERY = 120


def _signals_raise():
    """Make SIGTERM/SIGHUP raise (like Ctrl-C does) so `finally` blocks run; returns a function that undoes it."""
    def handler(signum, frame):
        raise KeyboardInterrupt
    old = {}
    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                old[sig] = signal.signal(sig, handler)
            except ValueError:  # not the main thread
                pass
    return lambda: [signal.signal(s, h) for s, h in old.items()]


def _renew_drain(last: float) -> float:
    if time.time() - last < RENEW_EVERY:
        return last
    try:
        hostctl("drain", "renew", str(DRAIN_MINUTES))
    except (RuntimeError, OSError, ValueError):
        pass  # control may be restarting; the 10-minute grace covers it
    return time.time()


def _drain_and_restart(a, port: str) -> None:
    if getattr(a, "no_drain", False):
        # The web UI's updater: control drained and waited for idle before asking, so go straight to the restart.
        compose("up", "-d", "--remove-orphans")
        if not wait_healthy(port):
            die("Control didn't come back within 2 minutes: `./l0w logs control`. The previous images are still on disk.")
        return
    try:
        hostctl("drain", "on", str(DRAIN_MINUTES), "l0w update")
    except (RuntimeError, OSError, ValueError) as e:
        print(f"Couldn't ask control to wait ({e}); updating anyway.")
    renewed = time.time()
    deadline = time.time() + a.timeout * 60
    shown = None
    while not a.force:
        try:
            st = hostctl("idle")
        except (RuntimeError, OSError, ValueError):
            break
        if st["idle"]:
            break
        if st["waiting_for"] != shown:
            shown = st["waiting_for"]
            print("  waiting for: " + "; ".join(shown))
        if time.time() > deadline:
            die(f"Still busy after {a.timeout} min. Run `./l0w update --force` to update anyway (a run in progress resumes after the restart).")
        time.sleep(10)
        renewed = _renew_drain(renewed)
    compose("up", "-d", "--remove-orphans")  # also removes containers of services since dropped (agent-web)
    if not wait_healthy(port):
        die("Control didn't come back within 2 minutes: `./l0w logs control`. The previous images are still on disk.")


def cmd_drain(a) -> None:
    """`./l0w drain off`: clear a stuck update hold (also `./l0w update --cancel`)."""
    need_docker()
    try:
        hostctl("drain", "off")
    except (RuntimeError, OSError, ValueError) as e:
        die(f"Couldn't clear it: {e}")
    print("New runs may start again.")


def cmd_2fa_reset(a) -> None:
    """`./l0w 2fa-reset`: turn off the owner's two-factor sign-in and delete the recovery codes."""
    need_docker()
    try:
        r = hostctl("2fa-reset")
    except (RuntimeError, OSError, ValueError) as e:
        die(f"Couldn't reset 2FA: {e}")
    if r.get("totp_was_enabled"):
        print(f"Two-factor sign-in is off and {r.get('recovery_codes_deleted', 0)} recovery code(s) were deleted. Sign in with your password, then set 2FA up again in Settings → Account.")
    else:
        print(f"2FA wasn't on. Deleted {r.get('recovery_codes_deleted', 0)} leftover recovery code(s).")


def cmd_compose(a) -> None:
    need_docker()
    svc = getattr(a, "service", None)
    args = {"up": ["up", "-d"], "down": ["down"], "restart": ["restart"], "ps": ["ps"], "logs": ["logs", "--tail", "200", "-f", *([svc] if svc else [])]}[a.cmd]
    compose(*args, check=False)


BACKUP_PLACEHOLDERS = {
    "L0W_AGENT_TOKEN": "x", "L0W_RUNNER_TOKEN": "x", "L0W_EGRESS_TOKEN": "x", "L0W_CHAT_RUNTIME_TOKEN": "x",
    "L0W_GATEWAY_TOKEN": "x", "L0W_GATEWAY_TUNNEL_TOKEN": "x", "L0W_OPS_TOKEN": "x", "L0W_OPS_DOCKER_TOKEN": "x",
    "SANDBOX_SUBNET": "172.31.250.0/24", "CHAT_SUBNET": "172.31.251.0/24", "TUNNEL_SUBNET": "172.31.252.0/24",
}


def backup_env(passphrase: str) -> dict[str, str]:
    """The environment for the backup container's compose call. compose.yaml interpolates required
    variables for the whole file, so on a machine with no .env yet give it placeholders."""
    env = dict(os.environ)
    env["L0W_BACKUP_PASSPHRASE"] = passphrase
    if not Path(".env").is_file():
        for k, v in BACKUP_PLACEHOLDERS.items():
            env.setdefault(k, v)
    return env


def backup_passphrase(confirm: bool) -> str:
    pw = os.environ.get("L0W_BACKUP_PASSPHRASE")
    if pw:
        return pw
    pw = getpass.getpass("Backup passphrase: ")
    if not pw:
        die("The passphrase can't be empty.")
    if confirm and getpass.getpass("Again: ") != pw:
        die("The passphrases differ.")
    return pw


BACKUP_RUN = ["docker", "compose", "run", "--rm", "-T", "-e", "L0W_BACKUP_PASSPHRASE", "backup"]


def cmd_backup(a) -> None:
    """`./l0w backup [-o FILE]`: one encrypted file with everything needed to rebuild this instance."""
    need_docker()
    out = Path(a.output or f"l0w-backup-{time.strftime('%Y-%m-%d')}.l0wbak")
    if out.exists():
        die(f"{out} already exists. Pick another name with -o.")
    pw = backup_passphrase(confirm=True)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        rc = subprocess.run([*BACKUP_RUN, "create"], stdout=f, env=backup_env(pw)).returncode
    if rc != 0:
        out.unlink(missing_ok=True)
        die("The backup failed (nothing was saved).", rc)
    out.chmod(0o600)
    print(f"Wrote {out}. Keep the passphrase: without it the file can't be opened.")


def cmd_restore(a) -> None:
    """`./l0w restore FILE [--yes]`: replace this instance's data with a backup, then start it."""
    need_docker()
    src = Path(a.file)
    if not src.is_file():
        die(f"{src} isn't a file.")
    if not a.yes and input(f"This replaces the data on this machine with {src.name} (the stack is stopped meanwhile). Continue? [y/N] ").strip().lower() not in ("y", "yes"):
        die("Cancelled.")
    pw = backup_passphrase(confirm=False)
    compose("stop", check=False)
    with open(src, "rb") as f:
        rc = subprocess.run([*BACKUP_RUN, "restore"], stdin=f, env=backup_env(pw)).returncode
    if rc != 0:
        die("The restore failed. If the passphrase was wrong or the file damaged, nothing was changed; the stack is still stopped (./l0w up starts it).", rc)
    ensure_install_files()
    compose("up", "-d", check=False)
    print("Restored and started.")


def cmd_claude_login(a) -> None:
    need_docker()
    print("Opening the Claude login on the assistant's computer. Follow the link, then paste the code back here.")
    subprocess.run(["docker", "compose", "exec", "-u", "agent", "agent", "claude", "auth", "login"])


def project_name(env: dict[str, str]) -> str:
    r = compose("config", "--format", "json", capture=True, check=False)
    if r.returncode == 0:
        try:
            return json.loads(r.stdout)["name"]
        except (ValueError, KeyError):
            pass
    return os.environ.get("COMPOSE_PROJECT_NAME") or env.get("COMPOSE_PROJECT_NAME") or Path.cwd().name.lower()


def cmd_migrate(a) -> None:
    need_docker()
    env = env_file()
    if not env.get("L0W_AGENT_TOKEN"):
        die("This instance isn't installed yet (no .env with L0W_AGENT_TOKEN). Run `./l0w install` first.")
    if not wait_healthy(env.get("L0W_PORT", "8000"), timeout=5):
        die("Control isn't running or healthy. Start it with `./l0w up`, then try again.")
    new = project_name(env)
    old_dir = Path(a.old_dir).expanduser().resolve()
    if not any((old_dir / n).is_file() for n in ("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml")):
        die(f"No compose file in {old_dir}. Point --from at the old assistant's folder.")
    old = a.old_project
    if old == new:
        die("The old and new compose projects have the same name; pass --old-project.")
    here = Path.cwd()
    running = subprocess.run(["docker", "compose", "-p", old, "ps", "-q"], capture_output=True, text=True, cwd=old_dir).stdout.split()
    if running:
        if not a.stop_old:
            die("The old assistant is still running. Let its runs finish, then re-run with --stop-old (stops it; keeps its volumes).")
        print("Stopping the old assistant (its volumes are kept)…")
        subprocess.run(["docker", "compose", "-p", old, "down"], cwd=old_dir, check=False)
        still = subprocess.run(["docker", "compose", "-p", old, "ps", "-q"], capture_output=True, text=True, cwd=old_dir).stdout.split()
        if still:
            die("The old containers are still running; stop them and try again.")
    for v in ("control-data", "egress-data", "agent-workspace", "agent-home"):
        if subprocess.run(["docker", "volume", "inspect", f"{old}_{v}"], capture_output=True).returncode != 0:
            die(f"Volume {old}_{v} not found. If the old project has another name, use --old-project.")
    script = Path(__file__).resolve().parent / "migrate_export.py"
    image = "python:3.12-slim"
    base = ["docker", "run", "--rm", "-v", f"{script}:/migrate_export.py:ro"]
    old_mounts = ["-v", f"{old}_control-data:/old/control:ro", "-v", f"{old}_egress-data:/old/egress:ro",
                  "-v", f"{old}_agent-workspace:/old/workspace:ro", "-v", f"{old}_agent-home:/old/home:ro"]
    print("Copying the old workspace, home and records into /work/_legacy (this can take a while)…")
    cmd = base + old_mounts + ["-v", f"{new}_agent-work:/work", image, "python", "/migrate_export.py", "export"]
    if a.force:
        cmd.append("--force")
    if subprocess.run(cmd).returncode != 0:
        die("The export failed (see above). Nothing was imported.")
    if not a.no_claude_login:
        cmd = base + ["-v", f"{old}_agent-home:/old/home:ro", "-v", f"{new}_agent-home:/new-home",
                                       image, "python", "/migrate_export.py", "claude-login"]
        if a.force:
            cmd.append("--force")
        if subprocess.run(cmd).returncode != 0:
            print("Couldn't copy the Claude login; run `./l0w claude-login` later.")
    print("Moving the secrets straight into the vault (never written to disk)…")
    src = subprocess.Popen(base + ["-v", f"{old}_control-data:/old/control:ro", image, "python", "/migrate_export.py", "secrets"],
                           stdout=subprocess.PIPE)
    dst = subprocess.run(["docker", "compose", "exec", "-T", "control", "python", "-m", "l0w_control.hostctl", "import-secrets"],
                         stdin=src.stdout, capture_output=True, text=True, cwd=here)
    src.stdout.close()
    if src.wait() != 0 or dst.returncode != 0:
        print("Importing the secrets failed: " + (dst.stderr.strip() or "see above") + "\nRe-enter them in Settings.")
    else:
        print(dst.stdout.strip())
    r = compose("exec", "-T", "control", "python", "-m", "l0w_control.hostctl", "import-legacy", capture=True, check=False)
    if r.returncode != 0:
        die("Importing the old threads failed: " + (r.stderr.strip() or r.stdout.strip()))
    print(r.stdout.strip())
    print("Restarting control so the new secrets reach the next session and redaction…")
    compose("restart", "control", check=False)
    if not wait_healthy(env.get("L0W_PORT", "8000")):
        die("Control didn't come back within 2 minutes: `./l0w logs control`.")
    print(f"""
Done. The old assistant's files are in /work/_legacy/ (read-only for the assistant).
The old stack is stopped, not deleted: its volumes ({old}_*) are untouched.
Next:
  1. Open the web UI: the old threads are there as archived threads named "… (old assistant)".
  2. The pinned thread "Set yourself back up after the move" holds the re-setup brief; the assistant's
     next run picks it up. Wake it from that thread when you're ready, with a generous budget.
  3. Once you're happy, remove the old stack yourself (`docker compose -p {old} down -v` in {old_dir}).""")


# ── commands: API ──────────────────────────────────────────────────────────
def cmd_login(a) -> None:
    url = (a.url or server()[0]).rstrip("/")
    user = input("Username: ").strip()
    pw = getpass.getpass("Password: ")
    label = f"l0w CLI on {platform.node()}"
    r = call("POST", "/auth/login", {"username": user, "password": pw, "want": "device_token", "device_label": label}, auth=False, url=url)
    if r["status"] == "totp_required":
        code = input("2FA code: ").strip()
        r = call("POST", "/auth/totp", {"challenge": r["challenge"], "code": code}, auth=False, url=url)
    if not r.get("device_token"):
        die("The server didn't issue a device token.")
    save_config({"url": url, "token": r["device_token"]})
    print(f"Logged in to {url} as {user}. Revoke this device any time in Settings → Account.")


def cmd_logout(a) -> None:
    if config().get("token"):
        call("POST", "/auth/logout", {})
    CONFIG.unlink(missing_ok=True)
    print("Logged out (the device token is revoked).")


def cmd_status(a) -> None:
    st = call("GET", "/status")
    if st["state"] == "working":
        print(f"Working (run #{st['run_id']}, {st['run_used'] or 0:.1f} of {st['run_budget'] or 0:g} pts): {st['now'] or '…'}")
    elif st.get("next_wakeup_at"):
        print(f"Asleep. Next wakeup {when(st['next_wakeup_at'])}.")
    else:
        print("Dormant: no wakeups planned.")
    b = st.get("budget")
    if b:
        print(f"Budget: {b['left']:.0f} of {b['allowance']:.0f} pts left this {period_word(b)} (you {b['used_by_owner']:.0f}, the assistant {b['used_by_assistant']:.0f}); "
              f"reply budget {st.get('reply_budget_left') or 0:.0f} pts.")
    print(f"Waiting on you: {st['inbox_waiting']}.")
    up = call("GET", "/system/update")
    if up.get("available"):
        print(f"Update available: {up['current']} → {up['latest']} (run ./l0w update on the server).")


def fmt_entry(e: dict) -> str:
    kind = e["kind"]
    obj = e.get(kind) or {}
    where = f"  [#{e['thread_id']} {e['thread_title']}]" if e.get("thread_id") else ""
    if kind == "question":
        opts = f"  options: {', '.join(obj.get('options') or [])}" if obj.get("options") else ""
        return f"question #{obj['id']} ({obj['kind']}): {obj['text']}{opts}  default: {obj.get('default')!r}{where}"
    if kind == "permission":
        acts = "".join(f"\n      if approved: {x['summary']}" for x in obj.get("actions") or [])
        return f"permission #{obj['id']}: {obj['text']}{where}{acts}"
    if kind == "task":
        return f"task #{obj['id']}: {obj['text']}{where}"
    if kind == "promote":
        return f"promote #{obj['id']}: {obj['reason']} (+{obj['budget']:g} pts){where}"
    if kind == "suggestion":
        return f"suggestion #{obj['id']}: {obj['text']}{where}"
    if kind == "security":
        return f"security #{obj['id']}: {obj['text']}"
    return f"{kind}: {obj}"


def cmd_inbox(a) -> None:
    rows = call("GET", "/inbox?view=" + ("history" if a.history else "waiting"))
    print("\n".join(fmt_entry(e) for e in rows) or "Nothing waiting on you.")


def cmd_answer(a) -> None:
    q = next((e["question"] for e in call("GET", "/inbox?view=waiting&kind=question") if e["question"]["id"] == a.id), None)
    if not q:
        die(f"No open question #{a.id}.")
    raw = " ".join(a.value)
    kind = q["kind"]
    value: object = raw
    if kind == "yes_no":
        value = raw.lower() in ("y", "yes", "true", "1")
    elif kind == "multi":
        value = [x.strip() for x in raw.split(",") if x.strip()]
    elif kind == "scale":
        value = int(raw)
    call("POST", f"/questions/{a.id}/answer", {"value": value, "note": a.note, "idempotency_key": key()})
    print(f"Answered question #{a.id}.")


def cmd_decide(a) -> None:
    if a.cmd == "approve":
        call("POST", f"/permissions/{a.id}/decide", {"decision": "allow", "scope": a.scope, "note": a.note})
        print(f"Approved permission #{a.id}.")
    else:
        call("POST", f"/permissions/{a.id}/decide", {"decision": "deny", "note": a.note})
        print(f"Denied permission #{a.id}.")


def cmd_task(a) -> None:
    call("POST", f"/tasks/{a.id}/resolve", {"outcome": "done" if a.cmd == "done" else "declined", "note": a.note, "idempotency_key": key()})
    print(f"Task #{a.id} {'done' if a.cmd == 'done' else 'declined'}.")


def cmd_promote(a) -> None:
    call("POST", f"/promote-requests/{a.id}/decide", {"decision": a.decision})
    print(f"Promote request #{a.id}: {a.decision}d.")


def cmd_threads(a) -> None:
    q = f"?project={urllib.parse.quote(a.project)}" if a.project else ""
    for t in call("GET", "/threads" + q):
        unread = f"  ({t['unread']} unread)" if t.get("unread") else ""
        print(f"#{t['id']:<5} {t['title']}  [{t.get('project') or 'General'}]{unread}")


def cmd_read(a) -> None:
    items = call("GET", f"/threads/{a.thread}/items?limit={a.n}")["items"]
    for it in items:
        if it["type"] == "message":
            m = it["message"]
            print(f"── {m['sender']} · {when(m['created_at'])}\n{m['body']}\n")
        else:
            obj = it.get(it["type"]) or {}
            print(f"── [{it['type']}] {obj.get('text') or obj.get('plan') or obj.get('reason') or ''}\n")


def cmd_send(a) -> None:
    body = text_arg(a.text)
    if a.new:
        t = call("POST", "/threads", {"title": a.new, "project": a.project, "first_message": body, "idempotency_key": key()})
        tid = t["id"]
        print(f"Started thread #{tid}.")
    else:
        if a.thread is None:
            die("Give a thread id, or --new \"Title\".")
        tid = a.thread
        call("POST", f"/threads/{tid}/messages", {"body": body, "upload_ids": [], "idempotency_key": key()})
        print(f"Sent to thread #{tid}.")
    if a.now:
        r = call("POST", f"/threads/{tid}/reply-now")
        print(f"Reply now: run #{r['id']} ({r['budget']:g} pts at most).")


def cmd_runs(a) -> None:
    for r in call("GET", f"/runs?limit={a.n}"):
        print(f"#{r['id']:<5} {r['kind']:<8} {r['status']:<8} {when(r['started_at'])}  {r['used']:.1f}/{r['budget'] or 0:g} pts  {r.get('about') or r['why']}")


def cmd_run(a) -> None:
    d = call("GET", f"/runs/{a.id}")
    r = d["run"]
    print(f"Run #{r['id']} ({r['kind']}, {r['why']}) · {r['status']} · {r['used']:.1f} of {r['budget'] or 0:g} pts · {r['tier']}")
    if r.get("about"):
        print(r["about"])
    for i in call("GET", f"/runs/{a.id}/timeline?view={'detailed' if a.detailed else 'simple'}"):
        print(f"  {when(i['at'])[-5:]}  {i['kind']:<9} {i['text'][:200]}")


def cmd_wake(a) -> None:
    r = call("POST", "/runs", {"budget": a.budget, "tier": a.tier, "reason": "manual"})
    print(f"Started run #{r['id']} ({r['budget']:g} pts, {r['tier']}).")


def cmd_stop(a) -> None:
    call("POST", f"/runs/{a.id}/stop")
    print(f"Stopping run #{a.id}.")


def cmd_jobs(a) -> None:
    for j in call("GET", "/jobs"):
        print(f"#{j['id']} {j['label']} [{j['status']}]" + (f" · {j['progress']}" if j.get("progress") else ""))
        tail = (j.get("log_tail") or "").strip().splitlines()[-3:]
        for line in tail:
            print(f"    {line}")


def cmd_budget(a) -> None:
    o = call("GET", "/budget")
    for p in o["providers"]:
        boost = f" (Boost {p['boost']['level']} until {when(p['boost']['until'])})" if p.get("boost") else ""
        print(f"{p['label']}: level {p['level']}{boost}; {p['left']:.0f} of {p['allowance']:.0f} pts left; {p['native']}.")
        print(f"  you {p['used_by_owner']:.0f} · chats {p['used_by_chat']:.0f} · the assistant {p['used_by_assistant']:.0f} pts;"
              f" reply budget {p['reply_left']:.0f}/{p['reply_allowance']:.0f}")
        print(f"  {p['pace']}")


def cmd_level(a) -> None:
    p = call("PATCH", f"/budget/{urllib.parse.quote(worker_provider(a))}", {"level": a.level})
    print(f"Level set to {p['level']} ({p['allowance']:.0f} pts a {period_word(p)}).")


def cmd_boost(a) -> None:
    body = {"level": a.level, "until_reset": a.until_reset} | ({} if a.until_reset else {"hours": a.hours})
    p = call("POST", f"/budget/{urllib.parse.quote(worker_provider(a))}/boost", body)
    print(f"Boost: {p['effective_level']} until {when(p['boost']['until'])}.")


def cmd_calendar(a) -> None:
    c = call("GET", "/calendar")
    for w in c["wakeups"]:
        print(f"{when(w['at'])}  {w['budget']:>5g} pts  {w['kind']:<8} {w['status']:<8} {w['reason']}")
    if not c["wakeups"]:
        print("Nothing this week.")


def cmd_secrets(a) -> None:
    if a.cmd == "secrets":
        for s in call("GET", "/secrets"):
            print(f"{s['name']:<28} reads {s['reads']:<4} {s.get('description') or ''}")
        return
    if a.action == "set":
        v = a.value
        if v is None or v == "-":
            v = getpass.getpass(f"Value for {a.name}: ") if v is None and sys.stdin.isatty() else sys.stdin.read()
        elif v.startswith("@"):
            v = Path(v[1:]).read_text()
        call("PUT", f"/secrets/{a.name}", {"value": v.strip(), "description": a.desc})
        print(f"Saved {a.name.upper()} (write-only; redacted everywhere).")
    elif a.action == "rm":
        call("DELETE", f"/secrets/{a.name}")
        print(f"Deleted {a.name.upper()}.")


def cmd_selftest(a) -> None:
    r = call("POST", "/system/selftest", timeout=120)
    for c in r["checks"]:
        print(f"{c['result']:<5} {c['label']}: {c.get('detail') or ''}")
    print("OK" if r["ok"] else "Some checks failed: see docs/troubleshooting.md.")


def cmd_update_check(a) -> None:
    u = call("POST", "/system/update/check")
    if u["available"]:
        print(f"Update available: {u['current']} → {u['latest']}. Run ./l0w update on the server.\n\n{u.get('notes') or ''}")
    else:
        print(f"Up to date ({u['current']}, {u['channel']})." + (f" Check failed: {u['error']}" if u.get("error") else ""))


def cmd_ops(a) -> None:
    """`./l0w ops enable-updates|disable-updates`: whether the web UI may update this install (the ops-docker container)."""
    need_docker()
    env = env_file()
    if not env.get("L0W_AGENT_TOKEN"):
        die("This instance isn't installed yet. Run `./l0w install` first.")
    on = a.action == "enable-updates"
    if on == (OPS_DOCKER in profiles(env)):
        print("Updates from the web UI are already " + ("on." if on else "off."))
        return
    if on:
        add_ops_settings(env)
        if not a.yes and input(UPDATES_ASK + ". Turn it on? [y/N] ").strip().lower() not in ("y", "yes"):
            die("Cancelled.")
    set_updates(env, on)
    write_env(env)
    if not on:
        compose("--profile", OPS_DOCKER, "rm", "-sf", OPS_DOCKER, check=False)
    pull = compose("pull", "--quiet", "ops-docker", capture=True, check=False) if on else None
    if pull is not None and pull.returncode != 0:
        print(f"Couldn't pull the ops image ({(pull.stderr.strip().splitlines() or ['?'])[-1]}); using the copy on this machine if there is one.")
    compose("up", "-d", "--remove-orphans")  # ops is recreated too: it reads COMPOSE_PROFILES to know whether updates are on
    print("The web UI can now update this install." if on else "The web UI can no longer update this install; ./l0w update on this machine still does.")


# ── commands: extensions ───────────────────────────────────────────────────
EXT_NAME = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}


def print_extensions(rows: list[dict]) -> None:
    lines = []
    for e in rows:
        line = f"{e['name']} {e['version']}  {'on' if e['enabled'] else 'off'}  {e.get('title') or ''}"
        err = (e.get("last_error") or "").strip().splitlines()
        if err:
            line += f" ! {err[0]}"
        lines.append(line)
    print("\n".join(lines) or "No extensions found.")


def cmd_extensions(a) -> None:
    if a.action == "sync":
        return cmd_extensions_sync()
    if a.action == "list":
        return print_extensions(call("GET", "/extensions"))
    if a.action == "rescan":
        return print_extensions(call("POST", "/extensions/rescan"))
    if not a.name:
        die(f"Give an extension name: ./l0w extensions {a.action} NAME")
    on = a.action == "enable"
    call("PUT", f"/extensions/{urllib.parse.quote(a.name, safe='')}/enabled", {"enabled": on})
    print(f"{a.name} {'enabled' if on else 'disabled'}")


def read_ext_entries(path: Path) -> dict:
    try:
        entries = tomllib.loads(path.read_text()).get("extension") or {}
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as e:
        die(f"{path} isn't valid TOML: {e}")
    return entries if isinstance(entries, dict) else {}


def cmd_extensions_sync() -> None:
    """Clone or update each [extension.NAME] in extensions.toml (yours) and extensions/.ops.toml (written by the web UI)
    to its pinned commit under extensions/NAME. An entry in extensions.toml wins over the same name in .ops.toml."""
    root = Path.cwd()
    entries = {**read_ext_entries(root / "extensions" / ".ops.toml"), **read_ext_entries(root / "extensions.toml")}
    if not entries:
        print("No [extension.NAME] entries in extensions.toml (next to compose.yaml) or extensions/.ops.toml.")
        return
    failed = 0
    for name, spec in entries.items():
        if not EXT_NAME.match(name):
            print(f"{name}: the name must be 2–31 lowercase letters, digits or dashes, starting with a letter; skipped.", file=sys.stderr)
            failed += 1
            continue
        url = spec.get("git") if isinstance(spec, dict) else None
        commit = spec.get("commit") if isinstance(spec, dict) else None
        if not isinstance(url, str) or not url or url.startswith("-") or not isinstance(commit, str) or not commit or commit.startswith("-"):
            print(f"{name}: needs git = \"URL\" and commit = \"COMMIT\" in extensions.toml; skipped.", file=sys.stderr)
            failed += 1
            continue
        if spec.get("path"):
            print(f"{name}: has a path (a folder inside the repository); ./l0w extensions sync can't do that. Install it from the web UI (Settings → Extensions); skipped.", file=sys.stderr)
            failed += 1
            continue
        target = root / "extensions" / name
        try:
            made_by_ops = (target / ".l0w-source.json").is_file()
        except OSError:
            made_by_ops = False
        if made_by_ops:
            try:
                have = json.loads((target / ".l0w-source.json").read_text()).get("commit")
            except (OSError, ValueError, AttributeError):
                have = None
            if have == commit:
                print(f"{name}: {url} @ {commit[:12]} (installed from the web UI)")
            else:
                print(f"{name}: installed from the web UI at {str(have)[:12] or 'another commit'}; update it there, or remove it there and sync again; skipped.", file=sys.stderr)
                failed += 1
            continue
        try:
            if not target.exists():
                subprocess.run(["git", "clone", "--quiet", "--", url, str(target)], check=True, capture_output=True, text=True, env=GIT_ENV)
            else:
                origin = subprocess.run(["git", "-C", str(target), "remote", "get-url", "origin"], capture_output=True, text=True, env=GIT_ENV)
                if origin.returncode != 0 or origin.stdout.strip() != url:
                    print(f"{name}: {target} exists with a different origin ({origin.stdout.strip() or 'none'}); left it alone. Move it aside to sync.", file=sys.stderr)
                    failed += 1
                    continue
                subprocess.run(["git", "-C", str(target), "fetch", "--quiet", "origin"], check=True, capture_output=True, text=True, env=GIT_ENV)
            subprocess.run(["git", "-C", str(target), "checkout", "--quiet", "--detach", commit], check=True, capture_output=True, text=True, env=GIT_ENV)
        except subprocess.CalledProcessError as e:
            reason = (e.stderr or "").strip().splitlines()[-1:] or ["git failed"]
            print(f"{name}: {reason[0]}; skipped.", file=sys.stderr)
            failed += 1
            continue
        except OSError as e:
            print(f"{name}: couldn't run git ({e}); skipped.", file=sys.stderr)
            failed += 1
            continue
        print(f"{name}: {url} @ {commit[:12]}")
    try:
        print_extensions(call("POST", "/extensions/rescan"))
    except SystemExit:
        print("Control isn't reachable, so it reads the extensions folder when it starts (or run ./l0w extensions rescan once it is up).")
    if failed:
        sys.exit(1)


def cmd_version(a) -> None:
    base, token = server()
    print(f"l0w CLI; server {base}")
    if token:
        print(f"server version {call('GET', '/meta', auth=False)['server_version']}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="l0w", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("install")
    i.add_argument("--channel", choices=["stable", "edge"], default="stable")
    i.add_argument("--port", type=int, default=8000)
    i.add_argument("--yes", "-y", action="store_true", help="accept the defaults")
    i.add_argument("--web-updates", action="store_true", help="let the web UI update this install (starts ops-docker, which holds the Docker socket)")
    u = sub.add_parser("update")
    u.add_argument("--timeout", type=float, default=120, help="minutes to wait for idle")
    u.add_argument("--force", action="store_true", help="don't wait for idle")
    u.add_argument("--version", help="pin a version (X.Y.Z), or stable / edge")
    u.add_argument("--cancel", action="store_true", help="clear a stuck update hold and exit")
    u.add_argument("--no-drain", action="store_true", help="don't wait for idle (control already did; used by the web UI's updater)")
    op = sub.add_parser("ops", help="the ops containers: let the web UI update this install, or not")
    op.add_argument("action", choices=["enable-updates", "disable-updates"])
    op.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation")
    dr = sub.add_parser("drain", help="clear a stuck update hold on new runs")
    dr.add_argument("action", choices=["off"])
    sub.add_parser("2fa-reset", help="turn off the owner's 2FA (lost your phone and recovery codes)")
    for name in ("up", "down", "restart", "ps"):
        sub.add_parser(name)
    lg = sub.add_parser("logs")
    lg.add_argument("service", nargs="?")
    sub.add_parser("claude-login")
    bk = sub.add_parser("backup", help="an encrypted backup of everything, in one file")
    bk.add_argument("-o", "--output", help="the file to write (default l0w-backup-DATE.l0wbak)")
    rt = sub.add_parser("restore", help="put a backup back (replaces the data here)")
    rt.add_argument("file")
    rt.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation")
    sub.add_parser("version")
    mg = sub.add_parser("migrate", help="move the old assistant into this instance")
    mg.add_argument("--from", dest="old_dir", required=True, metavar="OLD_DIR", help="the old assistant's folder")
    mg.add_argument("--old-project", default="assistant", help="the old compose project name")
    mg.add_argument("--stop-old", action="store_true", help="stop the old stack first (volumes are kept)")
    mg.add_argument("--force", action="store_true", help="replace an earlier /work/_legacy and Claude login")
    mg.add_argument("--no-claude-login", action="store_true")
    li = sub.add_parser("login")
    li.add_argument("url", nargs="?")
    sub.add_parser("logout")
    sub.add_parser("status")
    ib = sub.add_parser("inbox")
    ib.add_argument("--history", action="store_true")
    an = sub.add_parser("answer")
    an.add_argument("id", type=int)
    an.add_argument("value", nargs="+")
    an.add_argument("--note")
    for name in ("approve", "deny"):
        d = sub.add_parser(name)
        d.add_argument("id", type=int)
        d.add_argument("--note")
        if name == "approve":
            d.add_argument("--scope", choices=["once", "project", "always"], default="always")
    for name in ("done", "decline"):
        d = sub.add_parser(name)
        d.add_argument("id", type=int)
        d.add_argument("--note")
    pr = sub.add_parser("promote")
    pr.add_argument("id", type=int)
    pr.add_argument("decision", choices=["approve", "deny"])
    th = sub.add_parser("threads")
    th.add_argument("--project")
    rd = sub.add_parser("read")
    rd.add_argument("thread", type=int)
    rd.add_argument("-n", type=int, default=20)
    sd = sub.add_parser("send")
    sd.add_argument("thread", type=int, nargs="?")
    sd.add_argument("text", nargs="+")
    sd.add_argument("--new", metavar="TITLE")
    sd.add_argument("--project")
    sd.add_argument("--now", action="store_true", help="press Reply now")
    rs = sub.add_parser("runs")
    rs.add_argument("-n", type=int, default=15)
    rn = sub.add_parser("run")
    rn.add_argument("id", type=int)
    rn.add_argument("--detailed", action="store_true")
    wk = sub.add_parser("wake")
    wk.add_argument("--budget", type=float)
    wk.add_argument("--tier")
    sp = sub.add_parser("stop")
    sp.add_argument("id", type=int)
    sub.add_parser("jobs")
    sub.add_parser("budget")
    lv = sub.add_parser("level")
    lv.add_argument("level", choices=["dormant", "frugal", "steady", "busy", "max"])
    lv.add_argument("--provider", help="provider id (default: the worker provider)")
    bo = sub.add_parser("boost")
    bo.add_argument("level", choices=["frugal", "steady", "busy", "max"])
    bo.add_argument("--hours", type=float, default=24)
    bo.add_argument("--until-reset", action="store_true")
    bo.add_argument("--provider", help="provider id (default: the worker provider)")
    sub.add_parser("calendar")
    sub.add_parser("secrets")
    sc = sub.add_parser("secret")
    sc.add_argument("action", choices=["set", "rm"])
    sc.add_argument("name")
    sc.add_argument("value", nargs="?")
    sc.add_argument("--desc")
    sub.add_parser("selftest")
    sub.add_parser("update-check")
    ex = sub.add_parser("extensions", help="the owner's extensions: list, enable, disable, rescan, sync (git sources from extensions.toml and extensions/.ops.toml)")
    ex.add_argument("action", choices=["list", "enable", "disable", "rescan", "sync"])
    ex.add_argument("name", nargs="?")
    a = p.parse_args(argv)
    {
        "install": cmd_install, "update": cmd_update, "drain": cmd_drain,
        "2fa-reset": cmd_2fa_reset, "up": cmd_compose, "down": cmd_compose, "restart": cmd_compose, "ps": cmd_compose, "logs": cmd_compose,
        "claude-login": cmd_claude_login, "backup": cmd_backup, "restore": cmd_restore, "migrate": cmd_migrate, "version": cmd_version, "login": cmd_login, "logout": cmd_logout, "status": cmd_status, "inbox": cmd_inbox,
        "answer": cmd_answer, "approve": cmd_decide, "deny": cmd_decide, "done": cmd_task, "decline": cmd_task, "promote": cmd_promote,
        "threads": cmd_threads, "read": cmd_read, "send": cmd_send, "runs": cmd_runs, "run": cmd_run, "wake": cmd_wake, "stop": cmd_stop,
        "jobs": cmd_jobs, "budget": cmd_budget, "level": cmd_level, "boost": cmd_boost, "calendar": cmd_calendar, "secrets": cmd_secrets,
        "secret": cmd_secrets, "selftest": cmd_selftest, "update-check": cmd_update_check,
        "extensions": cmd_extensions, "ops": cmd_ops,
    }[a.cmd](a)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
