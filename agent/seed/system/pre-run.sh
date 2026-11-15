#!/bin/bash
# Runs as you before every new session; its output is added to your run prompt as the
# "Pre-run report", so put quick checks here instead of spending tool calls on them.
# Keep it fast (120 s limit) and the output short (8000 chars max).
set -uo pipefail
cd /workspace
echo "git: $(git log -1 --format='%h %cr: %s' 2>/dev/null)"
git status --short | head -20
