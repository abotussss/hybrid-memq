from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from sidecar.memq.audit import audit_output
from sidecar.memq.brain.schemas import BrainIngestPlan, BrainMergePlan, BrainRecallPlan
from sidecar.memq.brain.service import BrainService
from sidecar.memq.config import AuditConfig, BrainConfig, Budgets, Config
from sidecar.memq.db import MemqDB, SearchResult
from sidecar.memq.idle_consolidation import run_idle_consolidation
from sidecar.memq.memctx_pack import build_memctx, build_memrules, build_memstyle
from sidecar.memq.retrieval import retrieve_with_plan


class FakeMergeBrain:
    def __init__(self) -> None:
        self.called = False

    async def build_merge_plan(self, *, session_key: str, candidate_groups: list[dict]):
        self.called = True
        first = candidate_groups[0]["items"]
        target = int(first[0]["id"])
        source = [int(item["id"]) for item in first[1:]]
        return BrainMergePlan.model_validate(
            {
                "merges": [{"target_id": target, "source_ids": source, "merged_summary": "merged summary"}],
                "prunes": [],
            }
        ), "trace-merge", {}

    def apply_merge_plan(self, db: MemqDB, *, session_key: str, plan: BrainMergePlan):
        return BrainService(_cfg(Path.cwd())).apply_merge_plan(db, session_key=session_key, plan=plan)


