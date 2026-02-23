#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import math
import random
import statistics
import time
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np


def tok_est(chars: int) -> int:
    return max(1, math.ceil(chars / 4))


def p95(xs: List[float]) -> float:
    if not xs:
        return 0.0
    ys = sorted(xs)
    return ys[min(len(ys) - 1, int(len(ys) * 0.95))]


@dataclass
class MemoryItem:
    idx: int
    key: str
    topic: str
    text_chars: int
    token_len: int
    importance: float
    confidence: float


class SurfaceLRU:
    def __init__(self, cap: int):
        self.cap = cap
        self.od: OrderedDict[int, None] = OrderedDict()

    def touch(self, ids: List[int]) -> None:
        if self.cap <= 0:
            return
        for i in ids:
            if i in self.od:
                self.od.pop(i)
            self.od[i] = None
            if len(self.od) > self.cap:
                self.od.popitem(last=False)

    def items(self) -> List[int]:
        return list(self.od.keys())


def make_dataset(n_mem: int, n_queries: int, dim: int, seed: int, reuse_prob: float) -> Tuple[List[MemoryItem], np.ndarray, List[Dict], Dict[str, List[int]]]:
    rnd = random.Random(seed)
    np_rng = np.random.default_rng(seed)

    topics = ["arch", "security", "plan", "identity", "prefs", "constraints", "ops", "code"]
    topic_vecs = {t: np_rng.normal(size=dim).astype(np.float32) for t in topics}
    for t in topics:
        v = topic_vecs[t]
        topic_vecs[t] = v / (np.linalg.norm(v) + 1e-8)

    mem: List[MemoryItem] = []
    mem_vec = np.zeros((n_mem, dim), dtype=np.float32)
    inv: Dict[str, List[int]] = defaultdict(list)

    for i in range(n_mem):
        topic = topics[i % len(topics)]
        key = f"key_{i:06d}"
        base = topic_vecs[topic]
        noise = np_rng.normal(scale=0.35, size=dim).astype(np.float32)
        v = base + noise
        v /= np.linalg.norm(v) + 1e-8
        mem_vec[i] = v

        text_chars = rnd.randint(5500, 9500)
        mem.append(
            MemoryItem(
                idx=i,
                key=key,
                topic=topic,
                text_chars=text_chars,
                token_len=tok_est(text_chars),
                importance=0.55 + 0.4 * rnd.random(),
                confidence=0.6 + 0.35 * rnd.random(),
            )
        )
        inv[key].append(i)
        inv[topic].append(i)

    queries: List[Dict] = []
    prev = None
    for _ in range(n_queries):
        if prev is not None and rnd.random() < reuse_prob:
            gi = prev
        else:
            gi = rnd.randrange(0, n_mem)
            prev = gi
        g = mem[gi]
        qtype = rnd.choices(["exact", "semantic", "topic"], weights=[0.45, 0.35, 0.20])[0]

        if qtype == "exact":
            text = f"{g.key} の内容を要約して。"
            qv = mem_vec[gi] + np_rng.normal(scale=0.08, size=dim)
        elif qtype == "semantic":
            text = f"{g.topic} の重要な制約を思い出して"
            qv = mem_vec[gi] + np_rng.normal(scale=0.20, size=dim)
        else:
            text = f"{g.topic} 関連の過去決定を教えて"
            qv = mem_vec[gi] + np_rng.normal(scale=0.28, size=dim)

        qv = qv.astype(np.float32)
        qv /= np.linalg.norm(qv) + 1e-8
        queries.append({"text": text, "gold": gi, "type": qtype, "vec": qv})

    return mem, mem_vec, queries, inv


def topk_indices(scores: np.ndarray, k: int) -> List[int]:
    if k >= len(scores):
        return list(np.argsort(-scores))
    idx = np.argpartition(-scores, k)[:k]
    return idx[np.argsort(-scores[idx])].tolist()


def mode_memory_md(mem: List[MemoryItem], inv: Dict[str, List[int]], q: Dict, k: int = 5) -> Tuple[List[int], int, float]:
    t0 = time.perf_counter()
    txt = q["text"]
    cands = []
    for token in txt.split():
        cands.extend(inv.get(token, []))
    if not cands:
        # naive fallback: random-like by topic map miss
        cands = list(range(min(50, len(mem))))
    uniq = list(dict.fromkeys(cands))[: max(k, 20)]
    if len(uniq) < k:
        uniq = (uniq + list(range(len(mem))))[:k]
    top = uniq[:k]
    latency = (time.perf_counter() - t0) * 1000
    injected = sum(mem[i].token_len for i in top)
    return top, injected, latency


