<!-- Control-owned: the owner's message in the "Set yourself back up" thread, created by `./l0w migrate`
     (legacy.py). Archive the old text before editing, as for every prompt here. -->
You've moved to a new home: this is l0wagent, the new version of the system you ran on. Everything from
the old instance is in `/work/_legacy/` (read-only):

- `workspace/`: your old `/workspace`, with its full git history.
- `home/`: your old home folder, without caches and virtualenvs (e.g. `ml-research-repo/`, your old
  `.claude/` memory files).
- `control-export/`: the old control's records as Markdown and JSON: every thread (also in the app as
  archived threads named "… (old assistant)"), questions, permissions and tasks with my answers, runs with
  your notes and results, the reviewer's conversations. `README.md` there explains the files.

Your new `/workspace` starts from the new seed. Please set yourself back up:

1. Read `_legacy/` first, starting with the old `docs/STATE.md` and the control export's README.
2. Rebuild your knowledge in the new layout. Drop what control now records for you (the message log, the
   usage log, the long-jobs list, stop rules): `assistant help` and your core prompt say what control keeps.
3. Move project work into `/work/<project>/` (copy from `_legacy`; it's read-only).
4. Recreate projects and threads with `assistant`; for ones that probably already exist, suggest instead.
5. Restore the services and cron jobs you still need under `system/`. Nothing from the old
   `provision.sh`, `boot.sh` or crontab runs until you bring it back.
6. Ask again for capabilities you lost: network rules, secrets (most came over, by name and value), access.
7. Write a migration report in this thread: what you moved, what you left behind and why, and what you need
   from me.

This is a big job. If this run's budget isn't enough, do the reading and planning now, write down where you
are, and plan a wakeup with a generous budget for the rest (`assistant wake plan` shows what's left).
