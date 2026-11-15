"""Mock data for UI work, taken from the mockup (design/mockup/src/logic.js) and
adjusted where design/mockup/REVIEW.md says the plan wins (workspace seed,
_inbox/ folders, weekly review, port 8787).

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


def build(now: datetime | None = None) -> World:
    now = now or _now()
    w = World(now=now)
    ago = lambda **kw: now - timedelta(**kw)  # noqa: E731

    for name, desc, reads in (("GITHUB_TOKEN", "Fine-grained, repo: garden-irrigation", 14), ("HOME_ASSISTANT_TOKEN", "Long-lived token, read-only user", 3)):
        w.secrets[name] = s.SecretInfo(name=name, description=desc, scope="agent", created_at=ago(days=20), updated_at=ago(days=6), reads=reads, last_read_at=ago(hours=9))

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
    def run(rid, kind, why, about, start, minutes, used, budget, status, projects, why_detail=None, now_s=None, tier="sonnet/medium"):
        w.runs[rid] = s.Run(
            id=rid, kind=kind, why=why, why_detail=why_detail, about=about, now=now_s, provider="claude-subscription", tier=tier,
            budget=budget, used=used, deadline=start + timedelta(minutes=45) if status == "running" else None, status=status,
            started_at=start, ended_at=None if status == "running" else start + timedelta(minutes=minutes), projects=projects,
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