class RegressionV3Test(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = MemqDB(self.root / "memq_v3.sqlite3")
        self.cfg = _cfg(self.root)

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def test_apply_ingest_plan_updates_style_from_explicit_request(self) -> None:
        svc = BrainService(self.cfg)
        try:
            plan = BrainIngestPlan.model_validate(
                {
                    "facts": [
                        {
                            "fact_key": "persona",
                            "value": "ロックマンEXEのロックマン",
                            "confidence": 0.9,
                            "layer": "surface",
                            "evidence_quote": "ロックマンEXEのロックマンとして話して",
                        },
                        {
                            "fact_key": "callUser",
                            "value": "ヒロ",
                            "confidence": 0.9,
                            "layer": "surface",
                            "evidence_quote": "ヒロって呼んで",
                        },
                    ],
                    "events": ["style requested"],
                }
            )
            wrote = svc.apply_ingest_plan(
                self.db,
                session_key="s1",
                plan=plan,
                ts=int(time.time()),
                user_text="今後はロックマンEXEのロックマンとして話して。僕のことはヒロって呼んで。",
            )
            style = self.db.list_style("s1")
            self.assertGreaterEqual(wrote["style"], 2)
            self.assertEqual("ロックマンEXEのロックマン", style.get("persona"))
            self.assertEqual("ヒロ", style.get("callUser"))
        finally:
            asyncio.run(svc.close())

    def test_apply_ingest_plan_updates_rules_only_for_explicit_safe_keys(self) -> None:
        svc = BrainService(self.cfg)
        try:
            plan = BrainIngestPlan.model_validate(
                {
                    "rules_update": {
                        "apply": True,
                        "explicit": True,
                        "rules": {
                            "language.allowed": "ja,en",
                            "persona": "bad",
                        },
                    }
                }
            )
            wrote = svc.apply_ingest_plan(self.db, session_key="s1", plan=plan, ts=int(time.time()), user_text="今後は日本語と英語を許可して")
            rules = self.db.list_rules("s1")
            self.assertEqual(1, wrote["rules"])
            self.assertEqual("ja,en", rules.get("language.allowed"))
            self.assertNotIn("persona", rules)
        finally:
            asyncio.run(svc.close())

    def test_retrieve_with_plan_uses_global_profile_fallback(self) -> None:
        self.db.insert_memory(
            session_key="global",
            layer="deep",
            kind="carry",
            fact_key="profile.identity.card",
            value="僕はロックマンEXEのロックマン",
            text="僕はロックマンEXEのロックマン",
            summary="profile.identity.card:僕はロックマンEXEのロックマン",
            confidence=0.95,
            importance=0.9,
            strength=0.9,
        )
        plan = BrainRecallPlan.model_validate(
            {
                "intent": {"profile": 0.9},
                "fact_keys": ["profile.identity.card"],
                "fts_queries": ["君は誰 ロックマン"],
                "budget_split": {"profile": 60, "timeline": 20, "surface": 20, "deep": 20, "ephemeral": 0},
            }
        )
        bundle = retrieve_with_plan(self.db, session_key="s1", plan=plan)
        self.assertGreaterEqual(len(bundle.deep), 1)
        self.assertEqual("profile.identity.card", bundle.deep[0].fact_key)

    def test_build_memctx_prioritizes_timeline_when_timeline_intent_is_high(self) -> None:
        plan = BrainRecallPlan.model_validate(
            {
                "intent": {"timeline": 0.95, "profile": 0.1, "state": 0.0, "fact": 0.0, "overview": 0.0},
                "time_range": {"start_day": "2026-03-05", "end_day": "2026-03-05", "label": "yesterday"},
            }
        )
        bundle = type("Bundle", (), {
            "surface": [],
            "deep": [],
            "timeline": [{"summary": "昨日はMEMSTYLE更新とtimeline整理をした"}],
            "anchors": {
                "wm.surf": "現在地: MEMQ v3 の検証中",
                "wm.deep": "長期方針: Brain required",
                "p.snapshot": "callUser:ヒロ | firstPerson:僕",
                "t.recent": "2026-03-05:- [progress] MEMSTYLE更新",
            },
        })()
        out = build_memctx(plan, bundle, 120)
        lines = [line for line in out.splitlines() if line and not line.startswith("budget_tokens=")]
        self.assertTrue(lines[0].startswith("t.recent=") or lines[0].startswith("t.range="))
        self.assertIn("t.digest=", out)
        self.assertIn("t.range=2026-03-05..2026-03-05", out)

    def test_build_memstyle_only_uses_style_fields(self) -> None:
        out = build_memstyle(
            {
                "firstPerson": "僕",
                "callUser": "ヒロ",
                "persona": "ロックマンEXEのロックマン",
                "tone": "polite",
                "security.never_output_secrets": "true",
            },
            120,
        )
        self.assertIn("firstPerson=僕", out)
        self.assertIn("callUser=ヒロ", out)
        self.assertIn("persona=ロックマンEXEのロックマン", out)
        self.assertNotIn("security.never_output_secrets=true", out)
        self.assertTrue(out.splitlines()[0].startswith("budget_tokens="))

    def test_build_memrules_rejects_style_keys(self) -> None:
        out = build_memrules(
            {
                "language.allowed": "ja,en",
                "persona": "bad",
            },
            80,
        )
        self.assertIn("language.allowed=ja,en", out)
        self.assertNotIn("persona=bad", out)

    def test_build_memctx_can_be_null(self) -> None:
        plan = BrainRecallPlan.model_validate({"fts_queries": ["何か"]})
        bundle = type("Bundle", (), {"surface": [], "deep": [], "timeline": [], "anchors": {}})()
        out = build_memctx(plan, bundle, 120)
        self.assertEqual("", out)

    def test_build_memctx_uses_budget_split(self) -> None:
        plan = BrainRecallPlan.model_validate(
            {
                "intent": {"profile": 0.9, "timeline": 0.2, "state": 0.1, "fact": 0.8, "overview": 0.1},
                "budget_split": {"profile": 70, "timeline": 10, "surface": 10, "deep": 20, "ephemeral": 0},
                "time_range": {"start_day": "2026-03-05", "end_day": "2026-03-05", "label": "yesterday"},
            }
        )
        bundle = type("Bundle", (), {
            "surface": [SearchResult(1, "s1", "surface", "fact", "", "", "surface summary", 0.7, 0.7, 0.7, int(time.time()), 1.0)],
            "deep": [
                SearchResult(2, "s1", "deep", "fact", "profile.identity.card", "僕はロックマン", "profile identity", 0.9, 0.9, 0.9, int(time.time()), 1.2),
                SearchResult(3, "s1", "deep", "fact", "project.current", "MEMQ再構築", "project state", 0.8, 0.8, 0.8, int(time.time()), 1.1),
            ],
            "timeline": [{"summary": "昨日は検証を進めた"}],
            "anchors": {
                "wm.surf": "現在地: 検証中",
                "wm.deep": "長期方針: brain-required",
                "p.snapshot": "callUser:ヒロ | firstPerson:僕",
                "t.recent": "2026-03-05:- [progress] 検証",
            },
        })()
        out = build_memctx(plan, bundle, 120)
        self.assertIn("p.snapshot=", out)
        self.assertIn("d1=profile.identity.card", out)

    def test_audit_redacts_secret_even_without_block(self) -> None:
        result = asyncio.run(
            audit_output(
                cfg=replace(self.cfg, audit=replace(self.cfg.audit, secondary_enabled=False, block_threshold=0.99)),
                brain=BrainService(self.cfg),
                session_key="s1",
                text="token is sk-1234567890abcdef",
                allowed_languages=["ja", "en"],
                mode="primary",
            )
        )
        self.assertIn("[REDACTED_SECRET]", result["redactedText"])
        self.assertFalse(result["block"])

    def test_idle_consolidation_refreshes_digests_and_applies_merge_plan(self) -> None:
        now = int(time.time())
        a = self.db.insert_memory(
            session_key="global",
            layer="deep",
            kind="fact",
            fact_key="profile.identity.card",
            value="A",
            text="A",
            summary="identity A",
            confidence=0.8,
            importance=0.8,
            strength=0.8,
        )
        b = self.db.insert_memory(
            session_key="global",
            layer="deep",
            kind="fact",
            fact_key="profile.identity.card",
            value="A",
            text="A",
            summary="identity A duplicate",
            confidence=0.8,
            importance=0.8,
            strength=0.8,
        )
        self.assertNotEqual(a, b)
        self.db.insert_event(session_key="global", ts=now, actor="user", kind="progress", summary="MEMQ v3 を再構築した", salience=0.9)
        fake = FakeMergeBrain()
        stats, trace_id = asyncio.run(run_idle_consolidation(cfg=self.cfg, db=self.db, brain=fake, session_key="global"))
        self.assertIn("refresh_digests", stats["did"])
        self.assertIn("brain_merge_plan", stats["did"])
        self.assertEqual("trace-merge", trace_id)
        self.assertTrue(fake.called)
        self.assertTrue(self.db.recent_digest("global", days=1))

    def test_brain_recall_schema_accepts_minimal_plan(self) -> None:
        plan = BrainRecallPlan.model_validate({"fts_queries": ["君は誰 ロックマン"]})
        self.assertEqual(["君は誰 ロックマン"], plan.fts_queries)
        self.assertTrue(plan.retrieval.allow_surface)
        self.assertTrue(plan.retrieval.allow_deep)


def _cfg(root: Path) -> Config:
    return Config(
        root=root,
        db_path=root / "memq_v3.sqlite3",
        host="127.0.0.1",
        port=7781,
        timezone="Asia/Tokyo",
        budgets=Budgets(memctx_tokens=120, rules_tokens=80, style_tokens=120),
        total_max_input_tokens=4200,
        total_reserve_tokens=1800,
        recent_max_tokens=2600,
        recent_min_keep_messages=4,
        top_k=5,
        archive_enabled=True,
        idle_enabled=True,
        idle_seconds=120,
        brain=BrainConfig(
            enabled=True,
            mode="brain-required",
            provider="ollama",
            base_url="http://127.0.0.1:11434",
            model="gpt-oss:20b",
            keep_alive="30m",
            timeout_ms=60000,
            max_tokens=640,
            concurrency=1,
        ),
        audit=AuditConfig(
            primary_enabled=True,
            secondary_enabled=False,
            risk_threshold=0.35,
            block_threshold=0.85,
            allowed_languages_default=("ja", "en"),
        ),
    )


if __name__ == "__main__":
    unittest.main()
