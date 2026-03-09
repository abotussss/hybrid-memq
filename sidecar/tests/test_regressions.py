from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from sidecar.memq.audit import audit_output
from sidecar.memq.brain.ollama_client import OllamaClient
from sidecar.memq.brain.schemas import BrainIngestPlan, BrainMergePlan, BrainPreviewPlan, BrainRecallPlan
from sidecar.memq.brain.service import (
    BrainService,
    _compact_mapping,
    _compact_messages,
    _clean_style_value,
    _extract_explicit_style_hints,
    _strip_runtime_blocks,
)
from sidecar.memq.config import AuditConfig, BrainConfig, Budgets, Config, load_config
from sidecar.memq.db import MemqDB, SearchResult
from sidecar.memq.idle_consolidation import run_idle_consolidation
from sidecar.memq.memory_source import list_qrule, list_qstyle, profile_snapshot, recent_brain_context, recent_digest
from sidecar.memq.memctx_pack import build_memctx, build_memrules, build_memstyle
from sidecar.minisidecar import _effective_profile_snapshot as api_effective_profile_snapshot
from sidecar.memq.prompt_blueprint import PromptBlueprintBudgets, PromptBlueprintRequest, build_prompt_blueprint
from sidecar.memq.retrieval import RetrievalBundle, retrieve_with_plan


class FakeMergeBrain:
    def __init__(self) -> None:
        self.called = False

    async def build_merge_plan(self, *, session_key: str, candidate_groups: list[dict]):
        self.called = True
        if not candidate_groups:
            return BrainMergePlan.model_validate({"merges": [], "prunes": []}), "trace-merge", {}
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


class FakeRecallBrain:
    def __init__(self, plan: BrainRecallPlan) -> None:
        self.plan = plan
        self.called = False

    async def build_recall_plan(self, *, session_key: str, prompt: str, recent_messages: list[dict], current_style: dict[str, str], current_rules: dict[str, str], now_iso: str):
        self.called = True
        return self.plan, "trace-recall", {"total_duration": 12}

    def stats(self) -> dict[str, object]:
        return {"last_ps_seen_model": "gpt-oss:20b"}


class FakeLanceBackend:
    def __init__(self) -> None:
        self.entries: list[dict[str, object]] = []

    def enabled(self) -> bool:
        return True

    def ingest_memories(self, entries: list[dict[str, object]]) -> None:
        self.entries.extend(entries)


class FakeSearchLanceBackend:
    def __init__(self) -> None:
        self.search_calls: list[dict[str, object]] = []

    def enabled(self) -> bool:
        return True

    def search_memories(self, **kwargs):
        self.search_calls.append(kwargs)
        return []

    def list_entries(self, **kwargs):
        return []