def mode_lancedb(mem: List[MemoryItem], mem_vec: np.ndarray, q: Dict, k: int = 5) -> Tuple[List[int], int, float]:
    t0 = time.perf_counter()
    scores = mem_vec @ q["vec"]
    top = topk_indices(scores, k)
    latency = (time.perf_counter() - t0) * 1000
    injected = sum(mem[i].token_len for i in top)
    return top, injected, latency


def mode_memq_deep(mem: List[MemoryItem], mem_vec_q8: np.ndarray, q: Dict, budget_tokens: int = 120, k: int = 5) -> Tuple[List[int], int, float, bool, int]:
    t0 = time.perf_counter()
    scores = mem_vec_q8 @ q["vec"]
    top = topk_indices(scores, k)
    latency = (time.perf_counter() - t0) * 1000
    return top, budget_tokens, latency, True, 0


def mode_memq_surface(surface: SurfaceLRU, mem_vec_q8: np.ndarray, q: Dict, budget_tokens: int = 120, k: int = 5, threshold: float = 0.55) -> Tuple[List[int], int, float, bool, int]:
    t0 = time.perf_counter()
    surf_ids = surface.items()
    used_surface = 0
    if surf_ids:
        sub = mem_vec_q8[surf_ids]
        scores = sub @ q["vec"]
        sidx = topk_indices(scores, min(k, len(surf_ids)))
        top = [surf_ids[i] for i in sidx]
        best = float(scores[sidx[0]]) if sidx else -1.0
        deep_called = best <= threshold
        used_surface = 1 if (top and not deep_called) else 0
    else:
        top = []
        deep_called = True
    latency = (time.perf_counter() - t0) * 1000
    return top if used_surface else [], budget_tokens if used_surface else 0, latency, deep_called, used_surface


def mode_memq_hybrid(surface: SurfaceLRU, mem: List[MemoryItem], mem_vec_q8: np.ndarray, q: Dict, budget_tokens: int = 120, k: int = 5, threshold: float = 0.55) -> Tuple[List[int], int, float, bool, int]:
    t0 = time.perf_counter()
    surf_ids = surface.items()
    top_surface: List[int] = []
    best = -1.0
    if surf_ids:
        sub = mem_vec_q8[surf_ids]
        scores = sub @ q["vec"]
        sidx = topk_indices(scores, min(3, len(surf_ids)))
        top_surface = [surf_ids[i] for i in sidx]
        best = float(scores[sidx[0]]) if sidx else -1.0

    use_surface = bool(top_surface) and best > threshold
    deep_called = not use_surface
    deep: List[int] = []
    if deep_called:
        scores_d = mem_vec_q8 @ q["vec"]
        deep = topk_indices(scores_d, k)

    merged = []
    seen = set()
    for i in (top_surface if use_surface else []) + deep:
        if i not in seen:
            merged.append(i)
            seen.add(i)
        if len(merged) >= k:
            break

    latency = (time.perf_counter() - t0) * 1000
    return merged, budget_tokens if merged else 0, latency, deep_called, 1 if use_surface else 0


