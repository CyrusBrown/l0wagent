# Reviews

Notes from reviews of the code and the plan. Findings and proposals. What the owner accepts is folded into `docs/plans/`; each file says what happened to its proposals.

## 2026-11-16

| File | What's in it |
|---|---|
| [Bugs and code improvements](2026-11-16-bugs-and-code.md) | 28 security and 12 correctness findings, each with where it is and how to fix it; prompt wording that disagrees with the code; 14 code improvements. **Fixed on 2026-11-17** (with GitHub issues #30 and #31), except where the file notes otherwise. |
| [Hub plan review](2026-11-16-hub.md) | Six changes to make before building the hub, the reasoning, a second pass, Cloudflare's limits and a revised build order. **Accepted**; `hub.md` was rewritten to match. |
| [Wakeups across several providers](2026-11-16-wakeups-and-providers.md) | Why a second provider adds little today, and a seven-step design: schedule and pools, one unit for work, routing, continuations, delegates, event wakeups, a model proxy. **Accepted**; summarised in `agents-and-runs.md`, ordered as the Providers track in `development.md`. |
| [Ideas, ranked](2026-11-16-ideas.md) | Every idea from the review in one list of 90, best first, tagged by whether it is already in the plan. |
