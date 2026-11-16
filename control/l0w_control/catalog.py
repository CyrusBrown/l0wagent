"""The model catalog for opencode providers: which models opencode Go offers, their names and
catalog prices ($ per million tokens), read from models.dev (the catalog opencode itself uses)
and cached a day in `kv`. Used for the owner's model picker and for pricing the 1:1 chat.

Reasoning variants (opencode's --variant) aren't in models.dev; they're known only for the
models listed in KNOWN_VARIANTS (read from `opencode models opencode-go --verbose`, 2026-11).
For other models the effort is left to the model.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta

import httpx

from .auth import parse, utcnow
from .db import Database

log = logging.getLogger("l0w.catalog")
URL = "https://models.dev/api.json"
MAX_AGE = timedelta(days=1)
FALLBACK_PRICE = (1.0, 4.0)  # $/M tokens when a model's price is unknown: on the safe side
KNOWN_VARIANTS: dict[str, list[str]] = {
    "opencode-go/beam-free": ["low", "medium", "high", "xhigh", "max"],
    "opencode-go/longcat-2.5-free": ["low", "medium", "high"],
    "opencode-go/deepseek-v4.1-flash": ["low", "high", "max"],
    "opencode-go/deepseek-v4-pro": ["high", "max"],
    "opencode-go/glm-5.3": ["low", "high", "max"],
    "opencode-go/glm-5.3-flash": ["low", "high", "max"],
    "opencode-go/minimax-m3.1-flash": ["low", "medium", "high", "xhigh", "max"],
    "opencode-go/qwen3.8-flash": ["low", "medium", "xhigh"],
    "opencode-go/kimi-k3.1-pro": ["low", "high", "max"],
    "opencode-go/claude-haiku-5-5": ["low", "medium", "high", "xhigh", "max"],
}


def models(db: Database, provider: str = "opencode-go", refresh: bool = False) -> list[dict]:
    """[{id, name, input, output, reasoning, tools, context, variants}] for a provider, cheapest first."""
    row = db.q1("SELECT value, updated_at FROM kv WHERE key=?", (f"catalog:{provider}",))
    cached = json.loads(row["value"]) if row else None
    fresh = row and parse(row["updated_at"]) > utcnow() - MAX_AGE  # type: ignore[operator]
    if cached is None or not fresh or refresh:
        try:
            r = httpx.get(URL, timeout=15, headers={"User-Agent": "l0wagent"})
            r.raise_for_status()
            raw = r.json().get(provider, {}).get("models", {})
            cached = []
            for mid, m in raw.items():
                cost = m.get("cost") or {}
                full = f"{provider}/{mid}"
                cached.append({"id": full, "name": m.get("name") or mid, "input": float(cost.get("input") or 0), "output": float(cost.get("output") or 0),
                               "reasoning": bool(m.get("reasoning")), "tools": bool(m.get("tool_call", True)),
                               "context": int((m.get("limit") or {}).get("context") or 0), "variants": KNOWN_VARIANTS.get(full, [])})
            cached.sort(key=lambda x: (x["input"] + x["output"], x["id"]))
            db.ex("INSERT INTO kv(key, value, updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                  (f"catalog:{provider}", json.dumps(cached), db.now()))
        except (httpx.HTTPError, ValueError) as e:
            log.warning("couldn't read the model catalog: %s", e)
            if cached is None:
                cached = [{"id": k, "name": k.split("/", 1)[1], "input": 0.0, "output": 0.0, "reasoning": True, "tools": True, "context": 0, "variants": v}
                          for k, v in KNOWN_VARIANTS.items()]
                for m in cached:
                    m["input"], m["output"] = (None, None)  # unknown, not free
    return cached


def price(db: Database, model: str) -> tuple[float, float]:
    m = next((x for x in models(db, model.split("/", 1)[0]) if x["id"] == model), None)
    if not m or m["input"] is None:
        return FALLBACK_PRICE
    return float(m["input"]), float(m["output"])


def variants(model: str) -> list[str]:
    return KNOWN_VARIANTS.get(model, [])
