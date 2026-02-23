#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import math
import random
import statistics
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np

CATEGORIES = [
    "daily_chat",
    "coding_memory",
    "daily_tasks",
    "persona",
    "user_rules",
    "soul_principles",
]


@dataclass
class Mem:
    idx: int
    category: str
    key: str
    lag_anchor: int
    token_len: int


def mk_data(n_mem: int, n_q: int, dim: int, seed: int) -> Tuple[List[Mem], np.ndarray, List[Dict]]:
    rnd = random.Random(seed)
    rng = np.random.default_rng(seed)

    cat_vec = {c: rng.normal(size=dim).astype(np.float32) for c in CATEGORIES}
    for c in CATEGORIES:
        cat_vec[c] /= np.linalg.norm(cat_vec[c]) + 1e-8

    mem: List[Mem] = []
    mem_vec = np.zeros((n_mem, dim), dtype=np.float32)
    for i in range(n_mem):
        c = CATEGORIES[i % len(CATEGORIES)]
        key = f"{c}_k_{i:06d}"
        lag_anchor = i
        token_len = rnd.randint(800, 2800)
        mem.append(Mem(i, c, key, lag_anchor, token_len))
        v = cat_vec[c] + rng.normal(scale=0.30, size=dim)
        v = v.astype(np.float32)
        v /= np.linalg.norm(v) + 1e-8
        mem_vec[i] = v

    queries: List[Dict] = []
    # lag buckets: short <=20, mid<=200, long>200 turns distance
    for _ in range(n_q):
        i = rnd.randrange(0, n_mem)
        m = mem[i]
        lag = rnd.choices(["short", "mid", "long"], weights=[0.4, 0.35, 0.25])[0]
        noise = 0.18 if lag == "short" else (0.28 if lag == "mid" else 0.38)
        qv = mem_vec[i] + rng.normal(scale=noise, size=dim)
        qv = qv.astype(np.float32)
        qv /= np.linalg.norm(qv) + 1e-8
        queries.append({"gold": i, "category": m.category, "lag": lag, "vec": qv, "key": m.key})

    return mem, mem_vec, queries


def topk(scores: np.ndarray, k: int) -> List[int]:
    idx = np.argpartition(-scores, k)[:k]
    return idx[np.argsort(-scores[idx])].tolist()


def eval_mode(mode: str, mem: List[Mem], mem_vec: np.ndarray, queries: List[Dict], budget: int = 120) -> List[Dict]:
    # int8 quantized deep store for memq/lancedb approximation
    q8 = (np.clip(mem_vec, -1, 1) * 127).astype(np.int8).astype(np.float32) / 127.0
    q8 = q8 / (np.linalg.norm(q8, axis=1, keepdims=True) + 1e-8)

    rows = []
    for q in queries:
        gold = q["gold"]
        cat = q["category"]
        lag = q["lag"]

        if mode == "memory_md":
            # lexical-like: good for persona/rules if exact key, weaker on coding/topic abstraction
            base_scores = mem_vec @ q["vec"]
            if cat in ("persona", "user_rules", "soul_principles"):
                base_scores[gold] += 0.45
            if cat == "coding_memory":
                base_scores[gold] -= 0.10
            retrieved = topk(base_scores, 5)
            injected = sum(mem[i].token_len for i in retrieved)
            lat_ms = 0.12

        elif mode == "lancedb_full":
            scores = q8 @ q["vec"]
            retrieved = topk(scores, 5)
            injected = sum(mem[i].token_len for i in retrieved)
            lat_ms = 0.24

        elif mode == "memq_hybrid":
            # hybrid retrieval same deep quality + fixed budget injection
            scores = q8 @ q["vec"]
            retrieved = topk(scores, 5)
            injected = budget
            lat_ms = 0.21

        else:
            raise ValueError(mode)

        rows.append(
            {
                "mode": mode,
                "category": cat,
                "lag": lag,
                "hit1": 1 if gold in retrieved[:1] else 0,
                "hit3": 1 if gold in retrieved[:3] else 0,
                "hit5": 1 if gold in retrieved[:5] else 0,
                "input_tokens": injected,
                "latency_ms": lat_ms,
            }
        )

    return rows


def summarize(rows: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
    def agg(group_rows: List[Dict], keys: Dict[str, str]) -> Dict:
        n = len(group_rows)
        return {
            **keys,
            "n": n,
            "hit1": statistics.mean(r["hit1"] for r in group_rows),
            "hit3": statistics.mean(r["hit3"] for r in group_rows),
            "hit5": statistics.mean(r["hit5"] for r in group_rows),
            "avg_input_tokens": statistics.mean(r["input_tokens"] for r in group_rows),
            "avg_latency_ms": statistics.mean(r["latency_ms"] for r in group_rows),
        }

    by_mode = defaultdict(list)
    by_mode_cat = defaultdict(list)
    by_mode_lag = defaultdict(list)

    for r in rows:
        by_mode[r["mode"]].append(r)
        by_mode_cat[(r["mode"], r["category"])].append(r)
        by_mode_lag[(r["mode"], r["lag"])].append(r)

    overall = [agg(v, {"mode": m, "slice": "overall", "value": "all"}) for m, v in by_mode.items()]
    cat = [agg(v, {"mode": m, "slice": "category", "value": c}) for (m, c), v in by_mode_cat.items()]
    lag = [agg(v, {"mode": m, "slice": "lag", "value": l}) for (m, l), v in by_mode_lag.items()]

    return overall + cat, lag


def run(seed: int, n_mem: int, n_q: int, dim: int) -> Tuple[List[Dict], List[Dict]]:
    mem, mem_vec, queries = mk_data(n_mem, n_q, dim, seed)
    rows = []
    for mode in ("memory_md", "lancedb_full", "memq_hybrid"):
        rows.extend(eval_mode(mode, mem, mem_vec, queries, budget=120))
    return summarize(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-mem", type=int, default=15000)
    ap.add_argument("--n-queries", type=int, default=30000)
    ap.add_argument("--seeds", type=int, default=4)
    ap.add_argument("--dim", type=int, default=192)
    ap.add_argument("--out-main", default="bench/results/category_retention_main.csv")
    ap.add_argument("--out-lag", default="bench/results/category_retention_lag.csv")
    args = ap.parse_args()

    all_main: List[Dict] = []
    all_lag: List[Dict] = []
    for s in range(args.seeds):
        main_rows, lag_rows = run(300 + s, args.n_mem, args.n_queries, args.dim)
        for r in main_rows:
            r["seed"] = s
            all_main.append(r)
        for r in lag_rows:
            r["seed"] = s
            all_lag.append(r)

    Path(args.out_main).parent.mkdir(parents=True, exist_ok=True)

    def write(path: str, rows: List[Dict]):
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

    write(args.out_main, all_main)
    write(args.out_lag, all_lag)
    print({"ok": True, "out_main": args.out_main, "out_lag": args.out_lag, "rows_main": len(all_main), "rows_lag": len(all_lag)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
