# Docker Desktop (Mac and Windows)

l0wagent is built for Linux, but runs on Docker Desktop for trying it out or for a machine
that's always on anyway.

**The isolation must be checked.** Docker Desktop runs containers in a VM with its own
networking, so the sandbox network may not behave exactly as on Linux. Run the self-test
(the last wizard step, or Settings → System) after installing and after every Docker Desktop
update. Every check must pass before you give the assistant secrets.

Known differences:
- `host.docker.internal` resolves inside containers on Docker Desktop. The sandbox network is
  internal, so the agent still shouldn't reach it, and the self-test checks this.
- The GPU isn't available to containers on Mac.
- The machine sleeping pauses everything; runs resume when it wakes, but planned wakeups
  during sleep are missed (the assistant is told and re-plans).
- File access in volumes is slower than on Linux; heavy builds take longer.

Install as on Linux: `./l0w install` in a terminal (on Windows, from WSL 2 with Docker
Desktop's WSL integration on).
