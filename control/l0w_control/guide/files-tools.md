# Files, tools, notifications and the server

- **Uploads**: attach files to a message (or Files → Upload to a project); they land in
  `/work/_inbox/<project>/` for the assistant.
- **The shelf**: a project's Files tab lists deliverables; **Add to shelf** on any file (live,
  always current; or a snapshot, a copy as it is now).
- **Secrets** (Tools & Secrets): write-only values the assistant gets in its environment (agent
  only), redacted from everything shown or stored. It asks for one with a secure input card.
- **Network**: the assistant reaches the internet only through the egress proxy; model providers
  only during runs. Network rules allow or block hosts; the Network page lists requests.
- **Notifications** (Settings → Notifications): web push on this device (needs HTTPS, e.g.
  Tailscale HTTPS) or an ntfy topic; approvals and new sign-ins notify by default; quiet hours.
- **Updates**: a banner shows a new release; apply it on the server with `./l0w update`, which waits
  until the assistant is idle. `./l0w` also has status, logs, inbox, secrets and more for the host.
- **Self-test** (Settings → System) checks the assistant's isolation.
