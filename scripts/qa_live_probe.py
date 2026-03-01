#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib import request


ROOT = Path(__file__).resolve().parents[1]
PORT = 7785
BASE = f"http://127.0.0.1:{PORT}"


def http_get(path: str) -> dict:
    with request.urlopen(BASE + path, timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


def http_post(path: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = request.Request(BASE + path, data=data, headers={"Content-Type": "application/json"})
    with request.urlopen(req, timeout=8) as r:
        return json.loads(r.read().decode("utf-8"))


def wait_health(timeout_sec: int = 20) -> bool:
    until = time.time() + timeout_sec
    while time.time() < until:
        try:
            j = http_get("/health")
            if j.get("ok"):
                return True
        except Exception:
            time.sleep(0.25)
    return False


def main() -> None:
    venv_py = ROOT / "sidecar/.venv/bin/python"
    py = str(venv_py) if venv_py.exists() else sys.executable
    log_path = ROOT / ".memq/qa_live_probe_sidecar.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fp = open(log_path, "wb")
    proc = subprocess.Popen(
        [
            py,
            "-m",
            "uvicorn",
            "minisidecar:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(PORT),
            "--app-dir",
            str(ROOT / "sidecar"),
        ],
        cwd=str(ROOT),
        env=os.environ.copy(),
        stdout=log_fp,
        stderr=log_fp,
    )
    try:
        if not wait_health(20):
            log_fp.flush()
            try:
                snippet = log_path.read_text(encoding="utf-8", errors="ignore")[-4000:]
            except Exception:
                snippet = "<no log>"
            raise RuntimeError(f"sidecar health timeout\n{snippet}")

        idle_once = {}
        try:
            idle_once = http_post("/idle/run_once", {"nowTs": int(time.time())})
        except Exception:
            idle_once = {"ok": False}

        stats = http_get("/memory/stats")
        profile = http_get("/profile")
        memctx = http_post(
            "/memctx/query",
            {
                "sessionKey": "agent:main:main",
                "prompt": "これまでの重要な記憶を要点で示して",
                "recentMessages": [{"role": "user", "text": "これまでの重要な記憶を要点で示して"}],
                "budgets": {"memctxTokens": 120, "rulesTokens": 80, "styleTokens": 120},
                "topK": 5,
                "surfaceThreshold": 0.85,
                "deepEnabled": True,
            },
        )
        memctx_text = str(memctx.get("memctx", ""))
        forbidden = [
            "<MEMRULES v1>",
            "<MEMSTYLE v1>",
            "<MEMCTX v1>",
            "[MEMRULES v1]",
            "[MEMSTYLE v1]",
            "[MEMCTX v1]",
            "[[reply_to_current]]",
            "workspace context",
            "Read HEARTBEAT.md",
            "read heartbeat.md",
        ]
        hit = [x for x in forbidden if x in memctx_text]
        if hit:
            raise RuntimeError(f"memctx contains forbidden noise markers: {hit}")
        out = {
            "ok": True,
            "stats": stats.get("stats", {}),
            "idle_once": idle_once,
            "profile_keys": sorted((profile.get("preference_profile") or {}).keys()),
            "memctx_head": "\n".join(memctx_text.splitlines()[:12]),
            "memstyle_head": "\n".join(str(memctx.get("memstyle", "")).splitlines()[:10]),
            "memrules_head": "\n".join(str(memctx.get("memrules", "")).splitlines()[:10]),
            "meta": memctx.get("meta", {}),
        }
        # Capability probes
        probes = {
            "family": "家族構成を教えて",
            "persona": "あなたの人格設定を要約して",
            "recent": "10分前に覚えたことを要約して",
        }
        probe_out = {}
        for k, q in probes.items():
            probe_req = request.Request(
                BASE + "/memctx/query",
                data=json.dumps(
                    {
                        "sessionKey": "agent:main:direct:615082923691081759",
                        "prompt": q,
                        "recentMessages": [{"role": "user", "text": q}],
                        "budgets": {"memctxTokens": 120, "rulesTokens": 80, "styleTokens": 120},
                        "topK": 5,
                        "surfaceThreshold": 0.85,
                        "deepEnabled": True,
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with request.urlopen(probe_req, timeout=8) as r2:
                pj = json.loads(r2.read().decode("utf-8"))
            probe_out[k] = {
                "memctx_head": "\n".join(str(pj.get("memctx", "")).splitlines()[:8]),
                "used_ids": list((pj.get("meta") or {}).get("usedMemoryIds") or []),
            }
        out["probes"] = probe_out
        print(json.dumps(out, ensure_ascii=False, indent=2))
    finally:
        try:
            log_fp.close()
        except Exception:
            pass
        try:
            proc.send_signal(signal.SIGTERM)
        except Exception:
            pass
        try:
            proc.wait(timeout=4)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


if __name__ == "__main__":
    main()
