#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import statistics
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib import error, request


ROOT = Path(__file__).resolve().parents[2]
BASE = os.getenv("MEMQ_BASE_URL", "http://127.0.0.1:7781").rstrip("/")
ITERS = max(3, int(os.getenv("MEMQ_STABILITY_ITERS", "6")))


def http_json(
    url: str,
    *,
    method: str = "GET",
    payload: Dict[str, Any] | None = None,
    timeout: int = 30,
) -> Tuple[int, Dict[str, Any], str]:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = request.Request(url, data=data, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", errors="ignore")
            try:
                obj = json.loads(body) if body else {}
            except Exception:
                obj = {}
            return int(getattr(r, "status", 200) or 200), obj if isinstance(obj, dict) else {}, body
    except error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        try:
            obj = json.loads(body) if body else {}
        except Exception:
            obj = {}
        return int(e.code or 500), obj if isinstance(obj, dict) else {}, body
    except Exception as e:
        return 599, {"ok": False, "error": f"{type(e).__name__}:{e}"}, ""


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def as_int(v: Any, d: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return d


def wait_health(timeout_sec: int = 40) -> Dict[str, Any]:
    end = time.time() + timeout_sec
    while time.time() < end:
        code, obj, _ = http_json(f"{BASE}/health", timeout=5)
        if code == 200 and bool(obj.get("ok")):
            return obj
        time.sleep(0.4)
    raise AssertionError("sidecar /health not ready")


def _ops(stats: Dict[str, Any], op: str) -> Dict[str, int]:
    o = ((stats.get("ops") or {}).get(op) or {})
    return {"ok": as_int(o.get("ok"), 0), "err": as_int(o.get("err"), 0), "total": as_int(o.get("total"), 0)}


def main() -> None:
    out_path = ROOT / "bench" / "results" / "brain_required_stability.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    started = int(time.time())
    result: Dict[str, Any] = {"ok": False, "base": BASE, "iters": ITERS, "started_at": started}

    health = wait_health(40)
    cfg = health.get("config") or {}
    _assert(str(cfg.get("brainMode") or "").lower() == "required", f"brainMode must be required, got={cfg.get('brainMode')}")
    _assert(str(cfg.get("brainModel") or "") == "gpt-oss:20b", f"brainModel must be gpt-oss:20b, got={cfg.get('brainModel')}")

    code, ensure, body = http_json(f"{BASE}/brain/ensure?sessionKey=brain-stability", method="POST", timeout=20)
    _assert(code == 200 and bool(ensure.get("ok")), f"/brain/ensure failed status={code} body={body[:200]}")
    _assert(bool(ensure.get("seen")), f"/brain/ensure seen=false: {json.dumps(ensure, ensure_ascii=False)}")

    _, stats_before, _ = http_json(f"{BASE}/brain/stats", timeout=10)
    before = {
        "ingest_plan": _ops(stats_before, "ingest_plan"),
        "recall_plan": _ops(stats_before, "recall_plan"),
        "merge_plan": _ops(stats_before, "merge_plan"),
    }

    session = f"brain_stability_{started}"
    failures: List[Dict[str, Any]] = []
    latencies_ms: List[int] = []
    ingest_trace_ids: List[str] = []
    recall_trace_ids: List[str] = []

    query_prompts = [
        "今までの進捗を短く整理して。",
        "昨日から今日にかけて何をしたか時系列で要約して。",
        "今後の呼称・口調・言語ルールを確認して。",
        "家族構成と検索方針と期限を確認して。",
    ]
    ingest_pairs = [
        (
            "呼び方はヒロ。一人称は僕。口調は丁寧。日本語優先で英語も許可。",
            "了解。スタイルとルールを反映する。",
        ),
        (
            "覚えて。締切は2026-04-20。検索はBrave優先。未完はOpenClaw統合検証。",
            "了解。深層記憶に記録する。",
        ),
        (
            "覚えて。家族構成は妻ともこ、犬おこげ。",
            "了解。家族情報を更新した。",
        ),
        (
            "昨日は仕様整理、今日はproof強化とログ検証を進めた。",
            "了解。時系列イベントとして保持した。",
        ),
    ]

    for i in range(ITERS):
        ts = int(time.time()) - (ITERS - i) * 70
        u, a = ingest_pairs[i % len(ingest_pairs)]
        ingest_payload = {
            "sessionKey": session,
            "userText": u,
            "assistantText": a,
            "ts": ts,
            "metadata": {"actionSummaries": [f"iter={i}", "stability-run"]},
        }
        code, obj, body = http_json(f"{BASE}/memory/ingest_turn", method="POST", payload=ingest_payload, timeout=120)
        if not (code == 200 and bool(obj.get("ok")) and as_int((obj.get("wrote") or {}).get("brain"), 0) == 1):
            failures.append({"iter": i, "stage": "ingest_turn", "status": code, "body": body[:240]})
            continue
        ingest_trace_ids.append(str(obj.get("traceId") or ""))

        q = query_prompts[i % len(query_prompts)]
        query_payload = {
            "sessionKey": session,
            "prompt": q,
            "recentMessages": [{"role": "user", "text": q, "ts": ts}],
            "budgets": {"memctxTokens": 120, "rulesTokens": 80, "styleTokens": 120},
            "topK": 5,
            "surfaceThreshold": 0.85,
            "deepEnabled": True,
        }
        code, obj, body = http_json(f"{BASE}/memctx/query", method="POST", payload=query_payload, timeout=120)
        if not (code == 200 and bool(obj.get("ok"))):
            failures.append({"iter": i, "stage": "memctx_query", "status": code, "body": body[:240]})
            continue
        debug = ((obj.get("meta") or {}).get("debug") or {})
        if not (as_int(debug.get("brain_plan"), 0) == 1 and as_int(debug.get("ps_seen"), 0) == 1):
            failures.append({"iter": i, "stage": "memctx_debug", "debug": debug})
            continue
        latencies_ms.append(as_int(debug.get("brain_latency_ms"), 0))
        recall_trace_ids.append(str(obj.get("traceId") or ""))

        # Periodic idle merge to verify merge_plan stability under required mode.
        if i % 3 == 2:
            code, idle_obj, idle_body = http_json(
                f"{BASE}/idle/run_once",
                method="POST",
                payload={"nowTs": int(time.time()), "maxWorkMs": 4000},
                timeout=120,
            )
            if not (code == 200 and bool(idle_obj.get("ok")) and "brain_merge_plan" in [str(x) for x in (idle_obj.get("did") or [])]):
                failures.append({"iter": i, "stage": "idle_run_once", "status": code, "body": idle_body[:240]})

    _, stats_after, _ = http_json(f"{BASE}/brain/stats", timeout=10)
    after = {
        "ingest_plan": _ops(stats_after, "ingest_plan"),
        "recall_plan": _ops(stats_after, "recall_plan"),
        "merge_plan": _ops(stats_after, "merge_plan"),
    }
    deltas = {
        "ingest_ok_delta": after["ingest_plan"]["ok"] - before["ingest_plan"]["ok"],
        "recall_ok_delta": after["recall_plan"]["ok"] - before["recall_plan"]["ok"],
        "merge_ok_delta": after["merge_plan"]["ok"] - before["merge_plan"]["ok"],
    }

    _, trace_obj, _ = http_json(f"{BASE}/brain/trace/recent?n=400", timeout=20)
    traces = [x for x in (trace_obj.get("items") or []) if isinstance(x, dict)]
    scoped = [x for x in traces if str(x.get("session_key") or "") == session]
    trace_ops = {str(x.get("op") or "") for x in scoped}
    ps_seen_count = sum(
        1
        for x in scoped
        if str(x.get("op") or "").endswith("_plan") and bool(((x.get("ps_snapshot") or {}).get("seen")))
    )

    ok = True
    ok = ok and (len(failures) == 0)
    ok = ok and (deltas["ingest_ok_delta"] >= ITERS)
    ok = ok and (deltas["recall_ok_delta"] >= ITERS)
    ok = ok and ("ingest_plan" in trace_ops and "recall_plan" in trace_ops and "ingest_plan_apply" in trace_ops)
    ok = ok and (ps_seen_count >= max(2, ITERS))

    lat_summary = {
        "n": len(latencies_ms),
        "avg_ms": round(statistics.mean(latencies_ms), 2) if latencies_ms else 0,
        "p95_ms": round(sorted(latencies_ms)[max(0, int(len(latencies_ms) * 0.95) - 1)], 2) if latencies_ms else 0,
        "max_ms": max(latencies_ms) if latencies_ms else 0,
    }

    result.update(
        {
            "ok": ok,
            "session": session,
            "health": {"brainMode": cfg.get("brainMode"), "brainModel": cfg.get("brainModel")},
            "ensure": ensure,
            "failures": failures,
            "stats_before": before,
            "stats_after": after,
            "deltas": deltas,
            "latency": lat_summary,
            "trace_rows_scoped": len(scoped),
            "trace_ops": sorted(trace_ops),
            "trace_ps_seen_count": ps_seen_count,
            "ingest_trace_ids": ingest_trace_ids,
            "recall_trace_ids": recall_trace_ids,
            "finished_at": int(time.time()),
            "duration_sec": int(time.time()) - started,
        }
    )
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))

    _assert(ok, f"stability validation failed: {json.dumps(result, ensure_ascii=False)[:1200]}")


if __name__ == "__main__":
    main()
