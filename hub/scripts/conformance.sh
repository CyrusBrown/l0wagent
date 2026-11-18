#!/usr/bin/env bash
# Run the protocol conformance suite (protocol/conformance) against this hub running locally under
# `wrangler dev`: a fresh local D1 and Durable Object state in a temp folder, the dev configuration
# (wrangler.dev.toml: hub.localhost:8787), then the suite. Exits with the suite's code.
#   ./scripts/conformance.sh [-v]        (extra arguments go to the suite)
set -euo pipefail

export WRANGLER_SEND_METRICS=false
cd "$(dirname "$0")/.."
HUB_DIR=$(pwd)
PORT=8787
HUB_URL="http://hub.localhost:${PORT}"
CONNECT="127.0.0.1:${PORT}"
STATE=$(mktemp -d "${TMPDIR:-/tmp}/l0w-hub-state.XXXXXX")
LOG="${STATE}/wrangler.log"
WRANGLER_PID=""
MOCK_PID=""
MOCK_PORT=8788

cleanup() {
  if [ -n "${MOCK_PID}" ]; then kill "${MOCK_PID}" 2>/dev/null || true; fi
  if [ -n "${WRANGLER_PID}" ] && kill -0 "${WRANGLER_PID}" 2>/dev/null; then
    kill "${WRANGLER_PID}" 2>/dev/null || true
    for _ in $(seq 1 30); do kill -0 "${WRANGLER_PID}" 2>/dev/null || break; sleep 0.2; done
    kill -9 "${WRANGLER_PID}" 2>/dev/null || true
  fi
  rm -rf "${STATE}"
}
trap cleanup EXIT

if (exec 3<>"/dev/tcp/127.0.0.1/${PORT}") 2>/dev/null; then
  echo "conformance: port ${PORT} is already in use (is another wrangler dev running?)" >&2
  exit 2
fi

if (exec 3<>"/dev/tcp/127.0.0.1/${MOCK_PORT}") 2>/dev/null; then
  echo "conformance: port ${MOCK_PORT} (the mock Cloudflare API) is already in use" >&2
  exit 2
fi
echo "conformance: starting the mock Cloudflare API on :${MOCK_PORT}"
MOCK_CF_TOKEN=dev-cf-token node scripts/mock-cf-api.mjs --port "${MOCK_PORT}" >"${STATE}/mock-cf.log" 2>&1 &
MOCK_PID=$!

echo "conformance: applying migrations to a fresh local D1"
pnpm exec wrangler d1 migrations apply DB --local --config wrangler.dev.toml --persist-to "${STATE}" >"${STATE}/migrate.log" 2>&1 \
  || { cat "${STATE}/migrate.log" >&2; exit 2; }

echo "conformance: starting wrangler dev on ${HUB_URL}"
pnpm exec wrangler dev --config wrangler.dev.toml --local --port "${PORT}" --persist-to "${STATE}" >"${LOG}" 2>&1 &
WRANGLER_PID=$!

ready=""
for _ in $(seq 1 150); do
  if ! kill -0 "${WRANGLER_PID}" 2>/dev/null; then
    echo "conformance: wrangler dev exited early:" >&2
    cat "${LOG}" >&2
    exit 2
  fi
  if curl -sf -o /dev/null -H "Host: hub.localhost:${PORT}" "http://${CONNECT}/.well-known/l0wagent/hub"; then
    ready=1
    break
  fi
  sleep 0.5
done
if [ -z "${ready}" ]; then
  echo "conformance: the hub did not answer within 75 s:" >&2
  cat "${LOG}" >&2
  exit 2
fi

# the dev admin token is in wrangler.dev.toml (a public dev value)
export HUB_ADMIN_TOKEN="${HUB_ADMIN_TOKEN:-dev-admin-token}"
INVITE_CMD="node ${HUB_DIR}/scripts/invite.mjs --hub ${HUB_URL} --connect ${CONNECT}"

cd ../protocol/py
uv sync --frozen >/dev/null
set +e
uv run python -m conformance --hub "${HUB_URL}" --connect "${CONNECT}" --invite-cmd "${INVITE_CMD}" "$@"
code=$?
set -e
if [ "${code}" -ne 0 ]; then
  echo "conformance: the suite failed (exit ${code}); wrangler log tail:" >&2
  tail -n 40 "${LOG}" >&2
fi
exit "${code}"
