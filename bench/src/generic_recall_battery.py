from __future__ import annotations

import json
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sidecar.memq.db import MemqDB
from sidecar.memq.ingest import ingest_turn
from sidecar.memq.idle_consolidation import run_idle_consolidation
from sidecar.memq.memctx_pack import build_memctx, estimate_tokens
from sidecar.memq.retrieval import retrieve_candidates


def _query_memctx(db: MemqDB, session_key: str, prompt: str) -> Dict[str, Any]:
    surf, deep, meta = retrieve_candidates(
        db=db,
        session_key=session_key,
        prompt=prompt,
        dim=256,
        bits_per_dim=8,
        top_k=5,
        surface_threshold=0.85,
        deep_enabled=True,
    )
    ctx = build_memctx(
        db=db,
        session_key=session_key,
        prompt=prompt,
        surface=surf,
        deep=deep,
        budget_tokens=120,
    )
    return {"memctx": ctx, "surface": surf, "deep": deep, "meta": meta}


def _contains_any(text: str, needles: List[str]) -> bool:
    t = text.lower()
    return any(n.lower() in t for n in needles)


def _is_empty_material(memctx: str) -> bool:
    payload = [ln for ln in memctx.splitlines() if "=" in ln and not ln.startswith("budget_tokens=") and not ln.startswith("q=")]
    if not payload:
        return True
    for ln in payload:
        k = ln.split("=", 1)[0]
        if k.startswith(("wm.", "p.", "t.", "s", "d", "g", "e", "convsurf", "convdeep")):
            return False
    return True


def run_battery() -> Dict[str, Any]:
    now = int(time.time())
    with tempfile.TemporaryDirectory() as td:
        db = MemqDB(Path(td) / "memq-battery.sqlite3")
        session = "battery-s1"

        seed_turns = [
            (now - 86400 * 2 - 1800, "覚えて。俺の名前はヒロ。家族構成は妻ミナと子ども2人。ヒロって呼んで。", "了解。記憶したよ。"),
            (now - 86400 * 2 - 1200, "口調は丁寧だけど親しみのあるスタイルで。", "わかった、口調を合わせるよ。"),
            (now - 86400 - 2400, "昨日はREADME更新とテスト修正を進めた。", "進捗を記録したよ。"),
            (now - 86400 - 1200, "昨日はベンチも回して結果を確認した。", "ベンチ結果も記録したよ。"),
            (now - 3600, "今日はOpenClaw連携の確認を進めた。", "連携確認の内容を記録したよ。"),
        ]

        for ts, u, a in seed_turns:
            ingest_turn(
                db=db,
                session_key=session,
                user_text=u,
                assistant_text=a,
                ts=ts,
                dim=256,
                bits_per_dim=8,
                metadata={
                    "actionSummaries": [
                        "tool_call:edit README.md",
                        "tool_call:run unittest",
                    ]
                },
            )

        # Add noise turns so pruning/reconstruction paths are exercised.
        for i in range(30):
            ts = now - 1800 + i * 30
            ingest_turn(
                db=db,
                session_key=session,
                user_text=f"雑談ターン{i}: 今日はどう？",
                assistant_text=f"雑談応答{i}: 元気だよ。",
                ts=ts,
                dim=256,
                bits_per_dim=8,
            )

        run_idle_consolidation(db, session_key=session, dim=256, bits_per_dim=8)

        cases = [
            {"category": "profile", "prompt": "俺の名前覚えてる？", "needles": ["ヒロ", "p.snapshot", "profile"]},
            {"category": "profile", "prompt": "家族構成は？", "needles": ["家族", "ミナ", "子ども"]},
            {"category": "identity", "prompt": "君は誰？", "needles": ["p.snapshot", "persona", "callUser"]},
            {"category": "timeline", "prompt": "昨日何した？", "needles": ["t.range=", "t.digest", "t.ev"]},
            {"category": "timeline", "prompt": "最近の要点まとめて", "needles": ["t.recent", "wm.surf", "wm.deep"]},
            {"category": "overview", "prompt": "これまでの記憶を要約して", "needles": ["wm.surf", "wm.deep", "t.recent"]},
            {"category": "state", "prompt": "今の進捗は？", "needles": ["wm.surf", "wm.deep"]},
            {"category": "fact", "prompt": "呼び方の設定は？", "needles": ["callUser", "呼称", "p.snapshot"]},
        ]

        rows: List[Dict[str, Any]] = []
        by_cat = defaultdict(lambda: {"n": 0, "success": 0, "empty_material": 0, "memctx_tokens_sum": 0})
        for c in cases:
            out = _query_memctx(db, session, c["prompt"])
            ctx = str(out["memctx"])
            if c["category"] == "timeline":
                ok = ("t.range=" in ctx) and (("t.digest=" in ctx) or ("t.ev1=" in ctx))
            else:
                ok = _contains_any(ctx, c["needles"])
            empty = _is_empty_material(ctx)
            tks = estimate_tokens(ctx)
            rows.append(
                {
                    "category": c["category"],
                    "prompt": c["prompt"],
                    "success": bool(ok),
                    "empty_material": bool(empty),
                    "memctx_tokens": int(tks),
                    "surface_count": len(out["surface"]),
                    "deep_count": len(out["deep"]),
                }
            )
            s = by_cat[c["category"]]
            s["n"] += 1
            s["success"] += 1 if ok else 0
            s["empty_material"] += 1 if empty else 0
            s["memctx_tokens_sum"] += int(tks)

        summary = {
            "total_cases": len(rows),
            "success_rate": sum(1 for r in rows if r["success"]) / max(1, len(rows)),
            "empty_material_rate": sum(1 for r in rows if r["empty_material"]) / max(1, len(rows)),
            "avg_memctx_tokens": sum(int(r["memctx_tokens"]) for r in rows) / max(1, len(rows)),
            "by_category": {
                k: {
                    "n": int(v["n"]),
                    "success_rate": float(v["success"]) / max(1, int(v["n"])),
                    "empty_material_rate": float(v["empty_material"]) / max(1, int(v["n"])),
                    "avg_memctx_tokens": float(v["memctx_tokens_sum"]) / max(1, int(v["n"])),
                }
                for k, v in sorted(by_cat.items())
            },
        }

        db.close()

    return {"summary": summary, "cases": rows}


def main() -> None:
    result = run_battery()
    out_dir = ROOT / "bench" / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "generic_recall_battery.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    s = result["summary"]
    print(
        "generic_recall_battery: "
        f"cases={s['total_cases']} success_rate={s['success_rate']:.3f} "
        f"empty_material_rate={s['empty_material_rate']:.3f} avg_memctx_tokens={s['avg_memctx_tokens']:.1f}"
    )
    print(f"generic_recall_battery: wrote {out_path}")


if __name__ == "__main__":
    main()
