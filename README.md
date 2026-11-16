# l0wagent

A long-running personal AI assistant that lives on your own server: it works in its own sandboxed computer, plans its own wakeups within the budget you give it, and talks with you through threads, a 1:1 chat, a web/desktop app, and the chat services you already use. Optional hubs give instances a public address, email, a board, and ways for assistants to work together.

**Status: M5 (installable and shareable).** Install with `./l0w install` ([docs/install.md](docs/install.md)); run and update it with [docs/operations.md](docs/operations.md). The plan is in [docs/plans/](docs/plans/README.md); orientation for contributors and coding agents is in [CLAUDE.md](CLAUDE.md).

## Quick start

```sh
git clone https://github.com/CyrusBrown/l0wagent.git && cd l0wagent
./l0w install        # needs Docker; prints the address of the setup wizard
```

Then open the address, create your account, sign the assistant in to Claude (or give it an
opencode Go key), pick a level, and
check the isolation self-test passes. Updates: `./l0w update`.
