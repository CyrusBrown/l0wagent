# Installing l0wagent

l0wagent runs on a Linux machine you own (a home server, a NAS that runs Docker, a spare
PC). It needs Docker and about 4 GB of free memory; more if the assistant will build or run
heavy things. Mac and Windows work through Docker Desktop, with the caveats in
[docker-desktop.md](docker-desktop.md).

## 1. Docker

Install Docker Engine and the Compose plugin: <https://docs.docker.com/engine/install/>.
Check:

```sh
docker info >/dev/null && docker compose version
```

To give the assistant an NVIDIA GPU, also install `nvidia-container-toolkit`; the installer
notices it.

## 2. Get l0wagent

```sh
git clone https://github.com/CyrusBrown/l0wagent.git
cd l0wagent
```

(You only need `compose.yaml`, `compose.override.example.yaml`, `l0w` and `cli/`; the
images come prebuilt from GitHub's registry.)

## 3. Install

```sh
./l0w install
```

It checks Docker, picks a free private subnet for the assistant's sandbox network, writes
`.env` with fresh tokens (keep it private), asks how much memory to give the assistant (and
whether to use the GPU) for `compose.override.yaml`, pulls the images, starts everything and
prints the address, e.g. `http://192.168.1.20:8000`.

`--channel edge` follows the main branch instead of releases. `--port` changes the web port.

## 4. The setup wizard

Open the address in a browser. The wizard:

1. creates your account (the only one on this server; add 2FA later in Settings);
2. sets your time zone;
3. signs the assistant in to your Claude subscription: open the link, sign in, paste the
   code back. The login runs on the server and stays on the assistant's computer.
   **No Claude subscription?** Choose **Use opencode Go** instead and paste your opencode
   API key (opencode.ai → your workspace → API keys). The assistant then runs on opencode
   with open models, and every role moves onto it. Settings → Providers shows which model
   is behind each tier (small, medium, large) and lets you change them;
4. picks a level: how much of your week the assistant may plan with (you can change it any
   time);
5. runs the isolation self-test. Every check should pass; if one doesn't, see
   [troubleshooting.md](troubleshooting.md) before you rely on it.

## 5. On your phone (and push notifications)

Browsers only allow installing the app and push notifications on **HTTPS**. The simplest
way on a home network is [Tailscale](https://tailscale.com):

1. Install Tailscale on the server and your phone, in the same tailnet.
2. On the server: `sudo tailscale serve --bg --https=443 http://localhost:8000`.
   (For the files preview origin, also `sudo tailscale serve --bg --https=8001 http://localhost:8001`.)
3. Open `https://<server-name>.<tailnet>.ts.net` on the phone.
4. iPhone: Share → Add to Home Screen, then open it from the Home Screen. Android/desktop:
   the browser's Install button.
5. Settings → Notifications → **Turn on here**. Send a test.

Without HTTPS, add an [ntfy](https://ntfy.sh) topic in Settings → Notifications instead.

## 6. The `l0w` CLI

On the server (or any machine that can reach it):

```sh
./l0w login            # once: a revocable device token, kept in ~/.config/l0w/
./l0w status
./l0w inbox
./l0w send 12 "Thanks, go ahead" --now
```

`./l0w --help` lists everything; a coding agent working with you can use it too. Day-to-day
operations are in [operations.md](operations.md).
