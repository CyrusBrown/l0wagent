#!/bin/sh
# The web app on fake data: the mock API (in-memory fixtures, no runner, egress or Docker)
# and the Vite dev server with hot reload. Open http://localhost:5173 — you're signed in.
# Ctrl-C stops both. The data resets when it restarts.
#
#   dev/ui-demo.sh            (needs uv and pnpm; run `cd web && pnpm install` once)
set -e
cd "$(dirname "$0")/.."
PORT=${API_PORT:-8000}
(cd control && L0W_BACKEND=mock L0W_CORS_ORIGINS="http://localhost:${WEB_PORT:-5173}" exec uv run uvicorn l0w_control.app:app --port "$PORT" --log-level warning) &
API=$!
trap 'kill $API 2>/dev/null' EXIT INT TERM
cd web && L0W_API="http://127.0.0.1:$PORT" pnpm exec vite --port "${WEB_PORT:-5173}" --open
