#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Dict, List, Tuple
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

DIM = 256
DB = Path(".memq/minisidecar.sqlite3")
DB.parent.mkdir(parents=True, exist_ok=True)
LOCK = threading.Lock()

IDLE_THRESHOLD_SEC = 90
CONSOLIDATE_INTERVAL_SEC = 300
IDLE_POLL_SEC = 30

LLM_AUDIT_ENABLED = os.getenv("MEMQ_LLM_AUDIT_ENABLED", "0").strip() in {"1", "true", "TRUE", "yes", "on"}
OUTPUT_AUDIT_ENABLED = os.getenv("MEMQ_OUTPUT_AUDIT_ENABLED", "1").strip() in {"1", "true", "TRUE", "yes", "on"}
LLM_AUDIT_THRESHOLD = float(os.getenv("MEMQ_LLM_AUDIT_THRESHOLD", "0.20"))
LLM_AUDIT_URL = os.getenv("MEMQ_LLM_AUDIT_URL", "").strip()  # OpenAI-compatible /v1/chat/completions
LLM_AUDIT_MODEL = os.getenv("MEMQ_LLM_AUDIT_MODEL", "").strip()
LLM_AUDIT_API_KEY = os.getenv("MEMQ_LLM_AUDIT_API_KEY", "").strip()
LLM_AUDIT_TIMEOUT_SEC = float(os.getenv("MEMQ_LLM_AUDIT_TIMEOUT_SEC", "3.0"))
AUDIT_BLOCK_THRESHOLD = float(os.getenv("MEMQ_AUDIT_BLOCK_THRESHOLD", "0.85"))
AUDIT_LANG_ALWAYS_SECONDARY = os.getenv("MEMQ_AUDIT_LANG_ALWAYS_SECONDARY", "1").strip() in {"1", "true", "TRUE", "yes", "on"}
AUDIT_LANG_REPAIR_ENABLED = os.getenv("MEMQ_AUDIT_LANG_REPAIR_ENABLED", "1").strip() in {"1", "true", "TRUE", "yes", "on"}

LAST_ACTIVITY_AT = int(time.time())
LAST_CONSOLIDATE_AT = 0

PREF_TAU = {
    "tone": 21 * 24 * 3600,
    "verbosity": 14 * 24 * 3600,
    "suggestion_policy": 30 * 24 * 3600,
    "language": 30 * 24 * 3600,
    "format": 14 * 24 * 3600,
}
POLICY_TAU = {
    "retention.default": 45 * 24 * 3600,
    "ttl.default_days": 45 * 24 * 3600,
    "privacy.default": 60 * 24 * 3600,
    "remember_explicit_only": 45 * 24 * 3600,
}

SAFE_FACT_SPEC = {
    "tone": {"enum": {"keigo", "casual_polite"}, "max_len": 20, "critical": True},
    "avoid_suggestions": {"enum": {"0", "1"}, "max_len": 1, "critical": True},
    "format": {"enum": {"bullets", "plain", "table_ok"}, "max_len": 20, "critical": False},
    "language": {"enum": {"ja", "en"}, "max_len": 8, "critical": True},
    "verbosity": {"enum": {"low", "medium", "high"}, "max_len": 12, "critical": False},
    "remember_explicit_only": {"enum": {"0", "1"}, "max_len": 1, "critical": False},
}

INJECTION_PATTERNS = [
    re.compile(r"ignore\s+previous", re.I),
    re.compile(r"system\s+prompt", re.I),
    re.compile(r"developer\s+message", re.I),
    re.compile(r"\bapi[\s_-]*key\b", re.I),
    re.compile(r"you\s+are\s+chatgpt", re.I),
    re.compile(r"override\s+system", re.I),
]
SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bASIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"-----BEGIN\s+(RSA|EC|OPENSSH|PGP)\s+PRIVATE KEY-----", re.I),
    re.compile(r"\b(api[_ -]?key|secret|token|password|passwd|private[_ -]?key)\b\s*[:=]\s*\S{4,}", re.I),
]
OBFUSCATION_PATTERNS = [
    re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"),  # base64-like long token
    re.compile(r"\b[0-9a-fA-F]{40,}\b"),  # hex-like long token
]


def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def now_sec() -> int:
    return int(time.time())


def _update_activity(ts: int | None = None) -> None:
    global LAST_ACTIVITY_AT
    LAST_ACTIVITY_AT = int(ts or now_sec())


