# Troubleshooting

## The self-test fails

The self-test (Settings → System, or the last wizard step) checks from inside the assistant's
computer that it can't reach your LAN, the host or the internet except through the egress
proxy, and that the model provider is blocked outside runs.

- **"LAN reachable" / "host reachable"**: the sandbox network isn't isolated. On Linux, make
  sure nothing edited `compose.yaml`'s `sandbox` network (it must be `internal: true` with
  `inhibit_ipv4`). On Docker Desktop, see [docker-desktop.md](docker-desktop.md).
- **"Model provider reachable without a lease"**: egress isn't in the path. Check
  `./l0w logs egress` and that the agent's `HTTP(S)_PROXY` point at `egress:3128`.

Don't give the assistant secrets or connect it to anything important until every check passes.

## "Usage can't be read, so runs don't start"

Control reads your Claude subscription's usage with the assistant's login, and refuses to
start runs without it (so a broken meter can't spend your week). Sign the assistant in again
(Settings → Providers, or `./l0w claude-login`). If you really want runs without a reading,
set `USAGE_UNKNOWN_POLICY=allow` in `.env` and `./l0w up`.

## The app says "Reconnecting to the server…"

The live connection dropped. It retries on its own; if it doesn't come back, check
`./l0w ps` and `./l0w logs control`.

## Push notifications don't arrive

- The app must be on **HTTPS** (see [install.md](install.md#5-on-your-phone-and-push-notifications)).
- iPhone: only from the Home Screen app, after "Turn on here" in that app.
- Settings → Notifications → Send a test. If the device shows "failed", remove it and turn it
  on again (browsers expire subscriptions).
- Quiet hours hold back everything except approvals and new sign-ins.

## An update waits forever

`./l0w update` waits for the current run and for jobs marked "don't interrupt". It prints
what it's waiting for. Stop the run from the app, or use `./l0w update --force`.

## Starting over

`./l0w down`, then `docker volume rm` the `l0wagent_*` volumes you want to reset (this
deletes their data), then `./l0w up`.
