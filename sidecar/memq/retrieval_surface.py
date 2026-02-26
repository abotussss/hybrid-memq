from __future__ import annotations

import math
from typing import Any, Dict, List

import numpy as np

from .db import MemqDB
from .quant import dequantize, dot, from_f16_blob


def _score(sim: float, importance: float, usage_count: int, age_sec: int) -> float:
    recency = math.exp(-max(0, age_sec) / 172800.0)
    freq = math.log1p(max(0, usage_count))
    return sim + 0.45 * recency + 0.15 * freq + 0.5 * float(importance)


def search_surface(db: MemqDB, session_key: str, qvec: np.ndarray, top_k: int, bits: int) -> List[Dict[str, Any]]:
    rows = db.list_memory_items("surface", session_key, limit=2000)
    now = __import__("time").time()
    scored: List[Dict[str, Any]] = []
    for r in rows:
        emb = None
        if r["emb_f16"]:
            emb = from_f16_blob(r["emb_f16"], int(r["emb_dim"]))
        elif r["emb_q"]:
            emb = dequantize(r["emb_q"], int(r["emb_dim"]), bits)
        if emb is None:
            continue
        sim = dot(qvec, emb)
        age = int(now - int(r["last_access_at"]))
        s = _score(sim, float(r["importance"]), int(r["usage_count"]), age)
        scored.append(
            {
                "id": str(r["id"]),
                "score": float(s),
                "sim": float(sim),
                "summary": str(r["summary"]),
                "layer": "surface",
                "importance": float(r["importance"]),
            }
        )
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[: max(1, top_k)]
