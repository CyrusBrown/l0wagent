"""How a run ended, in plain sentences. One place turns end_reason, stop_reason, Claude Code's
result subtypes and the common error texts into what the owner reads: the timeline, the run
summary (`ending`), notifications and the next run's brief. The raw text is kept by the callers
(timeline `data`), never thrown away."""
from __future__ import annotations

import re

LOGIN_SENTENCE = "Claude's login expired: sign in again in Settings → Providers (or run ./l0w claude-login on the server)."
BUSY_SENTENCE = "Claude was overloaded or rate-limited; it'll retry next run."
UNEXPECTED_SENTENCE = "The session stopped unexpectedly"

_LOGIN = re.compile(r"\b401\b|oauth token has expired|invalid api key|please run /login|authentication_error|invalid authentication|token has expired", re.I)
_BUSY = re.compile(r"\b(429|529)\b|overloaded|rate[ _-]?limit|too many requests", re.I)
_SUBTYPES = {
    "error_max_turns": "The session hit its turn limit before it finished.",
    "error_max_budget_usd": "The session hit its spending limit before it finished.",
}


def _tail(stderr: str | None, n: int = 200) -> str:
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    return lines[-1][-n:] if lines else ""


def explain_error(text: str | None = None, subtype: str | None = None, returncode: int | None = None, stderr: str | None = None) -> str:
    """One sentence for a session or runner error that wasn't a stop. Text that already reads
    as a sentence (and matches nothing known) passes through unchanged."""
    text = (text or "").strip()
    seen = f"{text}\n{stderr or ''}"
    if _LOGIN.search(seen):
        return LOGIN_SENTENCE
    if _BUSY.search(seen):
        return BUSY_SENTENCE
    tail = _tail(stderr)
    if subtype in _SUBTYPES or text in _SUBTYPES:
        return _SUBTYPES[subtype if subtype in _SUBTYPES else text]
    if returncode not in (None, 0, 130):
        return f"The runner crashed (exit {returncode})." + (f" Last words: {tail}" if tail else "")
    if (subtype or text) == "error_during_execution" and (not text or text == "error_during_execution"):
        return UNEXPECTED_SENTENCE + (f": {tail}" if tail else ".")
    if not text or text == subtype:
        return UNEXPECTED_SENTENCE + (f": {tail}" if tail else ".")
    return text


def stopped_sentence(stop_reason: str | None, end_reason: str | None = None) -> str:
    """Why a run was stopped, from the reason control recorded when it asked the run to stop."""
    r = (stop_reason or "").strip()
    low = r.lower()
    m = re.search(r"(weekly|5-hour)\s+usage is at (\d+)%.*reserve \((\d+)%\)", r, re.I)
    if m:
        which = "weekly" if m.group(1).lower() == "weekly" else "5-hour"
        return f"Stopped: {which} usage reached your reserve ({m.group(3)}%)."
    if "budget" in low or end_reason == "budget":
        return "Stopped: its budget was used up."
    if "deadline" in low or end_reason == "deadline":
        return "Stopped: it reached its deadline."
    if "paused" in low:
        return "Stopped: you paused everything."
    if "update" in low or "drain" in low:
        return "Stopped: the server is updating."
    if "usage" in low or "reserve" in low or "ceiling" in low or end_reason == "usage":
        return "Stopped: usage reached your reserve."
    if not r or low in ("stopped", "stopped by you"):
        return "Stopped by you."
    return f"Stopped: {r[:1].lower() + r[1:]}".rstrip(".") + "."


def ending(status: str | None, end_reason: str | None = None, stop_reason: str | None = None, result: str | None = None) -> str | None:
    """The readable ending of a finished run (None while it's still active)."""
    if status in (None, "starting", "running"):
        return None
    if status == "stopped":
        return stopped_sentence(stop_reason, end_reason)
    if status == "partial":
        return "Finished, but some work was left open."
    if status == "done":
        return "Finished: nothing more to do." if end_reason == "idle" else "Finished."
    return explain_error(result) if (result or "").strip() else "The run ended with an error; the timeline has the details."


def api_error_sentence(status: int | None, body: str | None, who: str = "The provider") -> str:
    """A model API's HTTP refusal, for the chat: what happened and what to do."""
    body = (body or "").strip()[:300]
    if status in (401, 403):
        return f"{who} refused the API key (HTTP {status}). Check the key in Settings → Providers."
    if status in (429, 529) or (status or 0) >= 500:
        return f"{who} is overloaded or rate-limiting (HTTP {status}). Try again in a minute."
    return f"{who} returned an error (HTTP {status}): {body}"
