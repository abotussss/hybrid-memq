#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import random
import statistics
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List


@dataclass
class Item:
    idx: int
    key: str
    value: str
    topic: str
    body: str


def build_items(n: int, seed: int) -> List[Item]:
    rnd = random.Random(seed)
    topics = ["arch", "security", "plan", "prefs", "identity", "ops"]
    items: List[Item] = []
    for i in range(n):
        key = f"key_{i:05d}"
        value = f"value_{rnd.randint(100000, 999999)}"
        topic = topics[i % len(topics)]
        paragraph = (
            f"id={i} topic={topic} key={key} value={value} "
            "goal=token_minimize constraint=keep_polite_jp detail="
            + ("openclaw memq memory retrieval compression " * 80)
        )
        items.append(Item(i, key, value, topic, paragraph))
    return items


def topk_for_query(items: List[Item], q: str, k: int = 5) -> List[Item]:
    # deterministic lexical retrieval (baseline + hybrid share same retriever output)
    key = None
    for tok in q.split():
        if tok.startswith("key_"):
            key = tok.strip("。、,. ")
            break
    scored = []
    for it in items:
        s = 0
        if key and it.key == key:
            s += 10
        if it.topic in q:
            s += 3
        if "value" in q:
            s += 1
        scored.append((s, it.idx))
    scored.sort(reverse=True)
    return [items[i] for _, i in scored[:k]]


def make_baseline_context(cands: List[Item]) -> str:
    return "\n\n".join(f"[MEM {i+1}] {c.body}" for i, c in enumerate(cands))


def make_memctx(cands: List[Item], budget_tokens: int = 120) -> str:
    lines: List[str] = []
    used = 0

    def push(line: str) -> bool:
        nonlocal used
        t = est_tokens(line)
        if used + t > budget_tokens:
            return False
        lines.append(line)
        used += t
        return True

    if not push("[MEMCTX v1]"):
        return "[MEMCTX v1]"
    if not push(f"budget_tokens={budget_tokens}"):
        return "\n".join(lines)
    if not push("surface:"):
        return "\n".join(lines)
    if not push("deep:"):
        return "\n".join(lines)

    for c in cands:
        line = f"  - id={c.idx} facts=[key={c.key},value={c.value},topic={c.topic},goal=token_minimize]"
        if not push(line):
            break
    if push("rules:"):
        push("  - do=keep_polite_jp")
        push("  - do=avoid_extra_suggestions")
    return "\n".join(lines)


def run_openclaw(message: str, session_id: str, timeout_sec: int = 90) -> Dict:
    cmd = [
        "openclaw",
        "agent",
        "--local",
        "--agent",
        "main",
        "--session-id",
        session_id,
        "--message",
        message,
        "--json",
    ]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
    if p.returncode != 0:
        raise RuntimeError(f"openclaw failed: {p.stderr[:500]}")

    text = p.stdout
    # find final JSON object from output
    i = text.find("{")
    if i < 0:
        raise RuntimeError(f"json not found in output: {text[:500]}")
    data = json.loads(text[i:])
    return data


def extract_answer(resp: Dict) -> str:
    payloads = resp.get("payloads", [])
    if not payloads:
        return ""
    return str(payloads[0].get("text", "")).strip()


def score_accuracy(answer: str, expected_value: str) -> int:
    return 1 if expected_value in answer else 0


def est_tokens(s: str) -> int:
    return (len(s) + 3) // 4


