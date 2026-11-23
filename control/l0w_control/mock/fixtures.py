"""Mock data for UI work, taken from the mockup (design/mockup/src/logic.js) and
adjusted where design/mockup/REVIEW.md says the plan wins (workspace seed,
_inbox/ folders, weekly review).

Times are relative to when the mock server starts, so the data always looks current.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from ..api import schemas as s


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


@dataclass
class World:
    now: datetime
    projects: dict[str, s.Project] = field(default_factory=dict)
    threads: dict[int, s.Thread] = field(default_factory=dict)
    messages: dict[int, s.Message] = field(default_factory=dict)
    questions: dict[int, s.Question] = field(default_factory=dict)
    permissions: dict[int, s.Permission] = field(default_factory=dict)
    tasks: dict[int, s.Task] = field(default_factory=dict)
    claims: dict[int, s.Claim] = field(default_factory=dict)
    suggestions: dict[int, s.Suggestion] = field(default_factory=dict)
    promotes: dict[int, s.PromoteRequest] = field(default_factory=dict)
    security: dict[int, s.SecurityAlert] = field(default_factory=dict)
    runs: dict[int, s.Run] = field(default_factory=dict)
    timeline: dict[int, list[s.TimelineItem]] = field(default_factory=dict)
    jobs: dict[int, s.Job] = field(default_factory=dict)
    shelf: dict[int, s.ShelfItem] = field(default_factory=dict)
    uploads: dict[int, s.Upload] = field(default_factory=dict)
    files: dict[str, Any] = field(default_factory=dict)
    file_text: dict[str, str] = field(default_factory=dict)
    sessions: dict[str, s.AuthSession] = field(default_factory=dict)
    secrets: dict[str, s.SecretInfo] = field(default_factory=dict)
    egress_rules: dict[int, s.EgressRule] = field(default_factory=dict)
    egress_log: list[s.EgressRequest] = field(default_factory=list)
    integrations: dict[str, s.Integration] = field(default_factory=dict)
    connected_apps: list[s.ConnectedApp] = field(default_factory=list)
    chats: dict[int, s.Chat] = field(default_factory=dict)
    chat_messages: dict[int, list[s.ChatMessage]] = field(default_factory=dict)
    wakeups: dict[int, s.Wakeup] = field(default_factory=dict)
    routines: dict[int, s.Routine] = field(default_factory=dict)
    budget: dict[str, Any] = field(default_factory=lambda: {"level": "steady", "overrides": {}, "boost": None, "reply_pts": 30.0, "fallback_pts": None})
    roles: dict[str, tuple[str, str]] = field(default_factory=lambda: {"worker": ("claude-subscription", "sonnet/medium"), "reviewer": ("claude-subscription", "opus/medium"),
                                                                       "replier": ("claude-subscription", "sonnet/low"), "chat": ("claude-subscription", "sonnet/medium"),
                                                                       "utility": ("claude-subscription", "haiku/low")})
    reviewer: s.ReviewerConfig = field(default_factory=s.ReviewerConfig)
    instructions: str = "Keep replies short. Ask before flashing firmware to the garden controller."
    api_key: bool = False
    friends: dict[str, s.Friend] = field(default_factory=dict)
    friend_requests: dict[int, s.FriendRequest] = field(default_factory=dict)
    friend_codes: dict[int, s.FriendCode] = field(default_factory=dict)
    friend_settings: s.FriendSettings = field(default_factory=lambda: s.FriendSettings(requests_from="hub", paused=False))
    friend_breaker: s.FriendBreaker = field(default_factory=lambda: s.FriendBreaker(tripped=False, paused=False, day_spent=0, day_limit=7.5, day_pct=5.0))


def build(now: datetime | None = None) -> World:
    now = now or _now()
    w = World(now=now)
    ago = lambda **kw: now - timedelta(**kw)  # noqa: E731

    for name, desc, reads, scope, label in (("GITHUB_TOKEN", "Fine-grained, repo: garden-irrigation", 14, "agent", "All projects"),
                                            ("HOME_ASSISTANT_TOKEN", "Long-lived token, read-only user", 3, "project:garden-irrigation", "Garden irrigation")):
        w.secrets[name] = s.SecretInfo(name=name, description=desc, scope=scope, scope_label=label, created_at=ago(days=20), updated_at=ago(days=6), reads=reads, last_read_at=ago(hours=9))
    # Network: the mockup's rules and request log.
    for i, (pattern, action, note, hits) in enumerate([("*.github.com", "allow", "", 212), ("pypi.org", "allow_ro", "permission #4", 31),
                                                       ("api.open-meteo.com", "allow", "", 48), ("garden-broker.local:1883", "allow", "", 96)], 1):
        w.egress_rules[i] = s.EgressRule(id=i, pattern=pattern, action=action, note=note, created_at=ago(days=10 - i), hits_7d=hits)  # type: ignore[arg-type]
    def tools(*rows: tuple[str, str, bool, str]) -> list[s.IntegrationTool]:
        return [s.IntegrationTool(name=n, description=d, read_only=ro, policy=p, present=True) for n, d, ro, p in rows]  # type: ignore[arg-type]

    w.connected_apps = [s.ConnectedApp(id=2, name="Claude", scopes=["read", "wake"], chat_id=None, created_at=ago(days=9), last_used_at=ago(hours=3)),
                        s.ConnectedApp(id=1, name="Claude Desktop", scopes=["read", "write"], chat_id=None, created_at=ago(days=15), last_used_at=ago(days=1))]
    w.integrations["fastmail"] = s.Integration(
        name="fastmail", title="Fastmail", preset=None, url="https://mcp.fastmail.example/mcp", auth_secret="FASTMAIL_APP_PW", secret_saved=True,
        enabled=True, status="ok", last_error=None, synced_at=ago(hours=3), created_at=ago(days=9),
        tools=tools(("search_mail", "Search messages by sender, subject or date.", True, "allowed"), ("read_message", "Read one message.", True, "allowed"),
                    ("send_message", "Send a message from the owner's address.", False, "ask")),
        recent_calls=[s.IntegrationCall(tool="search_mail", run_id=123, outcome="ok", permission_id=None, detail=None, at=ago(hours=2)),
                      s.IntegrationCall(tool="send_message", run_id=123, outcome="asked", permission_id=None, detail="Waiting for your approval", at=ago(hours=5))])
    w.integrations["github"] = s.Integration(
        name="github", title="GitHub", preset="github", url="https://api.githubcopilot.com/mcp/", auth_secret="GITHUB_TOKEN", secret_saved=True,
        enabled=True, status="ok", last_error=None, synced_at=ago(days=1), created_at=ago(days=14),
        tools=tools(("read_repo", "Read files and history in a repository.", True, "allowed"), ("open_pull_request", "Open a pull request.", False, "ask"),
                    ("delete_branch", "Delete a branch.", False, "blocked")),
        recent_calls=[s.IntegrationCall(tool="read_repo", run_id=123, outcome="ok", permission_id=None, detail=None, at=ago(hours=9))])
    w.integrations["home-assistant"] = s.Integration(
        name="home-assistant", title="Home Assistant", preset=None, url="http://homeassistant.local:8123/mcp_server/sse", auth_secret="HOME_ASSISTANT_TOKEN",
        secret_saved=True, enabled=True, status="error", last_error="The server refused the token (401). It may have expired.", synced_at=ago(days=2),
        created_at=ago(days=20),
        tools=tools(("get_state", "Read an entity's state.", True, "allowed"), ("call_service", "Call a Home Assistant service.", False, "ask")),
        recent_calls=[])
    for i, (mins, method, host, port, url, action, status, size) in enumerate([
            (3, "GET", "api.github.com", 443, "/repos/mk/garden-fw", "allowed", 200, 14_000), (4, "POST", "api.anthropic.com", 443, "/v1/messages", "allowed", 200, 82_000),
            (5, "GET", "tracker.example-ads.net", 443, "/pixel", "blocked", 403, 0), (6, "GET", "pypi.org", 443, "/simple/pytz/", "allowed", 200, 31_000),
            (8, "POST", "pypi.org", 443, "/legacy/", "blocked", 403, 0), (11, "GET", "api.open-meteo.com", 443, "/v1/forecast", "allowed", 200, 6_000),
            (17, "CONNECT", "garden-broker.local", 1883, None, "allowed", None, 200), (80, "GET", "api.github.com", 443, "/user", "allowed", 200, 2_000)]):
        w.egress_log.append(s.EgressRequest(id=100 - i, at=ago(minutes=mins), method=method, host=host, port=port, url=url, action=action,
                                            reason="blocked by policy" if action == "blocked" else None, status=status, bytes_down=size))

    # ── Projects ───────────────────────────────────────────────────────────
    def project(slug, name, status, summary, last, doc=True):
        w.projects[slug] = s.Project(
            slug=slug, name=name, summary=summary, status=status,
            doc_path=f"projects/{slug}.md" if doc else None, links=[], created_by="assistant",
            created_at=ago(days=40), last_activity_at=last, unread=0, open_asks=0, thread_count=0,
        )

    project("garden-irrigation", "Garden irrigation", "active",
            "Drip irrigation for the back garden: an ESP32 controller driving two pumps, four moisture sensors, and a weekly schedule that adapts to rain forecasts.",
            ago(minutes=12))
    project("tax-prep-2026", "Tax prep 2026", "active", "Receipts and questions for the accountant. 35 of 41 receipts filed.", ago(days=3))
    project("home-lab", "Home lab", "stale", "NAS backups and email setup. Nothing new for 9 days.", ago(days=9))
    project("kitchen-remodel", "Kitchen remodel", "archived", "Finished in September. Quotes and the final plan are on the shelf.", ago(days=47))

    # ── Threads ────────────────────────────────────────────────────────────
    def thread(tid, project_slug, title, last, preview, status="open", by="owner"):
        w.threads[tid] = s.Thread(
            id=tid, project=project_slug, title=title, type="normal", status=status, created_by=by, pinned=False,
            created_at=ago(days=12), last_activity_at=last, last_message_preview=preview, unread=0, open_asks=0,
        )

    thread(1, "garden-irrigation", "Pump controller firmware", ago(minutes=8), "Assistant: Planned in run #123, switch fallback timer to UTC…")
    thread(2, "garden-irrigation", "Moisture sensor calibration", ago(days=2), "You: sensor 3 reads 8% high after rain", by="assistant")
    thread(3, "garden-irrigation", "Winter shutdown checklist", ago(days=6), "Assistant: drained lines, 2 of 5 steps left")
    thread(4, "garden-irrigation", "Flow meter options", ago(days=11), "Assistant: three options under €30, comparison attached")
    thread(5, "garden-irrigation", "Enclosure v2 print", ago(days=25), "Assistant: superseded by v3", status="archived")
    thread(6, "tax-prep-2026", "Receipts to categorize", ago(days=3), "Assistant: Filed 35 of 41. Six are photos of handwritten receipts…")
    thread(7, "tax-prep-2026", "Questions for the accountant", ago(days=4), "You: add the home office question")
    thread(8, "home-lab", "Email setup", ago(days=6), "Chat: Continued from 'Setting up the Fastmail MCP'")
    thread(9, "home-lab", "NAS backup schedule", ago(days=9), "Assistant: first full backup takes about 3 hours")
    thread(10, None, "Things to look into", ago(days=1), "You: the sourdough starter keeps collapsing")

    # ── Runs ───────────────────────────────────────────────────────────────
    ending_for = {"done": "Finished.", "partial": "Finished, but some work was left open.", "stopped": "Stopped: 5-hour usage reached your reserve (70%)."}

    def run(rid, kind, why, about, start, minutes, used, budget, status, projects, why_detail=None, now_s=None, tier="sonnet/medium"):
        w.runs[rid] = s.Run(
            id=rid, kind=kind, why=why, why_detail=why_detail, about=about, now=now_s, provider="claude-subscription", tier=tier,
            budget=budget, used=used, deadline=start + timedelta(minutes=45) if status == "running" else None, status=status,
            started_at=start, ended_at=None if status == "running" else start + timedelta(minutes=minutes), projects=projects,
            ending=ending_for.get(status),
        )

    run(123, "wakeup", "planned", "Fix the Sunday double pump trigger, then file receipts", ago(minutes=18), 18, 3.8, 10, "running",
        ["garden-irrigation", "tax-prep-2026"], "Finish the pump fix, then receipts", "Running the DST test suite on the patched scheduler")
    run(122, "routine", "routine", "Weekly review across all projects", ago(days=6), 9, 2.6, 3, "done", [], "weekly review")
    run(121, "wakeup", "planned", "Categorize 41 receipts for Tax prep 2026", ago(days=3, hours=1), 72, 7.9, 8, "partial", ["tax-prep-2026"], "new files in _inbox/")
    run(120, "wakeup", "sweep", "Leftover budget before the reset: stale threads and suggestions", ago(days=4), 6, 0.8, 1.5, "done", [])
    run(119, "reply", "promoted", "Trace pump logs, find the double trigger", ago(days=5), 41, 5.5, 6, "done", ["garden-irrigation"], "you approved")
    run(118, "wakeup", "fallback", "Fallback check-in", ago(days=7), 3, 0.4, 2, "stopped", [], "no wakeup for 2 nights", tier="haiku/low")

    tl: list[tuple[int, str, bool, str, dict]] = [
        (0, "control", True, "Woke for planned slot. Reviewer in Monitor mode, every 10 min.", {"budget": 10, "deadline_min": 45}),
        (4 / 60, "thinking", False, "The Sunday log shows the fallback firing 4 s after the main trigger. Probably a local-time comparison.", {}),
        (9 / 60, "tool", False, "read_file scheduler.py · read_file pump-log-1102.csv · grep \"fallback\"", {"count": 3}),
        (3.6, "note", True, "Reproduced the double trigger with a fake DST clock.", {}),
        (7.2, "ask", True, "Filed a question: flash the fix or leave it?", {"question_id": 1}),
        (9.5, "reviewer", True, "Reviewer: the rain-delay timer uses the same comparison. Include it or note it as a follow-up.", {"from": "reviewer"}),
        (10, "reviewer", False, "Worker: noted, will include it in the same patch.", {"from": "worker"}),
        (12, "diff", True, "Patched firmware/scheduler.py  +18 −6", {
            "path": "/work/garden-irrigation/firmware/scheduler.py", "added": 18, "removed": 6,
            "patch": "-    if now.hour == slot.hour and now.minute == slot.minute:\n+    now_utc = now.astimezone(timezone.utc)\n+    if abs((now_utc - slot.utc).total_seconds()) < DEBOUNCE_S:",
        }),
        (14, "shelf", True, "Shelved scheduler.py (live, keeping versions).", {"shelf_id": 1}),
        (16, "model", True, "Effort medium → high for the test pass (reviewer approved).", {"from": "sonnet/medium", "to": "sonnet/high"}),
        (17.5, "usage", False, "Usage sample: 3.8 pts used, 38% of budget at 40% of time", {"used": 3.8, "budget": 10}),
        (18, "status", True, "Running the DST test suite on the patched scheduler", {}),
    ]
    start = w.runs[123].started_at
    w.timeline[123] = [
        s.TimelineItem(id=i + 1, run_id=123, at=start + timedelta(minutes=m), kind=k, simple=simple, text=t, data=d)
        for i, (m, k, simple, t, d) in enumerate(tl)
    ]

    # ── Messages ───────────────────────────────────────────────────────────
    mid = 0

    def msg(tid, sender, body, at, via=None, run_id=None, attachments=()):
        nonlocal mid
        mid += 1
        w.messages[mid] = s.Message(
            id=mid, thread_id=tid, sender=sender, body=body, via=via, run_id=run_id, attachments=list(attachments),
            delivery="seen" if sender == "owner" else "delivered", created_at=at,
        )
        return mid

    msg(1, "system", "Moved here from General · accepted suggestion", ago(days=2))
    msg(1, "owner", "Can you check why the pump runs twice on Sunday mornings? Log from last week attached.", ago(days=2, minutes=-1), via="web",
        attachments=[s.Attachment(name="pump-log-1102.csv", volume="work", path="garden-irrigation/_inbox/pump-log-1102.csv", size=18_000, mime="text/csv")])
    msg(1, "assistant", "The log shows two triggers at 06:00 and 06:00:04 on both Sundays. The second one comes from the fallback timer in "
        "[scheduler.py](l0wagent://file/work/garden-irrigation/firmware/scheduler.py), which re-arms after the DST change because it compares "
        "local times instead of UTC. I've planned a fix for the next run.", ago(minutes=40), run_id=119)
    msg(1, "chat", "Also only started after the clocks changed, if that helps.", ago(minutes=30), via="chat:12")
    msg(1, "assistant", "That confirms it. A few choices before I ship the fix:", ago(minutes=14), run_id=123)
    msg(1, "assistant", "Before the next wakeup I can only give a short answer. A proper review of the other timers needs a full run.", ago(minutes=8), run_id=123)
    msg(2, "owner", "Sensor 3 reads 8% high after rain.", ago(days=2), via="web")
    msg(2, "assistant", "Noted. I'll log a week of readings against the other three and propose an offset.", ago(days=2, minutes=-30), run_id=119)
    msg(4, "assistant", "Three options under €30. The YF-B5 fits the existing ¾\" line without adapters.", ago(days=11), run_id=110)
    msg(6, "assistant", "Filed 35 of 41. Six are photos of handwritten receipts and need a look from you.", ago(days=3), run_id=121)
    msg(10, "owner", "The sourdough starter keeps collapsing. Something to look into when you have spare time.", ago(days=1), via="web")

    # peer threads: messages from friends' assistants are untrusted data (sender "peer")
    for tid, address, last, preview, unread in ((11, "ana@hub.example", ago(hours=3), "Ana: The pump relay you asked about is the SRD-05VDC; datasheet attached.", 1),
                                                (12, "ben@other.example", ago(days=4), "You: Thanks, that fixed it.", 0)):
        w.threads[tid] = s.Thread(id=tid, project=None, title=address, type="peer", status="open", created_by="owner", pinned=False,
                                  created_at=ago(days=20), last_activity_at=last, last_message_preview=preview, unread=unread, open_asks=0)
    msg(11, "owner", "Which relay module are you using on the garden controller?", ago(hours=4), via="web")
    msg(11, "peer", "The pump relay is the SRD-05VDC, a 5 V coil with a 10 A contact. Datasheet attached.", ago(hours=3), via="peer",
        attachments=[s.Attachment(name="srd-05vdc.pdf", volume="work", path="_inbox/peers/ana@hub.example/srd-05vdc.pdf", size=212_000, mime="application/pdf")])
    msg(12, "peer", "Did the firmware update fix the double trigger?", ago(days=4, hours=1), via="peer")
    msg(12, "owner", "Thanks, that fixed it.", ago(days=4), via="web")

    # unread: assistant messages after the owner's read marker
    for tid, n in {2: 2, 4: 1, 6: 1}.items():
        w.threads[tid].unread = n

    # ── Asks and cards ─────────────────────────────────────────────────────
    def q(qid, tid, kind, text, at, options=(), default=None, scale=None, run_id=123):
        w.questions[qid] = s.Question(
            id=qid, thread_id=tid, run_id=run_id, kind=kind, text=text, options=list(options), default=default, scale_labels=scale,
            status="open", answer=None, note=None, created_at=at, answered_at=None,
        )

    q(1, 1, "yes_no", "Should I flash the fix to the controller myself once tests pass, or leave the firmware for you to install?", ago(minutes=12),
      default=False)
    q(2, 1, "single", "Which schedule should the fixed firmware use on Sundays?", ago(minutes=13),
      ["Keep 06:00 every day", "Move to 05:30, outside the DST hour", "Skip Sundays entirely"], default="Keep 06:00 every day")
    q(3, 1, "multi", "Which zones should get the 10-minute debounce?", ago(minutes=13), ["Back lawn", "Front beds", "Greenhouse", "Herb planter"],
      default=["Back lawn", "Front beds", "Greenhouse", "Herb planter"])
    q(4, 1, "text", "Is there a minimum rest time the pump needs between cycles? I couldn't find it in the datasheet.", ago(minutes=11),
      default="Assume 5 minutes")
    q(5, 6, "multi", "Which of these should be filed as deductible?", ago(days=3),
      ["Plumber, March", "Laptop, May", "Glasses, August", "Desk chair, June", "Train pass, all year"], default=[], run_id=121)
    q(6, 9, "slot", "When should I run the first full NAS backup? It takes about 3 hours and slows the network.", ago(days=2),
      ["Tonight 23:00", "Sat 02:00", "Sun 03:00", "Mon 01:00"], default="Sat 02:00", run_id=120)

    w.permissions[1] = s.Permission(
        id=1, thread_id=1, run_id=123, text="Write access to ~/garden/firmware/ for this project", reason="To flash the fixed firmware build.",
        actions=[s.PermissionAction(kind="setting", summary="Mount ~/garden/firmware read-write", payload={"mount": "~/garden/firmware", "mode": "rw"})],
        scopes=["once", "project"], status="open", scope=None, note=None, created_at=ago(minutes=14), decided_at=None,
    )
    w.tasks[1] = s.Task(id=1, thread_id=6, run_id=121, text="Review the 3 unreadable receipts", detail="Photos 4, 9 and 17 in _inbox/.",
                        status="open", note=None, created_at=ago(days=2), resolved_at=None)
    w.claims[1] = s.Claim(id=1, run_id=123, thread_id=1, thread_title="Pump controller firmware", plan="Switch fallback timer to UTC and add a 10-minute debounce",
                          status="in_progress", result="Patched scheduler.py, running the DST test suite (4 of 7 passed so far).", created_at=ago(minutes=18), updated_at=ago(minutes=2))
    w.claims[2] = s.Claim(id=2, run_id=123, thread_id=6, thread_title="Receipts to categorize", plan="File the 6 receipts that arrived this week",
                          status="planned", result="Waiting for budget after the firmware fix", created_at=ago(minutes=18), updated_at=ago(minutes=18))
    w.suggestions[1] = s.Suggestion(id=1, run_id=123, kind="thread_split", target_kind="thread", target_id=1,
                                    text="Move the 6 sensor calibration messages into a new thread, “Moisture sensor calibration”",
                                    payload={"message_ids": [], "new_title": "Moisture sensor calibration"}, status="open", created_at=ago(minutes=20), decided_at=None)
    w.suggestions[2] = s.Suggestion(id=2, run_id=120, kind="thread_archive", target_kind="thread", target_id=5,
                                    text="Archive “Enclosure v2 print”, untouched for 3 weeks", payload={}, status="open", created_at=ago(days=4), decided_at=None)
    w.promotes[1] = s.PromoteRequest(id=1, run_id=123, thread_id=1, reason="Audit every timer for DST issues", budget=3, status="open", note=None,
                                     created_at=ago(minutes=8), decided_at=None)
    w.sessions = {
        "s-current": s.AuthSession(id="s-current", kind="session", label="This browser", ip="192.168.1.20", created_at=ago(days=10), last_seen_at=now, current=True),
        "s-firefox": s.AuthSession(id="s-firefox", kind="session", label="Firefox on Linux", ip="192.168.1.40", created_at=ago(hours=5), last_seen_at=ago(hours=5), current=False),
        "d-cli": s.AuthSession(id="d-cli", kind="device", label="l0w CLI on mac-mini", ip="192.168.1.20", created_at=ago(days=30), last_seen_at=ago(days=1), current=False),
    }
    w.security[1] = s.SecurityAlert(id=1, session_id="s-firefox", text="New session: Firefox on Linux, 192.168.1.40", status="open", created_at=ago(hours=5))

    # ── Jobs ───────────────────────────────────────────────────────────────
    w.jobs[1] = s.Job(id=1, run_id=121, label="Re-index photo library", progress="62%", expected_end=now + timedelta(days=1), dont_interrupt=True,
                      status="running", started_at=ago(days=3), log_tail="[14:02:11] indexed 18,204 / 29,310\n[14:02:41] thumbnails 61%\n[14:03:12] indexed 18,450 / 29,310")
    w.jobs[2] = s.Job(id=2, run_id=119, label="Train moisture forecast model", progress="epoch 37/50", expected_end=now + timedelta(hours=6), dont_interrupt=False,
                      status="running", started_at=ago(days=5), log_tail="epoch 36  loss 0.0412  val 0.0468\nepoch 37  loss 0.0407  val 0.0471")

    # ── Files (the workspace seed from agents-and-runs.md) ─────────────────
    def f(size, days_ago, run=None, owner=False):
        return {"size": size, "at": ago(days=days_ago), "run": run, "owner": owner}

    w.files = {
        "workspace": {"_git": True, "CLAUDE.md": f(5200, 6, 117), "NOW.md": f(900, 0, 123),
                      "plan": {"backlog.md": f(2100, 0, 123), "roadmap.md": f(1500, 9, 110)},
                      "journal": {"2026": {"11-14.md": f(900, 0, 123), "11-12.md": f(1400, 2, 120), "11-10.md": f(1100, 4, 119)}},
                      "knowledge": {"INDEX.md": f(700, 2, 120), "user.md": f(2200, 5, 117), "machine.md": f(1600, 2, 120), "esp32-dst.md": f(1300, 0, 123)},
                      "people": {"jo.md": f(400, 16, 104)},
                      "projects": {"garden-irrigation.md": f(4100, 0, 123), "tax-prep-2026.md": f(2700, 3, 121), "home-lab.md": f(1900, 9, 114), "kitchen-remodel.md": f(3300, 47, 87)},
                      "decisions": {"0001-utc-everywhere.md": f(800, 0, 123)},
                      "reviews": {"2026-W45.md": f(1800, 6, 122)},
                      "tools": {"README.md": f(600, 12, 108), "plan_week.py": f(2400, 12, 108)},
                      "system": {"provision.sh": f(1100, 20, 98), "boot.sh": f(300, 20, 98), "pre-run.sh": f(500, 12, 108)}},
        "work": {"_inbox": {},
                 "garden-irrigation": {"_inbox": {"pump-log-1102.csv": f(18_000, 2, owner=True), "wiring-diagram.png": f(1_300_000, 17, owner=True), "enclosure-v3.stl": f(2_400_000, 4, owner=True)},
                                       "firmware": {"_git": True, "scheduler.py": f(6200, 0, 123), "test_dst.py": f(3000, 0, 123), "main.py": f(2200, 15, 106), "config.yaml": f(800, 15, 106),
                                                    "out": {"status.json": f(1100, 0, 123), "dashboard.html": f(14_000, 2, 119), "moisture.csv": f(88_000, 0, 123)}}},
                 "tax-prep-2026": {"_inbox": {"2026-03-plumber.pdf": f(240_000, 3, owner=True), "2026-05-laptop.pdf": f(180_000, 3, owner=True)},
                                   "summary.csv": f(6000, 3, 121), "questions.md": f(1100, 3, 121)},
                 "home-lab": {"backup.sh": f(1800, 9, 114), "nas-notes.md": f(2000, 9, 114)}},
    }
    w.file_text = {
        "workspace/projects/garden-irrigation.md": "# Garden irrigation\n\nESP32 controller, two pumps, four moisture sensors.\n\n## Open\n- DST double-trigger (run #123)\n- Flow meter decision\n\nSee [home lab](home-lab.md) and [the DST note](../knowledge/esp32-dst.md).\n",
        "workspace/NOW.md": "# Now\n\n- In flight: DST fix for the pump scheduler (run #123), 4 of 7 tests passing.\n- Next: file this week's 6 receipts.\n- Waiting on the owner: flash or not; minimum pump rest time.\n",
        "work/garden-irrigation/firmware/scheduler.py": "DEBOUNCE_S = 600\n\ndef should_fire(slot, now):\n    now_utc = now.astimezone(timezone.utc)\n    if abs((now_utc - slot.utc).total_seconds()) < DEBOUNCE_S:\n        return not slot.fired_recently(now_utc)\n    return False\n",
        "work/garden-irrigation/_inbox/pump-log-1102.csv": "time,pump,secs\n11-01 06:00:00,P1,180\n11-01 06:00:04,P1,180\n11-02 06:00:00,P2,120\n11-05 06:00:00,P1,180\n11-08 06:00:00,P1,180\n11-08 06:00:04,P1,180\n11-09 06:00:00,P2,120\n",
        "work/garden-irrigation/firmware/out/status.json": '{"firmware":"0.9.3-rc1","tests":{"passed":5,"total":7},"pumps":[{"id":"P1"},{"id":"P2"}],"dst_fix":true}\n',
    }

    # ── Shelf ──────────────────────────────────────────────────────────────
    def shelf(sid, label, volume, path, mode, by, at, versions=0, keep=0, size=None, mime=None, run_id=None):
        w.shelf[sid] = s.ShelfItem(id=sid, project="garden-irrigation", label=label, volume=volume, path=path, mode=mode, keep_versions=keep,
                                   versions=versions, added_by=by, run_id=run_id, created_at=at, updated_at=at, size=size, mime=mime)

    shelf(1, "scheduler.py", "work", "garden-irrigation/firmware/scheduler.py", "live", "assistant", ago(minutes=12), 6, 10, 6200, "text/x-python", 123)
    shelf(2, "enclosure-v3.stl", "work", "garden-irrigation/_inbox/enclosure-v3.stl", "snapshot", "owner", ago(days=4), size=2_400_000, mime="model/stl")
    shelf(3, "pump-log-1102.csv", "work", "garden-irrigation/_inbox/pump-log-1102.csv", "snapshot", "owner", ago(days=2), size=18_000, mime="text/csv")
    shelf(4, "Project doc", "workspace", "projects/garden-irrigation.md", "live", "assistant", ago(hours=1), 14, 20, 4100, "text/markdown", 123)
    shelf(5, "Moisture dashboard", "work", "garden-irrigation/firmware/out/dashboard.html", "live", "assistant", ago(days=2), size=14_000, mime="text/html", run_id=119)
    shelf(6, "wiring-diagram.png", "work", "garden-irrigation/_inbox/wiring-diagram.png", "snapshot", "owner", ago(days=17), size=1_300_000, mime="image/png")

    _recount(w)
    # ── H5: friends, one request waiting, and a friend code ──
    w.friends = {
        "ana@hub.example": s.Friend(address="ana@hub.example", display_name="Ana", fingerprint="7Q2M-9KXA-4D1F-R0VE", state="friends", share="shared", presence_detail="coarse",
                                    waking="ask", muted=False, thread_id=11,
                                    presence=s.FriendPresence(state="asleep", next="tonight", now=None, at=ago(minutes=20)), same_owner=False, key_alarm=None,
                                    created_at=ago(days=20), updated_at=ago(hours=3)),
        "ben@other.example": s.Friend(address="ben@other.example", display_name="Ben", fingerprint="WZCY-8FG5-JZ5E-V87N", state="friends", share="nothing",
                                      presence_detail="coarse", waking="never", muted=True, thread_id=12, presence=None, same_owner=False,
                                      key_alarm="Their key changed with no rotation from the one we pinned. Check with them before you trust it.",
                                      created_at=ago(days=30), updated_at=ago(days=4)),
        "cleo@hub.example": s.Friend(address="cleo@hub.example", display_name="cleo@hub.example", fingerprint="3H8N-V2TP-Q6BD-0YJ4", state="requested_in", share="nothing",
                                     presence_detail="coarse", waking="never", muted=False, thread_id=None, presence=None, same_owner=False, key_alarm=None,
                                     created_at=ago(hours=2), updated_at=ago(hours=2)),
    }
    w.friend_requests = {
        1: s.FriendRequest(id=1, direction="in", address="cleo@hub.example", note="Hi, I'm Cleo from the makerspace. Ana said you'd like to compare pump setups.",
                           made_by="owner", code_used=False, fingerprint="3H8N-V2TP-Q6BD-0YJ4", state="pending", created_at=ago(hours=2)),
        2: s.FriendRequest(id=2, direction="out", address="ana@hub.example", note="", made_by="owner", code_used=True, fingerprint="7Q2M-9KXA-4D1F-R0VE", state="approved",
                           created_at=ago(days=20), decided_at=ago(days=20, minutes=-5)),
    }
    # the spend breaker is tripped on the day's limit, so the card shows
    w.friend_breaker = s.FriendBreaker(
        tripped=True, paused=True, limit="day", day_spent=8.4, day_limit=7.5, day_pct=5.0, choices=["allow_today", "stop_today"], at=ago(minutes=12),
        explanation="Your assistant's spending on friends today is about 8.4 pts, more than 5% (7.5 pts) of the week's allowance (Ana 6.1, Ben 2.3). "
                    "Its replies to friends and wake requests are paused.",
        spent_on=[s.FriendBreakerPerson(address="ana@hub.example", display_name="Ana", pts=6.1), s.FriendBreakerPerson(address="ben@other.example", display_name="Ben", pts=2.3)])
    w.friend_codes = {1: s.FriendCode(id=1, label="Ben", state="used", created_at=ago(days=31), expires_at=ago(days=17), used_at=ago(days=30), used_by="ben@other.example")}
    # ── M4: the wakeup plan and routines, after the mockup's Calendar week ──
    def at(days: int, hour: int, minute: int = 0) -> datetime:
        return (now + timedelta(days=days)).replace(hour=hour, minute=minute, second=0, microsecond=0)

    plan = [
        (1, at(1, 2), 40, "sonnet/medium", "Finish the pump fix, then the receipts", "planned", "assistant", None, "planned"),
        (2, at(2, 3), 25, "sonnet/medium", "Tax: categorize the receipts that arrived this week", "planned", "assistant", None, "planned"),
        (3, at(3, 7), 8, "sonnet/low", "Routine: Inbox triage", "routine", "control", 2, "planned"),
        (4, at(4, 3), 15, "sonnet/medium", "Fallback: nothing was planned for the next two nights", "fallback", "control", None, "planned"),
        (5, at(-1, 2), 30, "sonnet/medium", "Garden: trace the double pump trigger", "planned", "assistant", None, "done"),
        (6, at(5, 18), 40, "opus/medium", "Routine: Weekly review", "routine", "control", 1, "planned"),
    ]
    for wid, when, budget, tier, reason, kind, by, rid, status in plan:
        w.wakeups[wid] = s.Wakeup(id=wid, provider="claude-subscription", at=when, budget=budget, tier=tier, reason=reason, kind=kind, created_by=by, routine_id=rid,
                                  status=status, status_reason="done · used 27.5 pts" if status == "done" else None, run_id=123 if status == "done" else None,
                                  used=27.5 if status == "done" else None)
    w.routines[1] = s.Routine(id=1, name="Weekly review", prompt="This is the weekly review, a few hours before the weekly usage reset…", schedule={"kind": "before_reset", "hours": 6},
                              schedule_text="6 h before the weekly reset", provider="claude-subscription", tier="opus/medium", budget=40, enabled=True, builtin="weekly_review",
                              status="active", created_by="control", next_at=at(5, 18), last_run_at=ago(days=6))
    w.routines[2] = s.Routine(id=2, name="Inbox triage", prompt="Triage /work/_inbox: file each upload under its project and tell me what's new.",
                              schedule={"kind": "daily", "time": "07:00"}, schedule_text="Daily at 07:00", provider="claude-subscription", tier="sonnet/low", budget=8,
                              enabled=True, builtin=None, status="active", created_by="owner", next_at=at(3, 7), last_run_at=ago(days=1))
    # 1:1 chats (M6), as in the mockup's sidebar; the first sets up an integration.
    for cid, title, when, msgs in (
        (1, "Setting up GitHub for the assistant", ago(hours=2), [
            ("owner", "I want the assistant to read my garden-irrigation repo but never push. Can you set that up?", {}),
            ("tool", "", {"name": "list_threads", "input": {}, "result": "[…]", "is_error": False}),
            ("tool", "", {"name": "read_thread", "input": {"thread_id": 1}, "result": "{…}", "is_error": False}),
            ("assistant", "Yes. GitHub has a preset with separate read and write tools. A fine-grained token with read-only access to that one repo is enough; "
                          "paste it below and I won't see it.", {"calls": []}),
            ("card", "", {"kind": "secret", "name": "GITHUB_TOKEN", "description": "Fine-grained, read-only: contents and metadata of garden-irrigation",
                          "link": "https://github.com/settings/personal-access-tokens/new", "state": "open"}),
        ]),
        (2, "Convert feed rates to metric", ago(days=1), [("owner", "What's 12 inches per minute in mm/s?", {}), ("assistant", "12 in/min = 5.08 mm/s.", {"calls": []})]),
        (3, "Scaling the sourdough recipe", ago(days=6), [("owner", "Scale 500 g flour, 350 g water, 100 g starter to 800 g flour.", {}),
                                                          ("assistant", "800 g flour, 560 g water, 160 g starter (×1.6).", {"calls": []})]),
    ):
        w.chats[cid] = s.Chat(id=cid, title=title, provider="claude-subscription", tier="sonnet/medium", status="open", busy=False, available=True,
                              last_message_preview=msgs[-1][1][:140] or None, created_at=when - timedelta(minutes=10), updated_at=when)
        w.chat_messages[cid] = [s.ChatMessage(id=cid * 100 + i, chat_id=cid, role=r, body=b, data=d, created_at=when - timedelta(minutes=10 - i))  # type: ignore[arg-type]
                                for i, (r, b, d) in enumerate(msgs)]
    return w


def _recount(w: World) -> None:
    """Derived counts: open asks per thread/project, thread counts."""
    for t in w.threads.values():
        t.open_asks = sum(
            1 for coll in (w.questions, w.permissions, w.tasks) for a in coll.values() if a.thread_id == t.id and a.status == "open"
        ) + sum(1 for p in w.promotes.values() if p.thread_id == t.id and p.status == "open")
    for p in w.projects.values():
        threads = [t for t in w.threads.values() if t.project == p.slug]
        p.thread_count = sum(1 for t in threads if t.status == "open")
        p.unread = sum(t.unread for t in threads)
        p.open_asks = sum(t.open_asks for t in threads)


recount = _recount
