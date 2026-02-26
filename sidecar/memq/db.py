from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


def now_ts() -> int:
    return int(time.time())


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

        # default hard rules
        defaults = [
            ("r_security_no_secrets", 100, 1, "security", "security.no_secrets=true", now_ts()),
            ("r_security_refuse_keys", 95, 1, "security", "compliance.refuse_api_keys=true", now_ts()),
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
            sig = str(r["summary"]).strip().lower()
            if not sig:
                continue
            if sig in seen:
                self.conn.execute("DELETE FROM memory_items WHERE id=?", (sid,))
                removed += 1
            else:
                seen[sig] = sid
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
