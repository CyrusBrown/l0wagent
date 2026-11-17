"""Settings the 1:1 chat can read and propose changes to (owner request, 2026-11-17), so it can
help tune providers, models, budgets and the reviewer. It never applies anything itself: a
proposal becomes a "settings" card listing each change (before → after), and only the owner's
approval applies it, through the same backend methods the Settings screens use.

A change is {"setting": name, "value": ..., "provider"?: id}. Settings:
- per provider (default: the worker's): level, allowance, allowed_hours, max_per_day,
  min_gap_minutes, max_wakeup, tiers, fallback, sweep (the level's limits; null resets one to
  the level's), reply_pts, fallback_pts, period, reset_day, caps, models (opencode providers)
- role.<worker|reviewer|replier|chat|utility>: {"provider", "tier"}
- reviewer.<mode|provider|tier|interval_minutes|budget_share|change_tier>
- reserve_weekly_pct, reserve_five_hour_pct (the Claude subscription's reserve)
- instructions (the standing instructions' full text)
- notifications.<event> (true/false), notifications.quiet ({"start", "end"} or null)
Secrets, provider keys, accounts and sessions are never settings here.
"""

from __future__ import annotations

import json
from typing import Any

from .api import schemas as s
from .backends.base import BadRequest

LIMITS = ("allowance", "allowed_hours", "max_per_day", "min_gap_minutes", "max_wakeup", "tiers", "fallback", "sweep")
BUDGET = ("reply_pts", "fallback_pts", "period", "reset_day", "caps")
ROLES = ("worker", "reviewer", "replier", "chat", "utility")
REVIEWER = ("mode", "provider", "tier", "interval_minutes", "budget_share", "change_tier")


def _short(v: Any) -> str:
    if v is None:
        return "the level's"
    if isinstance(v, bool):
        return "on" if v else "off"
    if isinstance(v, str):
        return v if len(v) <= 80 else v[:77] + "…"
    text = json.dumps(v, separators=(", ", ": "))
    return text if len(text) <= 80 else text[:77] + "…"


def snapshot(be) -> dict:
    """Everything the chat may read about settings (no secrets, no keys)."""
    overview = be.budget_overview()
    providers = []
    for p in overview.providers:
        providers.append({
            "provider": p.provider, "label": p.label, "level": p.level, "limits": p.limits.model_dump(), "customized": p.custom,
            "period": p.period, "reset_day": p.reset_day, "caps": p.caps.model_dump() if p.caps else None, "reply_pts": p.reply_allowance,
            "fallback_pts": p.fallback_pts, "left": p.left, "used_by_assistant": p.used_by_assistant, "model_options": p.model_options,
        })
    return {
        "budgets": providers,
        "levels": {lv.name: {"text": lv.text, **lv.limits.model_dump()} for lv in overview.levels},
        "reserve": {"reserve_weekly_pct": overview.reserve_weekly_pct, "reserve_five_hour_pct": overview.reserve_five_hour_pct},
        "providers": [{"id": p.id, "kind": p.kind, "label": p.label, "enabled": p.enabled, "runtime": p.runtime, "status": p.status,
                       "models": [m.model_dump() for m in p.models] if p.models else None} for p in be.providers()],
        "roles": [r.model_dump() for r in be.roles().roles],
        "reviewer": be.reviewer_config().model_dump(),
        "instructions": be.instructions().text,
        "notifications": be.notification_settings().model_dump(exclude={"ntfy_url", "ntfy_token"}),
    }


