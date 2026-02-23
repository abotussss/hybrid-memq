#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import statistics
import time
import urllib.request
from pathlib import Path
from typing import Dict, List, Tuple


def post_json(url: str, payload: Dict, headers: Dict[str, str] | None = None) -> Dict:
    data = json.dumps(payload).encode("utf-8")
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, method="POST", data=data, headers=h)
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def read_jsonl(path: Path) -> List[Dict]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


def tok_est(s: str) -> int:
    return max(1, math.ceil(len(s) / 4))


def compile_memctx(items: List[Dict], budget: int = 120) -> str:
    lines = ["[MEMCTX v1]", f"budget_tokens={budget}", "surface:", "deep:"]
    used = sum(tok_est(x) for x in lines)
    for it in items:
        facts = it.get("facts", [])
        fact_line = ",".join([f"{f.get('k','note')}={str(f.get('v',''))[:60]}" for f in facts[:4]])
        line = (
            f"  - id={it.get('id')} type={it.get('type','note')} "
            f"conf={float(it.get('confidence',0.7)):.2f} imp={float(it.get('importance',0.5)):.2f} "
            f"facts=[{fact_line}]"
        )
        t = tok_est(line)
        if used + t > budget:
            break
        lines.append(line)
        used += t
    lines.extend([
        "rules:",
        "  - do=keep_polite_jp",
        "  - do=avoid_extra_suggestions",
        "notes:",
        "  - if_conflict=prefer_higher_imp_then_recent",
    ])
    return "\n".join(lines)


def percentile95(xs: List[float]) -> float:
    if not xs:
        return 0.0
    ys = sorted(xs)
    return ys[min(len(ys) - 1, int(len(ys) * 0.95))]


def call_llm(base_url: str, api_key: str, model: str, messages: List[Dict]) -> Tuple[Dict, float]:
    t0 = time.time()
    resp = post_json(
        f"{base_url.rstrip('/')}/chat/completions",
        {"model": model, "messages": messages, "temperature": 0.0},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    latency_ms = (time.time() - t0) * 1000
    return resp, latency_ms


def sidecar_search(sidecar: str, query: str, k: int) -> List[Dict]:
    emb = post_json(f"{sidecar}/embed", {"text": query})
    res = post_json(f"{sidecar}/index/search", {"vector": emb["vector"], "k": k})
    return res.get("items", [])


def build_baseline_context(items: List[Dict], max_chars: int = 2200) -> str:
    blocks = []
    used = 0
    for it in items:
        raw = (it.get("rawText") or "").strip()
        if not raw:
            continue
        chunk = f"- id={it.get('id')}\n{raw}\n"
        if used + len(chunk) > max_chars:
            break
        blocks.append(chunk)
        used += len(chunk)
    return "\n".join(blocks)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="bench/data/replay.jsonl")
    ap.add_argument("--output", default="bench/results/api_replay.csv")
    ap.add_argument("--sidecar", default="http://127.0.0.1:7781")
    ap.add_argument("--model", default=os.getenv("MEMQ_MODEL", "gpt-4.1-mini"))
    ap.add_argument("--base-url", default=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    ap.add_argument("--api-key", default=os.getenv("OPENAI_API_KEY", ""))
    ap.add_argument("--k", type=int, default=5)
    args = ap.parse_args()

    if not args.api_key:
        raise SystemExit("OPENAI_API_KEY (or --api-key) is required")

    items = read_jsonl(Path(args.dataset))
    rows = []

    for ex in items:
        q = ex["query"]
        retrieved = sidecar_search(args.sidecar, q, args.k)

        baseline_ctx = build_baseline_context(retrieved)
        baseline_msgs = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "system", "content": f"Memory context:\n{baseline_ctx}"},
            {"role": "user", "content": q},
        ]
        baseline_resp, baseline_lat = call_llm(args.base_url, args.api_key, args.model, baseline_msgs)
        b_usage = baseline_resp.get("usage", {})

        memctx = compile_memctx(retrieved, budget=120)
        memq_msgs = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "system", "content": memctx},
            {"role": "user", "content": q},
        ]
        memq_resp, memq_lat = call_llm(args.base_url, args.api_key, args.model, memq_msgs)
        m_usage = memq_resp.get("usage", {})

        rows.append(
            {
                "id": ex.get("id", ""),
                "query": q,
                "baseline_input_tokens": int(b_usage.get("prompt_tokens", 0)),
                "memq_input_tokens": int(m_usage.get("prompt_tokens", 0)),
                "baseline_output_tokens": int(b_usage.get("completion_tokens", 0)),
                "memq_output_tokens": int(m_usage.get("completion_tokens", 0)),
                "baseline_latency_ms": round(baseline_lat, 2),
                "memq_latency_ms": round(memq_lat, 2),
                "baseline_memctx_tokens_est": tok_est(baseline_ctx),
                "memq_memctx_tokens_est": tok_est(memctx),
            }
        )

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    b_in = [r["baseline_input_tokens"] for r in rows]
    m_in = [r["memq_input_tokens"] for r in rows]
    b_lat = [r["baseline_latency_ms"] for r in rows]
    m_lat = [r["memq_latency_ms"] for r in rows]

    summary = {
        "n": len(rows),
        "avg_baseline_input_tokens": round(statistics.mean(b_in), 2),
        "avg_memq_input_tokens": round(statistics.mean(m_in), 2),
        "input_token_reduction_pct": round((1 - (statistics.mean(m_in) / max(1, statistics.mean(b_in)))) * 100, 2),
        "avg_baseline_latency_ms": round(statistics.mean(b_lat), 2),
        "avg_memq_latency_ms": round(statistics.mean(m_lat), 2),
        "p95_baseline_latency_ms": round(percentile95(b_lat), 2),
        "p95_memq_latency_ms": round(percentile95(m_lat), 2),
        "output_csv": str(out_path),
    }

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
