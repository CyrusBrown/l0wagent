#!/bin/bash
# Runs as root on EVERY container start (after provision.sh on a fresh container).
# Use it to start services and background jobs. Keep it quick (300 s limit) and idempotent.
# Long-running processes must detach, and should run as the agent user, e.g.:
#   setsid -f runuser -u agent -- python3 worker.py >/tmp/worker.log 2>&1
# cron is already running; add jobs with `crontab -e` (as agent) or files in /etc/cron.d.
set -uo pipefail
