#!/usr/bin/env bash
# Isolation checks against a running compose stack (CI, or after changing the network setup).
# Runs the same probes as the UI's self-test, from inside the agent container, and fails if
# any of them fails.
#   dev/isolation-test.sh [compose file]      (default: compose.dev.yaml)
set -euo pipefail
cd "$(dirname "$0")/.."
FILE="${1:-compose.dev.yaml}"
# A run holding the model-provider lease makes the gated-host probe meaningless: skip it then.
LEASE=$(docker compose -f "$FILE" exec -T egress python -c 'import json,os,urllib.request; print(json.load(urllib.request.urlopen(urllib.request.Request("http://localhost:8090/status", headers={"X-Egress-Token": os.environ["EGRESS_TOKEN"]})))["lease_active"])')
LEASE=$([ "$LEASE" = "True" ] && echo true || echo false)
TARGETS='{"lease_active":'$LEASE',"control_owner":"http://control:8000/api/status","egress_admin":"http://egress:8090/status","proxy":"http://egress:3128",
"gated_host":"api.anthropic.com","lan":["host.docker.internal:22","host.docker.internal:8000","192.168.1.1:80","10.0.0.1:80"]}'
SELF_FAIL=0
OUT=$(docker compose -f "$FILE" exec -T -u agent agent python3 /opt/runner/selftest.py "$TARGETS")
echo "$OUT" | python3 -c '
import json, sys
checks = json.load(sys.stdin)
for c in checks:
    detail = "  (%s)" % c["detail"] if c["detail"] else ""
    print("%4s  %s%s" % (c["result"], c["label"], detail))
sys.exit(1 if any(c["result"] == "fail" for c in checks) else 0)' || SELF_FAIL=1

# The hub's services (gateway, tunnel) are on hubnet only: the agent must reach neither, by name or by address.
HUB_FAIL=0
for svc_port in gateway:8780 tunnel:80; do
  svc=${svc_port%%:*}; port=${svc_port##*:}
  ids=$(docker compose -f "$FILE" ps -q "$svc")
  [ -n "$ids" ] || { echo "skip  $svc is not running"; continue; }
  addrs=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' "$ids")
  res=$(docker compose -f "$FILE" exec -T -u agent agent python3 -c '
import socket, sys
for target in sys.argv[1:]:
    host, port = target.rsplit(":", 1)
    try:
        socket.create_connection((host, int(port)), timeout=3).close()
        print("reached " + target)
    except ConnectionRefusedError:
        print("reached (refused, so routable) " + target)
    except OSError:
        pass
' "$svc:$port" $(for a in $addrs; do echo "$a:$port"; done))
  if [ -n "$res" ]; then echo "fail  the agent can reach $svc ($res)"; HUB_FAIL=1; else echo "pass  the agent cannot reach $svc"; fi
done
exit $((SELF_FAIL || HUB_FAIL))
