"""Probe an l0wagent instance from outside its network.

Run it from a machine outside the owner's LAN, against the instance as it is
exposed on the internet (for example over Tailscale Funnel):

    python3 dev/outside-probe.py --ui https://box.example.ts.net --files https://box.example.ts.net:8443 [--user NAME]

Each check prints PASS or FAIL with a one-line reason. The exit code is 1 if
any check fails. Check 4 sends up to 15 wrong passwords from this address, so
it may lock this address out of sign-in for a while. --allow-http skips the
https requirement, for a local test instance only.
"""

import argparse
import json
import ssl
import sys
import urllib.error
import urllib.request

TIMEOUT = 10
LOGIN_ATTEMPTS = 15
CONTEXT = ssl.create_default_context()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Turn redirects into HTTPError so the status and headers are seen as-is."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


STRICT = urllib.request.build_opener(NoRedirect, urllib.request.HTTPSHandler(context=CONTEXT))
FOLLOW = urllib.request.build_opener(urllib.request.HTTPSHandler(context=CONTEXT))
results = []
set_cookies = []  # (source, raw Set-Cookie header)


def fetch(opener, url, method="GET", headers=None, body=None):
    """Return (status, headers, body). HTTP errors are results, not crashes."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with opener.open(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.headers, err.read()


def record(name, ok, reason):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'} {name}: {reason}")


def collect(source, headers):
    for raw in headers.get_all("Set-Cookie") or []:
        set_cookies.append((source, raw))


def check_cookies():
    if not set_cookies:
        return record("5 cookies", True, "no cookie set to check")
    problems = []
    for source, raw in set_cookies:
        parts = [p.strip() for p in raw.split(";")]
        name = parts[0].split("=", 1)[0]
        attrs = {p.split("=", 1)[0].lower() for p in parts[1:]}
        if "secure" not in attrs:
            problems.append(f"{name} from {source} has no Secure")
        if "session" in name and not name.startswith("__Host-"):
            problems.append(f"session cookie {name} from {source} lacks __Host- prefix")
    if problems:
        return record("5 cookies", False, "; ".join(problems))
    names = sorted({raw.split("=", 1)[0] for _, raw in set_cookies})
    record("5 cookies", True, f"all Secure, session cookie prefixed ({', '.join(names)})")


def main():
    parser = argparse.ArgumentParser(description="Probe an l0wagent instance from outside.")
    parser.add_argument("--ui", required=True, help="base URL of the UI and API, e.g. https://box.example.ts.net")
    parser.add_argument("--files", required=True, help="base URL of the files origin")
    parser.add_argument("--user", default="probe-nobody", help="username to use for the sign-in check")
    parser.add_argument("--allow-http", action="store_true", help="allow http:// URLs (local test only)")
    args = parser.parse_args()
    ui, files = args.ui.rstrip("/"), args.files.rstrip("/")

    # 8. Both URLs must be https.
    for label, url in (("ui", ui), ("files", files)):
        if url.startswith("https://"):
            record(f"8 https {label}", True, "https")
        elif args.allow_http:
            record(f"8 https {label}", True, f"{url} is not https (allowed by --allow-http)")
        else:
            record(f"8 https {label}", False, f"{url} is not https")

    # 1. The agent API must refuse a wrong agent token.
    try:
        status, headers, _ = fetch(STRICT, f"{ui}/api/agent/ping", headers={"X-Agent-Token": "probe"})
        collect("GET /api/agent/ping", headers)
        record("1 agent api", status in (401, 403), f"status {status} (want 401 or 403)")
    except OSError as err:
        record("1 agent api", False, f"request failed: {err}")

    # 2. The chat-runtime MCP path must not answer from outside.
    try:
        status, headers, _ = fetch(STRICT, f"{ui}/api/chat-runtime/mcp")
        collect("GET /api/chat-runtime/mcp", headers)
        record("2 chat-runtime", status in (401, 403, 404), f"status {status} (want 401, 403 or 404)")
    except OSError as err:
        record("2 chat-runtime", False, f"request failed: {err}")

    # 3. The instance must answer its metadata endpoint with JSON.
    try:
        status, headers, body = fetch(FOLLOW, f"{ui}/api/meta")
        collect("GET /api/meta", headers)
        json.loads(body)
        record("3 meta", status == 200, f"status {status}, JSON body")
    except ValueError:
        record("3 meta", False, f"status {status}, body is not JSON")
    except OSError as err:
        record("3 meta", False, f"request failed: {err}")

    # 4. Sign-in must be rate-limited.
    print(f"WARNING check 4 sends up to {LOGIN_ATTEMPTS} wrong passwords and may lock this address out of sign-in for a while")
    payload = json.dumps({"username": args.user, "password": "wrong-password-probe"}).encode()
    login_headers = {"Content-Type": "application/json", "X-L0W-Client": "probe"}
    limited = False
    try:
        for attempt in range(1, LOGIN_ATTEMPTS + 1):
            status, headers, _ = fetch(FOLLOW, f"{ui}/api/auth/login", "POST", login_headers, payload)
            collect(f"POST /api/auth/login #{attempt}", headers)
            if status == 429:
                limited = True
                break
        record("4 rate limit", limited, f"429 after {attempt} attempts" if limited else f"no 429 in {LOGIN_ATTEMPTS} attempts")
    except OSError as err:
        record("4 rate limit", False, f"request failed: {err}")

    # 5. Cookies from the sign-in responses and the UI root.
    try:
        status, headers, _ = fetch(FOLLOW, f"{ui}/")
        collect("GET /", headers)
        check_cookies()
    except OSError as err:
        record("5 cookies", False, f"could not fetch the UI root: {err}")

    # 6. Every files-origin response carries the sandbox headers.
    for path in ("/", "/nope", "/f/bad.token/x"):
        url = files + path
        try:
            status, headers, _ = fetch(STRICT, url)
            csp = (headers.get("Content-Security-Policy") or "").strip()
            nosniff = (headers.get("X-Content-Type-Options") or "").strip().lower() == "nosniff"
            ok = csp.lower().startswith("sandbox") and nosniff
            reason = f"status {status}, CSP {'sandbox' if csp.lower().startswith('sandbox') else 'missing or not sandbox'}, nosniff {'yes' if nosniff else 'no'}"
            record(f"6 files headers {path}", ok, reason)
        except OSError as err:
            record(f"6 files headers {path}", False, f"request failed: {err}")

    # 7. The files origin must not serve the control API.
    try:
        status, _, _ = fetch(STRICT, f"{files}/api/auth/me")
        record("7 files isolation", status != 200, f"status {status} (must not be 200)")
    except OSError as err:
        record("7 files isolation", False, f"request failed: {err}")

    failed = results.count(False)
    print(f"{len(results) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
