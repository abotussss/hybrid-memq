from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from sidecar.memq.db import MemqDB
from sidecar.memq.ingest import _sanitize_turn_text, ingest_turn
from sidecar.memq.idle_consolidation import run_idle_consolidation
from sidecar.memq.memctx_pack import build_memctx
from sidecar.memq.retrieval_deep import NOISE_SUMMARY_RE as DEEP_NOISE_RE
from sidecar.memq.retrieval_surface import NOISE_SUMMARY_RE as SURFACE_NOISE_RE


class RegressionGuardsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "memq-test.sqlite3"
        self.db = MemqDB(self.db_path)

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def test_noise_regex_does_not_match_plain_text(self) -> None:
        self.assertIsNone(SURFACE_NOISE_RE.search(""))
        self.assertIsNone(DEEP_NOISE_RE.search(""))
        self.assertIsNone(SURFACE_NOISE_RE.search("hello world"))
        self.assertIsNone(DEEP_NOISE_RE.search("hello world"))

    def test_cleanup_patterns_reject_empty_matching_and_no_mass_delete(self) -> None:
        compiled = self.db._compile_safe_cleanup_patterns(["", "(?:|foo)", "foo", r"<MEMCTX>"])
        pats = [p.pattern for p in compiled]
        self.assertIn("foo", pats)
        self.assertIn(r"<MEMCTX>", pats)
        self.assertEqual(2, len(compiled))

        for i in range(80):
            self.db.add_memory_item(
                session_key="s1",
                layer="surface",
                text=f"normal text {i}",
                summary=f"normal text {i}",
                importance=0.5,
                tags={"kind": "turn"},
                emb_f16=None,
                emb_q=None,
                emb_dim=0,
                source="turn",
            )

        stats = self.db.cleanup_noisy_memory(max_delete=50)
        self.assertEqual(0, int(stats.get("removed_memory_items", 0)))
        # ensure existing memory survives cleanup path
        rows = self.db.list_memory_items("surface", "s1", limit=200)
        self.assertGreaterEqual(len(rows), 70)

    def test_memctx_clean_summary_not_character_spaced(self) -> None:
        self.db.upsert_conv_summary("s1", "surface_only", "hello world")
        out = build_memctx(
            db=self.db,
            session_key="s1",
            prompt="memory overview",
            surface=[],
            deep=[],
            budget_tokens=120,
        )
        self.assertNotIn("h e l l o", out)
        self.assertIn("hello world", out)

    def test_ingest_sanitize_keeps_normal_text(self) -> None:
        src = "Please remember my name is Hiro and I prefer concise Japanese answers."
        out = _sanitize_turn_text(src)
        self.assertIn("remember", out.lower())
        self.assertIn("hiro", out.lower())
        self.assertGreaterEqual(len(out), int(len(src) * 0.6))

    def test_ingest_respects_do_not_remember(self) -> None:
        now = int(time.time())
        wrote = ingest_turn(
            db=self.db,
            session_key="s1",
            user_text="妻はミナです。これは覚えなくていい。",
            assistant_text="了解です。",
            ts=now,
            dim=64,
            bits_per_dim=8,
        )
        self.assertEqual(0, int(wrote.get("deep", 0)))
        deep = self.db.list_memory_items("deep", "s1", limit=50)
        self.assertEqual(0, len(deep))

    def test_deep_ttl_is_materialized_and_enforced(self) -> None:
        self.db.upsert_memory_policy("ttl.default_days", "1", 0.95)
        now = int(time.time())
        wrote = ingest_turn(
            db=self.db,
            session_key="s1",
            user_text="妻はアヤです。",
            assistant_text="了解です。",
            ts=now,
            dim=64,
            bits_per_dim=8,
        )
        self.assertGreaterEqual(int(wrote.get("deep", 0)), 1)
        rows = self.db.list_memory_items("deep", "s1", limit=20)
        self.assertGreaterEqual(len(rows), 1)
        self.assertTrue(any(r["ttl_expires_at"] is not None for r in rows))
        self.db.conn.execute(
            "UPDATE memory_items SET ttl_expires_at=? WHERE layer='deep'",
            (now - 10,),
        )
        self.db.conn.commit()
        rows2 = self.db.list_memory_items("deep", "s1", limit=20)
        self.assertEqual(0, len(rows2))

    def test_fact_index_fetch_bypasses_recent_limit(self) -> None:
        target_id = self.db.add_memory_item(
            session_key="s1",
            layer="deep",
            text="家族: ペット=タロ",
            summary="家族: ペット=タロ | subject=user | conf=0.95 | src=user_msg | ttl=365d",
            importance=0.9,
            tags={
                "kind": "structured_fact",
                "fact_keys": ["profile.family.pet"],
                "fact": {
                    "fact_key": "profile.family.pet",
                    "value": "タロ",
                    "confidence": 0.95,
                    "source": "user_msg",
                    "ts": int(time.time()),
                },
            },
            emb_f16=None,
            emb_q=None,
            emb_dim=64,
            source="turn",
        )
        for i in range(5200):
            self.db.add_memory_item(
                session_key="s1",
                layer="deep",
                text=f"filler {i}",
                summary=f"filler {i}",
                importance=0.2,
                tags={"kind": "turn"},
                emb_f16=None,
                emb_q=None,
                emb_dim=64,
                source="turn",
            )
        self.db.conn.execute(
            "UPDATE memory_items SET updated_at=1,last_access_at=1 WHERE id=?",
            (target_id,),
        )
        self.db.conn.commit()
        recent = self.db.list_memory_items("deep", "s1", limit=5000)
        recent_ids = {str(r["id"]) for r in recent}
        self.assertNotIn(target_id, recent_ids)
        indexed = self.db.fetch_deep_items_by_fact_keys(
            session_key="s1",
            fact_keys=["profile.family.pet"],
            limit=32,
            include_global=True,
        )
        indexed_ids = {str(r["id"]) for r in indexed}
        self.assertIn(target_id, indexed_ids)

    def test_durable_global_fact_has_no_ttl(self) -> None:
        now = int(time.time())
        wrote = ingest_turn(
            db=self.db,
            session_key="s1",
            user_text="覚えて。妻はナナです。",
            assistant_text="了解です。",
            ts=now,
            dim=64,
            bits_per_dim=8,
        )
        self.assertGreaterEqual(int(wrote.get("deep", 0)), 2)
        rows = self.db.conn.execute(
            "SELECT ttl_expires_at,tags FROM memory_items WHERE layer='deep' AND session_key='global' ORDER BY updated_at DESC LIMIT 10"
        ).fetchall()
        self.assertGreaterEqual(len(rows), 1)
        self.assertTrue(any(r["ttl_expires_at"] is None for r in rows))

    def test_ephemeral_not_fixed_one_day_ttl(self) -> None:
        now = int(time.time())
        wrote = ingest_turn(
            db=self.db,
            session_key="s1",
            user_text="了解",
            assistant_text="OK",
            ts=now,
            dim=64,
            bits_per_dim=8,
        )
        self.assertGreaterEqual(int(wrote.get("ephemeral", 0)), 1)
        rows = self.db.conn.execute(
            "SELECT ttl_expires_at,importance FROM memory_items WHERE layer='ephemeral' AND session_key='s1' ORDER BY updated_at DESC LIMIT 1"
        ).fetchall()
        self.assertEqual(1, len(rows))
        self.assertIsNone(rows[0]["ttl_expires_at"])

    def test_idle_consolidation_profile_promotion_does_not_crash(self) -> None:
        self.db.upsert_style("persona", "calm_pragmatic")
        self.db.upsert_style("tone", "polite")
        self.db.upsert_style("callUser", "ヒロ")
        res = run_idle_consolidation(self.db, session_key="s1", dim=64, bits_per_dim=8)
        did = list(res.get("did", []))
        stats = dict(res.get("stats", {}))
        self.assertIn("promote_profile_facts", did)
        self.assertIn("profile_facts_promoted", stats)
        rows = self.db.conn.execute(
            "SELECT COUNT(*) AS c FROM memory_items WHERE layer='deep' AND session_key='global'"
        ).fetchone()
        self.assertGreaterEqual(int(rows["c"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
