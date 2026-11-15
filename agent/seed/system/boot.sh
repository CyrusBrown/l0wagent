#!/bin/bash
# Runs as root on EVERY container start (after provision.sh on a fresh container).
# Use it to start services and background jobs. Keep it quick (300 s limit) and idempotent.
# Long-running processes must detach, and should run as the agent user:
#   setsid -f runuser -u agent -- python3 -m http.server 8787 --directory /workspace/projects/site >/tmp/site.log 2>&1
# Port 8787 is reachable from the user's LAN (see "Your computer" in your instructions).
# cron is already running; add jobs with `crontab -e` (as agent) or files in /etc/cron.d.
set -uo pipefail