def describe(be, changes: list[dict]) -> list[dict]:
    """Check proposed changes and record what each changes from and to. Raises BadRequest."""
    if not isinstance(changes, list) or not changes:
        raise BadRequest("bad_input", "changes is a non-empty list of {setting, value, provider?}")
    if len(changes) > 20:
        raise BadRequest("bad_input", "At most 20 changes at once.")
    snap = snapshot(be)
    worker = next(r["provider"] for r in snap["roles"] if r["role"] == "worker")
    out = []
    for c in changes:
        if not isinstance(c, dict) or not isinstance(c.get("setting"), str):
            raise BadRequest("bad_input", "Each change is {setting, value, provider?}.")
        name, value = c["setting"].strip(), c.get("value")
        provider = c.get("provider") or worker
        label, before = name, None
        if name in ("level", *LIMITS, *BUDGET, "models"):
            b = next((p for p in snap["budgets"] if p["provider"] == provider), None)
            if name == "models":
                prov = next((p for p in snap["providers"] if p["id"] == provider), None)
                if not prov or prov["models"] is None:
                    raise BadRequest("bad_setting", f"{provider} has no model list (only opencode providers do).")
                if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
                    raise BadRequest("bad_setting", "models is a list of model ids, e.g. [\"opencode-go/glm-5.3\"].")
                before = [m["id"] for m in prov["models"]]
            else:
                if not b:
                    raise BadRequest("bad_setting", f"Unknown or disabled provider {provider}.")
                if name == "level" and value not in snap["levels"]:
                    raise BadRequest("bad_setting", f"Levels are {', '.join(snap['levels'])}.")
                before = b["level"] if name == "level" else b["limits"].get(name) if name in LIMITS else \
                    {"reply_pts": b["reply_pts"], "fallback_pts": b["fallback_pts"], "period": b["period"], "reset_day": b["reset_day"], "caps": b["caps"]}[name]
            label = f"{name.replace('_', ' ')} ({b['label'] if b else provider})"
        elif name.startswith("role."):
            role = name[5:]
            if role not in ROLES or not isinstance(value, dict) or not value.get("provider") or not value.get("tier"):
                raise BadRequest("bad_setting", f"role.<{'|'.join(ROLES)}> takes {{provider, tier}}.")
            r = next(x for x in snap["roles"] if x["role"] == role)
            before, label = {"provider": r["provider"], "tier": r["tier"]}, f"{role} role"
        elif name.startswith("reviewer."):
            field = name[9:]
            if field not in REVIEWER:
                raise BadRequest("bad_setting", f"reviewer.<{'|'.join(REVIEWER)}>.")
            before, label = snap["reviewer"].get(field), f"reviewer {field.replace('_', ' ')}"
        elif name in ("reserve_weekly_pct", "reserve_five_hour_pct"):
            if not isinstance(value, (int, float)) or not 0 <= value <= 100:
                raise BadRequest("bad_setting", f"{name} is a percentage.")
            before, label = snap["reserve"][name], name.replace("_pct", "").replace("_", " ") + " %"
        elif name == "instructions":
            if not isinstance(value, str) or len(value) > 8000:
                raise BadRequest("bad_setting", "instructions is the full text (at most 8,000 characters).")
            before, label = snap["instructions"], "standing instructions"
        elif name.startswith("notifications."):
            key = name[14:]
            events = snap["notifications"]["events"]
            if key == "quiet":
                before, label = {"start": snap["notifications"].get("quiet_start"), "end": snap["notifications"].get("quiet_end")}, "quiet hours"
            elif key in events and isinstance(value, bool):
                before, label = events[key], f"notify on {key.replace('_', ' ')}"
            else:
                raise BadRequest("bad_setting", f"notifications.<{'|'.join(events)}> (true/false) or notifications.quiet.")
        else:
            raise BadRequest("bad_setting", f"Unknown setting {name}. See this tool's description.")
        out.append({"setting": name, "provider": provider, "value": value, "label": label, "before": _short(before), "after": _short(value)})
    return out


def apply(be, changes: list[dict]) -> list[str]:
    """Apply approved changes in order; returns what was done (for the chat's note)."""
    done = []
    for c in changes:
        name, value, provider = c["setting"], c["value"], c.get("provider")
        if name == "level":
            be.patch_budget(provider, s.BudgetPatch(level=value))
        elif name in LIMITS:
            be.patch_budget(provider, s.BudgetPatch(overrides=s.BudgetLimitsPatch(**{name: value})))
        elif name in ("reply_pts", "fallback_pts", "period", "reset_day"):
            be.patch_budget(provider, s.BudgetPatch(**{name: value}))
        elif name == "caps":
            be.patch_budget(provider, s.BudgetPatch(caps=s.BudgetCaps(**(value or {}))))
        elif name == "models":
            be.patch_provider(provider, s.ProviderPatch(models=value))
        elif name.startswith("role."):
            roles = be.roles().roles
            be.set_roles(s.Roles(roles=[r.model_copy(update={"provider": value["provider"], "tier": value["tier"]}) if r.role == name[5:] else r for r in roles]))
        elif name.startswith("reviewer."):
            be.set_reviewer_config(be.reviewer_config().model_copy(update={name[9:]: value}))
        elif name in ("reserve_weekly_pct", "reserve_five_hour_pct"):
            be.patch_reserve(s.ReserveSettings(**{name: value}))
        elif name == "instructions":
            be.set_instructions(s.StandingInstructions(text=value))
        elif name.startswith("notifications."):
            cur = be.notification_settings()
            key = name[14:]
            if key == "quiet":
                q = value or {}
                cur = cur.model_copy(update={"quiet_start": q.get("start"), "quiet_end": q.get("end")})
            else:
                cur = cur.model_copy(update={"events": {**cur.events, key: bool(value)}})
            be.set_notification_settings(cur)
        done.append(f"{c['label']}: {c['before']} → {c['after']}")
    return done
