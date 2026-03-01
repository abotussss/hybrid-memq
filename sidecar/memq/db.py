from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .fact_keys import infer_text_fact_keys


def now_ts() -> int:
    return int(time.time())


def normalize_sig(text: str) -> str:
    raw = " ".join((text or "").strip().lower().split())
    sig = re.sub(r"[^a-z0-9ぁ-んァ-ヶ一-龠]+", "", raw)
    return sig or raw


def token_set(text: str) -> set[str]:
    s = (text or "").lower()
    out = set(re.findall(r"[a-z0-9_]{2,}", s))
    out.update(re.findall(r"[ぁ-んァ-ヶ一-龠]{1,8}", text or ""))
    return out


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return float(len(a & b)) / float(max(1, len(a | b)))


def infer_fact_keys_from_text(text: str) -> List[str]:
    return infer_text_fact_keys(text)


@dataclass
class MemoryRow:
    id: str
    session_key: str
    layer: str
    created_at: int
    updated_at: int
    last_access_at: int
    ttl_expires_at: Optional[int]
    importance: float
    usage_count: int
    text: str
    summary: str
    tags: str
    emb_f16: Optional[bytes]
    emb_q: Optional[bytes]
    emb_dim: int
    emb_norm: float
    source: str


class MemqDB:
    def __init__(self, db_path: Path):
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        self._init_schema()

    def _init_schema(self) -> None:
        c = self.conn.cursor()
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS memory_items (
              id TEXT PRIMARY KEY,
              session_key TEXT NOT NULL,
              layer TEXT NOT NULL,
              created_at INTEGER NOT NULL,
              updated_at INTEGER NOT NULL,
              last_access_at INTEGER NOT NULL,
              ttl_expires_at INTEGER NULL,
              importance REAL NOT NULL DEFAULT 0.5,
              usage_count INTEGER NOT NULL DEFAULT 0,
              text TEXT NOT NULL,
              summary TEXT NOT NULL,
              tags TEXT NOT NULL DEFAULT '[]',
              emb_f16 BLOB NULL,
              emb_q BLOB NULL,
              emb_dim INTEGER NOT NULL DEFAULT 0,
              emb_norm REAL NOT NULL DEFAULT 1.0,
              source TEXT NOT NULL DEFAULT 'turn'
            );
            CREATE INDEX IF NOT EXISTS idx_memory_layer_session ON memory_items(layer, session_key);
            CREATE INDEX IF NOT EXISTS idx_memory_last_access ON memory_items(last_access_at);
            CREATE INDEX IF NOT EXISTS idx_memory_ttl ON memory_items(ttl_expires_at);

            CREATE TABLE IF NOT EXISTS conv_summaries (
              id TEXT PRIMARY KEY,
              session_key TEXT NOT NULL,
              retention_scope TEXT NOT NULL,
              updated_at INTEGER NOT NULL,
              summary TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_conv_session_scope ON conv_summaries(session_key, retention_scope);

            CREATE TABLE IF NOT EXISTS rules (
              id TEXT PRIMARY KEY,
              priority INTEGER NOT NULL,
              enabled INTEGER NOT NULL,
              kind TEXT NOT NULL,
              body TEXT NOT NULL,
              updated_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_rules_kind ON rules(kind);

            CREATE TABLE IF NOT EXISTS style_profile (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL,
              updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS preference_profile (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL,
              confidence REAL NOT NULL,
              updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS preference_event (
              id TEXT PRIMARY KEY,
              key TEXT NOT NULL,
              value TEXT NOT NULL,
              weight REAL NOT NULL,
              explicit INTEGER NOT NULL,
              source TEXT NOT NULL,
              evidence_uri TEXT NULL,
              created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_pref_event_key_time ON preference_event(key, created_at);

            CREATE TABLE IF NOT EXISTS memory_policy_profile (
              policy_key TEXT PRIMARY KEY,
              policy_value TEXT NOT NULL,
              confidence REAL NOT NULL,
              updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS memory_quarantine (
              id TEXT PRIMARY KEY,
              trace_id TEXT,
              raw_text TEXT,
              reason TEXT NOT NULL,
              risk_score REAL NOT NULL,
              created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS conflict_group (
              id TEXT PRIMARY KEY,
              fact_key TEXT NOT NULL,
              members_json TEXT NOT NULL,
              policy TEXT NOT NULL,
              created_at INTEGER NOT NULL,
              updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS audit_events (
              id TEXT PRIMARY KEY,
              sessionKey TEXT NOT NULL,
              ts INTEGER NOT NULL,
              risk REAL NOT NULL,
              block INTEGER NOT NULL,
              reasons TEXT NOT NULL,
              sample_hash TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_events(ts);

            CREATE TABLE IF NOT EXISTS kv_state (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL,
              updated_at INTEGER NOT NULL
            );
            """
        )
        self.conn.commit()

        # migrate away from over-restrictive legacy key-refusal rules
        ts = now_ts()
        self.conn.execute(
            "DELETE FROM rules WHERE id='r_security_refuse_keys' OR body LIKE 'compliance.refuse_api_keys=%'"
        )
        self.conn.execute(
            "UPDATE rules SET body='security.never_output_secrets=true', kind='security', updated_at=? WHERE body='security.no_secrets=true'",
            (ts,),
        )

        # default hard rules (output-safe, no input-side refusal)
        defaults = [
            ("r_security_never_output_secrets", 100, 1, "security", "security.never_output_secrets=true", ts),
            ("r_operation_allow_local_config", 90, 1, "operation", "operation.allow_user_requested_local_config=true", ts),
        ]
        for row in defaults:
            self.conn.execute(
                "INSERT OR IGNORE INTO rules(id,priority,enabled,kind,body,updated_at) VALUES(?,?,?,?,?,?)",
                row,
            )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def set_state(self, key: str, value: str) -> None:
        ts = now_ts()
        self.conn.execute(
            "INSERT INTO kv_state(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            (key, value, ts),
        )
        self.conn.commit()

    def get_state(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM kv_state WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        return str(row["value"])

    def add_memory_item(
        self,
        *,
        session_key: str,
        layer: str,
        text: str,
        summary: str,
        importance: float,
        tags: Dict[str, Any],
        emb_f16: Optional[bytes],
        emb_q: Optional[bytes],
        emb_dim: int,
        ttl_expires_at: Optional[int] = None,
        source: str = "turn",
    ) -> str:
        item_id = str(uuid.uuid4())
        ts = now_ts()
        self.conn.execute(
            """
            INSERT INTO memory_items(
              id,session_key,layer,created_at,updated_at,last_access_at,ttl_expires_at,
              importance,usage_count,text,summary,tags,emb_f16,emb_q,emb_dim,emb_norm,source
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                item_id,
                session_key,
                layer,
                ts,
                ts,
                ts,
                ttl_expires_at,
                float(importance),
                0,
                text,
                summary,
                json.dumps(tags, ensure_ascii=False),
                emb_f16,
                emb_q,
                int(emb_dim),
                1.0,
                source,
            ),
        )
        self.conn.commit()
        return item_id

    def add_or_merge_memory_item(
        self,
        *,
        session_key: str,
        layer: str,
        text: str,
        summary: str,
        importance: float,
        tags: Dict[str, Any],
        emb_f16: Optional[bytes],
        emb_q: Optional[bytes],
        emb_dim: int,
        ttl_expires_at: Optional[int] = None,
        source: str = "turn",
        similar_threshold: float = 0.86,
    ) -> Tuple[str, bool]:
        """Insert new memory item, or merge into a near-duplicate recent item.
        Returns: (item_id, merged_existing)
        """
        ts = now_ts()
        new_sig = normalize_sig(summary)
        new_tok = token_set(summary)
        rows = self.conn.execute(
            """
            SELECT id,summary,tags,importance,usage_count,updated_at FROM memory_items
            WHERE layer=? AND session_key=?
            ORDER BY updated_at DESC
            LIMIT 300
            """,
            (layer, session_key),
        ).fetchall()
        for r in rows:
            old_summary = str(r["summary"] or "")
            old_sig = normalize_sig(old_summary)
            old_tok = token_set(old_summary)
            sim = jaccard(new_tok, old_tok)
            if old_sig == new_sig:
                sim = 1.0
            elif len(new_sig) >= 20 and len(old_sig) >= 20 and (new_sig in old_sig or old_sig in new_sig):
                sim = max(sim, 0.93)
            if sim < similar_threshold:
                continue

            rid = str(r["id"])
            try:
                old_tags = json.loads(str(r["tags"] or "{}"))
            except Exception:
                old_tags = {}
            merged_tags = dict(old_tags)
            for k, v in (tags or {}).items():
                if k == "fact_keys":
                    a = set(old_tags.get("fact_keys") or [])
                    b = set(v or [])
                    merged_tags["fact_keys"] = sorted([x for x in (a | b) if x])
                else:
                    merged_tags[k] = v

            new_importance = max(float(r["importance"]), float(importance))
            self.conn.execute(
                """
                UPDATE memory_items
                SET text=?, summary=?, importance=?, tags=?, emb_f16=?, emb_q=?, emb_dim=?, updated_at=?, last_access_at=?, source=?
                WHERE id=?
                """,
                (
                    text,
                    summary,
                    new_importance,
                    json.dumps(merged_tags, ensure_ascii=False),
                    emb_f16,
                    emb_q,
                    int(emb_dim),
                    ts,
                    ts,
                    source,
                    rid,
                ),
            )
            self.conn.commit()
            return rid, True

        item_id = self.add_memory_item(
            session_key=session_key,
            layer=layer,
            text=text,
            summary=summary,
            importance=importance,
            tags=tags,
            emb_f16=emb_f16,
            emb_q=emb_q,
            emb_dim=emb_dim,
            ttl_expires_at=ttl_expires_at,
            source=source,
        )
        return item_id, False

    def list_memory_items(self, layer: str, session_key: str, limit: int = 5000) -> List[sqlite3.Row]:
        now = now_ts()
        rows = self.conn.execute(
            """
            SELECT * FROM memory_items
            WHERE layer=?
              AND (session_key=? OR session_key='global')
              AND (ttl_expires_at IS NULL OR ttl_expires_at > ?)
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (layer, session_key, now, int(limit)),
        ).fetchall()
        return rows

    def list_memory_items_any(self, layer: str, limit: int = 5000) -> List[sqlite3.Row]:
        now = now_ts()
        rows = self.conn.execute(
            """
            SELECT * FROM memory_items
            WHERE layer=?
              AND (ttl_expires_at IS NULL OR ttl_expires_at > ?)
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (layer, now, int(limit)),
        ).fetchall()
        return rows

    def touch_items(self, item_ids: Sequence[str]) -> None:
        if not item_ids:
            return
        ts = now_ts()
        for item_id in item_ids:
            self.conn.execute(
                "UPDATE memory_items SET last_access_at=?, usage_count=usage_count+1, updated_at=? WHERE id=?",
                (ts, ts, item_id),
            )
        self.conn.commit()

    def upsert_conv_summary(self, session_key: str, retention_scope: str, summary: str) -> str:
        ts = now_ts()
        row = self.conn.execute(
            "SELECT id FROM conv_summaries WHERE session_key=? AND retention_scope=?",
            (session_key, retention_scope),
        ).fetchone()
        if row:
            cid = str(row["id"])
            self.conn.execute(
                "UPDATE conv_summaries SET summary=?, updated_at=? WHERE id=?",
                (summary, ts, cid),
            )
            self.conn.commit()
            return cid
        cid = str(uuid.uuid4())
        self.conn.execute(
            "INSERT INTO conv_summaries(id,session_key,retention_scope,updated_at,summary) VALUES(?,?,?,?,?)",
            (cid, session_key, retention_scope, ts, summary),
        )
        self.conn.commit()
        return cid

    def get_conv_summary(self, session_key: str, retention_scope: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT summary FROM conv_summaries WHERE session_key=? AND retention_scope=?",
            (session_key, retention_scope),
        ).fetchone()
        if not row:
            return None
        return str(row["summary"])

    def list_rules(self) -> List[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM rules WHERE enabled=1 ORDER BY priority DESC, updated_at DESC"
        ).fetchall()

    def prune_stale_user_rules(self, now_sec: int, max_age_sec: int = 86400 * 45) -> int:
        rows = self.conn.execute(
            "SELECT id,kind,updated_at FROM rules WHERE id LIKE 'user_%' AND enabled=1"
        ).fetchall()
        removed = 0
        for r in rows:
            rid = str(r["id"])
            kind = str(r["kind"])
            updated = int(r["updated_at"])
            if now_sec - updated <= max_age_sec:
                continue
            # keep language/security overrides longer; expire procedural hints first
            if kind in {"security", "language"}:
                continue
            self.conn.execute("UPDATE rules SET enabled=0, updated_at=? WHERE id=?", (now_sec, rid))
            removed += 1
        self.conn.commit()
        return removed

    def upsert_rule(self, rule_id: str, priority: int, enabled: bool, kind: str, body: str) -> None:
        ts = now_ts()
        self.conn.execute(
            "INSERT INTO rules(id,priority,enabled,kind,body,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET priority=excluded.priority,enabled=excluded.enabled,kind=excluded.kind,body=excluded.body,updated_at=excluded.updated_at",
            (rule_id, int(priority), 1 if enabled else 0, kind, body, ts),
        )
        self.conn.commit()

    def upsert_style(self, key: str, value: str) -> None:
        ts = now_ts()
        self.conn.execute(
            "INSERT INTO style_profile(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            (key, value, ts),
        )
        self.conn.commit()

    def get_style_profile(self) -> Dict[str, str]:
        rows = self.conn.execute("SELECT key,value FROM style_profile ORDER BY key").fetchall()
        return {str(r["key"]): str(r["value"]) for r in rows}

    def add_preference_event(
        self,
        *,
        key: str,
        value: str,
        weight: float,
        explicit: bool,
        source: str,
        evidence_uri: Optional[str],
        created_at: Optional[int] = None,
    ) -> str:
        ev_id = str(uuid.uuid4())
        ts = int(created_at or now_ts())
        self.conn.execute(
            "INSERT INTO preference_event(id,key,value,weight,explicit,source,evidence_uri,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (ev_id, key, value, float(weight), 1 if explicit else 0, source, evidence_uri, ts),
        )
        self.conn.commit()
        return ev_id

    def iter_preference_events(self, key: str) -> List[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM preference_event WHERE key=? ORDER BY created_at DESC",
            (key,),
        ).fetchall()

    def upsert_preference_profile(self, key: str, value: str, confidence: float) -> None:
        ts = now_ts()
        self.conn.execute(
            "INSERT INTO preference_profile(key,value,confidence,updated_at) VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,confidence=excluded.confidence,updated_at=excluded.updated_at",
            (key, value, float(confidence), ts),
        )
        self.conn.commit()

    def get_preference_profile(self) -> Dict[str, Dict[str, Any]]:
        rows = self.conn.execute("SELECT key,value,confidence,updated_at FROM preference_profile").fetchall()
        return {
            str(r["key"]): {
                "value": str(r["value"]),
                "confidence": float(r["confidence"]),
                "updated_at": int(r["updated_at"]),
            }
            for r in rows
        }

    def upsert_memory_policy(self, key: str, value: str, confidence: float) -> None:
        ts = now_ts()
        self.conn.execute(
            "INSERT INTO memory_policy_profile(policy_key,policy_value,confidence,updated_at) VALUES(?,?,?,?) ON CONFLICT(policy_key) DO UPDATE SET policy_value=excluded.policy_value,confidence=excluded.confidence,updated_at=excluded.updated_at",
            (key, value, float(confidence), ts),
        )
        self.conn.commit()

    def get_memory_policy_profile(self) -> Dict[str, Dict[str, Any]]:
        rows = self.conn.execute("SELECT policy_key,policy_value,confidence,updated_at FROM memory_policy_profile").fetchall()
        return {
            str(r["policy_key"]): {
                "value": str(r["policy_value"]),
                "confidence": float(r["confidence"]),
                "updated_at": int(r["updated_at"]),
            }
            for r in rows
        }

    def add_quarantine(self, trace_id: Optional[str], raw_text: str, reason: str, risk_score: float) -> str:
        qid = str(uuid.uuid4())
        self.conn.execute(
            "INSERT INTO memory_quarantine(id,trace_id,raw_text,reason,risk_score,created_at) VALUES(?,?,?,?,?,?)",
            (qid, trace_id, raw_text[:1200], reason, float(risk_score), now_ts()),
        )
        self.conn.commit()
        return qid

    def get_quarantine(self, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM memory_quarantine ORDER BY created_at DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            out.append({k: r[k] for k in r.keys()})
        return out

    def add_audit_event(self, session_key: str, risk: float, block: bool, reasons: List[str], sample_hash: str) -> None:
        self.conn.execute(
            "INSERT INTO audit_events(id,sessionKey,ts,risk,block,reasons,sample_hash) VALUES(?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), session_key, now_ts(), float(risk), 1 if block else 0, json.dumps(reasons, ensure_ascii=False), sample_hash),
        )
        self.conn.commit()

    def decay_and_prune_ephemeral(self) -> Dict[str, int]:
        ts = now_ts()
        self.conn.execute(
            "UPDATE memory_items SET importance=MAX(0.0, importance*0.92), updated_at=? WHERE layer='ephemeral'",
            (ts,),
        )
        deleted = self.conn.execute(
            "DELETE FROM memory_items WHERE layer='ephemeral' AND (ttl_expires_at IS NOT NULL AND ttl_expires_at<=? OR importance<0.05)",
            (ts,),
        ).rowcount
        self.conn.commit()
        return {"ephemeral_deleted": int(deleted)}

    def dedup_layer(self, layer: str, session_key: str) -> int:
        rows = self.conn.execute(
            "SELECT id,summary FROM memory_items WHERE layer=? AND session_key=? ORDER BY updated_at DESC",
            (layer, session_key),
        ).fetchall()
        seen: Dict[str, str] = {}
        removed = 0
        for r in rows:
            sid = str(r["id"])
            raw = str(r["summary"]).strip().lower()
            sig = re.sub(r"[^a-z0-9ぁ-んァ-ヶ一-龠]+", "", raw)
            if len(sig) < 8:
                sig = raw
            if not sig:
                continue
            if sig in seen:
                self.conn.execute("DELETE FROM memory_items WHERE id=?", (sid,))
                removed += 1
            else:
                seen[sig] = sid
        self.conn.commit()
        return removed

    def dedup_layer_fuzzy(self, layer: str, session_key: str, threshold: float = 0.84) -> int:
        rows = self.conn.execute(
            "SELECT id,summary FROM memory_items WHERE layer=? AND session_key=? ORDER BY updated_at DESC",
            (layer, session_key),
        ).fetchall()
        kept_tokens: List[set[str]] = []
        kept_sigs: List[str] = []
        removed = 0
        for r in rows:
            sid = str(r["id"])
            summary = str(r["summary"] or "")
            sig = normalize_sig(summary)
            toks = token_set(summary)
            is_dup = False
            for i, ks in enumerate(kept_tokens):
                js = jaccard(toks, ks)
                if js >= threshold:
                    is_dup = True
                    break
                ps = kept_sigs[i]
                if len(sig) >= 20 and len(ps) >= 20 and (sig in ps or ps in sig):
                    is_dup = True
                    break
            if is_dup:
                self.conn.execute("DELETE FROM memory_items WHERE id=?", (sid,))
                removed += 1
                continue
            kept_tokens.append(toks)
            kept_sigs.append(sig)
        self.conn.commit()
        return removed

    def expire_conflicting_fact_keys(
        self,
        layer: str,
        session_key: str,
        fact_keys: Sequence[str],
        keep_id: str,
        *,
        include_all_sessions: bool = False,
    ) -> int:
        keys = [k for k in fact_keys if k]
        if not keys:
            return 0
        now = now_ts()

        def _fact_conf(tags: Dict[str, Any]) -> float:
            fact = tags.get("fact")
            if isinstance(fact, dict):
                try:
                    c = float(fact.get("confidence"))
                    if c > 0.0:
                        return max(0.0, min(1.0, c))
                except Exception:
                    pass
            gate = tags.get("gate")
            if isinstance(gate, dict):
                try:
                    c = float(gate.get("score"))
                    if c > 0.0:
                        return max(0.0, min(1.0, c))
                except Exception:
                    pass
            kind = str(tags.get("kind", ""))
            if kind in {"structured_fact", "durable_global_fact"}:
                return 0.82
            if kind in {"durable_global", "convdeep_global"}:
                return 0.72
            return 0.52

        def _fact_ts(tags: Dict[str, Any], updated_at: int) -> int:
            fact = tags.get("fact")
            if isinstance(fact, dict):
                try:
                    ts = int(fact.get("ts"))
                    if ts > 0:
                        return ts
                except Exception:
                    pass
            try:
                ts = int(tags.get("ts"))
                if ts > 0:
                    return ts
            except Exception:
                pass
            return int(updated_at)

        def _source_trust(source: str) -> float:
            s = (source or "").strip().lower()
            if s == "turn":
                return 1.0
            if s == "conv_summarize":
                return 0.75
            if s == "bootstrap":
                return 0.7
            return 0.6

        def _rank(row: sqlite3.Row, tags: Dict[str, Any]) -> float:
            conf = _fact_conf(tags)
            ts = _fact_ts(tags, int(row["updated_at"]))
            age_days = max(0.0, float(now - ts) / 86400.0)
            freshness = pow(2.718281828, -age_days / 120.0)
            imp = max(0.0, min(1.0, float(row["importance"])))
            src = _source_trust(str(row["source"]))
            s = 0.52 * conf + 0.30 * freshness + 0.12 * imp + 0.06 * src
            if str(row["id"]) == keep_id:
                s += 0.03
            return float(s)

        if include_all_sessions:
            rows = self.conn.execute(
                """
                SELECT id,tags,importance,updated_at,source FROM memory_items
                WHERE layer=?
                ORDER BY updated_at DESC
                """,
                (layer,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT id,tags,importance,updated_at,source FROM memory_items
                WHERE layer=? AND (session_key=? OR session_key='global')
                ORDER BY updated_at DESC
                """,
                (layer, session_key),
            ).fetchall()
        removed = 0
        keyset = set(keys)
        key_rows: Dict[str, List[Tuple[sqlite3.Row, Dict[str, Any]]]] = {}
        for k in keyset:
            key_rows[k] = []
        for r in rows:
            try:
                tags = json.loads(str(r["tags"] or "{}"))
            except Exception:
                tags = {}
            rk = set(tags.get("fact_keys") or [])
            hits = rk & keyset
            if not hits:
                continue
            for k in hits:
                key_rows.setdefault(k, []).append((r, tags))

        for fk, candidates in key_rows.items():
            if not candidates:
                continue
            winner_id = None
            winner_score = -1.0
            member_ids: List[str] = []
            for r, tags in candidates:
                rid = str(r["id"])
                member_ids.append(rid)
                score = _rank(r, tags)
                if score > winner_score:
                    winner_score = score
                    winner_id = rid
            if winner_id is None:
                continue
            gid = f"cg:{fk}"
            self.conn.execute(
                "INSERT INTO conflict_group(id,fact_key,members_json,policy,created_at,updated_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET members_json=excluded.members_json,policy=excluded.policy,updated_at=excluded.updated_at",
                (
                    gid,
                    fk,
                    json.dumps(sorted(set(member_ids)), ensure_ascii=False),
                    "prefer_recent_x_confidence",
                    now,
                    now,
                ),
            )
            for rid in member_ids:
                if rid == winner_id:
                    continue
                self.conn.execute("DELETE FROM memory_items WHERE id=?", (rid,))
                removed += 1
        self.conn.commit()
        return removed

    def trim_layer_size(self, layer: str, session_key: str, max_items: int) -> int:
        rows = self.conn.execute(
            "SELECT id FROM memory_items WHERE layer=? AND session_key=? ORDER BY last_access_at DESC, updated_at DESC",
            (layer, session_key),
        ).fetchall()
        if len(rows) <= max_items:
            return 0
        drop = rows[max_items:]
        for r in drop:
            self.conn.execute("DELETE FROM memory_items WHERE id=?", (str(r["id"]),))
        self.conn.commit()
        return len(drop)

    def cleanup_noisy_memory(self) -> Dict[str, int]:
        patterns = [
            r"<MEM(?:RULES|STYLE|CTX)\s+v1>",
            r"\[MEM(?:RULES|STYLE|CTX)\s+v1\]",
            r"MEMSTYLEを更新してください",
            r"MEMRULESを更新してください",
            r"Conversation info \(untrusted metadata\):",
            r"read\s+(?:agents|soul|identity|heartbeat)\.md",
            r"workspace context",
            r"follow it strictly",
            r"do not infer or repeat old tasks",
            r"^x:",
            r"/Users/",
            r"openclaw\.json",
            r"EXT_TOOL_RESULT",
            r"externalContent",
            r"長期記憶.*(?:不足|不完全|課題|弱い)",
            r"参照できる記憶コンテキスト.*不足",
            r"取り扱い不足",
            r"OpenClawで動く.*アシスタント",
            r"I am .*assistant",
            r"(?:お前自身は|あなたは).*(?:ロックマン|persona|キャラ|roleplay|act as)",
        ]
        removed_items = 0
        rows = self.conn.execute(
            "SELECT id,summary,session_key,layer,importance FROM memory_items WHERE layer IN ('surface','deep')"
        ).fetchall()
        durable_pat = re.compile(r"(覚えて(?!る)|必ず|ルール|方針|制約|remember|always|must|rule|policy)", re.IGNORECASE)
        question_like_pat = re.compile(r"(教えて|覚えてる|何|だれ|誰|どこ|いつ|how|what|who|where|when)\??$", re.IGNORECASE)
        for r in rows:
            s = str(r["summary"] or "")
            try:
                tags = json.loads(str(r["tags"] or "{}"))
            except Exception:
                tags = {}
            kind = str(tags.get("kind", ""))
            fact = tags.get("fact") if isinstance(tags, dict) else None
            fks = [str(x) for x in (tags.get("fact_keys") or [])] if isinstance(tags, dict) else []
            if (
                str(r["layer"]) == "deep"
                and kind in {"convdeep", "convdeep_global", "durable_global", "deep_global", "signal_deep", "auto_deep"}
                and not isinstance(fact, dict)
                and any(fk.startswith(("profile.", "pref.", "rule.")) for fk in fks)
            ):
                self.conn.execute("DELETE FROM memory_items WHERE id=?", (str(r["id"]),))
                removed_items += 1
                continue
            if any(re.search(p, s, re.IGNORECASE) for p in patterns):
                self.conn.execute("DELETE FROM memory_items WHERE id=?", (str(r["id"]),))
                removed_items += 1
                continue
            # prune global deep rows that are plain questions and unlikely to be durable memory
            if str(r["layer"]) == "deep":
                q_like = "？" in s or "?" in s
                if question_like_pat.search("".join(s.split())):
                    q_like = True
                if q_like and not durable_pat.search(s) and float(r["importance"]) < 0.85:
                    self.conn.execute("DELETE FROM memory_items WHERE id=?", (str(r["id"]),))
                    removed_items += 1
                    continue
                if len(" ".join(s.split())) < 12 and not durable_pat.search(s) and float(r["importance"]) < 0.85:
                    self.conn.execute("DELETE FROM memory_items WHERE id=?", (str(r["id"]),))
                    removed_items += 1
                    continue

        sanitized_conv = 0
        conv_rows = self.conn.execute("SELECT id,summary FROM conv_summaries").fetchall()
        for r in conv_rows:
            old = str(r["summary"] or "")
            lines: List[str] = []
            seen = set()
            for ln in old.split("\n"):
                t = " ".join(ln.strip().split())
                if not t:
                    continue
                if any(re.search(p, t, re.IGNORECASE) for p in patterns):
                    continue
                k = t.lower()
                if k in seen:
                    continue
                seen.add(k)
                lines.append(t)
            new = "\n".join(lines)
            if new != old:
                self.conn.execute(
                    "UPDATE conv_summaries SET summary=?, updated_at=? WHERE id=?",
                    (new, now_ts(), str(r["id"])),
                )
                sanitized_conv += 1

        self.conn.commit()
        return {"removed_memory_items": removed_items, "sanitized_conv_summaries": sanitized_conv}

    def memory_stats(self) -> Dict[str, int]:
        rows = self.conn.execute(
            "SELECT layer,COUNT(*) AS c FROM memory_items GROUP BY layer"
        ).fetchall()
        out: Dict[str, int] = {}
        for r in rows:
            out[str(r["layer"])] = int(r["c"])
        return out

    def list_memory_debug(self, layer: str | None = None, session_key: str | None = None, limit: int = 50) -> List[Dict[str, Any]]:
        params: List[Any] = []
        where: List[str] = []
        if layer:
            where.append("layer=?")
            params.append(layer)
        if session_key:
            where.append("(session_key=? OR session_key='global')")
            params.append(session_key)
        sql = "SELECT id,session_key,layer,updated_at,importance,usage_count,summary,tags FROM memory_items"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(int(limit))
        rows = self.conn.execute(sql, tuple(params)).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            out.append(
                {
                    "id": str(r["id"]),
                    "session_key": str(r["session_key"]),
                    "layer": str(r["layer"]),
                    "updated_at": int(r["updated_at"]),
                    "importance": float(r["importance"]),
                    "usage_count": int(r["usage_count"]),
                    "summary": str(r["summary"]),
                    "tags": str(r["tags"]),
                }
            )
        return out

    def backfill_fact_keys(self, layer: str = "deep", limit: int = 5000) -> int:
        rows = self.conn.execute(
            "SELECT id,summary,tags FROM memory_items WHERE layer=? ORDER BY updated_at DESC LIMIT ?",
            (layer, int(limit)),
        ).fetchall()
        updated = 0
        for r in rows:
            rid = str(r["id"])
            summary = str(r["summary"] or "")
            try:
                tags = json.loads(str(r["tags"] or "{}"))
            except Exception:
                tags = {}
            kind = str(tags.get("kind", ""))
            fact = tags.get("fact") if isinstance(tags, dict) else {}
            # Only backfill key metadata for fact-oriented rows.
            if kind not in {"structured_fact", "durable_global_fact"} and not isinstance(fact, dict):
                continue
            if isinstance(fact, dict) and str(fact.get("fact_key") or "") == "memory.note":
                # Keep generic note facts isolated; avoid heuristic key pollution.
                continue
            cur = set(tags.get("fact_keys") or [])
            if isinstance(fact, dict):
                fk = str(fact.get("fact_key") or "").strip()
                if fk:
                    add = {fk}
                else:
                    add = set(infer_fact_keys_from_text(summary))
            else:
                add = set(infer_fact_keys_from_text(summary))
            if not add:
                continue
            if isinstance(fact, dict) and str(fact.get("fact_key") or "").strip():
                merged = sorted([x for x in add if x])
            else:
                merged = sorted([x for x in (cur | add) if x])
            if merged == sorted([x for x in cur if x]):
                continue
            tags["fact_keys"] = merged
            self.conn.execute(
                "UPDATE memory_items SET tags=?, updated_at=? WHERE id=?",
                (json.dumps(tags, ensure_ascii=False), now_ts(), rid),
            )
            updated += 1
        self.conn.commit()
        return updated