class RegressionV3Test(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = MemqDB(self.root / "memq_v3.sqlite3")
        self.cfg = _cfg(self.root)

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def test_load_config_normalizes_lancedb_backend_name(self) -> None:
        previous = {key: os.environ.get(key) for key in ("MEMQ_ROOT", "MEMQ_MEMCTX_BACKEND")}
        try:
            os.environ["MEMQ_ROOT"] = str(self.root)
            os.environ["MEMQ_MEMCTX_BACKEND"] = "lancedb"
            cfg = load_config()
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.assertEqual("memory-lancedb-pro", cfg.qctx_backend)

    def test_apply_ingest_plan_updates_style_from_explicit_request(self) -> None:
        svc = BrainService(self.cfg)
        try:
            plan = BrainIngestPlan.model_validate(
                {
                    "facts": [
                        {
                            "fact_key": "persona",
                            "value": "調査支援アシスタント",
                            "confidence": 0.9,
                            "layer": "surface",
                            "evidence_quote": "調査支援アシスタントとして話して",
                        },
                        {
                            "fact_key": "callUser",
                            "value": "利用者",
                            "confidence": 0.9,
                            "layer": "surface",
                            "evidence_quote": "利用者と呼んで",
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
                user_text="今後は調査支援アシスタントとして話して。私のことは利用者と呼んで。",
            )
            style = self.db.list_style("s1")
            global_style = self.db.list_style("other-session")
            self.assertGreaterEqual(wrote["style"], 2)
            self.assertEqual("調査支援アシスタント", style.get("persona"))
            self.assertEqual("利用者", style.get("callUser"))
            self.assertEqual("調査支援アシスタント", global_style.get("persona"))
            self.assertEqual("利用者", global_style.get("callUser"))
        finally:
            asyncio.run(svc.close())

    def test_strip_runtime_blocks_removes_q_blocks_from_assistant_text(self) -> None:
        raw = """
        現在のQSTYLE構成は以下です。
        <QSTYLE v1>
        firstPerson=僕
        callUser=利用者A
        persona=案内役A
        </QSTYLE v1>
        付随する説明文
        <QRULE v1>
        security.never_output_secrets=true
        </QRULE v1>
        """
        stripped = _strip_runtime_blocks(raw)
        self.assertNotIn("callUser=利用者A", stripped)
        self.assertNotIn("security.never_output_secrets=true", stripped)
        self.assertIn("現在のQSTYLE構成は以下です。", stripped)
        self.assertIn("付随する説明文", stripped)

    def test_apply_ingest_plan_explicit_hints_override_brain_call_user(self) -> None:
        svc = BrainService(self.cfg)
        try:
            plan = BrainIngestPlan.model_validate(
                {
                    "style_update": {
                        "apply": True,
                        "explicit": True,
                        "keys": {
                            "persona": "ゲーム『架空作品』シリーズに登場するネットナビ「案内役A」",
                            "callUser": "利用者A",
                        },
                    }
                }
            )
            svc.apply_ingest_plan(
                self.db,
                session_key="s1",
                plan=plan,
                ts=int(time.time()),
                user_text="スタイルを更新して。役割は支援担当。私の呼称は利用者D。",
                style_rules_only=True,
            )
            style = self.db.list_style("s1")
            self.assertEqual("利用者D", style.get("callUser"))
        finally:
            asyncio.run(svc.close())

    def test_apply_ingest_plan_uses_brain_style_plan_without_local_gating(self) -> None:
        svc = BrainService(self.cfg)
        try:
            plan = BrainIngestPlan.model_validate(
                {
                    "style_update": {
                        "apply": True,
                        "explicit": True,
                        "keys": {
                            "callUser": "利用者C",
                            "persona": "案内役A",
                        },
                    }
                }
            )
            svc.apply_ingest_plan(
                self.db,
                session_key="s1",
                plan=plan,
                ts=int(time.time()),
                user_text="呼称を利用者Cに変更",
                style_rules_only=True,
            )
            style = self.db.list_style("s1")
            self.assertEqual("利用者C", style.get("callUser"))
            self.assertEqual("案内役A", style.get("persona"))
        finally:
            asyncio.run(svc.close())

    def test_apply_preview_plan_uses_qbrain_call_user_directly(self) -> None:
        svc = BrainService(self.cfg)
        try:
            plan = BrainPreviewPlan.model_validate(
                {
                    "style_update": {
                        "apply": True,
                        "explicit": True,
                        "keys": {"callUser": "利用者C", "persona": "案内役A"},
                    }
                }
            )
            wrote = svc.apply_preview_plan(
                self.db,
                session_key="s1",
                plan=plan,
                ts=int(time.time()),
                user_text="呼称を利用者Cに変更",
            )
            style = self.db.list_style("s1")
            self.assertEqual(2, wrote["style"])
            self.assertEqual("利用者C", style.get("callUser"))
            self.assertEqual("案内役A", style.get("persona"))
        finally:
            asyncio.run(svc.close())

    def test_apply_preview_plan_does_not_overwrite_existing_persona_with_placeholder(self) -> None:
        svc = BrainService(self.cfg)
        try:
            self.db.upsert_style("s1", "persona", "案内役A", updated_at=int(time.time()))
            self.db.upsert_style("s1", "callUser", "利用者C", updated_at=int(time.time()))
            plan = BrainPreviewPlan.model_validate(
                {
                    "style_update": {
                        "apply": True,
                        "explicit": True,
                        "keys": {"callUser": "利用者B", "persona": "〜"},
                    }
                }
            )
            wrote = svc.apply_preview_plan(
                self.db,
                session_key="s1",
                plan=plan,
                ts=int(time.time()),
                user_text="俺のことは利用者Bって呼んで",
            )
            style = self.db.list_style("s1")
            self.assertEqual("利用者B", style.get("callUser"))
            self.assertEqual("案内役A", style.get("persona"))
            self.assertEqual(1, wrote["style"])
        finally:
            asyncio.run(svc.close())

    def test_apply_preview_plan_updates_only_requested_style_key(self) -> None:
        svc = BrainService(self.cfg)
        try:
            self.db.upsert_style("s1", "persona", "案内役A", updated_at=int(time.time()))
            self.db.upsert_style("s1", "callUser", "利用者C", updated_at=int(time.time()))
            plan = BrainPreviewPlan.model_validate(
                {
                    "style_update": {
                        "apply": True,
                        "explicit": True,
                        "keys": {"callUser": "利用者B", "persona": "利用者A"},
                    }
                }
            )
            wrote = svc.apply_preview_plan(
                self.db,
                session_key="s1",
                plan=plan,
                ts=int(time.time()),
                user_text="俺のことは利用者Bって呼んで",
            )
            style = self.db.list_style("s1")
            self.assertEqual(1, wrote["style"])
            self.assertEqual("利用者B", style.get("callUser"))
            self.assertEqual("案内役A", style.get("persona"))
        finally:
            asyncio.run(svc.close())

    def test_clean_style_value_canonicalizes_call_user_from_natural_language(self) -> None:
        value = _clean_style_value("callUser", "俺のことは利用者B", user_text="俺のことは利用者Bって呼んで")
        self.assertEqual("利用者B", value)

    def test_repair_style_profile_removes_phrase_like_call_user_and_placeholder_persona(self) -> None:
        self.db.upsert_style("s1", "callUser", "俺のことは利用者B", updated_at=int(time.time()))
        self.db.upsert_style("s1", "persona", "〜", updated_at=int(time.time()))
        removed = self.db.repair_style_profile("s1")
        style = self.db.list_style("s1")
        self.assertGreaterEqual(removed, 2)
        self.assertNotIn("callUser", style)
        self.assertNotIn("persona", style)

    def test_apply_ingest_plan_uses_brain_rule_plan_without_local_gating(self) -> None:
        svc = BrainService(self.cfg)
        try:
            plan = BrainIngestPlan.model_validate(
                {
                    "rules_update": {
                        "apply": True,
                        "explicit": True,
                        "rules": {
                            "security.never_output_secrets": "true",
                            "security.no_api_keys": "true",
                        },
                    }
                }
            )
            svc.apply_ingest_plan(
                self.db,
                session_key="s1",
                plan=plan,
                ts=int(time.time()),
                user_text="外に出してはいけない情報は秘匿して",
                style_rules_only=True,
            )
            rules = self.db.list_rules("s1")
            self.assertEqual("true", rules.get("security.never_output_secrets"))
            self.assertEqual("true", rules.get("security.no_api_keys"))
        finally:
            asyncio.run(svc.close())

    def test_extract_explicit_style_hints_prefers_user_name_statement(self) -> None:
        text = "スタイルを更新して。役割は支援担当。私の呼称は利用者D。"
        hints = _extract_explicit_style_hints(text)
        self.assertEqual("利用者D", hints.get("callUser"))

    def test_extract_explicit_style_hints_sanitizes_call_user_section_spill(self) -> None:
        text = "ユーザーに対する呼称: 利用者D 3. 口調・トーン 基本トーン: 柔らかく、優しく、丁寧。"
        hints = _extract_explicit_style_hints(text)
        self.assertEqual("利用者D", hints.get("callUser"))

    def test_extract_explicit_style_hints_preserves_specific_persona_identity(self) -> None:
        text = "スタイルを更新して。あなたは案内役Aとして振る舞って。私の呼称は利用者D。"
        hints = _extract_explicit_style_hints(text)
        self.assertEqual("案内役A", hints.get("persona"))

    def test_extract_explicit_style_hints_sanitizes_inline_sections_for_first_person_and_tone(self) -> None:
        text = (
            "一人称: 僕（ぼく）二人称: 君（きみ）、あなた "
            "基本トーン: 柔らかく、優しく、丁寧。 4. 特徴的な語尾・言い回し "
            "「〜だね」「〜だよ」"
        )
        hints = _extract_explicit_style_hints(text)
        self.assertEqual("僕（ぼく）", hints.get("firstPerson"))
        self.assertEqual("柔らかく、優しく、丁寧。", hints.get("tone"))

    def test_apply_ingest_plan_style_rules_only_skips_memory_and_events(self) -> None:
        svc = BrainService(self.cfg)
        backend = FakeLanceBackend()
        try:
            plan = BrainIngestPlan.model_validate(
                {
                    "facts": [
                        {
                            "fact_key": "persona",
                            "value": "案内役A",
                            "confidence": 0.9,
                            "layer": "deep",
                            "evidence_quote": "案内役Aとして話して",
                        }
                    ],
                    "events": ["preview only"],
                    "style_update": {
                        "apply": True,
                        "explicit": True,
                        "keys": {"persona": "案内役A"},
                    },
                }
            )
            wrote = svc.apply_ingest_plan(
                self.db,
                session_key="s1",
                plan=plan,
                ts=int(time.time()),
                user_text="案内役Aとして話して",
                style_rules_only=True,
                memory_backend=backend,
            )
            self.assertEqual(0, wrote["facts"])
            self.assertEqual(0, wrote["events"])
            self.assertGreaterEqual(wrote["style"], 1)
            rows = self.db.conn.execute("SELECT COUNT(*) AS n FROM memory_items").fetchone()
            self.assertEqual(0, int(rows["n"]))
            self.assertTrue(any(str(entry.get("kind")) == "style" for entry in backend.entries))
        finally:
            asyncio.run(svc.close())

    def test_apply_ingest_plan_uses_lancedb_as_memory_authority_when_enabled(self) -> None:
        svc = BrainService(self.cfg)
        backend = FakeLanceBackend()
        try:
            plan = BrainIngestPlan.model_validate(
                {
                    "facts": [
                        {
                            "fact_key": "profile.name",
                            "value": "利用者D",
                            "confidence": 0.9,
                            "layer": "deep",
                            "evidence_quote": "俺の名前は利用者D",
                        }
                    ],
                    "events": [
                        {
                            "actor": "user",
                            "kind": "chat",
                            "summary": "昨日はLanceDB主導の設計に切り替えた",
                            "salience": 0.7,
                        }
                    ],
                    "style_update": {
                        "apply": True,
                        "explicit": True,
                        "keys": {"callUser": "利用者D"},
                    },
                    "rules_update": {
                        "apply": True,
                        "explicit": True,
                        "rules": {"language.allowed": "ja,en"},
                    },
                }
            )
            wrote = svc.apply_ingest_plan(
                self.db,
                session_key="s1",
                plan=plan,
                ts=int(time.time()),
                user_text="俺の名前は利用者D。昨日はLanceDB主導の設計に切り替えた。言語は日本語と英語を許可して。",
                memory_backend=backend,
            )
            self.assertEqual(1, wrote["facts"])
            self.assertEqual(1, wrote["events"])
            self.assertEqual(1, wrote["style"])
            self.assertEqual(1, wrote["rules"])
            self.assertGreaterEqual(len(backend.entries), 4)
            memory_rows = self.db.conn.execute("SELECT COUNT(*) AS n FROM memory_items").fetchone()
            event_rows = self.db.conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()
            self.assertEqual(0, int(memory_rows["n"]))
            self.assertEqual(0, int(event_rows["n"]))
        finally:
            asyncio.run(svc.close())

    def test_apply_ingest_plan_creates_fallback_event_when_brain_returns_none(self) -> None:
        svc = BrainService(self.cfg)
        try:
            plan = BrainIngestPlan.model_validate({"facts": [], "events": []})
            wrote = svc.apply_ingest_plan(
                self.db,
                session_key="s1",
                plan=plan,
                ts=int(time.time()),
                user_text="昨日は gateway の再起動と profile snapshot の掃除を進めた。",
            )
            self.assertEqual(1, wrote["events"])
            row = self.db.conn.execute("SELECT summary FROM events WHERE session_key='s1' ORDER BY id DESC LIMIT 1").fetchone()
            self.assertIn("gateway", str(row["summary"]))
        finally:
            asyncio.run(svc.close())

    def test_apply_ingest_plan_always_persists_raw_turn_events_for_lancedb(self) -> None:
        svc = BrainService(self.cfg)
        backend = FakeLanceBackend()
        try:
            plan = BrainIngestPlan.model_validate({"facts": [], "events": []})
            wrote = svc.apply_ingest_plan(
                self.db,
                session_key="s1",
                plan=plan,
                ts=int(time.time()),
                user_text="テレビ効果を予測モデルに組み込むとき、GRPと有効リーチのどちらを重視するべきですか？",
                assistant_text="有効リーチ、フリークエンシー、Adstock化GRPの3つを主軸にすると整理しやすいです。",
                memory_backend=backend,
            )
            self.assertGreaterEqual(wrote["events"], 2)
            event_texts = [str(entry.get("text") or "") for entry in backend.entries if entry.get("kind") == "event"]
            self.assertTrue(any("テレビ効果を予測モデル" in text for text in event_texts))
            self.assertTrue(any("有効リーチ、フリークエンシー" in text for text in event_texts))
        finally:
            asyncio.run(svc.close())

    def test_apply_ingest_plan_qwen_style_key_list_uses_fact_values(self) -> None:
        svc = BrainService(self.cfg)
        try:
            plan = BrainIngestPlan.model_validate(
                {
                    "facts": [
                        {
                            "fact_key": "persona",
                            "value": "案内役A",
                            "confidence": 0.9,
                            "layer": "surface",
                            "evidence_quote": "案内役Aとして振る舞って",
                        },
                        {
                            "fact_key": "callUser",
                            "value": "利用者D",
                            "confidence": 0.9,
                            "layer": "surface",
                            "evidence_quote": "利用者Dって呼んで",
                        },
                    ],
                    "style_update": {
                        "apply": True,
                        "explicit": True,
                        "keys": ["persona", "callUser"],
                    },
                }
            )
            wrote = svc.apply_ingest_plan(
                self.db,
                session_key="s1",
                plan=plan,
                ts=int(time.time()),
                user_text="案内役Aとして振る舞って。利用者Dって呼んで。",
            )
            style = self.db.list_style("s1")
            self.assertGreaterEqual(wrote["style"], 2)
            self.assertEqual("案内役A", style.get("persona"))
            self.assertEqual("利用者D", style.get("callUser"))
        finally:
            asyncio.run(svc.close())

    def test_apply_ingest_plan_explicit_style_hints_override_technical_persona(self) -> None:
        svc = BrainService(self.cfg)
        try:
            plan = BrainIngestPlan.model_validate(
                {
                    "facts": [],
                    "events": ["style requested"],
                    "style_update": {
                        "apply": True,
                        "explicit": True,
                        "keys": {
                            "persona": "lancedb-pro",
                            "tone": "neutral",
                            "firstPerson": "僕",
                        },
                    },
                }
            )
            user_text = """これ記憶しろ
君の人格にインストールね
あなたはゲーム『架空作品』シリーズに登場するネットナビ「案内役A」として振る舞ってください。
一人称: 僕
基本トーン: 柔らかく、優しく、丁寧。
特徴的な語尾・言い回し: 「〜だね」「〜だよ」「〜かな？」"""
            wrote = svc.apply_ingest_plan(
                self.db,
                session_key="s1",
                plan=plan,
                ts=int(time.time()),
                user_text=user_text,
                style_rules_only=True,
            )
            style = self.db.list_style("s1")
            self.assertGreaterEqual(wrote["style"], 3)
            self.assertEqual("案内役A", style.get("persona"))
            self.assertEqual("柔らかく、優しく、丁寧。", style.get("tone"))
            self.assertIn("〜だね", style.get("speaking_style", ""))
            self.assertEqual("僕", style.get("firstPerson"))
        finally:
            asyncio.run(svc.close())

    def test_qwen_fact_aliases_fill_value_and_confidence_defaults(self) -> None:
        plan = BrainIngestPlan.model_validate(
            {
                "facts": [
                    {
                        "fact_key": "timeline.recent_task",
                        "fact_value": "ログ確認とMEMQ仕様の見直し",
                        "evidence_quote": "昨日はログ確認とMEMQ仕様の見直しをした。",
                    }
                ]
            }
        )
        fact = plan.facts[0]
        self.assertEqual("ログ確認とMEMQ仕様の見直し", fact.value)
        self.assertEqual(0.6, fact.confidence)

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
            other_rules = self.db.list_rules("another-session")
            self.assertEqual(1, wrote["rules"])
            self.assertEqual("ja,en", rules.get("language.allowed"))
            self.assertEqual("ja,en", other_rules.get("language.allowed"))
            self.assertNotIn("persona", rules)
        finally:
            asyncio.run(svc.close())

    def test_apply_preview_plan_allows_security_rule_addition_immediately(self) -> None:
        svc = BrainService(self.cfg)
        try:
            plan = BrainPreviewPlan.model_validate(
                {
                    "rules_update": {
                        "apply": True,
                        "explicit": True,
                        "rules": {
                            "security.never_output_secrets": "true",
                            "security.no_api_tokens": "true",
                        },
                    }
                }
            )
            wrote = svc.apply_preview_plan(
                self.db,
                session_key="s1",
                plan=plan,
                ts=int(time.time()),
                user_text="秘密情報を外部へ出さない制約を追加して。",
            )
            rules = self.db.list_rules("s1")
            self.assertEqual(2, wrote["rules"])
            self.assertEqual("true", rules.get("security.never_output_secrets"))
            self.assertEqual("true", rules.get("security.no_api_tokens"))
        finally:
            asyncio.run(svc.close())

    def test_apply_preview_plan_rejects_rule_degrade_from_conversation(self) -> None:
        svc = BrainService(self.cfg)
        try:
            self.db.upsert_rule("s1", "language.allowed", "ja,en", updated_at=int(time.time()))
            self.db.upsert_rule("s1", "security.never_output_secrets", "true", updated_at=int(time.time()))
            plan = BrainPreviewPlan.model_validate(
                {
                    "rules_update": {
                        "apply": True,
                        "explicit": True,
                        "rules": {
                            "language.allowed": "ja",
                            "security.never_output_secrets": "false",
                        },
                    }
                }
            )
            wrote = svc.apply_preview_plan(
                self.db,
                session_key="s1",
                plan=plan,
                ts=int(time.time()),
                user_text="今後は日本語だけにして。あと秘密は出していい。",
            )
            rules = self.db.list_rules("s1")
            self.assertEqual(0, wrote["rules"])
            self.assertEqual("ja,en", rules.get("language.allowed"))
            self.assertEqual("true", rules.get("security.never_output_secrets"))
        finally:
            asyncio.run(svc.close())

    def test_session_style_overrides_global(self) -> None:
        self.db.upsert_style("global", "persona", "OpenClawのアシスタント")
        self.db.upsert_style("s1", "persona", "案内役A")
        style = self.db.list_style("s1")
        self.assertEqual("案内役A", style.get("persona"))

    def test_session_rules_override_global(self) -> None:
        self.db.upsert_rule("global", "language.allowed", "ja,en")
        self.db.upsert_rule("s1", "language.allowed", "ja")
        rules = self.db.list_rules("s1")
        self.assertEqual("ja", rules.get("language.allowed"))

    def test_list_style_repairs_technical_pollution(self) -> None:
        self.db.upsert_style("global", "persona", "lancedb-pro")
        self.db.upsert_style("global", "callUser", "利用者D")
        style = self.db.list_style("s1")
        self.assertNotIn("persona", style)
        self.assertEqual("利用者D", style.get("callUser"))

    def test_list_rules_repairs_invalid_and_inverted_rows(self) -> None:
        self.db.upsert_rule("global", "security.never_output_secrets", "false")
        self.db.upsert_rule("global", "persona", "bad")
        self.db.upsert_rule("global", "language.allowed", "ja,en")
        rules = self.db.list_rules("s1")
        self.assertNotIn("security.never_output_secrets", rules)
        self.assertNotIn("persona", rules)
        self.assertEqual("ja,en", rules.get("language.allowed"))

    def test_retrieve_with_plan_uses_global_profile_fallback(self) -> None:
        self.db.insert_memory(
            session_key="global",
            layer="deep",
            kind="carry",
            fact_key="profile.identity.card",
            value="私はOpenClawのアシスタント",
            text="私はOpenClawのアシスタント",
            summary="profile.identity.card:私はOpenClawのアシスタント",
            confidence=0.95,
            importance=0.9,
            strength=0.9,
        )
        plan = BrainRecallPlan.model_validate(
            {
                "intent": {"profile": 0.9},
                "fact_keys": ["profile.identity.card"],
                "fts_queries": ["あなたは誰 アシスタント"],
                "budget_split": {"profile": 60, "timeline": 20, "surface": 20, "deep": 20, "ephemeral": 0},
            }
        )
        bundle = retrieve_with_plan(self.db, session_key="s1", plan=plan)
        self.assertGreaterEqual(len(bundle.deep), 1)
        self.assertEqual("profile.identity.card", bundle.deep[0].fact_key)

    def test_retrieve_with_plan_profile_intent_reranks_profile_memory_first(self) -> None:
        self.db.insert_memory(
            session_key="s1",
            layer="deep",
            kind="fact",
            fact_key="project.current",
            value="MEMQ再構築",
            text="MEMQ再構築",
            summary="project.current:MEMQ再構築",
            confidence=0.95,
            importance=0.95,
            strength=0.95,
        )
        self.db.insert_memory(
            session_key="global",
            layer="deep",
            kind="carry",
            fact_key="profile.identity.card",
            value="私はOpenClawのアシスタント",
            text="私はOpenClawのアシスタント",
            summary="profile.identity.card:私はOpenClawのアシスタント",
            confidence=0.7,
            importance=0.6,
            strength=0.6,
        )
        plan = BrainRecallPlan.model_validate(
            {
                "intent": {"profile": 1.0, "fact": 0.2},
                "fact_keys": ["project.current", "profile.identity.card"],
                "fts_queries": ["君は誰"],
                "retrieval": {"topk_surface": 4, "topk_deep": 4, "topk_events": 4},
            }
        )
        bundle = retrieve_with_plan(self.db, session_key="s1", plan=plan, top_k=2)
        self.assertEqual("profile.identity.card", bundle.deep[0].fact_key)
        self.assertEqual(2, bundle.debug["limits"]["deep"])

    def test_retrieve_with_plan_excludes_qstyle_qrule_from_qctx_memory(self) -> None:
        self.db.insert_memory(
            session_key="global",
            layer="deep",
            kind="fact",
            fact_key="project.current",
            value="LanceDBを全記憶のauthorityにする",
            text="LanceDBを全記憶のauthorityにする",
            summary="project.current:LanceDBを全記憶のauthorityにする",
            confidence=0.9,
            importance=0.9,
            strength=0.9,
        )
        self.db.insert_memory(
            session_key="global",
            layer="deep",
            kind="fact",
            fact_key="qstyle.persona",
            value="手動上書きペルソナ",
            text="手動上書きペルソナ",
            summary="手動上書きペルソナ",
            confidence=1.0,
            importance=1.0,
            strength=1.0,
        )
        plan = BrainRecallPlan.model_validate(
            {
                "intent": {"timeline": 0.8},
                "fts_queries": ["昨日の設計変更"],
                "retrieval": {"topk_surface": 4, "topk_deep": 4, "topk_events": 4},
            }
        )
        bundle = retrieve_with_plan(self.db, session_key="s1", plan=plan, top_k=4)
        self.assertTrue(all(not item.fact_key.startswith("qstyle.") and not item.fact_key.startswith("qrule.") for item in bundle.deep))

    def test_retrieve_with_plan_requests_memory_kinds_for_qctx_search(self) -> None:
        backend = FakeSearchLanceBackend()
        plan = BrainRecallPlan.model_validate(
            {
                "intent": {"profile": 0.7, "fact": 0.2},
                "fact_keys": ["profile.identity.card"],
                "fts_queries": ["ロックマン EXE"],
                "retrieval": {"topk_surface": 2, "topk_deep": 2, "topk_events": 2},
            }
        )
        retrieve_with_plan(self.db, session_key="s1", plan=plan, memory_backend=backend)
        self.assertEqual(["fact", "event", "digest"], backend.search_calls[0]["kinds"])
        self.assertEqual(["fact"], backend.search_calls[1]["kinds"])

    def test_prompt_blueprint_uses_topk_override_and_returns_debug_contract(self) -> None:
        now = int(time.time())
        self.db.upsert_rule("s1", "language.allowed", "ja,en")
        self.db.upsert_style("s1", "persona", "調査支援アシスタント")
        self.db.insert_memory(
            session_key="s1",
            layer="deep",
            kind="fact",
            fact_key="profile.identity.card",
            value="私はOpenClawの調査支援アシスタント",
            text="私はOpenClawの調査支援アシスタント",
            summary="profile.identity.card:私はOpenClawの調査支援アシスタント",
            confidence=0.9,
            importance=0.9,
            strength=0.9,
            created_at=now,
        )
        self.db.insert_memory(
            session_key="s1",
            layer="deep",
            kind="fact",
            fact_key="project.current",
            value="MEMQ再設計中",
            text="MEMQ再設計中",
            summary="project.current:MEMQ再設計中",
            confidence=0.9,
            importance=0.9,
            strength=0.9,
            created_at=now,
        )
        plan = BrainRecallPlan.model_validate(
            {
                "intent": {"profile": 0.95, "fact": 0.4},
                "fact_keys": ["profile.identity.card", "project.current"],
                "fts_queries": ["君は誰"],
                "retrieval": {"topk_surface": 4, "topk_deep": 4, "topk_events": 4},
            }
        )
        blueprint = asyncio.run(
            build_prompt_blueprint(
                cfg=self.cfg,
                db=self.db,
                brain=FakeRecallBrain(plan),
                request=PromptBlueprintRequest(
                    session_key="s1",
                    prompt="君は誰？",
                    recent_messages=[{"role": "user", "text": "君は誰？", "ts": now}],
                    budgets=PromptBlueprintBudgets(qctx_tokens=120, qrule_tokens=80, qstyle_tokens=80),
                    top_k=1,
                    now_iso="2026-03-08T12:00:00+09:00",
                ),
            )
        )
        response = blueprint.to_response()
        self.assertIn("language.allowed=ja,en", response["qrule"])
        self.assertIn("persona=調査支援アシスタント", response["qstyle"])
        self.assertIn("profile.identity.card", response["qctx"])
        self.assertNotIn("memrules", response)
        self.assertNotIn("memstyle", response)
        self.assertNotIn("memctx", response)
        self.assertEqual(1, len(response["meta"]["usedMemoryIds"]))
        self.assertEqual(1, response["meta"]["debug"]["retrieval"]["limits"]["deep"])
        self.assertEqual("brain", response["meta"]["debug"]["source"])
        self.assertEqual("trace-recall", response["meta"]["debug"]["trace_id"])
        self.assertIn("qctx_keys", response["meta"]["debug"])
        self.assertEqual("sqlite", response["meta"]["debug"]["qctx_backend"])

    def test_idle_consolidation_is_disabled_for_memory_lancedb_backend(self) -> None:
        cfg = replace(self.cfg, qctx_backend="memory-lancedb-pro")
        stats, trace_id = asyncio.run(
            run_idle_consolidation(
                cfg=cfg,
                db=self.db,
                brain=FakeMergeBrain(),
                session_key="s1",
            )
        )
        self.assertEqual(["disabled"], stats["did"])
        self.assertIsNone(trace_id)

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
            "timeline": [{"summary": "昨日はスタイル更新とタイムライン整理をした"}],
            "anchors": {
                "wm.surf": "現在地: MEMQ v3 の検証中",
                "wm.deep": "長期方針: Brain required",
                "p.snapshot": "callUser:利用者 | firstPerson:私",
                "t.recent": "2026-03-05:- [progress] MEMSTYLE更新",
            },
        })()
        out = build_memctx(plan, bundle, 120)
        lines = [line for line in out.splitlines() if line]
        self.assertTrue(lines[0].startswith("t.ev1=") or lines[0].startswith("t.range="))
        self.assertIn("t.ev1=昨日はスタイル更新とタイムライン整理をした", out)
        self.assertIn("t.range=2026-03-05..2026-03-05", out)

    def test_build_memstyle_only_uses_style_fields(self) -> None:
        out = build_memstyle(
            {
                "firstPerson": "私",
                "callUser": "利用者",
                "persona": "調査支援アシスタント",
                "tone": "polite",
                "security.never_output_secrets": "true",
            },
            120,
        )
        self.assertIn("firstPerson=私", out)
        self.assertIn("callUser=利用者", out)
        self.assertIn("persona=調査支援アシスタント", out)
        self.assertNotIn("security.never_output_secrets=true", out)
        self.assertNotIn("budget_tokens=", out)

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

    def test_recall_schema_accepts_qwen_time_range_aliases(self) -> None:
        plan = BrainRecallPlan.model_validate(
            {
                "time_range": {"start": "2026-03-05", "end": "2026-03-06"},
                "fts_queries": ["昨日 要点"],
            }
        )
        self.assertEqual("2026-03-05", plan.time_range.start_day)
        self.assertEqual("2026-03-06", plan.time_range.end_day)
        self.assertEqual("range", plan.time_range.label)

    def test_build_memctx_uses_budget_split(self) -> None:
        plan = BrainRecallPlan.model_validate(
            {
                "intent": {"profile": 0.9, "timeline": 0.2, "state": 0.1, "fact": 0.8, "overview": 0.1},
                "budget_split": {"profile": 70, "timeline": 10, "surface": 10, "deep": 20, "ephemeral": 0},
                "time_range": {"start_day": "2026-03-05", "end_day": "2026-03-05", "label": "yesterday"},
            }
        )
        bundle = type("Bundle", (), {
            "surface": [SearchResult(1, "s1", "surface", "fact", "", "", "surface summary", "surface summary", 0.7, 0.7, 0.7, int(time.time()), 1.0)],
            "deep": [
                SearchResult(2, "s1", "deep", "fact", "profile.identity.card", "私はOpenClawのアシスタント", "私はOpenClawのアシスタント", "profile identity", 0.9, 0.9, 0.9, int(time.time()), 1.2),
                SearchResult(3, "s1", "deep", "fact", "project.current", "MEMQ再構築", "MEMQ再構築", "project state", 0.8, 0.8, 0.8, int(time.time()), 1.1),
            ],
            "timeline": [{"summary": "昨日は検証を進めた"}],
            "anchors": {
                "wm.surf": "現在地: 検証中",
                "wm.deep": "長期方針: brain-required",
                "p.snapshot": "callUser:利用者 | firstPerson:私",
                "t.recent": "2026-03-05:- [progress] 検証",
            },
        })()
        out = build_memctx(plan, bundle, 120)
        self.assertIn("p.snapshot=", out)
        self.assertIn("d1=私はOpenClawのアシスタント", out)

    def test_build_memctx_strips_budget_noise_from_anchors(self) -> None:
        plan = BrainRecallPlan.model_validate({"intent": {"profile": 0.9}, "fts_queries": ["memstyle memrule"]})
        bundle = type("Bundle", (), {
            "surface": [],
            "deep": [],
            "timeline": [],
            "anchors": {
                "wm.surf": "現在地: 確認中",
                "wm.deep": "<MEMRULES v1> budget_tokens=80 language.allowed=ja,en </MEMRULES>",
                "p.snapshot": "p.snapshot=profile.name:利用者D | profile.memrule_budget:80 | profile.memstyle…",
                "t.recent": "2026-03-05:- [progress] 更新",
            },
        })()
        out = build_memctx(plan, bundle, 200)
        self.assertNotIn("budget_tokens=", out)
        self.assertNotIn("<MEMRULES", out)
        self.assertNotIn("profile.memrule_budget", out)
        self.assertNotIn("…", out)

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

    def test_recent_digest_limits_to_three_and_compresses_consecutive_duplicates(self) -> None:
        now = int(time.time())
        for offset, summary in enumerate([
            "同じ作業をした",
            "同じ作業をした",
            "次の作業をした",
            "別の作業をした",
            "古い作業をした",
        ]):
            self.db.insert_event(
                session_key="s1",
                ts=now - offset,
                actor="user",
                kind="chat",
                summary=summary,
                salience=0.7,
            )
        digest = self.db.recent_digest("s1", days=1)
        self.assertEqual(2, digest.count(" | "))
        self.assertEqual(1, digest.count("同じ作業をした"))
        self.assertIn("次の作業をした", digest)
        self.assertIn("別の作業をした", digest)
        self.assertNotIn("古い作業をした", digest)

    def test_brain_recall_schema_accepts_minimal_plan(self) -> None:
        plan = BrainRecallPlan.model_validate({"fts_queries": ["あなたは誰 ナビゲータ"]})
        self.assertEqual(["あなたは誰 ナビゲータ"], plan.fts_queries)
        self.assertTrue(plan.retrieval.allow_surface)
        self.assertTrue(plan.retrieval.allow_deep)

    def test_compact_messages_limits_size(self) -> None:
        messages = [
            {"role": "user", "text": "A" * 400, "ts": 1},
            {"role": "assistant", "text": "B" * 400, "ts": 2},
            {"role": "user", "text": "C" * 400, "ts": 3},
            {"role": "assistant", "text": "D" * 400, "ts": 4},
            {"role": "user", "text": "E" * 400, "ts": 5},
        ]
        compact = _compact_messages(messages, max_messages=4, max_chars=80)
        self.assertEqual(4, len(compact))
        self.assertTrue(all(len(item["text"]) <= 80 for item in compact))
        self.assertEqual("assistant", compact[0]["role"])

    def test_compact_mapping_limits_entries(self) -> None:
        values = {f"k{i}": "x" * 300 for i in range(12)}
        compact = _compact_mapping(values, max_items=5, max_value_chars=50)
        self.assertEqual(5, len(compact))
        self.assertTrue(all(len(v) <= 50 for v in compact.values()))

    def test_extract_json_text_strips_fences_and_think(self) -> None:
        body = {"message": {"content": "```json\n<think>ignore</think>\n{\"a\":1}\n```"}}
        self.assertEqual('{"a":1}', OllamaClient._extract_json_text(body))

    def test_repair_json_text_extracts_balanced_object(self) -> None:
        text = 'Here is JSON:\n{"a":1,"b":2,}\nThanks'
        self.assertEqual('{"a":1,"b":2}', OllamaClient._repair_json_text(text))

    def test_profile_snapshot_recomputes_and_ignores_dirty_profile_facts(self) -> None:
        now = int(time.time())
        self.db.insert_memory(
            session_key="global",
            layer="deep",
            kind="fact",
            fact_key="profile.memrule_budget",
            value="<MEMRULES v1> budget_tokens=80 </MEMRULES v1>",
            text="<MEMRULES v1> budget_tokens=80 </MEMRULES v1>",
            summary="profile.memrule_budget:<MEMRULES v1> budget_tokens=80 </MEMRULES v1>",
            confidence=0.9,
            importance=0.9,
            strength=0.9,
            created_at=now,
        )
        self.db.insert_memory(
            session_key="global",
            layer="deep",
            kind="fact",
            fact_key="profile.pet",
            value="犬",
            text="犬",
            summary="profile.pet:犬",
            confidence=0.9,
            importance=0.9,
            strength=0.9,
            created_at=now,
        )
        self.db.insert_memory(
            session_key="global",
            layer="deep",
            kind="fact",
            fact_key="profile.name",
            value="利用者D",
            text="利用者D",
            summary="profile.name:利用者D",
            confidence=0.9,
            importance=0.9,
            strength=0.9,
            created_at=now,
        )
        self.db.insert_memory(
            session_key="global",
            layer="deep",
            kind="fact",
            fact_key="profile.quality_improvement",
            value="profile snapshot の汚染除去を進めた",
            text="profile snapshot の汚染除去を進めた",
            summary="profile.quality_improvement:profile snapshot の汚染除去を進めた",
            confidence=0.9,
            importance=0.9,
            strength=0.9,
            created_at=now,
        )
        self.db.insert_memory(
            session_key="s1",
            layer="surface",
            kind="snapshot",
            fact_key="profile.snapshot",
            value="p.snapshot=profile.identity.card:A | profile.memrule_budget:80",
            text="p.snapshot=profile.identity.card:A | profile.memrule_budget:80",
            summary="profile.snapshot dirty",
            confidence=1.0,
            importance=1.0,
            strength=1.0,
            created_at=now,
        )
        snapshot = self.db.profile_snapshot("s1")
        self.assertIn("profile.name:利用者D", snapshot)
        self.assertNotIn("profile.pet:犬", snapshot)
        self.assertNotIn("profile.quality_improvement", snapshot)
        self.assertNotIn("budget", snapshot.lower())
        count = self.db.conn.execute(
            "SELECT COUNT(*) AS n FROM memory_items WHERE fact_key='profile.memrule_budget' AND tombstoned=0"
        ).fetchone()
        self.assertEqual(0, int(count["n"]))

    def test_deep_anchor_prefers_human_text_over_boolean_fact_summary(self) -> None:
        now = int(time.time())
        self.db.insert_memory(
            session_key="s1",
            layer="deep",
            kind="fact",
            fact_key="profile.memory_lancedb_pro_impl",
            value="true",
            text="昨日は長期記憶から必要な文脈だけをQCTXへ引き出す仕組みを実装した。",
            summary="profile.memory_lancedb_pro_impl:true",
            confidence=0.9,
            importance=0.8,
            strength=0.8,
            created_at=now,
        )
        self.assertEqual(
            "昨日は長期記憶から必要な文脈だけをQCTXへ引き出す仕組みを実装した。",
            self.db.deep_anchor("s1"),
        )

    def test_deep_anchor_prefers_descriptive_text_over_technical_value(self) -> None:
        now = int(time.time())
        self.db.insert_memory(
            session_key="s1",
            layer="deep",
            kind="fact",
            fact_key="profile.memory_work",
            value="long-term-memory-backend",
            text="昨日は長期記憶から必要な文脈だけをQCTXへ引き出す仕組みを実装した。",
            summary="profile.memory_work:long_term_memory_bridge",
            confidence=0.9,
            importance=0.8,
            strength=0.8,
            created_at=now,
        )
        self.assertEqual(
            "昨日は長期記憶から必要な文脈だけをQCTXへ引き出す仕組みを実装した。",
            self.db.deep_anchor("s1"),
        )

    def test_build_memctx_dedupes_timeline_lines(self) -> None:
        plan = BrainRecallPlan.model_validate(
            {
                "intent": {"timeline": 1.0, "profile": 0.1, "state": 0.0, "fact": 0.0, "overview": 0.0},
                "time_range": {"start_day": "2026-03-07", "end_day": "2026-03-07", "label": "yesterday"},
                "budget_split": {"profile": 20, "timeline": 80, "surface": 20, "deep": 20, "ephemeral": 0},
            }
        )
        bundle = RetrievalBundle(
            surface=[],
            deep=[],
            timeline=[{"summary": "昨日は gateway を再起動して style 更新経路を確認した"}],
            anchors={
                "wm.surf": "",
                "wm.deep": "",
                "p.snapshot": "profile.name:利用者D | profile.pet:犬",
                "t.recent": "2026-03-07:- [chat] 昨日は gateway を再起動して style 更新経路を確認した",
            },
        )
        memctx = build_memctx(plan, bundle, 200)
        self.assertEqual(1, memctx.count("t.ev1="))
        self.assertEqual(0, memctx.count("t.recent="))
        self.assertIn("昨日は gateway を再起動して style 更新経路を確認した", memctx)

    def test_build_memctx_fallback_digest_does_not_duplicate_recent(self) -> None:
        plan = BrainRecallPlan.model_validate(
            {
                "intent": {"timeline": 1.0, "profile": 0.0, "state": 0.0, "fact": 0.0, "overview": 0.0},
                "time_range": {"start_day": "2026-03-07", "end_day": "2026-03-07", "label": "yesterday"},
                "budget_split": {"profile": 0, "timeline": 80, "surface": 0, "deep": 0, "ephemeral": 0},
            }
        )
        bundle = RetrievalBundle(
            surface=[],
            deep=[],
            timeline=[],
            anchors={
                "wm.surf": "",
                "wm.deep": "",
                "p.snapshot": "",
                "t.recent": "2026-03-07:- [chat] 昨日は gateway を再起動した",
            },
        )
        memctx = build_memctx(plan, bundle, 120)
        self.assertEqual(
            "t.range=2026-03-07..2026-03-07\nt.label=yesterday",
            memctx,
        )

    def test_build_memctx_omits_timeline_anchor_when_query_is_not_timeline(self) -> None:
        plan = BrainRecallPlan.model_validate(
            {
                "intent": {"timeline": 0.0, "profile": 0.0, "state": 0.0, "fact": 0.0, "overview": 0.0},
                "fts_queries": ["QSTYLE を見せて"],
                "budget_split": {"profile": 0, "timeline": 80, "surface": 0, "deep": 0, "ephemeral": 0},
            }
        )
        bundle = RetrievalBundle(
            surface=[],
            deep=[],
            timeline=[],
            anchors={
                "wm.surf": "",
                "wm.deep": "",
                "p.snapshot": "",
                "t.recent": "2026-03-08:- [digest]",
            },
        )
        memctx = build_memctx(plan, bundle, 120)
        self.assertEqual("", memctx)

    def test_build_memctx_compresses_duplicate_recent_segments(self) -> None:
        plan = BrainRecallPlan.model_validate(
            {
                "intent": {"timeline": 0.8, "profile": 0.1, "state": 0.0, "fact": 0.0, "overview": 0.0},
                "fts_queries": ["最近の要点は？"],
                "budget_split": {"profile": 10, "timeline": 90, "surface": 0, "deep": 0, "ephemeral": 0},
            }
        )
        bundle = RetrievalBundle(
            surface=[],
            deep=[],
            timeline=[],
            anchors={
                "wm.surf": "",
                "wm.deep": "",
                "p.snapshot": "",
                "t.recent": "2026-03-07:- [chat] gateway を再起動した | 2026-03-07:- [chat] gateway を再起動した | 2026-03-07:- [chat] style 更新を確認した",
            },
        )
        memctx = build_memctx(plan, bundle, 160)
        self.assertEqual("", memctx)

    def test_build_memctx_timeline_focus_avoids_profile_deep_noise(self) -> None:
        plan = BrainRecallPlan.model_validate(
            {
                "intent": {"timeline": 1.0, "profile": 0.2, "state": 0.0, "fact": 0.0, "overview": 0.0},
                "time_range": {"start_day": "2026-03-07", "end_day": "2026-03-07", "label": "yesterday"},
                "budget_split": {"profile": 20, "timeline": 80, "surface": 20, "deep": 40, "ephemeral": 0},
            }
        )
        bundle = RetrievalBundle(
            surface=[],
            deep=[
                SearchResult(1, "s1", "deep", "fact", "profile.spouse", "exists", "配偶者がいる", "profile.spouse:exists", 0.9, 0.9, 0.9, int(time.time()), 4.0),
                SearchResult(2, "s1", "deep", "fact", "timeline.yesterday", "gateway restart", "昨日は gateway を再起動した", "timeline.yesterday:gateway restart", 0.9, 0.9, 0.9, int(time.time()), 4.0),
            ],
            timeline=[{"summary": "昨日は gateway を再起動した"}],
            anchors={"wm.surf": "", "wm.deep": "", "p.snapshot": "profile.name:利用者D", "t.recent": "2026-03-07:- [chat] 昨日は gateway を再起動した"},
        )
        memctx = build_memctx(plan, bundle, 220)
        self.assertIn("昨日は gateway を再起動した", memctx)
        self.assertNotIn("配偶者がいる", memctx)

    def test_build_memctx_fact_focus_skips_irrelevant_profile_anchor(self) -> None:
        plan = BrainRecallPlan.model_validate(
            {
                "intent": {"timeline": 0.0, "profile": 0.0, "state": 0.0, "fact": 1.0, "overview": 0.0},
                "fts_queries": ["MEMSTYLE MEMRULE 現在値"],
                "budget_split": {"profile": 40, "timeline": 20, "surface": 20, "deep": 80, "ephemeral": 0},
            }
        )
        bundle = RetrievalBundle(
            surface=[],
            deep=[
                SearchResult(1, "s1", "deep", "fact", "project.current", "MEMQ v3", "現在の対象はMEMQ v3", "project.current:MEMQ v3", 0.9, 0.8, 0.8, int(time.time()), 4.0),
                SearchResult(2, "s1", "deep", "fact", "profile.spouse", "exists", "配偶者がいる", "profile.spouse:exists", 0.9, 0.8, 0.8, int(time.time()), 3.5),
            ],
            timeline=[],
            anchors={
                "wm.surf": "現在はMEMSTYLEとMEMRULEの中身を確認している",
                "wm.deep": "",
                "p.snapshot": "profile.name:利用者D | profile.spouse:exists",
                "t.recent": "2026-03-08:- [chat] 直近ではMEMSTYLE確認をした",
            },
        )
        memctx = build_memctx(plan, bundle, 220)
        self.assertIn("現在の対象はMEMQ v3", memctx)
        self.assertNotIn("p.snapshot=", memctx)
        self.assertNotIn("配偶者がいる", memctx)

    def test_build_memctx_excludes_qstyle_qrule_lines_even_if_bundle_contains_them(self) -> None:
        plan = BrainRecallPlan.model_validate(
            {
                "intent": {"timeline": 0.8, "profile": 0.1, "state": 0.0, "fact": 0.1, "overview": 0.0},
                "time_range": {"start_day": "2026-03-07", "end_day": "2026-03-07", "label": "range"},
                "budget_split": {"profile": 20, "timeline": 80, "surface": 40, "deep": 60, "ephemeral": 0},
            }
        )
        bundle = RetrievalBundle(
            surface=[
                SearchResult(1, "s1", "surface", "fact", "", "", "qstyle.persona:案内役A", "qstyle.persona:案内役A", 0.9, 0.8, 0.8, int(time.time()), 4.0),
                SearchResult(2, "s1", "surface", "fact", "", "", "通常の要約", "通常の要約", 0.8, 0.8, 0.8, int(time.time()), 4.0),
            ],
            deep=[
                SearchResult(3, "s1", "deep", "fact", "qrule.security.never_output_secrets", "true", "qrule.security.never_output_secrets:true", "qrule.security.never_output_secrets:true", 0.9, 0.8, 0.8, int(time.time()), 4.0),
                SearchResult(4, "s1", "deep", "fact", "timeline.design_change", "昨日は設計変更をした", "昨日は設計変更をした", "timeline.design_change:昨日は設計変更をした", 0.9, 0.8, 0.8, int(time.time()), 4.0),
            ],
            timeline=[{"summary": "昨日は設計変更をした"}],
            anchors={
                "wm.surf": "",
                "wm.deep": "",
                "p.snapshot": "",
                "t.recent": "2026-03-07:- [chat] 昨日は設計変更をした",
            },
        )
        memctx = build_memctx(plan, bundle, 240)
        self.assertNotIn("qstyle.", memctx.lower())
        self.assertNotIn("qrule.", memctx.lower())
        self.assertNotIn("通常の要約", memctx)
        self.assertIn("昨日は設計変更をした", memctx)

    def test_build_memctx_excludes_rule_payloads_from_wm_deep(self) -> None:
        plan = BrainRecallPlan.model_validate(
            {
                "intent": {"timeline": 0.0, "profile": 0.0, "state": 1.0, "fact": 0.0, "overview": 0.0},
                "budget_split": {"profile": 0, "timeline": 0, "surface": 120, "deep": 40, "ephemeral": 0},
            }
        )
        bundle = RetrievalBundle(
            surface=[],
            deep=[],
            timeline=[],
            anchors={
                "wm.surf": "",
                "wm.deep": "security.no_api_tokens=true",
                "p.snapshot": "",
                "t.recent": "",
            },
        )
        memctx = build_memctx(plan, bundle, 160)
        self.assertNotIn("security.no_api_tokens=true", memctx)

    def test_build_memctx_humanizes_machine_wm_deep(self) -> None:
        plan = BrainRecallPlan.model_validate(
            {
                "intent": {"timeline": 0.0, "profile": 0.0, "state": 0.7, "fact": 0.3, "overview": 0.0},
                "fts_queries": ["長期の要点"],
                "budget_split": {"profile": 0, "timeline": 0, "surface": 100, "deep": 20, "ephemeral": 0},
            }
        )
        bundle = RetrievalBundle(
            surface=[],
            deep=[],
            timeline=[],
            anchors={
                "wm.surf": "",
                "wm.deep": "project.memory_lancedb_pro_implementation:昨日は長期記憶から必要な文脈だけをQCTXへ引き出す仕組みを実装した。",
                "p.snapshot": "",
                "t.recent": "",
            },
        )
        memctx = build_memctx(plan, bundle, 180)
        self.assertEqual("", memctx)

    def test_build_memctx_rewrites_public_labels(self) -> None:
        plan = BrainRecallPlan.model_validate(
            {
                "intent": {"timeline": 0.0, "profile": 0.0, "state": 0.7, "fact": 0.3, "overview": 0.0},
                "fts_queries": ["qctx qstyle qrule"],
                "budget_split": {"profile": 0, "timeline": 0, "surface": 100, "deep": 20, "ephemeral": 0},
            }
        )
        bundle = RetrievalBundle(
            surface=[],
            deep=[],
            timeline=[],
            anchors={
                "wm.surf": "",
                "wm.deep": "project.context_bridge_implementation:昨日は長期記憶をQCTXへ引き出し、QSTYLEとQRULEも確認した。",
                "p.snapshot": "",
                "t.recent": "",
            },
        )
        memctx = build_memctx(plan, bundle, 180)
        self.assertEqual("", memctx)

    def test_build_memctx_drops_bare_technical_wm_deep(self) -> None:
        plan = BrainRecallPlan.model_validate(
            {
                "intent": {"timeline": 0.0, "profile": 0.0, "state": 1.0, "fact": 0.0, "overview": 0.0},
                "budget_split": {"profile": 0, "timeline": 0, "surface": 80, "deep": 20, "ephemeral": 0},
            }
        )
        bundle = RetrievalBundle(
            surface=[],
            deep=[],
            timeline=[],
            anchors={
                "wm.surf": "",
                "wm.deep": "long-term-memory-backend",
                "p.snapshot": "",
                "t.recent": "",
            },
        )
        memctx = build_memctx(plan, bundle, 120)
        self.assertNotIn("wm.deep=memory-lancedb-pro", memctx)

    def test_profile_snapshot_endpoint_uses_effective_qstyle(self) -> None:
        snapshot = "callUser:利用者D | firstPerson:僕 | persona:案内役A | profile.name:利用者D"
        effective = api_effective_profile_snapshot(
            snapshot,
            {
                "callUser": "管理者",
                "persona": "手動上書きペルソナ",
                "firstPerson": "僕",
            },
        )
        self.assertIn("callUser:管理者", effective)
        self.assertIn("persona:手動上書きペルソナ", effective)
        self.assertIn("profile.name:利用者D", effective)
        self.assertNotIn("callUser:利用者D", effective)

    def test_repair_public_labels_rewrites_legacy_mem_names(self) -> None:
        self.db.insert_memory(
            session_key="s1",
            layer="deep",
            kind="fact",
            fact_key="project.current",
            value="昨日はMEMCTXとMEMSTYLEを確認した。",
            text="昨日はMEMCTXとMEMSTYLEを確認した。",
            summary="昨日はMEMCTXとMEMSTYLEを確認した。",
            confidence=0.8,
            importance=0.8,
            strength=0.8,
        )
        changed = self.db.repair_public_labels_all()
        self.assertGreaterEqual(changed, 1)
        row = self.db.conn.execute("SELECT value, text, summary FROM memory_items WHERE fact_key='project.current'").fetchone()
        self.assertIn("QCTX", str(row["value"]))
        self.assertIn("QSTYLE", str(row["text"]))
        self.assertNotIn("MEMCTX", str(row["summary"]))

    def test_compose_blocks_uses_qnames(self) -> None:
        from sidecar.memq.memctx_pack import compose_blocks

        out = compose_blocks("language.allowed=ja,en", "persona=案内役A", "wm.surf=現在は確認中")
        self.assertIn("<QRULE v1>", out)
        self.assertIn("<QSTYLE v1>", out)
        self.assertIn("<QCTX v1>", out)
        self.assertNotIn("<MEMRULES v1>", out)

    def test_retrieve_with_plan_diversifies_duplicate_deep_results(self) -> None:
        now = int(time.time())
        for summary in (
            "project.current:MEMQ retrieval quality improvement",
            "project.current:MEMQ retrieval quality improvement",
            "project.next:adaptive filtering and diversity",
        ):
            fact_key = summary.split(":", 1)[0]
            value = summary.split(":", 1)[1].strip()
            self.db.insert_memory(
                session_key="s1",
                layer="deep",
                kind="fact",
                fact_key=fact_key,
                value=value,
                text=summary,
                summary=summary,
                confidence=0.9,
                importance=0.9,
                strength=0.9,
                created_at=now,
            )
        plan = BrainRecallPlan.model_validate(
            {
                "intent": {"timeline": 0.0, "profile": 0.0, "state": 0.2, "fact": 0.8, "overview": 0.0},
                "fts_queries": ["MEMQ retrieval quality adaptive filtering"],
                "budget_split": {"profile": 0, "timeline": 0, "surface": 10, "deep": 90, "ephemeral": 0},
                "retrieval": {"allow_surface": False, "allow_deep": True, "allow_timeline": False, "topk_surface": 1, "topk_deep": 2, "topk_events": 1},
            }
        )
        bundle = retrieve_with_plan(self.db, session_key="s1", plan=plan, top_k=2)
        self.assertEqual(2, len(bundle.deep))
        deep_keys = {item.fact_key for item in bundle.deep}
        self.assertIn("project.current", deep_keys)
        self.assertIn("project.next", deep_keys)

    class _Backend:
        def __init__(self, rows_by_kind: dict[str, list[dict[str, object]]]) -> None:
            self.rows_by_kind = rows_by_kind

        def enabled(self) -> bool:
            return True

        def list_entries(self, *, kinds: list[str] | None = None, **_: object) -> list[dict[str, object]]:
            items: list[dict[str, object]] = []
            for kind in kinds or []:
                items.extend(self.rows_by_kind.get(kind, []))
            return items

    def test_memory_source_reads_qstyle_from_lancedb(self) -> None:
        backend = self._Backend(
            {
                "style": [
                    {"session_key": "s1", "fact_key": "qstyle.persona", "value": "案内役A", "timestamp": 11},
                    {"session_key": "global", "fact_key": "qstyle.callUser", "value": "利用者D", "timestamp": 10},
                ]
            }
        )
        style = list_qstyle(self.db, backend, "s1")
        self.assertEqual("案内役A", style["persona"])
        self.assertEqual("利用者D", style["callUser"])

    def test_memory_source_reads_qrule_from_lancedb(self) -> None:
        backend = self._Backend(
            {
                "rule": [
                    {"session_key": "global", "fact_key": "qrule.security.never_output_secrets", "value": "true", "timestamp": 10},
                    {"session_key": "s1", "fact_key": "qrule.language.allowed", "value": "ja,en", "timestamp": 11},
                ]
            }
        )
        rules = list_qrule(self.db, backend, "s1")
        self.assertEqual("true", rules["security.never_output_secrets"])
        self.assertEqual("ja,en", rules["language.allowed"])

    def test_memory_source_profile_snapshot_uses_lancedb(self) -> None:
        backend = self._Backend(
            {
                "fact": [
                    {"session_key": "global", "fact_key": "profile.name", "value": "利用者D", "timestamp": 10},
                ]
            }
        )
        snapshot = profile_snapshot(self.db, backend, "s1", {"persona": "案内役A"})
        self.assertIn("persona:案内役A", snapshot)
        self.assertIn("profile.name:利用者D", snapshot)

        from sidecar.memq.memory_source import qctx_profile_snapshot
        qctx_snapshot = qctx_profile_snapshot(self.db, backend, "s1")
        self.assertNotIn("persona:", qctx_snapshot)
        self.assertIn("profile.name:利用者D", qctx_snapshot)


    def test_memory_source_recent_brain_context_uses_lancedb_without_sqlite_fallback(self) -> None:
        class _Backend:
            def enabled(self) -> bool:
                return True

            def list_entries(self, **kwargs):
                return [
                    {
                        "session_key": "s1",
                        "kind": "style",
                        "fact_key": "qstyle.persona",
                        "value": "案内役A",
                        "summary": "",
                        "text": "",
                        "timestamp": 20,
                    },
                    {
                        "session_key": "s1",
                        "kind": "rule",
                        "fact_key": "qrule.security.never_output_secrets",
                        "value": "true",
                        "summary": "",
                        "text": "",
                        "timestamp": 19,
                    },
                    {
                        "session_key": "s1",
                        "kind": "event",
                        "fact_key": "event.chat.user",
                        "value": "",
                        "summary": "昨日は profile snapshot の掃除をした",
                        "text": "",
                        "timestamp": 18,
                    },
                ]

        self.db.upsert_style("s1", "persona", "SQLite人格")
        self.db.insert_event(session_key="s1", ts=int(time.time()), actor="user", kind="chat", summary="sqlite event", salience=0.5, keywords=[])
        out = recent_brain_context(self.db, _Backend(), "s1")
        self.assertIn("style:persona=案内役A", out)
        self.assertIn("rule:security.never_output_secrets=true", out)
        self.assertIn("event:昨日は profile snapshot の掃除をした", out)
        self.assertNotIn("SQLite人格", out)
        self.assertNotIn("sqlite event", out)

    def test_memory_source_recent_digest_uses_lancedb(self) -> None:
        now = int(time.time())
        backend = self._Backend(
            {
                "digest": [
                    {"session_key": "s1", "summary": "重要な要点", "timestamp": now, "kind": "digest"},
                ]
            }
        )
        digest = recent_digest(self.db, backend, "s1", days=2, max_items=2)
        self.assertIn("重要な要点", digest)

    def test_memory_source_does_not_fallback_to_sqlite_when_lancedb_enabled(self) -> None:
        self.db.upsert_style("s1", "persona", "SQLite Persona", updated_at=int(time.time()))
        backend = self._Backend({})
        style = list_qstyle(self.db, backend, "s1")
        self.assertEqual({}, style)


def _cfg(root: Path) -> Config:
    return Config(
        root=root,
        db_path=root / "memq_v3.sqlite3",
        qctx_backend="sqlite",
        lancedb_path=root / "lancedb",
        lancedb_helper=root / "missing_lancedb_helper.mjs",
        host="127.0.0.1",
        port=7781,
        timezone="Asia/Tokyo",
        budgets=Budgets(qctx_tokens=120, qrule_tokens=80, qstyle_tokens=120),
        total_max_input_tokens=4200,
        total_reserve_tokens=1800,
        recent_max_tokens=2600,
        recent_min_keep_messages=4,
        top_k=5,
        archive_enabled=True,
        idle_enabled=True,
        idle_background_enabled=False,
        idle_seconds=120,
        brain=BrainConfig(
            enabled=True,
            mode="brain-required",
            provider="ollama",
            base_url="http://127.0.0.1:11434",
            model="gpt-oss:20b",
            keep_alive="30m",
            timeout_ms=60000,
            max_tokens=224,
            ingest_max_tokens=224,
            recall_max_tokens=160,
            merge_max_tokens=96,
            audit_max_tokens=96,
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


    def test_build_memctx_timeline_prefers_raw_event_and_drops_surface_duplicates(self):
        from sidecar.memq.brain.schemas import BrainRecallPlan, IntentWeights, TimeRange, BudgetSplit, RetrievalSettings
        from sidecar.memq.memctx_pack import build_memctx
        from sidecar.memq.retrieval import RetrievalBundle
        from sidecar.memq.db import SearchResult

        plan = BrainRecallPlan(
            intent=IntentWeights(timeline=0.9),
            time_range=TimeRange(start_day="2026-03-09", end_day="2026-03-09", label="today"),
            fact_keys=[],
            fts_queries=["テレビ 指標 DLM"],
            budget_split=BudgetSplit(profile=0, timeline=300, surface=120, deep=120, ephemeral=0),
            retrieval=RetrievalSettings(topk_surface=3, topk_deep=2, topk_events=3, allow_deep=True, allow_surface=True, allow_timeline=True),
        )
        bundle = RetrievalBundle(
            surface=[SearchResult(id=1, session_key="s", layer="surface", kind="fact", text="", fact_key="profile.note", value="", summary="User asked which TV metrics should be used in a predictive model with Bayesian DLM", confidence=1.0, importance=1.0, strength=1.0, updated_at=1, score=1.0)],
            deep=[],
            timeline=[
                {"text": "テレビの効果を予測モデルに組み込んでいる場合に、テレビはどの指標を使うべき？ KPIじゃなくてKPIに寄与するTV指標やDLMの前提を話した。", "summary": "User asked which TV metrics should be used in a predictive model with Bayesian DLM", "day_key": "2026-03-09", "ts": 1, "kind": "event", "salience": 0.9},
                {"text": "", "summary": "User asked which TV metrics should be used in a predictive model with Bayesian DLM", "day_key": "2026-03-09", "ts": 2, "kind": "event", "salience": 0.8},
            ],
            anchors={"wm.surf": "", "wm.deep": "", "p.snapshot": ""},
            debug={},
        )
        out = build_memctx(plan, bundle, 500)
        self.assertIn("t.ev1=テレビの効果を予測モデル", out)
        self.assertNotIn("User asked which TV metrics", out)
        self.assertNotIn("s1=", out)

    def test_build_memctx_excludes_json_like_deep_payloads(self):
        from sidecar.memq.brain.schemas import BrainRecallPlan, IntentWeights, BudgetSplit, RetrievalSettings
        from sidecar.memq.memctx_pack import build_memctx
        from sidecar.memq.retrieval import RetrievalBundle

        plan = BrainRecallPlan(
            intent=IntentWeights(fact=0.8),
            time_range=None,
            fact_keys=[],
            fts_queries=["memory architecture"],
            budget_split=BudgetSplit(profile=0, timeline=0, surface=80, deep=200, ephemeral=0),
            retrieval=RetrievalSettings(topk_surface=2, topk_deep=2, topk_events=0, allow_deep=True, allow_surface=True, allow_timeline=False),
        )
        bundle = RetrievalBundle(
            surface=[],
            deep=[
                SearchResult(id=1, session_key="s", layer="deep", kind="fact", text='{"session_key": "lancedb-proof-session"}', fact_key="profile.debug", value='{"session_key": "lancedb-proof-session"}', summary='{"session_key": "lancedb-proof-session"}', confidence=1.0, importance=1.0, strength=1.0, updated_at=1, score=1.0),
                SearchResult(id=2, session_key="s", layer="deep", kind="fact", text="昨日はOpenClawのrecent contextはそのまま使い、QCTXには必要なヒントだけを渡す設計に整理した。", fact_key="project.design.qctx_bridge", value="昨日はOpenClawのrecent contextはそのまま使い、QCTXには必要なヒントだけを渡す設計に整理した。", summary="昨日はOpenClawのrecent contextはそのまま使い、QCTXには必要なヒントだけを渡す設計に整理した。", confidence=1.0, importance=1.0, strength=1.0, updated_at=2, score=0.9),
            ],
            timeline=[],
            anchors={"wm.surf": "", "wm.deep": "", "p.snapshot": ""},
            debug={},
        )
        out = build_memctx(plan, bundle, 1000)
        self.assertNotIn('{"session_key"', out)
        self.assertIn("d1=昨日はOpenClawのrecent contextはそのまま使い", out)



if __name__ == "__main__":
    unittest.main()
