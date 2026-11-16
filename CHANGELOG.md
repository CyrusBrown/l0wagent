# Changelog

Each release's section becomes its GitHub release notes, which the app shows in the
"Update available" banner. Versions follow the milestones until 1.0.

## 0.5.0

First friends release (M5).
- Install with `./l0w install`; update with `./l0w update`, which waits until the assistant is idle.
- The web app is installable, works offline (cached threads and an outbox), and sends push notifications.
- The `l0w` CLI covers what the old `ops` script did, for you or a coding agent on the host.
- No Claude subscription needed: the assistant can run on **opencode Go** (open models through
  opencode). Choose it in the setup wizard or add the key under Settings → Providers.

## 0.4.0

Budgets, wakeups and runs (M4): points, levels and Boost, the assistant's own wakeup plan with
fallback and sweep wakeups, routines with the weekly review, the reviewer, long jobs, the Calendar.

## 0.3.0

Files (M3): the file browser, project shelves with live items, previews, the assistant's web page.

## 0.2.0

Conversations (M2): projects, threads, asks, claims, suggestions, Reply now, secrets.

## 0.1.0

The core platform (M1): accounts, runs that survive restarts, the usage gate, egress.
