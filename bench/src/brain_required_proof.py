#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import socket
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Tuple


def http_json(url: str, method: str = "GET", payload: Dict[str, Any] | None = None, timeout: float = 75.0) -> Tuple[int, Dict[str, Any] | None, str]:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            obj = None
            try:
                obj = json.loads(body) if body else {}
            except Exception:
                obj = None
            return int(resp.getcode() or 200), obj, body
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        obj = None
        try:
            obj = json.loads(body) if body else {}
        except Exception:
            obj = None
        return int(e.code or 500), obj, body
    except (urllib.error.URLError, TimeoutError, socket.timeout) as e:
        return 599, None, str(e)
    except Exception as e:
        return 599, None, str(e)


def get_op_count(stats: Dict[str, Any], op: str) -> int:
    ops = stats.get("ops") or {}
    item = ops.get(op) or {}
    return int(item.get("total", 0) or 0)


def wait_health(base: str, timeout_sec: int = 25) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout_sec:
        code, obj, _ = http_json(f"{base}/health")
        if code == 200 and isinstance(obj, dict) and obj.get("ok"):
            return True
        time.sleep(0.5)
    return False


def count_global_ok_ops(trace_path: Path, *, model: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    if not trace_path.exists():
        return out
    for line in trace_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if not isinstance(row, dict):
            continue
        if str(row.get("model") or "") != model:
            continue
        if not bool(row.get("ok")):
            continue
        op = str(row.get("op") or "").strip()
        if not op:
            continue
        out[op] = int(out.get(op, 0)) + 1
    return out


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    out_path = root / "bench" / "results" / "brain_required_proof.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    base = os.getenv("MEMQ_SIDECAR_BASE", "http://127.0.0.1:7781")
    model = os.getenv("MEMQ_BRAIN_MODEL", "gpt-oss:20b")
    req_timeout = float(os.getenv("MEMQ_PROOF_TIMEOUT_SEC", "150"))
    session = f"brain_required_proof_{int(time.time())}"
    target_ingest = int(os.getenv("MEMQ_PROOF_TARGET_INGEST", "10"))
    target_recall = int(os.getenv("MEMQ_PROOF_TARGET_RECALL", "10"))
    target_merge = int(os.getenv("MEMQ_PROOF_TARGET_MERGE", "1"))
    min_delta_ingest = max(0, int(os.getenv("MEMQ_PROOF_MIN_DELTA_INGEST", "1")))
    min_delta_recall = max(0, int(os.getenv("MEMQ_PROOF_MIN_DELTA_RECALL", "1")))
    min_delta_merge = max(0, int(os.getenv("MEMQ_PROOF_MIN_DELTA_MERGE", "1")))

    result: Dict[str, Any] = {
        "ok": False,
        "base": base,
        "model": model,
        "session": session,
        "assertions": {},
        "notes": [],
    }

    if not wait_health(base):
        result["notes"].append("sidecar health check failed")
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2

    code, health, _ = http_json(f"{base}/health")
    if code != 200 or not isinstance(health, dict):
        result["notes"].append("invalid health response")
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2

    brain_mode = str(((health.get("config") or {}).get("brainMode") or ""))
    result["brain_mode"] = brain_mode

    _, stats_before, _ = http_json(f"{base}/brain/stats", timeout=req_timeout)
    stats_before = stats_before or {}
    trace_path = Path((stats_before.get("trace_path") or str(root / ".memq" / "brain_trace.jsonl")))
    global_before = count_global_ok_ops(trace_path, model=model)

    ingest_prompts = [
        ("僕の名前はヒロ。", "了解、ヒロ。"),
        ("家族構成は妻ミナと子ども2人。", "家族情報を保持するね。"),
        ("昨日はMEMQの検証をした。", "昨日の作業として記録した。"),
        ("最近はOpenClawの設定を調整してる。", "最近の進捗として整理した。"),
        ("呼び方はヒロで固定して。", "呼称を維持する。"),
    ]

    ingest_needed = max(min_delta_ingest, max(0, target_ingest - int(global_before.get("ingest_plan", 0))))
    recall_needed = max(min_delta_recall, max(0, target_recall - int(global_before.get("recall_plan", 0))))
    merge_needed = max(min_delta_merge, max(0, target_merge - int(global_before.get("merge_plan", 0))))

    for i in range(ingest_needed):
        u, a = ingest_prompts[i % len(ingest_prompts)]
        payload = {
            "sessionKey": session,
            "userText": u,
            "assistantText": a,
            "ts": int(time.time()),
            "metadata": {"actionSummaries": [f"turn:{i}"]},
        }
        code, obj, body = http_json(f"{base}/memory/ingest_turn", method="POST", payload=payload, timeout=req_timeout)
        if code != 200:
            result["notes"].append(f"ingest failed i={i} code={code} body={body[:240]}")
            out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 3
        if not isinstance(obj, dict) or not obj.get("ok"):
            result["notes"].append(f"ingest invalid response i={i}")
            out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 3

    simple_queries = [
        "昨日何した？",
        "最近の要点を教えて",
        "君は誰？",
        "家族構成を教えて",
        "前回の進捗を教えて",
    ]
    queries = [simple_queries[i % len(simple_queries)] for i in range(recall_needed)]

    for q in queries:
        payload = {
            "sessionKey": session,
            "prompt": q,
            "recentMessages": [{"role": "user", "text": q, "ts": int(time.time())}],
            "budgets": {"memctxTokens": 120, "rulesTokens": 80, "styleTokens": 120},
            "topK": 3,
        }
        code, obj, body = http_json(f"{base}/memctx/query", method="POST", payload=payload, timeout=req_timeout)
        if code != 200:
            result["notes"].append(f"query failed q={q} code={code} body={body[:240]}")
            out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 4
        if not isinstance(obj, dict) or not obj.get("ok"):
            result["notes"].append(f"query invalid response q={q}")
            out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 4

    idle_skipped = False
    if merge_needed > 0:
        code, idle_obj, idle_body = http_json(
            f"{base}/idle/run_once",
            method="POST",
            payload={"nowTs": int(time.time()), "maxWorkMs": 1000},
            timeout=req_timeout,
        )
        if code != 200:
            result["notes"].append(f"idle failed code={code} body={idle_body[:240]}")
            out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 5
    else:
        idle_skipped = True

    _, stats_after, _ = http_json(f"{base}/brain/stats", timeout=req_timeout)
    stats_after = stats_after or {}
    _, trace_recent, _ = http_json(f"{base}/brain/trace/recent?n=240", timeout=req_timeout)
    traces = (trace_recent or {}).get("items") if isinstance(trace_recent, dict) else []
    if not isinstance(traces, list):
        traces = []

    _, ps_obj, _ = http_json("http://127.0.0.1:11434/api/ps", timeout=req_timeout)
    models = (ps_obj or {}).get("models") if isinstance(ps_obj, dict) else []
    if not isinstance(models, list):
        models = []

    ingest_delta = get_op_count(stats_after, "ingest_plan") - get_op_count(stats_before, "ingest_plan")
    recall_delta = get_op_count(stats_after, "recall_plan") - get_op_count(stats_before, "recall_plan")
    merge_delta = get_op_count(stats_after, "merge_plan") - get_op_count(stats_before, "merge_plan")
    global_after = count_global_ok_ops(trace_path, model=model)

    trace_for_session = [t for t in traces if isinstance(t, dict) and str(t.get("session_key") or "") == session]
    trace_with_model = [t for t in trace_for_session if str(t.get("model") or "") == model]
    trace_with_ps = [
        t
        for t in trace_for_session
        if isinstance(t.get("ps_snapshot"), dict) and bool((t.get("ps_snapshot") or {}).get("seen"))
    ]

    model_seen_in_ps = False
    for m in models:
        if not isinstance(m, dict):
            continue
        name = str(m.get("name") or "")
        mdl = str(m.get("model") or "")
        if model == name or model == mdl or model in name or model in mdl:
            model_seen_in_ps = True
            break

    # failure test with isolated sidecar instance and broken Ollama URL
    fail_test = {"ok": False, "code": None, "body": "", "error": ""}
    fail_port = int(os.getenv("MEMQ_FAIL_TEST_PORT", "8791"))
    fail_py = os.getenv("MEMQ_FAIL_TEST_PYTHON", "")
    if not fail_py:
        venv_py = root / "sidecar" / ".venv" / "bin" / "python"
        fail_py = str(venv_py) if venv_py.exists() else sys.executable
    proc = None
    try:
        env = os.environ.copy()
        env.update(
            {
                "MEMQ_ROOT": str(root),
                "MEMQ_DB_PATH": ".memq/brain_required_fail.sqlite3",
                "MEMQ_BRAIN_MODE": "required",
                "MEMQ_BRAIN_ENABLED": "1",
                "MEMQ_BRAIN_PROVIDER": "ollama",
                "MEMQ_BRAIN_BASE_URL": "http://127.0.0.1:59999",
                "MEMQ_BRAIN_MODEL": model,
                "MEMQ_BRAIN_TIMEOUT_MS": "1500",
            }
        )
        cmd = [fail_py, "-m", "uvicorn", "minisidecar:app", "--host", "127.0.0.1", "--port", str(fail_port)]
        proc = subprocess.Popen(cmd, cwd=str(root / "sidecar"), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        fail_base = f"http://127.0.0.1:{fail_port}"
        if wait_health(fail_base, timeout_sec=45):
            payload = {
                "sessionKey": "fail_closed_test",
                "prompt": "昨日何した？",
                "recentMessages": [{"role": "user", "text": "昨日何した？", "ts": int(time.time())}],
                "budgets": {"memctxTokens": 120, "rulesTokens": 80, "styleTokens": 120},
                "topK": 5,
            }
            code, obj, body = http_json(f"{fail_base}/memctx/query", method="POST", payload=payload, timeout=req_timeout)
            fail_test["code"] = code
            fail_test["body"] = body[:300]
            fail_test["ok"] = code == 503
            if isinstance(obj, dict):
                fail_test["resp"] = obj
        else:
            fail_test["error"] = "temporary sidecar health timeout"
            if proc is not None and proc.stderr is not None:
                try:
                    fail_test["stderr"] = (proc.stderr.read() or b"").decode("utf-8", errors="ignore")[-400:]
                except Exception:
                    pass
    except Exception as e:
        fail_test["error"] = str(e)
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except Exception:
                proc.kill()

    trace_required = (ingest_needed + recall_needed + merge_needed) > 0
    assertions = {
        "brain_mode_required": brain_mode == "required",
        "global_ingest_plan_ge_target": int(global_after.get("ingest_plan", 0)) >= target_ingest,
        "global_recall_plan_ge_target": int(global_after.get("recall_plan", 0)) >= target_recall,
        "global_merge_plan_ge_target": int(global_after.get("merge_plan", 0)) >= target_merge,
        "delta_ingest_ge_min": ingest_delta >= min_delta_ingest,
        "delta_recall_ge_min": recall_delta >= min_delta_recall,
        "delta_merge_ge_min": merge_delta >= min_delta_merge,
        "trace_has_model": (len(trace_with_model) >= 1) if trace_required else True,
        "trace_has_ps_seen": (len(trace_with_ps) >= 1) if trace_required else True,
        "ollama_ps_has_model": model_seen_in_ps,
        "fail_closed_returns_503": bool(fail_test.get("ok")),
    }

    result.update(
        {
            "stats_before": stats_before,
            "stats_after": stats_after,
            "delta": {
                "ingest_plan": ingest_delta,
                "recall_plan": recall_delta,
                "merge_plan": merge_delta,
            },
            "targets": {
                "ingest": target_ingest,
                "recall": target_recall,
                "merge": target_merge,
            },
            "min_delta": {
                "ingest": min_delta_ingest,
                "recall": min_delta_recall,
                "merge": min_delta_merge,
            },
            "needed": {
                "ingest": ingest_needed,
                "recall": recall_needed,
                "merge": merge_needed,
            },
            "idle_skipped": idle_skipped,
            "global_before": global_before,
            "global_after": global_after,
            "trace_session_count": len(trace_for_session),
            "trace_model_count": len(trace_with_model),
            "trace_ps_seen_count": len(trace_with_ps),
            "ollama_ps_models_n": len(models),
            "fail_test": fail_test,
            "assertions": assertions,
        }
    )

    ok = all(bool(v) for v in assertions.values())
    result["ok"] = ok
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if ok else 6


if __name__ == "__main__":
    raise SystemExit(main())
    req_timeout = float(os.getenv("MEMQ_PROOF_TIMEOUT_SEC", "150"))
