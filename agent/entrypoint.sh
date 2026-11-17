#!/bin/bash
# Runs as root at container start: wire up the proxy for tools that ignore env vars,
# run the agent's own provisioning script on a fresh container, start cron, run the agent's
# boot script (every start), then drop to the agent user for the runner.
set -uo pipefail

if [ -n "${HTTP_PROXY:-}" ]; then
  cat > /etc/apt/apt.conf.d/99egress <<APT
Acquire::http::Proxy "${HTTP_PROXY}";
Acquire::https::Proxy "${HTTP_PROXY}";
APT
  {
    echo "HTTP_PROXY=${HTTP_PROXY}"; echo "HTTPS_PROXY=${HTTPS_PROXY:-$HTTP_PROXY}"
    echo "http_proxy=${HTTP_PROXY}"; echo "https_proxy=${HTTPS_PROXY:-$HTTP_PROXY}"
    echo "NO_PROXY=${NO_PROXY:-}"; echo "no_proxy=${NO_PROXY:-}"
    # So cron jobs and services the agent starts can use `assistant` (the control API) too.
    echo "CONTROL_URL=${CONTROL_URL:-http://control:8000}"; echo "AGENT_API_TOKEN=${AGENT_API_TOKEN:-}"
  } > /etc/environment
  sudo -u agent -H git config --global http.proxy "${HTTP_PROXY}" 2>/dev/null || true
fi

# Volumes are normally initialised with the image's ownership, but be safe.
chown agent:agent /workspace /work /cache /home/agent 2>/dev/null || true
mkdir -p /work/_inbox && chown agent:agent /work/_inbox
# The reviewer (a second unix user, no sudo) reads /workspace and /work as "other" but never the agent's home.
chmod 0700 /home/agent 2>/dev/null || true
chmod o+rx /workspace /work /work/_inbox 2>/dev/null || true
if id reviewer >/dev/null 2>&1; then
  mkdir -p /home/reviewer && chown reviewer:reviewer /home/reviewer 2>/dev/null || true
  chmod 0755 /home/reviewer 2>/dev/null || true
fi

# The proxy may still be starting (containers are recreated together): wait for it before apt runs.
for _ in $(seq 90); do (exec 3<>/dev/tcp/egress/3128) 2>/dev/null && break; sleep 1; done

# First start of a fresh container (e.g. after an image rebuild): replay the agent's system setup.
# Only a successful run counts, so a failure is retried on the next container start.
MARK=/var/lib/assistant/provisioned
if [ ! -f "$MARK" ]; then
  mkdir -p /var/lib/assistant
  if [ -f /workspace/system/provision.sh ]; then
    echo "[entrypoint] running /workspace/system/provision.sh"
    if timeout 1800 bash /workspace/system/provision.sh > /workspace/system/provision.log 2>&1; then
      echo "[entrypoint] provision ok"
      touch "$MARK"
    else
      echo "[entrypoint] provision FAILED (see /workspace/system/provision.log); will retry on next start"
    fi
    chown agent:agent /workspace/system/provision.log 2>/dev/null || true
  else
    touch "$MARK"
  fi
fi

# Cron (system crontabs, /etc/cron.d and the agent's own `crontab -e`).
if command -v cron >/dev/null; then cron && echo "[entrypoint] cron started"; fi

# The agent's boot script, on every container start, as root. Long-running services it starts
# must detach (e.g. `setsid -f runuser -u agent -- cmd >log 2>&1`).
if [ -f /workspace/system/boot.sh ]; then
  echo "[entrypoint] running /workspace/system/boot.sh"
  if timeout 300 bash /workspace/system/boot.sh > /workspace/system/boot.log 2>&1 < /dev/null; then
    echo "[entrypoint] boot ok"
  else
    echo "[entrypoint] boot FAILED or timed out (see /workspace/system/boot.log)"
  fi
  chown agent:agent /workspace/system/boot.log 2>/dev/null || true
fi

# FAKE_CLAUDE=1 (development): the runner drives fake runtimes (Claude Code and opencode), no login or usage needed.
if [ "${FAKE_CLAUDE:-0}" = "1" ]; then
  export CLAUDE_BIN=/opt/runner/fake_claude.py OPENCODE_BIN=/opt/runner/fake_opencode.py
  echo "[entrypoint] using the fake claude and opencode"
fi

exec env HOME=/home/agent USER=agent LOGNAME=agent \
  setpriv --reuid=agent --regid=agent --init-groups \
  /opt/runner/venv/bin/python /opt/runner/runner.py
