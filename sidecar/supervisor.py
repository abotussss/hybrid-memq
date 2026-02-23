#!/usr/bin/env python3
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = ROOT / ".memq"
STATE_DIR.mkdir(parents=True, exist_ok=True)
CHILD_PID_FILE = STATE_DIR / "minisidecar.child.pid"

_stop = False


def _on_signal(signum, _frame):
    global _stop
    _stop = True
    print(f"[memq-supervisor] received signal={signum}, shutting down", flush=True)


def _write_child_pid(pid: int | None) -> None:
    if pid is None:
        try:
            CHILD_PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        return
    CHILD_PID_FILE.write_text(f"{pid}\n", encoding="utf-8")


def main() -> int:
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    configured = os.getenv("MEMQ_SIDECAR_PYTHON", "").strip()
    candidates = [
        configured,
        "/opt/homebrew/bin/python3",
        "/usr/bin/python3",
        shutil.which("python3") or "",
        sys.executable or "",
    ]
    python = ""
    for c in candidates:
        if c and Path(c).exists():
            python = c
            break
    if not python:
        python = "python3"
    cmd = [python, str(ROOT / "sidecar" / "minisidecar.py")]

    restart_count = 0
    while not _stop:
        try:
            print(f"[memq-supervisor] starting sidecar: {' '.join(cmd)}", flush=True)
            child = subprocess.Popen(cmd, cwd=str(ROOT), env=os.environ.copy())
            _write_child_pid(child.pid)
            rc = child.wait()
            _write_child_pid(None)
            if _stop:
                break
            restart_count += 1
            delay = min(10, 1 + restart_count)
            print(f"[memq-supervisor] sidecar exited rc={rc}, restart in {delay}s", flush=True)
            time.sleep(delay)
        except Exception as e:
            restart_count += 1
            delay = min(10, 1 + restart_count)
            print(f"[memq-supervisor] launch error={e}, retry in {delay}s", flush=True)
            time.sleep(delay)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
