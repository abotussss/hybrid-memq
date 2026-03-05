#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib import error, request


ROOT = Path(__file__).resolve().parents[2]
BASE = os.getenv("MEMQ_BASE_URL", "http://127.0.0.1:7781").rstrip("/")


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


def as_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def wait_health(timeout_sec: int = 40) -> Dict[str, Any]:
    end = time.time() + max(5, int(timeout_sec))
    last: Dict[str, Any] = {}
    while time.time() < end:
        code, obj, _ = http_json(f"{BASE}/health", timeout=5)
        last = {"status": code, "obj": obj}
        if code == 200 and bool(obj.get("ok")):
            return obj
        time.sleep(0.5)
    raise AssertionError(f"/health failed after wait, last={json.dumps(last, ensure_ascii=False)}")


def _ops(stats: Dict[str, Any], op: str) -> Dict[str, int]:
    d = ((stats.get("ops") or {}).get(op) or {})
    return {
        "total": as_int(d.get("total"), 0),
        "ok": as_int(d.get("ok"), 0),
        "err": as_int(d.get("err"), 0),
    }


def main() -> None:
    out_path = ROOT / "bench" / "results" / "brain_complex_required_validation.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    result: Dict[str, Any] = {"ok": False, "base": BASE, "ts": int(time.time())}

    health = wait_health(40)
    cfg = health.get("config") or {}
    _assert(str(cfg.get("brainMode") or "").lower() == "required", f"brainMode must be required, got={cfg.get('brainMode')}")
    _assert(str(cfg.get("brainModel") or "") == "gpt-oss:20b", f"brainModel must be gpt-oss:20b, got={cfg.get('brainModel')}")

    code, ensure, body = http_json(
        f"{BASE}/brain/ensure?sessionKey=brain-complex-proof",
        method="POST",
        timeout=20,
    )
    _assert(code == 200 and bool(ensure.get("ok")), f"/brain/ensure failed status={code} body={body[:240]}")
    _assert(bool(ensure.get("seen")), f"/brain/ensure seen=false: {json.dumps(ensure, ensure_ascii=False)}")
    _assert(str(((ensure.get("ps") or {}).get("matched") or {}).get("model") or "") == "gpt-oss:20b", "ps matched model != gpt-oss:20b")

    _, stats_before, _ = http_json(f"{BASE}/brain/stats", timeout=10)
    ops_before = {
        "ingest_plan": _ops(stats_before, "ingest_plan"),
        "recall_plan": _ops(stats_before, "recall_plan"),
        "merge_plan": _ops(stats_before, "merge_plan"),
    }

    now = int(time.time())
    # Split events across yesterday/today to validate timeline reconstruction.
    ts_y0 = now - 36 * 3600
    ts_y1 = now - 28 * 3600
    ts_t0 = now - 3 * 3600
    ts_t1 = now - 2 * 3600
    ts_t2 = now - 1 * 3600
    session = f"brain_complex_{now}"

    turns: List[Dict[str, Any]] = [
        {
            "ts": ts_y0,
            "user": (
                "重要設定です。今後の呼び方はヒロ。口調は丁寧で落ち着いた実務調。"
                "回答は日本語を第一、英語も許可。余計な提案はしないで、要点先出し。"
            ),
            "assistant": "了解。呼称・口調・言語方針を反映する。",
            "meta": {"actionSummaries": ["style.update request parsed", "rules.update language+format"]},
        },
        {
            "ts": ts_y1,
            "user": (
                "覚えて。プロジェクトはHybrid MEMQ v2の本番化。"
                "目標は入力トークン削減と長期記憶の安定化。検索はBrave優先、"
                "締切は2026-04-20、未完タスクはOpenClaw統合検証とベンチ整備。"
            ),
            "assistant": "了解。計画・期限・検索方針・未完タスクを記録する。",
            "meta": {"actionSummaries": ["project.plan captured", "deadline captured"]},
        },
        {
            "ts": ts_t0,
            "user": (
                "覚えて。家族構成は妻ともこ、犬おこげ。"
                "短期で消すべき雑談は揮発で扱って。"
            ),
            "assistant": "了解。家族情報と記憶方針を反映する。",
            "meta": {"actionSummaries": ["family.profile update", "memory.policy update"]},
        },
        {
            "ts": ts_t1,
            "user": (
                "昨日は仕様整理、今日はrequiredモードのproof強化とtrace検証を進めた。"
                "次はOpenClaw側の検知ログを読みやすくする。"
            ),
            "assistant": "進捗を時系列イベントとして保存する。",
            "meta": {"actionSummaries": ["timeline update: spec->proof->log improvements"]},
        },
        {
            "ts": ts_t2,
            "user": "雑談: 昼ごはんはラーメン。これは長期記憶には要らない。",
            "assistant": "了解。低価値情報として扱う。",
            "meta": {"actionSummaries": ["ephemeral low-value note"]},
        },
    ]

    ingest_rows: List[Dict[str, Any]] = []
    for i, turn in enumerate(turns):
        payload = {
            "sessionKey": session,
            "userText": turn["user"],
            "assistantText": turn["assistant"],
            "ts": int(turn["ts"]),
            "metadata": turn.get("meta"),
        }
        code, obj, body = http_json(f"{BASE}/memory/ingest_turn", method="POST", payload=payload, timeout=90)
        _assert(code == 200 and bool(obj.get("ok")), f"ingest_turn[{i}] failed status={code} body={body[:300]}")
        wrote = obj.get("wrote") or {}
        _assert(as_int(wrote.get("brain"), 0) == 1, f"ingest_turn[{i}] brain write flag is not 1: {wrote}")
        _assert(str(obj.get("traceId") or ""), f"ingest_turn[{i}] traceId missing")
        ingest_rows.append({"idx": i, "traceId": obj.get("traceId"), "wrote": wrote})

    # Force consolidation once so digest/merge paths run under required mode.
    idle_once: Dict[str, Any] = {}
    idle_code = 0
    idle_body = ""
    for _ in range(3):
        idle_code, idle_once, idle_body = http_json(
            f"{BASE}/idle/run_once",
            method="POST",
            payload={"nowTs": now, "maxWorkMs": 4000},
            timeout=120,
        )
        if idle_code == 200 and bool(idle_once.get("ok")):
            break
        # Best effort: re-ensure model residency before retrying merge_plan.
        http_json(f"{BASE}/brain/ensure?sessionKey={session}", method="POST", timeout=20)
        time.sleep(0.8)
    _assert(idle_code == 200 and bool(idle_once.get("ok")), f"idle/run_once failed status={idle_code} body={idle_body[:300]}")
    did = [str(x) for x in (idle_once.get("did") or [])]
    _assert("brain_merge_plan" in did, f"idle/run_once missing brain_merge_plan, did={did}")
    idle_trace = str(idle_once.get("traceId") or "")
    _assert(idle_trace, "idle/run_once traceId missing")

    recent_messages = [{"role": "user", "text": t["user"], "ts": t["ts"]} for t in turns[-4:]]

    queries = [
        {
            "name": "style_and_rules",
            "prompt": "今後の呼称、口調、出力言語、禁止事項を短く確認して。",
        },
        {
            "name": "deep_profile_project",
            "prompt": "家族構成、プロジェクト目標、締切、検索方針、未完タスクを整理して。",
        },
        {
            "name": "timeline_yesterday",
            "prompt": "昨日から今日にかけて何を進めたか、時系列で要点を教えて。",
        },
    ]

    query_rows: List[Dict[str, Any]] = []
    for q in queries:
        payload = {
            "sessionKey": session,
            "prompt": q["prompt"],
            "recentMessages": recent_messages,
            "budgets": {"memctxTokens": 120, "rulesTokens": 80, "styleTokens": 120},
            "topK": 5,
            "surfaceThreshold": 0.85,
            "deepEnabled": True,
        }
        code, obj, body = http_json(f"{BASE}/memctx/query", method="POST", payload=payload, timeout=120)
        _assert(code == 200 and bool(obj.get("ok")), f"memctx/query[{q['name']}] failed status={code} body={body[:300]}")
        meta = obj.get("meta") or {}
        debug = meta.get("debug") or {}
        _assert(as_int(debug.get("brain_plan"), 0) == 1, f"memctx/query[{q['name']}] brain_plan != 1")
        _assert(as_int(debug.get("ps_seen"), 0) == 1, f"memctx/query[{q['name']}] ps_seen != 1")
        _assert(str(obj.get("traceId") or ""), f"memctx/query[{q['name']}] traceId missing")
        query_rows.append(
            {
                "name": q["name"],
                "traceId": obj.get("traceId"),
                "memrules": str(obj.get("memrules") or ""),
                "memstyle": str(obj.get("memstyle") or ""),
                "memctx": str(obj.get("memctx") or ""),
                "debug": debug,
            }
        )

    code, profile, body = http_json(f"{BASE}/profile", timeout=20)
    _assert(code == 200 and bool(profile.get("ok")), f"/profile failed status={code} body={body[:240]}")
    pref_profile = profile.get("preference_profile") or {}
    _assert(bool(pref_profile), "preference_profile is empty")

    q_style = next(x for x in query_rows if x["name"] == "style_and_rules")
    _assert("callUser=ヒロ" in q_style["memstyle"], "MEMSTYLE missing callUser=ヒロ")
    _assert("language.allowed=" in q_style["memrules"], "MEMRULES missing language.allowed")

    q_deep = next(x for x in query_rows if x["name"] == "deep_profile_project")
    deep_ctx = q_deep["memctx"]
    _assert("p.snapshot=" in deep_ctx, f"deep query missing p.snapshot: {deep_ctx[:320]}")
    expected_terms = ["家族構成", "ともこ", "おこげ", "token", "brave", "2026-04-20", "未完"]
    hit_terms = sum(1 for t in expected_terms if t.lower() in deep_ctx.lower())
    _assert(hit_terms >= 1, f"deep query memctx seems too weak; hit_terms={hit_terms} memctx={deep_ctx[:400]}")

    q_timeline = next(x for x in query_rows if x["name"] == "timeline_yesterday")
    tl_ctx = q_timeline["memctx"]
    _assert("t.range=" in tl_ctx, f"timeline query missing t.range: {tl_ctx[:240]}")
    _assert(("t.digest=" in tl_ctx) or ("t.ev" in tl_ctx), f"timeline query missing digest/events: {tl_ctx[:260]}")

    code, mem_stats, body = http_json(f"{BASE}/memory/stats", timeout=20)
    _assert(code == 200 and bool(mem_stats.get("ok")), f"/memory/stats failed status={code}")
    stats_payload = mem_stats.get("stats") or {}
    _assert(as_int(stats_payload.get("deep"), 0) >= 1, f"deep too small: {stats_payload}")

    code, deep_rows_resp, body = http_json(
        f"{BASE}/memory/list?layer=deep&sessionKey={session}&limit=50",
        timeout=20,
    )
    _assert(code == 200 and bool(deep_rows_resp.get("ok")), f"/memory/list deep failed status={code}")
    deep_rows = deep_rows_resp.get("items") or []
    _assert(len(deep_rows) >= 1, "no deep rows persisted for complex session")

    _, stats_after, _ = http_json(f"{BASE}/brain/stats", timeout=10)
    ops_after = {
        "ingest_plan": _ops(stats_after, "ingest_plan"),
        "recall_plan": _ops(stats_after, "recall_plan"),
        "merge_plan": _ops(stats_after, "merge_plan"),
    }
    ingest_delta = ops_after["ingest_plan"]["ok"] - ops_before["ingest_plan"]["ok"]
    recall_delta = ops_after["recall_plan"]["ok"] - ops_before["recall_plan"]["ok"]
    merge_delta = ops_after["merge_plan"]["ok"] - ops_before["merge_plan"]["ok"]
    _assert(ingest_delta >= len(turns), f"ingest_plan delta too small: {ingest_delta}")
    _assert(recall_delta >= len(queries), f"recall_plan delta too small: {recall_delta}")
    _assert(merge_delta >= 1, f"merge_plan delta too small: {merge_delta}")

    _, trace_recent, _ = http_json(f"{BASE}/brain/trace/recent?n=240", timeout=20)
    items = [x for x in (trace_recent.get("items") or []) if isinstance(x, dict)]
    scoped = [x for x in items if str(x.get("session_key") or "") == session]
    ops_seen = {str(x.get("op") or "") for x in scoped}
    _assert("ingest_plan" in ops_seen, "trace missing ingest_plan for complex session")
    _assert("recall_plan" in ops_seen, "trace missing recall_plan for complex session")
    _assert(any(str(x.get("op")) == "ingest_plan_apply" for x in scoped), "trace missing ingest_plan_apply")
    _assert(any(str(x.get("model") or "") == "gpt-oss:20b" for x in scoped), "trace missing model=gpt-oss:20b")
    _assert(any(bool(((x.get("ps_snapshot") or {}).get("seen"))) for x in scoped if str(x.get("op") or "").endswith("_plan")), "trace missing ps_snapshot.seen=true")

    result.update(
        {
            "ok": True,
            "session": session,
            "health": {"brainMode": cfg.get("brainMode"), "brainModel": cfg.get("brainModel")},
            "ensure": ensure,
            "ingest_rows": ingest_rows,
            "idle_once": {"did": did, "traceId": idle_trace},
            "queries": [
                {
                    "name": r["name"],
                    "traceId": r["traceId"],
                    "memrules_head": "\n".join(r["memrules"].splitlines()[:8]),
                    "memstyle_head": "\n".join(r["memstyle"].splitlines()[:8]),
                    "memctx_head": "\n".join(r["memctx"].splitlines()[:12]),
                    "debug": r["debug"],
                }
                for r in query_rows
            ],
            "preference_keys": sorted(pref_profile.keys()),
            "stats_delta": {
                "ingest_plan_ok_delta": ingest_delta,
                "recall_plan_ok_delta": recall_delta,
                "merge_plan_ok_delta": merge_delta,
            },
            "memory_stats": stats_payload,
            "deep_rows_n": len(deep_rows),
            "trace_scope_rows": len(scoped),
        }
    )
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
