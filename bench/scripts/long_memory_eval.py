#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import math
import random
import re
import statistics
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple


def tok_est(s: str) -> int:
    return max(1, math.ceil(len(s) / 4))


def embed_text(text: str, dim: int = 256) -> List[float]:
    h = hashlib.sha256(text.encode("utf-8")).digest()
    arr = [((h[i % len(h)] - 127.5) / 127.5) for i in range(dim)]
    n = math.sqrt(sum(x * x for x in arr)) or 1.0
    return [x / n for x in arr]


def cos(a: List[float], b: List[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


@dataclass
class MemoryItem:
    id: str
    raw_text: str
    facts: Dict[str, str]
    emb: List[float]
    importance: float
    confidence: float
    strength: float
    vol: str
    ts: int
    access_count: int = 0
    last_access: int = 0


class SurfaceLRU:
    def __init__(self, max_size: int):
        self.max = max_size
        self.map: OrderedDict[str, MemoryItem] = OrderedDict()

    def touch(self, item: MemoryItem):
        if item.id in self.map:
            self.map.pop(item.id)
        self.map[item.id] = item
        while len(self.map) > self.max:
            self.map.popitem(last=False)

    def top(self, qemb: List[float], k: int, key: str | None = None) -> List[MemoryItem]:
        cand = list(self.map.values())
        def score(x: MemoryItem) -> float:
            lexical = 1.0 if key and x.facts.get("target_key") == key else 0.0
            return cos(qemb, x.emb) + 1.2 * lexical
        cand.sort(key=score, reverse=True)
        return cand[:k]


def compile_memctx(surface: List[MemoryItem], deep: List[MemoryItem], budget: int = 120) -> str:
    lines = ["[MEMCTX v1]", f"budget_tokens={budget}", "surface:"]
    used = sum(tok_est(x) for x in lines)

    def add_items(items: List[MemoryItem]):
        nonlocal used
        for it in items:
            fact_line = ",".join(f"{k}={v}" for k, v in list(it.facts.items())[:4])
            line = f"  - id={it.id} conf={it.confidence:.2f} imp={it.importance:.2f} facts=[{fact_line}]"
            t = tok_est(line)
            if used + t > budget:
                break
            lines.append(line)
            used += t

    add_items(surface)
    lines.append("deep:")
    used += tok_est("deep:")
    add_items(deep)
    lines.append("rules:")
    for r in ["do=keep_polite_jp", "do=avoid_extra_suggestions", "do=prefer_surface_then_deep"]:
        t = tok_est(r)
        if used + t > budget:
            break
        lines.append(f"  - {r}")
        used += t
    return "\n".join(lines)


def extract_target_key(query: str) -> str | None:
    m = re.search(r"key_\d{6}", query)
    return m.group(0) if m else None


def make_long_text(i: int, target: str, topic: str, length_chars: int) -> str:
    seed = (
        f"memory_id={i} topic={topic} target={target} "
        "policy=surface_deep_evaporation quant=scalar_int8 constraint=keep_polite_jp "
        "goal=token_minimize objective=context_persistence "
    )
    body = (seed + "details=" + ("lorem ipsum openclaw memq " * 30)).strip()
    s = []
    while len(" ".join(s)) < length_chars:
        s.append(body)
    return "\n".join([
        f"title: record {i}",
        f"topic: {topic}",
        f"target_key: {target}",
        f"constraint: keep_polite_jp",
        f"goal: token_minimize",
        "summary: " + " ".join(s)[:length_chars],
    ])


def generate_dataset(
    n_mem: int,
    n_queries: int,
    long_chars: int,
    seed: int = 7,
    reuse_prob: float = 0.0
) -> Tuple[List[MemoryItem], List[Tuple[str, str]]]:
    rnd = random.Random(seed)
    topics = ["architecture", "security", "planning", "identity", "preferences", "constraints"]
    mem: List[MemoryItem] = []
    for i in range(n_mem):
        target = f"key_{i:06d}"
        topic = topics[i % len(topics)]
        raw = make_long_text(i, target, topic, long_chars)
        facts = {
            "topic": topic,
            "target_key": target,
            "goal": "token_minimize",
            "constraint": "keep_polite_jp",
        }
        mem.append(
            MemoryItem(
                id=f"m{i:06d}",
                raw_text=raw,
                facts=facts,
                emb=embed_text(raw),
                importance=0.65 + 0.25 * rnd.random(),
                confidence=0.7 + 0.25 * rnd.random(),
                strength=0.5,
                vol="medium",
                ts=i,
            )
        )

    queries: List[Tuple[str, str]] = []
    last_idx = None
    for _ in range(n_queries):
        if last_idx is not None and rnd.random() < reuse_prob:
            idx = last_idx
        else:
            idx = rnd.randrange(0, n_mem)
            last_idx = idx
        key = f"key_{idx:06d}"
        q = f"target_key={key} の記憶を要約して。余計な提案なしで。"
        queries.append((q, f"m{idx:06d}"))
    return mem, queries


def deep_search(mem: List[MemoryItem], query: str, qemb: List[float], topm: int, topk: int) -> List[MemoryItem]:
    key = extract_target_key(query)
    scored = [(cos(qemb, m.emb), m) for m in mem]
    scored.sort(key=lambda x: x[0], reverse=True)
    top = [m for _, m in scored[:topm]]
    if key:
        key_hits = [m for m in mem if m.facts.get("target_key") == key]
        if key_hits:
            seen = {x.id for x in top}
            for m in key_hits:
                if m.id not in seen:
                    top.append(m)
    # activation-like rerank
    reranked = []
    now = time.time()
    for m in top:
        recency = math.exp(-(max(0.0, now - m.last_access) / (2 * 24 * 3600))) if m.last_access else 0.1
        freq = math.log(1 + m.access_count)
        lexical = 1.0 if key and m.facts.get("target_key") == key else 0.0
        score = 1.0 * cos(qemb, m.emb) + 1.3 * lexical + 0.4 * recency + 0.2 * freq + 0.6 * m.importance
        reranked.append((score, m))
    reranked.sort(key=lambda x: x[0], reverse=True)
    return [m for _, m in reranked[:topk]]


def run_eval(
    n_mem: int,
    n_queries: int,
    long_chars: int,
    surface_max: int,
    budget: int,
    topm: int,
    topk: int,
    reuse_prob: float
):
    mem, queries = generate_dataset(n_mem, n_queries, long_chars, reuse_prob=reuse_prob)
    surf = SurfaceLRU(surface_max)

    baseline_tokens = []
    memq_tokens = []
    base_lat = []
    memq_lat = []
    deep_calls = 0
    surface_hits = 0
    h1 = 0
    h3 = 0
    h5 = 0

    for q, gold in queries:
        qemb = embed_text(q)
        qkey = extract_target_key(q)

        t0 = time.perf_counter()
        base_deep = deep_search(mem, q, qemb, topm, 5)
        base_ctx = "\n\n".join(x.raw_text for x in base_deep)
        base_lat.append((time.perf_counter() - t0) * 1000)
        baseline_tokens.append(tok_est(base_ctx))

        t1 = time.perf_counter()
        s = surf.top(qemb, 3, key=qkey)
        key_surface_hit = any((qkey is not None and x.facts.get("target_key") == qkey) for x in s)
        best_surface_sim = cos(qemb, s[0].emb) if s else -1.0
        if s and (best_surface_sim > 0.72 or key_surface_hit):
            surface_hits += 1
        deep = []
        if (not s) or (best_surface_sim <= 0.72 and not key_surface_hit):
            deep = deep_search(mem, q, qemb, topm, topk)
            deep_calls += 1
        memctx = compile_memctx(s, deep, budget=budget)
        memq_lat.append((time.perf_counter() - t1) * 1000)
        memq_tokens.append(tok_est(memctx))

        ranked = (s + deep)[:5]
        ids = [x.id for x in ranked]
        if gold in ids[:1]:
            h1 += 1
        if gold in ids[:3]:
            h3 += 1
        if gold in ids[:5]:
            h5 += 1

        for m in ranked[:5]:
            m.access_count += 1
            m.last_access = time.time()
            m.strength = min(1.0, m.strength + 0.1)
            surf.touch(m)

    return {
        "n_mem": n_mem,
        "n_queries": n_queries,
        "long_chars": long_chars,
        "avg_baseline_tokens": statistics.mean(baseline_tokens),
        "avg_memq_tokens": statistics.mean(memq_tokens),
        "token_reduction_pct": (1 - statistics.mean(memq_tokens) / max(1.0, statistics.mean(baseline_tokens))) * 100,
        "avg_baseline_latency_ms": statistics.mean(base_lat),
        "avg_memq_latency_ms": statistics.mean(memq_lat),
        "p95_baseline_latency_ms": sorted(base_lat)[min(len(base_lat)-1, int(len(base_lat)*0.95))],
        "p95_memq_latency_ms": sorted(memq_lat)[min(len(memq_lat)-1, int(len(memq_lat)*0.95))],
        "deep_call_rate": deep_calls / n_queries,
        "surface_hit_rate": surface_hits / n_queries,
        "hit_at_1": h1 / n_queries,
        "hit_at_3": h3 / n_queries,
        "hit_at_5": h5 / n_queries,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-mem", type=int, default=5000)
    ap.add_argument("--n-queries", type=int, default=1200)
    ap.add_argument("--long-chars", type=int, default=6000)
    ap.add_argument("--surface-max", type=int, default=120)
    ap.add_argument("--budget", type=int, default=120)
    ap.add_argument("--topm", type=int, default=200)
    ap.add_argument("--topk", type=int, default=5)
    ap.add_argument("--reuse-prob", type=float, default=0.0)
    ap.add_argument("--output", default="bench/results/long_memory_eval.csv")
    args = ap.parse_args()

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    row = run_eval(
        args.n_mem,
        args.n_queries,
        args.long_chars,
        args.surface_max,
        args.budget,
        args.topm,
        args.topk,
        args.reuse_prob,
    )

    with open(args.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        w.writeheader()
        w.writerow(row)

    print(row)
    print(f"wrote: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