def one_turn(mode: str, items: List[Item], q_item: Item, turn_id: int, timeout_sec: int) -> Dict:
    q = f"{q_item.key} に対応する value を1語だけ返して。"
    cands = topk_for_query(items, q, 5)

    if mode == "baseline_full":
        ctx = make_baseline_context(cands)
        msg = (
            "以下は長期記憶全文です。\n"
            f"{ctx}\n\n"
            f"質問: {q}\n"
            "出力は value_XXXXXX 形式のみ。"
        )
        memory_payload_tokens_est = est_tokens(ctx)
        memctx_tokens_est = 0
    elif mode == "hybrid_memctx":
        memctx = make_memctx(cands, 120)
        msg = (
            f"{memctx}\n\n"
            f"質問: {q}\n"
            "出力は value_XXXXXX 形式のみ。"
        )
        memory_payload_tokens_est = est_tokens(memctx)
        memctx_tokens_est = est_tokens(memctx)
    else:
        raise ValueError(mode)
    message_tokens_est = est_tokens(msg)

    sid = f"bench-{mode}-{turn_id}"
    t0 = time.time()
    try:
        resp = run_openclaw(msg, sid, timeout_sec=timeout_sec)
    except Exception as e:
        return {
            "mode": mode,
            "turn": turn_id,
            "key": q_item.key,
            "expected": q_item.value,
            "answer": "",
            "correct": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "duration_ms": 0.0,
            "provider": "",
            "model": "",
            "memory_payload_tokens_est": memory_payload_tokens_est,
            "memctx_tokens_est": memctx_tokens_est,
            "message_tokens_est": message_tokens_est,
            "prompt_tokens": 0,
            "last_call_input_tokens": 0,
            "error": str(e)[:200],
        }
    wall_ms = (time.time() - t0) * 1000

    ans = extract_answer(resp)
    ok = score_accuracy(ans, q_item.value)
    meta = resp.get("meta", {})
    am = meta.get("agentMeta", {})
    usage = am.get("usage", {})
    last_call = am.get("lastCallUsage", {})

    return {
        "mode": mode,
        "turn": turn_id,
        "key": q_item.key,
        "expected": q_item.value,
        "answer": ans,
        "correct": ok,
        "input_tokens": int(usage.get("input", 0)),
        "output_tokens": int(usage.get("output", 0)),
        "duration_ms": float(meta.get("durationMs", wall_ms)),
        "provider": am.get("provider", ""),
        "model": am.get("model", ""),
        "memory_payload_tokens_est": memory_payload_tokens_est,
        "memctx_tokens_est": memctx_tokens_est,
        "message_tokens_est": message_tokens_est,
        "prompt_tokens": int(am.get("promptTokens", 0)),
        "last_call_input_tokens": int(last_call.get("input", 0)),
        "error": "",
    }


def summarize(rows: List[Dict]) -> List[Dict]:
    out = []
    for mode in sorted({r["mode"] for r in rows}):
        rs = [r for r in rows if r["mode"] == mode]
        out.append(
            {
                "mode": mode,
                "n": len(rs),
                "accuracy": statistics.mean(r["correct"] for r in rs),
                "avg_input_tokens": statistics.mean(r["input_tokens"] for r in rs),
                "p95_input_tokens": sorted(r["input_tokens"] for r in rs)[int(len(rs) * 0.95)],
                "avg_memory_payload_tokens_est": statistics.mean(r["memory_payload_tokens_est"] for r in rs),
                "avg_memctx_tokens_est": statistics.mean(r["memctx_tokens_est"] for r in rs),
                "avg_message_tokens_est": statistics.mean(r["message_tokens_est"] for r in rs),
                "avg_prompt_tokens": statistics.mean(r["prompt_tokens"] for r in rs),
                "avg_last_call_input_tokens": statistics.mean(r["last_call_input_tokens"] for r in rs),
                "avg_duration_ms": statistics.mean(r["duration_ms"] for r in rs),
                "p95_duration_ms": sorted(r["duration_ms"] for r in rs)[int(len(rs) * 0.95)],
            }
        )
    return out


def write_csv(path: Path, rows: List[Dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        return
    keys = list(rows[0].keys())
    import csv

    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-mem", type=int, default=300)
    ap.add_argument("--n-queries", type=int, default=120)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--timeout-sec", type=int, default=90)
    ap.add_argument("--out-detail", default="bench/results/prod_like_detail.csv")
    ap.add_argument("--out-summary", default="bench/results/prod_like_summary.csv")
    args = ap.parse_args()

    items = build_items(args.n_mem, args.seed)
    rnd = random.Random(args.seed + 7)
    picks = [items[rnd.randrange(0, len(items))] for _ in range(args.n_queries)]

    rows: List[Dict] = []
    detail_path = Path(args.out_detail)
    summary_path = Path(args.out_summary)
    detail_path.parent.mkdir(parents=True, exist_ok=True)
    turn = 0
    for qi, q_item in enumerate(picks, start=1):
        turn += 1
        rows.append(one_turn("baseline_full", items, q_item, turn, args.timeout_sec))
        turn += 1
        rows.append(one_turn("hybrid_memctx", items, q_item, turn, args.timeout_sec))
        if qi % 5 == 0:
            write_csv(detail_path, rows)
            print(f"progress {qi}/{len(picks)}", flush=True)

    summary = summarize(rows)
    write_csv(detail_path, rows)
    write_csv(summary_path, summary)

    print(json.dumps({"ok": True, "detail": args.out_detail, "summary": args.out_summary, "summary_rows": summary}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
