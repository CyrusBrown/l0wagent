# Migrating the current instance

Done later, once the new server side works. Part of the [overhaul plan](README.md).

**Approach**:
1. Start a fresh l0wagent instance.
2. Put everything old in a legacy folder.
3. Import the old threads as archived.
4. Later, dedicate a run to the assistant setting itself back up.

## What goes where

| From the old instance | To | Notes |
|---|---|---|
| `/workspace` (with full git history) | `/work/_legacy/workspace/` (read-only copy) | The new `/workspace` starts from the new seed. |
| Large project data (ML runs, honeypot, games) | Inside that copy | Moved into `/work/<project>/` by the re-setup run. |
| Control DB: threads, messages, questions/permissions/tasks with answers, runs + notes, reviewer conversations | `/work/_legacy/control-export/` as Markdown + JSON | Also imported into the new UI as **archived read-only threads**. |
| Secrets | **Not** in the folder | Names only. Values are re-entered (or moved with a one-time encrypted transfer, `l0w migrate`, never written to the agent's disk). |
| Egress rules | Imported into the new egress | e.g. the LAN read-only rules for Jellyfin. |
| `system/provision.sh`, `boot.sh`, crontab | Legacy folder only | Not run automatically; the re-setup run decides what comes back. |
| `/home/agent` (venvs, caches) | Not migrated | Rebuilt as needed. |

## The re-setup run
A one-time routine the owner starts when ready (generous budget, strong model). Its prompt:
- read `_legacy/`
- rebuild knowledge in the new layout, dropping what control now records (the message log, the usage log, the long-jobs list, the stop rules)
- move project work into `/work/<project>/`
- recreate projects and threads (as suggestions for existing ones)
- restore services and cron in `system/`
- re-request capabilities it lost
- write a migration report

Ongoing ML jobs can't resume mid-run, so stop them cleanly first. The old `docs/STATE.md` lists what's in flight.

## Cutover
1. Pause the old scheduler and let any run finish.
2. Export (`l0w migrate export` on the old server).
3. Bring up the new instance and import.
4. Keep the old stack **stopped, not deleted**, with its volumes, until you're happy.
5. The old assistant's Cloudflare/GitHub accounts and public sites keep running independently. Their tokens come over as secrets you re-enter.