def init_db():
    with conn() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_trace (
              id TEXT PRIMARY KEY,
              ts_sec INTEGER,
              type TEXT,
              importance REAL,
              confidence REAL,
              strength REAL,
              volatility_class TEXT,
              facts_json TEXT,
              tags_json TEXT,
              raw_text TEXT,
              emb_json TEXT,
              access_count INTEGER,
              last_access_at_sec INTEGER,
              updated_at_sec INTEGER,
              retention_scope TEXT DEFAULT 'deep',
              ttl_days INTEGER DEFAULT 365,
              privacy_scope TEXT DEFAULT 'private'
            )
            """
        )
        cols = {r["name"] for r in c.execute("PRAGMA table_info(memory_trace)").fetchall()}
        if "updated_at_sec" not in cols:
            c.execute("ALTER TABLE memory_trace ADD COLUMN updated_at_sec INTEGER")
        if "retention_scope" not in cols:
            c.execute("ALTER TABLE memory_trace ADD COLUMN retention_scope TEXT DEFAULT 'deep'")
        if "ttl_days" not in cols:
            c.execute("ALTER TABLE memory_trace ADD COLUMN ttl_days INTEGER DEFAULT 365")
        if "privacy_scope" not in cols:
            c.execute("ALTER TABLE memory_trace ADD COLUMN privacy_scope TEXT DEFAULT 'private'")
        c.execute("CREATE INDEX IF NOT EXISTS idx_trace_access ON memory_trace(last_access_at_sec)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_trace_updated ON memory_trace(updated_at_sec)")

        c.execute(
            """
            CREATE TABLE IF NOT EXISTS preference_profile (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL,
              confidence REAL NOT NULL,
              updated_at INTEGER NOT NULL
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS preference_event (
              id TEXT PRIMARY KEY,
              key TEXT NOT NULL,
              value TEXT NOT NULL,
              weight REAL NOT NULL,
              explicit INTEGER NOT NULL,
              source TEXT NOT NULL,
              evidence_uri TEXT,
              created_at INTEGER NOT NULL
            )
            """
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_pref_event_key_time ON preference_event(key, created_at)")

        c.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_policy_profile (
              policy_key TEXT PRIMARY KEY,
              policy_value TEXT NOT NULL,
              confidence REAL NOT NULL,
              updated_at INTEGER NOT NULL
            )
            """
        )

        c.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_quarantine (
              id TEXT PRIMARY KEY,
              trace_id TEXT,
              raw_text TEXT,
              reason TEXT NOT NULL,
              risk_score REAL NOT NULL,
              created_at INTEGER NOT NULL
            )
            """
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_quarantine_trace ON memory_quarantine(trace_id)")

        c.execute(
            """
            CREATE TABLE IF NOT EXISTS conflict_group (
              id TEXT PRIMARY KEY,
              fact_key TEXT NOT NULL,
              members_json TEXT NOT NULL,
              policy TEXT NOT NULL,
              created_at INTEGER NOT NULL,
              updated_at INTEGER NOT NULL
            )
            """
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_conflict_fact_key ON conflict_group(fact_key)")

        c.execute(
            """
            CREATE TABLE IF NOT EXISTS capsule (
              id TEXT PRIMARY KEY,
              title TEXT NOT NULL,
              member_count INTEGER NOT NULL,
              facts_json TEXT NOT NULL,
              updated_at INTEGER NOT NULL
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS output_audit (
              id TEXT PRIMARY KEY,
              session_id TEXT,
              passed INTEGER NOT NULL,
              risk_score REAL NOT NULL,
              reasons_json TEXT NOT NULL,
              secondary_called INTEGER NOT NULL DEFAULT 0,
              secondary_blocked INTEGER NOT NULL DEFAULT 0,
              text_sample TEXT NOT NULL,
              created_at INTEGER NOT NULL
            )
            """
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_output_audit_created ON output_audit(created_at)")
        cols = {r["name"] for r in c.execute("PRAGMA table_info(output_audit)").fetchall()}
        if "secondary_called" not in cols:
            c.execute("ALTER TABLE output_audit ADD COLUMN secondary_called INTEGER NOT NULL DEFAULT 0")
        if "secondary_blocked" not in cols:
            c.execute("ALTER TABLE output_audit ADD COLUMN secondary_blocked INTEGER NOT NULL DEFAULT 0")
        c.commit()


def embed_text(text: str) -> List[float]:
    h = hashlib.sha256(text.encode("utf-8")).digest()
    arr = [((h[i % len(h)] - 127.5) / 127.5) for i in range(DIM)]
    n = math.sqrt(sum(x * x for x in arr)) or 1.0
    return [x / n for x in arr]


def cos(a: List[float], b: List[float]) -> float:
    return float(sum(x * y for x, y in zip(a, b)))


def sanitize_text(v: str, max_len: int) -> str:
    x = "".join(ch for ch in str(v) if ch.isprintable())
    return x.strip()[:max_len]


def contains_injection_like(text: str) -> bool:
    s = str(text or "")
    return any(p.search(s) is not None for p in INJECTION_PATTERNS)


def quarantine(trace_id: str | None, raw_text: str, reason: str, risk_score: float) -> None:
    with conn() as c:
        c.execute(
            """
            INSERT INTO memory_quarantine (id, trace_id, raw_text, reason, risk_score, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (uuid.uuid4().hex, trace_id, sanitize_text(raw_text, 500), reason, float(risk_score), now_sec()),
        )
        c.commit()


def sanitize_fact(key: str, value: str, source_text: str) -> Tuple[str, str] | None:
    k = sanitize_text(key.lower().replace(" ", "_"), 64)
    v = sanitize_text(value, 240)
    if not k or not v:
        return None
    if contains_injection_like(source_text) or contains_injection_like(v):
        return None
    spec = SAFE_FACT_SPEC.get(k)
    if spec is None:
        if len(v) > 120:
            return None
        return (k, v)
    if len(v) > int(spec["max_len"]):
        return None
    enum = spec.get("enum")
    if enum is not None and v not in enum:
        return None
    return (k, v)


def extract_preference_events(text: str, source: str, evidence_uri: str | None = None) -> List[Dict]:
    s = str(text or "")
    out: List[Dict] = []

    def add(key: str, value: str, weight: float, explicit: int):
        out.append(
            {
                "id": uuid.uuid4().hex,
                "key": key,
                "value": value,
                "weight": weight,
                "explicit": explicit,
                "source": source,
                "evidence_uri": evidence_uri,
                "created_at": now_sec(),
            }
        )

    if re.search(r"(敬語|keigo|polite)", s, re.I):
        add("tone", "keigo", 1.0, 1)
    if re.search(r"(カジュアル|casual)", s, re.I):
        add("tone", "casual_polite", 0.8, 1)
    if re.search(r"(余計な提案(するな|不要)|avoid extra suggestions|no suggestions)", s, re.I):
        add("suggestion_policy", "avoid_extra", 1.0, 1)
        add("avoid_suggestions", "1", 1.0, 1)
    if re.search(r"(提案して|suggest)", s, re.I):
        add("suggestion_policy", "normal", 0.5, 0)
        add("avoid_suggestions", "0", 0.5, 0)
    if re.search(r"(箇条書き|bullet)", s, re.I):
        add("format", "bullets", 0.8, 1)
    if re.search(r"(結論から|concise|short)", s, re.I):
        add("verbosity", "low", 0.7, 0)
    if re.search(r"(詳しく|detailed|detail)", s, re.I):
        add("verbosity", "high", 0.7, 0)
    if re.search(r"(日本語|japanese)", s, re.I):
        add("language", "ja", 0.9, 1)
    if re.search(r"(英語|english)", s, re.I):
        add("language", "en", 0.9, 1)
    return out


def extract_policy_events(text: str, source: str, evidence_uri: str | None = None) -> List[Dict]:
    s = str(text or "")
    out: List[Dict] = []

    def add(k: str, v: str, w: float):
        out.append(
            {
                "id": uuid.uuid4().hex,
                "key": k,
                "value": v,
                "weight": w,
                "explicit": 1,
                "source": source,
                "evidence_uri": evidence_uri,
                "created_at": now_sec(),
            }
        )

    if re.search(r"(覚えて|remember)", s, re.I):
        add("retention.default", "deep", 1.0)
        add("remember_explicit_only", "0", 0.7)
    if re.search(r"(覚えなくて|don't remember|do not remember)", s, re.I):
        add("retention.default", "surface_only", 1.0)
        add("remember_explicit_only", "1", 1.0)
    if re.search(r"(短期|temporary|short term)", s, re.I):
        add("ttl.default_days", "7", 0.9)
    if re.search(r"(長期|long term|permanent)", s, re.I):
        add("ttl.default_days", "365", 0.9)
    if re.search(r"(プライベート|private)", s, re.I):
        add("privacy.default", "private", 0.8)
    if re.search(r"(共有|shareable|public)", s, re.I):
        add("privacy.default", "shareable", 0.8)
    return out


def insert_pref_events(events: List[Dict]) -> int:
    if not events:
        return 0
    with conn() as c:
        for e in events:
            c.execute(
                """
                INSERT OR REPLACE INTO preference_event
                (id, key, value, weight, explicit, source, evidence_uri, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    e["id"],
                    e["key"],
                    e["value"],
                    e["weight"],
                    e["explicit"],
                    e["source"],
                    e.get("evidence_uri"),
                    e["created_at"],
                ),
            )
        c.commit()
    return len(events)


def refresh_profiles(now: int) -> Dict[str, int]:
    with conn() as c:
        rows = c.execute("SELECT key, value, weight, created_at FROM preference_event").fetchall()

    score: Dict[Tuple[str, str], float] = {}
    total_by_key: Dict[str, float] = {}
    for r in rows:
        key = r["key"]
        val = r["value"]
        tau = PREF_TAU.get(key, POLICY_TAU.get(key, 14 * 24 * 3600))
        decay = math.exp(-max(0, now - int(r["created_at"])) / max(1, tau))
        s = float(r["weight"]) * decay
        score[(key, val)] = score.get((key, val), 0.0) + s
        total_by_key[key] = total_by_key.get(key, 0.0) + s

    pref_rows = []
    policy_rows = []
    for key in total_by_key.keys():
        cands = [(v, s) for (k, v), s in score.items() if k == key]
        cands.sort(key=lambda x: x[1], reverse=True)
        v_star, s_star = cands[0]
        conf = s_star / max(1e-9, total_by_key[key])
        if key in ("retention.default", "ttl.default_days", "privacy.default", "remember_explicit_only"):
            policy_rows.append((key, v_star, conf, now))
        else:
            pref_rows.append((key, v_star, conf, now))

    with conn() as c:
        for r in pref_rows:
            c.execute(
                """
                INSERT INTO preference_profile (key, value, confidence, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                  value=excluded.value, confidence=excluded.confidence, updated_at=excluded.updated_at
                """,
                r,
            )
        for r in policy_rows:
            c.execute(
                """
                INSERT INTO memory_policy_profile (policy_key, policy_value, confidence, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(policy_key) DO UPDATE SET
                  policy_value=excluded.policy_value, confidence=excluded.confidence, updated_at=excluded.updated_at
                """,
                r,
            )
        c.commit()

    return {"pref_keys": len(pref_rows), "policy_keys": len(policy_rows)}


def conflict_group_refresh(now: int) -> int:
    with conn() as c:
        rows = c.execute("SELECT id, facts_json FROM memory_trace").fetchall()
    by_key_val: Dict[str, Dict[str, List[str]]] = {}
    for r in rows:
        for f in json.loads(r["facts_json"] or "[]"):
            k = str(f.get("k", "")).strip()
            v = str(f.get("v", "")).strip()
            if not k or not v:
                continue
            by_key_val.setdefault(k, {}).setdefault(v, []).append(r["id"])

    groups = []
    for k, vals in by_key_val.items():
        if len(vals.keys()) <= 1:
            continue
        members = sorted({x for ids in vals.values() for x in ids})
        gid = hashlib.sha1(f"{k}:{','.join(members)}".encode("utf-8")).hexdigest()[:24]
        groups.append((gid, k, json.dumps(members, ensure_ascii=True), "prefer_user_explicit", now, now))

    with conn() as c:
        c.execute("DELETE FROM conflict_group")
        c.executemany(
            """
            INSERT INTO conflict_group (id, fact_key, members_json, policy, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            groups,
        )
        c.commit()
    return len(groups)


def build_capsules(now: int) -> int:
    with conn() as c:
        rows = c.execute("SELECT id, type, facts_json FROM memory_trace").fetchall()
    by_type: Dict[str, List[sqlite3.Row]] = {}
    for r in rows:
        by_type.setdefault(r["type"] or "note", []).append(r)
    caps = []
    for t, items in by_type.items():
        freq: Dict[str, int] = {}
        for r in items:
            for f in json.loads(r["facts_json"] or "[]"):
                kv = f"{f.get('k','')}={f.get('v','')}"
                if kv.strip("="):
                    freq[kv] = freq.get(kv, 0) + 1
        top = [kv for kv, _ in sorted(freq.items(), key=lambda x: x[1], reverse=True)[:5]]
        facts = []
        for kv in top:
            k, v = kv.split("=", 1)
            facts.append({"k": k, "v": v})
        cid = hashlib.sha1(f"type:{t}".encode("utf-8")).hexdigest()[:24]
        caps.append((cid, f"type:{t}", len(items), json.dumps(facts, ensure_ascii=True), now))

    with conn() as c:
        c.execute("DELETE FROM capsule")
        c.executemany(
            "INSERT INTO capsule (id, title, member_count, facts_json, updated_at) VALUES (?, ?, ?, ?, ?)",
            caps,
        )
        c.commit()
    return len(caps)


def consolidate(now: int, dry_run: bool = False) -> Dict:
    removed = 0
    merged = 0
    decayed = 0

    with LOCK:
        with conn() as c:
            rows = c.execute(
                "SELECT id, type, updated_at_sec, strength, importance, volatility_class, ttl_days, last_access_at_sec, facts_json FROM memory_trace"
            ).fetchall()
            updates = []
            deletes = []
            for r in rows:
                age = max(0, now - int(r["updated_at_sec"] or r["last_access_at_sec"] or now))
                lam = 1.6e-5 if r["volatility_class"] == "high" else (8e-6 if r["volatility_class"] == "medium" else 2.5e-6)
                s = float(r["strength"]) * float(math.exp(-lam * age))
                decayed += 1
                ttl_days = int(r["ttl_days"] or 365)
                ttl_expired = age > ttl_days * 24 * 3600
                keep_type = r["type"] in {"preference", "constraint", "identity"}
                if (s < 0.05 and float(r["importance"]) < 0.2 and age > 7 * 24 * 3600 and not keep_type) or ttl_expired:
                    deletes.append((r["id"],))
                else:
                    updates.append((s, now, r["id"]))

            if not dry_run:
                c.executemany("UPDATE memory_trace SET strength=?, updated_at_sec=? WHERE id=?", updates)
                c.executemany("DELETE FROM memory_trace WHERE id=?", deletes)
            removed += len(deletes)

            rows2 = c.execute("SELECT id, type, facts_json FROM memory_trace ORDER BY importance DESC").fetchall()
            by_key: Dict[str, List[str]] = {}
            for r in rows2:
                key = hashlib.sha256(f"{r['type']}:{r['facts_json']}".encode("utf-8")).hexdigest()
                by_key.setdefault(key, []).append(r["id"])
            for ids in by_key.values():
                if len(ids) <= 1:
                    continue
                keep = ids[0]
                drops = ids[1:]
                if not dry_run:
                    c.execute(
                        "UPDATE memory_trace SET strength=MIN(1.0, strength + 0.04 * ?), importance=MIN(1.0, importance + 0.02 * ?), updated_at_sec=? WHERE id=?",
                        (len(drops), len(drops), now, keep),
                    )
                    for d in drops:
                        c.execute("DELETE FROM memory_trace WHERE id=?", (d,))
                merged += len(drops)
            if not dry_run:
                c.commit()

    conflict_n = conflict_group_refresh(now)
    profile_stat = refresh_profiles(now)
    caps_n = build_capsules(now)
    summary = {
        "ok": True,
        "removed": removed,
        "merged": merged,
        "decayed": decayed,
        "conflict_groups": conflict_n,
        "capsules": caps_n,
        "profile": profile_stat,
        "dryRun": bool(dry_run),
    }
    return summary


def profile_snapshot() -> Dict:
    with conn() as c:
        pref = c.execute("SELECT key, value, confidence, updated_at FROM preference_profile ORDER BY key").fetchall()
        pol = c.execute(
            "SELECT policy_key, policy_value, confidence, updated_at FROM memory_policy_profile ORDER BY policy_key"
        ).fetchall()
    return {
        "preferences": [
            {"key": r["key"], "value": r["value"], "confidence": float(r["confidence"]), "updatedAt": int(r["updated_at"])}
            for r in pref
        ],
        "memoryPolicies": [
            {
                "key": r["policy_key"],
                "value": r["policy_value"],
                "confidence": float(r["confidence"]),
                "updatedAt": int(r["updated_at"]),
            }
            for r in pol
        ],
    }


def quarantine_rows(limit: int) -> List[Dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT id, trace_id, raw_text, reason, risk_score, created_at FROM memory_quarantine ORDER BY created_at DESC LIMIT ?",
            (max(1, min(limit, 200)),),
        ).fetchall()
    return [
        {
            "id": r["id"],
            "traceId": r["trace_id"],
            "rawText": r["raw_text"],
            "reason": r["reason"],
            "riskScore": float(r["risk_score"]),
            "createdAt": int(r["created_at"]),
        }
        for r in rows
    ]


def detect_language_violation(text: str, allowed: List[str]) -> List[str]:
    reasons: List[str] = []
    allowed = [str(x).strip() for x in (allowed or []) if str(x).strip()]
    if not allowed:
        return reasons
    t = str(text or "")
    allowed_set = {x.lower() for x in allowed}
    if "en" not in allowed:
        if re.search(r"[A-Za-z]", t):
            reasons.append("contains_english_but_not_allowed")
    if "ja" not in allowed:
        if re.search(r"[\u3040-\u30ff\u31f0-\u31ff]", t):
            reasons.append("contains_japanese_but_not_allowed")
    if "ko" not in allowed and re.search(r"[\uAC00-\uD7AF]", t):
        reasons.append("contains_korean_but_not_allowed")
    # Cyrillic is always disallowed unless explicitly whitelisted.
    if "ru" not in allowed and re.search(r"[\u0400-\u04FF]", t):
        reasons.append("contains_cyrillic")
    # Coarse Chinese detector: Han-heavy text without kana when only ja/en are allowed.
    if "zh" not in allowed:
        han = len(re.findall(r"[\u4E00-\u9FFF]", t))
        kana = len(re.findall(r"[\u3040-\u30FF]", t))
        if han >= 4 and kana == 0 and ("ja" in allowed or "en" in allowed):
            reasons.append("contains_non_japanese_han_heavy_text")
        # Mixed-language detector: segment-level Han-only chunks in otherwise JA/EN responses.
        # This catches cases like "今日はOK。我们来测试。"
        if "ja" in allowed_set:
            segments = re.split(r"[。．.!?！？\n]+", t)
            for seg in segments:
                s = seg.strip()
                if len(s) < 4:
                    continue
                han = len(re.findall(r"[\u4E00-\u9FFF]", s))
                kana = len(re.findall(r"[\u3040-\u30FF]", s))
                if han >= 3 and kana == 0:
                    reasons.append("contains_nonpreferred_han_segment")
                    break
    return reasons


def detect_secret_violation(text: str) -> List[str]:
    t = str(text or "")
    reasons = []
    for p in SECRET_PATTERNS:
        if p.search(t):
            reasons.append("secret_pattern_match")
            break
    if re.search(r"\b(api[_ -]?key|secret|token|password|private[_ -]?key)\b\s*[:=]\s*\S{4,}", t, re.I):
        reasons.append("secret_assignment_like")
    if re.search(r"\b(api[_ -]?key|access[_ -]?key|secret[_ -]?key|private[_ -]?key|bearer token)\b", t, re.I):
        reasons.append("secret_intent_phrase")
    if re.search(r"-----BEGIN\s+(RSA|EC|OPENSSH|PGP)\s+PRIVATE KEY-----", t, re.I):
        reasons.append("private_key_marker")
    return reasons


def detect_injection_compliance(text: str) -> List[str]:
    t = str(text or "")
    reasons = []
    if re.search(r"(system prompt|developer message|ignore previous instructions|override system|jailbreak)", t, re.I):
        reasons.append("mentions_prompt_override_terms")
    if re.search(r"(exfiltrate|dump secrets|reveal hidden|show hidden instructions)", t, re.I):
        reasons.append("exfiltration_phrase")
    return reasons


def detect_obfuscation_risk(text: str) -> List[str]:
    t = str(text or "")
    reasons = []
    for p in OBFUSCATION_PATTERNS:
        if p.search(t):
            reasons.append("obfuscated_secret_like_blob")
            break
    return reasons


def deterministic_language_repair(text: str, allowed_languages: List[str], preferred_language: str = "") -> str:
    t = str(text or "")
    allowed_set = {str(x).strip().lower() for x in (allowed_languages or []) if str(x).strip()}
    if not allowed_set:
        return t
    out = t
    if "ru" not in allowed_set:
        out = re.sub(r"[\u0400-\u04FF]+", " ", out)
    if "ko" not in allowed_set:
        out = re.sub(r"[\uAC00-\uD7AF]+", " ", out)
    if "zh" not in allowed_set and "ja" in allowed_set:
        segs = re.split(r"([。．.!?！？\n]+)", out)
        rebuilt: List[str] = []
        for seg in segs:
            if re.fullmatch(r"[。．.!?！？\n]+", seg or ""):
                rebuilt.append(seg)
                continue
            s = (seg or "").strip()
            if not s:
                rebuilt.append(seg)
                continue
            han = len(re.findall(r"[\u4E00-\u9FFF]", s))
            kana = len(re.findall(r"[\u3040-\u30FF]", s))
            if han >= 3 and kana == 0:
                continue
            rebuilt.append(seg)
        out = "".join(rebuilt)
    if "en" not in allowed_set:
        out = re.sub(r"[A-Za-z]+", " ", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def secondary_llm_audit(text: str, allowed_languages: List[str], preferred_language: str = "", want_repair: bool = False) -> Dict:
    if not (LLM_AUDIT_ENABLED and LLM_AUDIT_URL and LLM_AUDIT_MODEL):
        return {"enabled": False, "called": False}
    snippet = sanitize_text(text, 480)
    allow_str = ",".join(allowed_languages) if allowed_languages else "(none)"
    system = (
        "You are a strict output security auditor. "
        "Return JSON only: {\"block\":bool,\"reasons\":[string],\"risk\":number,\"repairText\":string}. "
        "If only language policy is violated and no secret leakage exists, provide repairText in allowed languages. "
        "Prefer preferred_language when present."
    )
    user = (
        f"Allowed languages: {allow_str}\n"
        f"Preferred language: {(preferred_language or '(none)')}\n"
        f"Output text:\n{snippet}\n"
        "Judge policy violation. Keep reasons short."
    )
    payload = {
        "model": LLM_AUDIT_MODEL,
        "temperature": 0,
        "max_tokens": 220 if want_repair else 100,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }
    headers = {"content-type": "application/json"}
    if LLM_AUDIT_API_KEY:
        headers["authorization"] = f"Bearer {LLM_AUDIT_API_KEY}"

    def _call(p: Dict) -> Dict:
        req = Request(LLM_AUDIT_URL, data=json.dumps(p).encode("utf-8"), headers=headers, method="POST")
        with urlopen(req, timeout=LLM_AUDIT_TIMEOUT_SEC) as r:
            return json.loads(r.read().decode("utf-8"))

    try:
        raw = _call(payload)
    except Exception:
        # Provider compatibility fallback: retry without response_format.
        payload2 = dict(payload)
        payload2.pop("response_format", None)
        try:
            raw = _call(payload2)
        except Exception as e:
            return {
                "enabled": True,
                "called": True,
                "ok": False,
                "block": False,
                "risk": 0.0,
                "reasons": [f"llm_audit_error:{sanitize_text(str(e), 80)}"],
            }

    try:
        content = raw.get("choices", [{}])[0].get("message", {}).get("content", "{}")
        if isinstance(content, str):
            try:
                parsed = json.loads(content)
            except Exception:
                m = re.search(r"\{.*\}", content, re.S)
                parsed = json.loads(m.group(0)) if m else {}
        else:
            parsed = {}
        block = bool(parsed.get("block", False))
        reasons = parsed.get("reasons", [])
        if not isinstance(reasons, list):
            reasons = [str(reasons)]
        risk = float(parsed.get("risk", 0.8 if block else 0.0))
        repair_text = sanitize_text(str(parsed.get("repairText", "")), 1200)
        return {
            "enabled": True,
            "called": True,
            "ok": True,
            "block": block,
            "risk": max(0.0, min(1.0, risk)),
            "reasons": [sanitize_text(str(x), 80) for x in reasons][:6],
            "repairText": repair_text,
        }
    except Exception as e:
        return {
            "enabled": True,
            "called": True,
            "ok": False,
            "block": False,
            "risk": 0.0,
            "reasons": [f"llm_audit_parse_error:{sanitize_text(str(e), 80)}"],
        }


def audit_output_text(session_id: str, text: str, allowed_languages: List[str], preferred_language: str = "") -> Dict:
    if not OUTPUT_AUDIT_ENABLED:
        return {
            "ok": True,
            "passed": True,
            "riskScore": 0.0,
            "reasons": ["output_audit_disabled"],
            "secondary": {"enabled": LLM_AUDIT_ENABLED, "called": False},
            "repairedApplied": False,
            "repairedText": "",
        }
    lang_r = detect_language_violation(text, allowed_languages)
    secret_r = detect_secret_violation(text)
    inj_r = detect_injection_compliance(text)
    obf_r = detect_obfuscation_risk(text)
    reasons = lang_r + secret_r + inj_r + obf_r
    risk = 0.0
    if reasons:
        weights = {
            "secret_pattern_match": 0.65,
            "private_key_marker": 0.75,
            "secret_assignment_like": 0.45,
            "secret_intent_phrase": 0.20,
            "mentions_prompt_override_terms": 0.35,
            "exfiltration_phrase": 0.45,
            "obfuscated_secret_like_blob": 0.35,
            "contains_english_but_not_allowed": 0.2,
            "contains_japanese_but_not_allowed": 0.2,
            "contains_korean_but_not_allowed": 0.2,
            "contains_cyrillic": 0.25,
            "contains_non_japanese_han_heavy_text": 0.2,
            "contains_nonpreferred_han_segment": 0.2,
        }
        risk = min(1.0, sum(weights.get(r, 0.15) for r in reasons))
        cats = {
            "secret": bool(secret_r),
            "policy": bool(inj_r),
            "lang": bool(lang_r),
            "obf": bool(obf_r),
        }
        if sum(1 for v in cats.values() if v) >= 2:
            risk = min(1.0, risk + 0.15)
    secondary = {"enabled": LLM_AUDIT_ENABLED, "called": False}
    has_lang_violation = len(lang_r) > 0
    secondary_trigger = (
        risk >= LLM_AUDIT_THRESHOLD
        or ("secret_intent_phrase" in reasons)
        or ("obfuscated_secret_like_blob" in reasons)
        or (AUDIT_LANG_ALWAYS_SECONDARY and has_lang_violation)
    )
    repaired_applied = False
    repaired_text = ""
    if secondary_trigger:
        secondary = secondary_llm_audit(
            text,
            allowed_languages,
            preferred_language=preferred_language,
            want_repair=bool(has_lang_violation and AUDIT_LANG_REPAIR_ENABLED),
        )
        if secondary.get("called") and secondary.get("block"):
            reasons.extend([f"llm:{r}" for r in secondary.get("reasons", [])])
            risk = max(risk, float(secondary.get("risk", 0.9)))
        candidate = str(secondary.get("repairText", "")).strip()
        if has_lang_violation and candidate:
            post_lang = detect_language_violation(candidate, allowed_languages)
            if not post_lang:
                repaired_applied = True
                repaired_text = candidate
                reasons = [r for r in reasons if r not in lang_r]
                if not secret_r and not inj_r and not obf_r:
                    risk = min(risk, 0.05)
    if has_lang_violation and not repaired_applied and AUDIT_LANG_REPAIR_ENABLED:
        candidate = deterministic_language_repair(text, allowed_languages, preferred_language)
        if candidate and candidate != text:
            post_lang = detect_language_violation(candidate, allowed_languages)
            if not post_lang:
                repaired_applied = True
                repaired_text = candidate
                reasons = [r for r in reasons if r not in lang_r]
                if not secret_r and not inj_r and not obf_r:
                    risk = min(risk, 0.05)
    unresolved_lang_violation = bool(has_lang_violation and not repaired_applied)
    if unresolved_lang_violation and "unresolved_language_violation" not in reasons:
        reasons.append("unresolved_language_violation")
    blocked = bool(
        unresolved_lang_violation or risk >= AUDIT_BLOCK_THRESHOLD or (secondary.get("called") and secondary.get("block"))
    )
    passed = 0 if blocked else 1
    rec = {
        "id": uuid.uuid4().hex,
        "session_id": session_id,
        "passed": passed,
        "risk_score": risk,
        "reasons_json": json.dumps(reasons, ensure_ascii=True),
        "secondary_called": 1 if secondary.get("called") else 0,
        "secondary_blocked": 1 if secondary.get("called") and secondary.get("block") else 0,
        "text_sample": sanitize_text(text, 220),
        "created_at": now_sec(),
    }
    with conn() as c:
        c.execute(
            """
            INSERT INTO output_audit (id, session_id, passed, risk_score, reasons_json, secondary_called, secondary_blocked, text_sample, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rec["id"],
                rec["session_id"],
                rec["passed"],
                rec["risk_score"],
                rec["reasons_json"],
                rec["secondary_called"],
                rec["secondary_blocked"],
                rec["text_sample"],
                rec["created_at"],
            ),
        )
        c.commit()
    if not passed:
        quarantine(None, text, "output_policy_violation", risk)
    return {
        "ok": True,
        "passed": bool(passed),
        "riskScore": risk,
        "reasons": reasons,
        "secondary": secondary,
        "repairedApplied": repaired_applied,
        "repairedText": repaired_text,
    }


def idle_loop():
    global LAST_CONSOLIDATE_AT
    while True:
        time.sleep(IDLE_POLL_SEC)
        now = now_sec()
        if now - LAST_ACTIVITY_AT < IDLE_THRESHOLD_SEC:
            continue
        if now - LAST_CONSOLIDATE_AT < CONSOLIDATE_INTERVAL_SEC:
            continue
        try:
            summary = consolidate(now, dry_run=False)
            LAST_CONSOLIDATE_AT = now
            print(f"[idle-consolidate] {json.dumps(summary, ensure_ascii=True)}", flush=True)
        except Exception as e:
            print(f"[idle-consolidate] error: {e}", flush=True)


class H(BaseHTTPRequestHandler):
    def _read_json(self) -> Dict:
        n = int(self.headers.get("Content-Length", "0"))
        if n <= 0:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def _json(self, obj: Dict, code: int = 200):
        b = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        _update_activity()
        pr = urlparse(self.path)
        p = pr.path
        qs = parse_qs(pr.query)
        if p == "/health":
            with conn() as c:
                n = c.execute("SELECT COUNT(*) n FROM memory_trace").fetchone()["n"]
            return self._json(
                {
                    "ok": True,
                    "size": int(n),
                    "dim": DIM,
                    "idleThresholdSec": IDLE_THRESHOLD_SEC,
                    "lastActivityAt": LAST_ACTIVITY_AT,
                    "lastConsolidateAt": LAST_CONSOLIDATE_AT,
                }
            )
        if p == "/stats":
            with conn() as c:
                row = c.execute("SELECT COUNT(*) n, COALESCE(AVG(strength),0) s FROM memory_trace").fetchone()
                q = c.execute("SELECT COUNT(*) n FROM memory_quarantine").fetchone()
                cg = c.execute("SELECT COUNT(*) n FROM conflict_group").fetchone()
                oa = c.execute(
                    "SELECT COUNT(*) n, COALESCE(SUM(CASE WHEN passed=0 THEN 1 ELSE 0 END),0) v, "
                    "COALESCE(SUM(secondary_called),0) sc, COALESCE(SUM(secondary_blocked),0) sb "
                    "FROM output_audit"
                ).fetchone()
            return self._json(
                {
                    "size": int(row["n"]),
                    "avgStrength": float(row["s"]),
                    "quarantineSize": int(q["n"]),
                    "conflictGroups": int(cg["n"]),
                    "outputAuditCount": int(oa["n"]),
                    "outputAuditViolations": int(oa["v"]),
                    "secondaryAuditCalled": int(oa["sc"]),
                    "secondaryAuditBlocked": int(oa["sb"]),
                    "lastActivityAt": LAST_ACTIVITY_AT,
                    "lastConsolidateAt": LAST_CONSOLIDATE_AT,
                }
            )
        if p == "/audit/stats":
            with conn() as c:
                row = c.execute(
                    "SELECT COUNT(*) n, COALESCE(SUM(CASE WHEN passed=0 THEN 1 ELSE 0 END),0) v, "
                    "COALESCE(AVG(risk_score),0) r, COALESCE(SUM(secondary_called),0) sc, COALESCE(SUM(secondary_blocked),0) sb "
                    "FROM output_audit"
                ).fetchone()
            return self._json(
                {
                    "count": int(row["n"]),
                    "violations": int(row["v"]),
                    "avgRisk": float(row["r"]),
                    "passRate": 0.0 if int(row["n"]) == 0 else float((int(row["n"]) - int(row["v"])) / int(row["n"])),
                    "secondaryCalled": int(row["sc"]),
                    "secondaryBlocked": int(row["sb"]),
                }
            )
        if p == "/profile":
            return self._json(profile_snapshot())
        if p == "/quarantine":
            lim = int((qs.get("limit", ["20"])[0] or "20"))
            return self._json({"items": quarantine_rows(lim)})
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        body = self._read_json()
        p = self.path.split("?", 1)[0]
        _update_activity(body.get("nowSec") if isinstance(body, dict) else None)

        if p == "/embed":
            return self._json({"vector": embed_text(str(body.get("text", "")))})

        if p == "/idle_tick":
            return self._json({"ok": True, "lastActivityAt": LAST_ACTIVITY_AT})

        if p == "/preference/event":
            events = body.get("events", [])
            wrote = insert_pref_events(events)
            refresh_profiles(now_sec())
            return self._json({"ok": True, "written": wrote})

        if p == "/audit/output":
            sid = str(body.get("sessionId", "default"))
            text = str(body.get("text", ""))
            allowed = body.get("allowedLanguages") or []
            preferred = str(body.get("preferredLanguage", "") or "").strip()
            if not isinstance(allowed, list):
                allowed = []
            allowed = [str(x).strip() for x in allowed if str(x).strip()]
            return self._json(audit_output_text(sid, text, allowed, preferred_language=preferred))

        if p == "/index/add":
            trace_id = str(body.get("id"))
            raw_text = str(body.get("rawText", ""))
            if contains_injection_like(raw_text):
                quarantine(trace_id, raw_text, "prompt_injection_like", 0.95)
                return self._json({"ok": False, "quarantined": True, "reason": "prompt_injection_like"})

            facts_in = body.get("facts", [])
            sanitized = []
            for f in facts_in:
                key = str(f.get("k", ""))
                val = str(f.get("v", ""))
                sv = sanitize_fact(key, val, raw_text)
                if sv is None:
                    quarantine(trace_id, f"{key}={val}", "invalid_or_injection_fact", 0.8)
                    continue
                sanitized.append({"k": sv[0], "v": sv[1], "conf": float(f.get("conf", 0.6))})

            events = extract_preference_events(raw_text, "user_msg", body.get("evidenceUri"))
            policy_events = extract_policy_events(raw_text, "memory_policy", body.get("evidenceUri"))
            insert_pref_events(events + policy_events)
            refresh_profiles(now_sec())

            # Apply learned memory policy defaults.
            pol = profile_snapshot()["memoryPolicies"]
            polmap = {x["key"]: x["value"] for x in pol}
            retention = str(polmap.get("retention.default", "deep"))
            ttl_days = int(polmap.get("ttl.default_days", "365"))
            privacy = str(polmap.get("privacy.default", "private"))

            with LOCK:
                with conn() as c:
                    c.execute(
                        """
                        INSERT INTO memory_trace
                        (id, ts_sec, type, importance, confidence, strength, volatility_class,
                         facts_json, tags_json, raw_text, emb_json, access_count, last_access_at_sec,
                         updated_at_sec, retention_scope, ttl_days, privacy_scope)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET
                          ts_sec=excluded.ts_sec,
                          type=excluded.type,
                          importance=excluded.importance,
                          confidence=excluded.confidence,
                          strength=excluded.strength,
                          volatility_class=excluded.volatility_class,
                          facts_json=excluded.facts_json,
                          tags_json=excluded.tags_json,
                          raw_text=excluded.raw_text,
                          emb_json=excluded.emb_json,
                          updated_at_sec=excluded.updated_at_sec,
                          retention_scope=excluded.retention_scope,
                          ttl_days=excluded.ttl_days,
                          privacy_scope=excluded.privacy_scope
                        """,
                        (
                            trace_id,
                            int(body.get("tsSec", now_sec())),
                            body.get("type", "note"),
                            float(body.get("importance", 0.5)),
                            float(body.get("confidence", 0.7)),
                            float(body.get("strength", 0.5)),
                            body.get("volatilityClass", "medium"),
                            json.dumps(sanitized, ensure_ascii=True),
                            json.dumps(body.get("tags", []), ensure_ascii=True),
                            raw_text,
                            json.dumps(body.get("vector", [])),
                            now_sec(),
                            now_sec(),
                            retention,
                            ttl_days,
                            privacy,
                        ),
                    )
                    c.commit()
            return self._json({"ok": True, "acceptedFacts": len(sanitized), "quarantinedFacts": len(facts_in) - len(sanitized)})

        if p == "/index/search":
            q = body.get("vector") or [0.0] * DIM
            k = max(1, int(body.get("k", 5)))
            with conn() as c:
                rows = c.execute(
                    """
                    SELECT * FROM memory_trace
                    WHERE id NOT IN (SELECT trace_id FROM memory_quarantine WHERE trace_id IS NOT NULL)
                      AND retention_scope != 'surface_only'
                    """
                ).fetchall()
            scored = []
            for r in rows:
                v = json.loads(r["emb_json"]) if r["emb_json"] else [0.0] * DIM
                scored.append((cos(q, v), r))
            scored.sort(key=lambda x: x[0], reverse=True)
            items = []
            for s, r in scored[:k]:
                items.append(
                    {
                        "id": r["id"],
                        "score": float(s),
                        "tsSec": int(r["ts_sec"]),
                        "type": r["type"],
                        "importance": float(r["importance"]),
                        "confidence": float(r["confidence"]),
                        "strength": float(r["strength"]),
                        "volatilityClass": r["volatility_class"],
                        "facts": json.loads(r["facts_json"] or "[]"),
                        "tags": json.loads(r["tags_json"] or "[]"),
                        "rawText": r["raw_text"],
                        "accessCount": int(r["access_count"] or 0),
                        "lastAccessAtSec": int(r["last_access_at_sec"] or 0),
                        "updatedAtSec": int(r["updated_at_sec"] or r["ts_sec"]),
                    }
                )
            return self._json({"items": items})

        if p == "/index/touch":
            ids = body.get("ids", [])
            now = now_sec()
            with LOCK:
                with conn() as c:
                    for id_ in ids:
                        c.execute(
                            "UPDATE memory_trace SET access_count=access_count+1,last_access_at_sec=?,updated_at_sec=?,strength=MIN(1.0,strength+0.12) WHERE id=?",
                            (now, now, id_),
                        )
                    c.commit()
            return self._json({"ok": True, "touched": len(ids)})

        if p == "/consolidate" or p == "/index/consolidate":
            dry = bool(body.get("dryRun", False))
            summary = consolidate(int(body.get("nowSec", now_sec())), dry_run=dry)
            global LAST_CONSOLIDATE_AT
            LAST_CONSOLIDATE_AT = now_sec()
            return self._json(summary)

        if p == "/index/rebuild":
            return self._json({"ok": True})

        return self._json({"error": "not found"}, 404)


def main():
    init_db()
    threading.Thread(target=idle_loop, daemon=True).start()
    s = HTTPServer(("127.0.0.1", 7781), H)
    print("minisidecar listening on 127.0.0.1:7781")
    s.serve_forever()


if __name__ == "__main__":
    main()
