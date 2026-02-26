from __future__ import annotations

import argparse
import os
import signal
import subprocess
import time
from pathlib import Path


STOP = False
CHILD: subprocess.Popen[bytes] | None = None


def _handle_stop(_signum, _frame) -> None:
    global STOP, CHILD
    STOP = True
    if CHILD and CHILD.poll() is None:
        try:
            CHILD.terminate()
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Hybrid MEMQ sidecar supervisor")
    parser.add_argument("--python", required=True, help="Python executable for minisidecar")
    parser.add_argument("--app", required=True, help="Path to minisidecar.py")
    parser.add_argument("--log", required=True, help="Log file path")
    parser.add_argument("--restart-delay-sec", type=float, default=1.0)
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    py = str(Path(args.python))
    app = str(Path(args.app))
    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    delay = max(0.2, float(args.restart_delay_sec))

    with log_path.open("a", encoding="utf-8") as logf:
        while not STOP:
            logf.write(f"[memq-supervisor] starting child: {py} {app}\n")
            logf.flush()
            global CHILD
            CHILD = subprocess.Popen(
                [py, app],
                stdin=subprocess.DEVNULL,
                stdout=logf,
                stderr=logf,
                env=env,
            )
            rc = CHILD.wait()
            logf.write(f"[memq-supervisor] child exited rc={rc}\n")
            logf.flush()
            CHILD = None
            if STOP:
                break
            time.sleep(delay)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

