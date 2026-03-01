from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sidecar.memq.db import MemqDB
from sidecar.memq.ingest import _sanitize_turn_text
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