def eval_mode(name: str, mem: List[MemoryItem], mem_vec: np.ndarray, queries: List[Dict], inv: Dict[str, List[int]], surface_cap: int, budget_tokens: int) -> Dict[str, float]:
    # quantized deep vectors (int8 surrogate for compressed deep store)
    mem_vec_q8 = (np.clip(mem_vec, -1, 1) * 127).astype(np.int8).astype(np.float32) / 127.0
    norms = np.linalg.norm(mem_vec_q8, axis=1, keepdims=True) + 1e-8
    mem_vec_q8 = mem_vec_q8 / norms

    surface = SurfaceLRU(surface_cap)

    tokens, lats = [], []
    hit1 = hit3 = hit5 = 0
    deep_calls = 0
    surface_hits = 0

    for q in queries:
        if name == "memory_md":
            top, inj, lat = mode_memory_md(mem, inv, q, 5)
            deep_called = False
            surf_hit = 0
        elif name == "lancedb":
            top, inj, lat = mode_lancedb(mem, mem_vec, q, 5)
            deep_called = True
            surf_hit = 0
        elif name == "memq_surface":
            top, inj, lat, deep_called, surf_hit = mode_memq_surface(surface, mem_vec_q8, q, budget_tokens, 5)
            if deep_called:
                # pure surface mode doesn't fallback; keep no result
                top = top[:5]
        elif name == "memq_deep":
            top, inj, lat, deep_called, surf_hit = mode_memq_deep(mem, mem_vec_q8, q, budget_tokens, 5)
        elif name == "memq_hybrid":
            top, inj, lat, deep_called, surf_hit = mode_memq_hybrid(surface, mem, mem_vec_q8, q, budget_tokens, 5)
        else:
            raise ValueError(name)

        g = q["gold"]
        if g in top[:1]:
            hit1 += 1
        if g in top[:3]:
            hit3 += 1
        if g in top[:5]:
            hit5 += 1

        if top:
            # only retrieved ids become recent surface traces
            surface.touch(top[:3])

        tokens.append(inj)
        lats.append(lat)
        deep_calls += 1 if deep_called else 0
        surface_hits += surf_hit

    n = len(queries)
    acc = hit5 / n
    avg_tok = statistics.mean(tokens)
    return {
        "mode": name,
        "turns": n,
        "avg_input_tokens": avg_tok,
        "p95_input_tokens": p95(tokens),
        "avg_latency_ms": statistics.mean(lats),
        "p95_latency_ms": p95(lats),
        "deep_call_rate": deep_calls / n,
        "surface_hit_rate": surface_hits / n,
        "hit_at_1": hit1 / n,
        "hit_at_3": hit3 / n,
        "hit_at_5": hit5 / n,
        "context_efficiency": acc / (avg_tok / 1000.0 if avg_tok > 0 else 1.0),
    }


def run(seed: int, n_mem: int, n_queries: int, dim: int, reuse_prob: float, surface_cap: int, budget_tokens: int) -> List[Dict[str, float]]:
    mem, mem_vec, queries, inv = make_dataset(n_mem, n_queries, dim, seed, reuse_prob)
    modes = ["memory_md", "lancedb", "memq_surface", "memq_deep", "memq_hybrid"]
    return [eval_mode(m, mem, mem_vec, queries, inv, surface_cap, budget_tokens) for m in modes]


def aggregate(rows: List[Dict[str, float]]) -> List[Dict[str, float]]:
    by_mode: Dict[str, List[Dict[str, float]]] = defaultdict(list)
    for r in rows:
        by_mode[r["mode"]].append(r)

    out = []
    for mode, rs in by_mode.items():
        agg = {"mode": mode, "seeds": len(rs)}
        keys = [k for k in rs[0].keys() if k != "mode"]
        for k in keys:
            vals = [float(x[k]) for x in rs]
            agg[k] = statistics.mean(vals)
            agg[f"{k}_std"] = statistics.pstdev(vals) if len(vals) > 1 else 0.0
        out.append(agg)
    out.sort(key=lambda x: x["mode"])
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-mem", type=int, default=12000)
    ap.add_argument("--n-queries", type=int, default=6000)
    ap.add_argument("--dim", type=int, default=192)
    ap.add_argument("--reuse-prob", type=float, default=0.35)
    ap.add_argument("--surface-cap", type=int, default=120)
    ap.add_argument("--budget", type=int, default=120)
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--out", default="bench/results/mode_compare.csv")
    ap.add_argument("--out-raw", default="bench/results/mode_compare_raw.csv")
    args = ap.parse_args()

    t0 = time.time()
    raw: List[Dict[str, float]] = []
    for s in range(args.seeds):
        rows = run(seed=101 + s, n_mem=args.n_mem, n_queries=args.n_queries, dim=args.dim, reuse_prob=args.reuse_prob, surface_cap=args.surface_cap, budget_tokens=args.budget)
        for r in rows:
            r["seed"] = s
            raw.append(r)

    agg = aggregate(raw)

    out_raw = Path(args.out_raw)
    out_raw.parent.mkdir(parents=True, exist_ok=True)
    with out_raw.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(raw[0].keys()))
        w.writeheader()
        w.writerows(raw)

    out = Path(args.out)
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(agg[0].keys()))
        w.writeheader()
        w.writerows(agg)

    print({
        "ok": True,
        "out": str(out),
        "out_raw": str(out_raw),
        "elapsed_sec": round(time.time() - t0, 2),
        "n_mem": args.n_mem,
        "n_queries": args.n_queries,
        "seeds": args.seeds,
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
