"""Run control + the runner (with the fake claude) + egress locally, without Docker.

    uv run python scripts/dev_stack.py [--port 8100] [--dir /tmp/l0w-stack] [--extensions DIR]

Extensions load from --extensions (default: the repo's extensions/ folder; the browser tests pass a
copy holding the example, extensions/examples/plantlog).

For the browser tests (web/e2e) and quick manual runs. SIGUSR1 restarts control (kill -9
then start), which is how the browser test restarts it mid-run. Ctrl-C stops everything.
"""

import argparse
import shutil
import signal
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.stack import Stack  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8100)
    p.add_argument("--dir")
    p.add_argument("--steps", type=int, default=10)
    p.add_argument("--delay", type=float, default=1.0)
    p.add_argument("--hold", type=float, default=2.0, help="minutes a run waits for your reply after reaching out")
    p.add_argument("--extensions", help="the extensions folder; 'example' = a fresh copy with only the example extension")
    a = p.parse_args()
    tmp = Path(a.dir) if a.dir else Path(tempfile.mkdtemp(prefix="l0w-stack-"))
    if a.dir and tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True, exist_ok=True)
    repo = Path(__file__).resolve().parents[2]
    ext_dir = Path(a.extensions) if a.extensions and a.extensions != "example" else repo / "extensions"
    if a.extensions == "example":
        ext_dir = tmp / "extensions"
        shutil.copytree(repo / "extensions" / "examples" / "plantlog", ext_dir / "plantlog")
    stack = Stack(tmp, steps=a.steps, delay=a.delay, own_groups=False, hold_minutes=a.hold,
                  control_env={"L0W_SCHEDULER": "1", "L0W_EXTENSIONS_DIR": str(ext_dir)})
    stack.ports["control"] = a.port
    stack.url = f"http://127.0.0.1:{a.port}"
    stack.client.base_url = stack.url
    stack.up()
    (tmp / "stack.pid").write_text(str(__import__("os").getpid()))
    print(f"control on {stack.url} (state in {tmp}); SIGUSR1 restarts control", flush=True)
    restart = threading.Event()
    signal.signal(signal.SIGUSR1, lambda *_: restart.set())
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        while not stop.is_set():
            if restart.wait(0.5):
                restart.clear()
                stack.kill("control")
                stack.start_control()
                print("control restarted", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        stack.down()


if __name__ == "__main__":
    main()
