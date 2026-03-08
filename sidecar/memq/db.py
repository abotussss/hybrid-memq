from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import re
import sqlite3
import time
from typing import Any, Iterable
from zoneinfo import ZoneInfo


def _utc_now() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _normalize_text(text: str) -> str:
    return " ".join(str(text or "").replace("\x00", " ").split())


def _slug_tokens(text: str) -> list[str]:
    clean = re.sub(r"[^0-9A-Za-z\u3040-\u30ff\u3400-\u9fff_-]+", " ", _normalize_text(text)).strip()
    if not clean:
        return []
    return [tok for tok in clean.split() if tok]


def _ngrams(text: str, n: int = 2) -> str:
    clean = re.sub(r"\s+", "", _normalize_text(text))
    if not clean:
        return ""
    if len(clean) <= n:
        return clean
    grams = [clean[i : i + n] for i in range(len(clean) - n + 1)]
    return " ".join(grams)


def _dirty_profile_fact(fact_key: str, value: str = "", summary: str = "") -> bool:
    fk = str(fact_key or "").strip().lower()
    if not fk.startswith("profile."):
        return False
    cleaned_value = _clean_fact_value(fact_key, value)
    payload = f"{cleaned_value or ''} {summary or ''}".lower()
    if fk in {"profile.rules", "profile.memstyle"}:
        return True
    if "budget" in fk or fk.startswith("profile.memrule") or fk.startswith("profile.memstyle") or fk.startswith("profile.memctx"):
        return True
    if ".t.recent" in fk or ".t.digest" in fk or ".wm." in fk:
        return True
    for marker in ("<memrules", "<memstyle", "<memctx", "<qrule", "<qstyle", "<qctx", "budget_tokens=", "p.snapshot=", "t.recent=", "t.digest=", "wm.surf=", "wm.deep="):
        if marker in payload:
            return True
    if fk == "profile.identity.card":
        cleaned = _normalize_text(cleaned_value or summary)
        if len(cleaned.strip()) < 3 or cleaned.lower().startswith("p.snapshot="):
            return True
    return False


STYLE_ALLOWED_KEYS = {
    "tone",
    "persona",
    "verbosity",
    "speaking_style",
    "callUser",
    "firstPerson",
}

RULE_ALLOWED_PREFIXES = (
    "security.",
    "language.",
    "procedure.",
    "compliance.",
    "output.",
    "operation.",
)

STYLE_TECHNICAL_TERMS = (
    "memory-lancedb-pro",
    "lancedb",
    "memq",
    "qctx",
    "qstyle",
    "qrule",
    "memctx",
    "memstyle",
    "memrule",
    "openclaw",
    "sqlite",
    "ollama",
    "backend",
    "adapter",
    "bridge",
    "helper",
)


def _dirty_style_value(key: str, value: str) -> bool:
    clean_key = str(key or "").strip()
    clean_value = _normalize_text(value or "")
    lowered = clean_value.lower()
    if clean_key not in STYLE_ALLOWED_KEYS:
        return True
    if not clean_value:
        return True
    for marker in ("<memrules", "<memstyle", "<memctx", "<qrule", "<qstyle", "<qctx", "budget_tokens="):
        if marker in lowered:
            return True
    if clean_key == "persona":
        for term in STYLE_TECHNICAL_TERMS:
            if term in lowered:
                return True
    return False


def _dirty_rule_value(key: str, value: str) -> bool:
    clean_key = str(key or "").strip()
    clean_value = _normalize_text(value or "")
    lowered = clean_value.lower()
    if not any(clean_key.startswith(prefix) for prefix in RULE_ALLOWED_PREFIXES):
        return True
    if not clean_value:
        return True
    for marker in ("<memrules", "<memstyle", "<memctx", "<qrule", "<qstyle", "<qctx", "budget_tokens="):
        if marker in lowered:
            return True
    strict_true_keys = {
        "security.never_output_secrets",
        "security.no_api_keys",
        "security.no_api_tokens",
        "output.redact_secret_like",
    }
    if clean_key in strict_true_keys and lowered not in {"true", "1", "yes"}:
        return True
    return False


def _clean_fact_value(fact_key: str, value: str) -> str:
    clean = _normalize_text(value or "")
    fk = str(fact_key or "").strip()
    if not clean:
        return ""
    prefix = f"{fk}:"
    if fk and clean.lower().startswith(prefix.lower()):
        clean = clean[len(prefix):].strip()
    if clean.lower().startswith("p.snapshot="):
        clean = clean.split("=", 1)[1].strip()
    return clean


def _prefer_human_anchor_text(fact_key: str, value: str, summary: str, text: str) -> str:
    clean_value = _normalize_text(value or "")
    clean_summary = _normalize_text(summary or "")
    clean_text = _normalize_text(text or "")
    lowered_summary = clean_summary.lower()
    lowered_text = clean_text.lower()
    lowered_value = clean_value.lower()
    technical_terms = STYLE_TECHNICAL_TERMS + ("memory-lancedb-pro-adapted",)
    summary_is_machineish = (
        (fact_key and clean_summary.lower().startswith(f"{str(fact_key).lower()}:"))
        or lowered_summary in {"true", "false", "exists"}
        or lowered_summary == lowered_value
        or any(term == lowered_summary or lowered_summary.endswith(f":{term}") for term in technical_terms)
    )
    text_is_human = bool(clean_text) and (
        len(clean_text) >= 16
        or any(ch in clean_text for ch in ("。", "、", " ", "，"))
    ) and not any(term == lowered_text for term in technical_terms)
    if clean_text and (
        lowered_value in {"true", "false", "exists"}
        or clean_summary.lower().endswith(":true")
        or clean_summary.lower().endswith(":false")
        or clean_summary.lower().endswith(":exists")
    ):
        return clean_text
    if text_is_human and summary_is_machineish:
        return clean_text
    if clean_summary:
        return clean_summary
    if clean_text:
        return clean_text
    return clean_value


def _anchor_candidate_score(fact_key: str, value: str, summary: str, text: str) -> float:
    candidate = _prefer_human_anchor_text(fact_key, value, summary, text)
    lowered = candidate.lower()
    score = 0.0
    if not candidate:
        return score
    score += min(len(candidate), 180) / 10.0
    if any(ch in candidate for ch in ("。", "、", " ", "，")):
        score += 4.0
    if candidate == _normalize_text(text or ""):
        score += 3.0
    if not any(term == lowered or lowered.endswith(f":{term}") for term in STYLE_TECHNICAL_TERMS):
        score += 2.0
    if str(fact_key or "").startswith("profile."):
        score -= 1.0
    if str(fact_key or "").startswith("profile.task_") or str(fact_key or "").startswith("profile.memory_"):
        score += 1.5
    if lowered in {"true", "false", "exists", "memory-lancedb-pro", "memory-lancedb-pro-adapted"}:
        score -= 10.0
    return score


def _rewrite_public_labels(text: str) -> str:
    return (
        _normalize_text(text or "")
        .replace("MEMRULES", "QRULE")
        .replace("MEMRULE", "QRULE")
        .replace("MEMSTYLE", "QSTYLE")
        .replace("MEMCTX", "QCTX")
    )


def _fts_match_query(text: str) -> str:
    terms: list[str] = []
    for token in _slug_tokens(text)[:8]:
        safe = token.replace('"', "").strip()
        if safe:
            terms.append(f'"{safe}"')
    for token in _ngrams(text).split()[:8]:
        safe = token.replace('"', "").strip()
        if safe:
            terms.append(f'"{safe}"')
    if not terms:
        safe = _normalize_text(text).replace('"', "").strip()
        return f'"{safe}"' if safe else '""'
    return " OR ".join(dict.fromkeys(terms))


def _dedupe_consecutive_strings(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    previous = ""
    for value in values:
        clean = _normalize_text(value)
        if not clean:
            continue
        marker = clean.lower()
        if marker == previous:
            continue
        out.append(clean)
        previous = marker
    return out


def _dedupe_consecutive_rows_by_summary(rows: Iterable[Any]) -> list[Any]:
    out: list[Any] = []
    previous = ""
    for row in rows:
        summary = _normalize_text(str(row["summary"] or ""))
        if not summary:
            continue
        marker = summary.lower()
        if marker == previous:
            continue
        out.append(row)
        previous = marker
    return out


@dataclass
class SearchResult:
    id: int
    session_key: str
    layer: str
    kind: str
    fact_key: str
    value: str
    summary: str
    confidence: float
    importance: float
    strength: float
    updated_at: int
    score: float


class MemqDB:
    def __init__(self, path: Path, timezone_name: str = "Asia/Tokyo") -> None:
        self.path = path
        self.timezone = ZoneInfo(timezone_name)
        if self._needs_reset(path):
            backup = path.with_suffix(path.suffix + f".legacy.{int(time.time())}.bak")
            path.rename(backup)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def _needs_reset(self, path: Path) -> bool:
        if not path.exists():
            return False
        conn = sqlite3.connect(str(path))
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(memory_items)").fetchall()}
            required = {"session_key", "layer", "kind", "fact_key", "value", "summary", "confidence"}
            return bool(cols) and not required.issubset(cols)
        finally:
            conn.close()

    def close(self) -> None:
        self.conn.close()

    def _init_schema(self) -> None:
        cur = self.conn.cursor()
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS memory_items (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              session_key TEXT NOT NULL,
              layer TEXT NOT NULL,
              kind TEXT NOT NULL,
              fact_key TEXT NOT NULL DEFAULT '',
              value TEXT NOT NULL DEFAULT '',
              text TEXT NOT NULL DEFAULT '',
              summary TEXT NOT NULL DEFAULT '',
              keywords TEXT NOT NULL DEFAULT '',
              ngrams TEXT NOT NULL DEFAULT '',
              confidence REAL NOT NULL DEFAULT 0.0,
              importance REAL NOT NULL DEFAULT 0.0,
              strength REAL NOT NULL DEFAULT 0.0,
              tags_json TEXT NOT NULL DEFAULT '{}',
              source_quote TEXT NOT NULL DEFAULT '',
              ttl_expires_at INTEGER,
              tombstoned INTEGER NOT NULL DEFAULT 0,
              created_at INTEGER NOT NULL,
              updated_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_memory_session_layer ON memory_items(session_key, layer, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_memory_fact ON memory_items(session_key, fact_key, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_memory_ttl ON memory_items(ttl_expires_at);

            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              session_key TEXT NOT NULL,
              ts INTEGER NOT NULL,
              day_key TEXT NOT NULL,
              actor TEXT NOT NULL,
              kind TEXT NOT NULL,
              summary TEXT NOT NULL,
              keywords TEXT NOT NULL DEFAULT '',
              salience REAL NOT NULL DEFAULT 0.0,
              ttl_expires_at INTEGER,
              created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_events_session_day ON events(session_key, day_key, ts DESC);
            CREATE INDEX IF NOT EXISTS idx_events_ttl ON events(ttl_expires_at);

            CREATE TABLE IF NOT EXISTS daily_digests (
              day_key TEXT NOT NULL,
              session_key TEXT NOT NULL,
              digest_micro TEXT NOT NULL DEFAULT '',
              digest_meso TEXT NOT NULL DEFAULT '',
              updated_at INTEGER NOT NULL,
              PRIMARY KEY(day_key, session_key)
            );

            CREATE TABLE IF NOT EXISTS rules (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              session_key TEXT NOT NULL,
              key TEXT NOT NULL,
              value TEXT NOT NULL,
              priority INTEGER NOT NULL DEFAULT 100,
              enabled INTEGER NOT NULL DEFAULT 1,
              source TEXT NOT NULL DEFAULT 'brain',
              updated_at INTEGER NOT NULL,
              UNIQUE(session_key, key)
            );

            CREATE TABLE IF NOT EXISTS style_profile (
              session_key TEXT NOT NULL,
              key TEXT NOT NULL,
              value TEXT NOT NULL,
              updated_at INTEGER NOT NULL,
              PRIMARY KEY(session_key, key)
            );

            CREATE TABLE IF NOT EXISTS fact_index (
              session_key TEXT NOT NULL,
              fact_key TEXT NOT NULL,
              item_id INTEGER NOT NULL,
              updated_at INTEGER NOT NULL,
              PRIMARY KEY(session_key, fact_key)
            );

            CREATE TABLE IF NOT EXISTS quarantine (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              session_key TEXT NOT NULL,
              raw_text TEXT NOT NULL,
              reason TEXT NOT NULL,
              risk REAL NOT NULL DEFAULT 0.0,
              created_at INTEGER NOT NULL
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
              summary,
              keywords,
              ngrams,
              content=''
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
              summary,
              keywords,
              ngrams,
              content=''
            );
            """
        )
        self.conn.commit()
        self.repair_style_and_rules_all()
        self.repair_public_labels_all()

    def _row_to_search_result(self, row: sqlite3.Row, score: float) -> SearchResult:
        return SearchResult(
            id=int(row["id"]),
            session_key=str(row["session_key"]),
            layer=str(row["layer"]),
            kind=str(row["kind"]),
            fact_key=str(row["fact_key"] or ""),
            value=str(row["value"] or ""),
            summary=str(row["summary"] or ""),
            confidence=float(row["confidence"] or 0.0),
            importance=float(row["importance"] or 0.0),
            strength=float(row["strength"] or 0.0),
            updated_at=int(row["updated_at"] or 0),
            score=score,
        )

    def _upsert_memory_fts(self, item_id: int, summary: str, keywords: str, ngrams: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO memory_fts(rowid, summary, keywords, ngrams) VALUES(?,?,?,?)",
            (item_id, summary, keywords, ngrams),
        )

    def _upsert_event_fts(self, event_id: int, summary: str, keywords: str, ngrams: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO events_fts(rowid, summary, keywords, ngrams) VALUES(?,?,?,?)",
            (event_id, summary, keywords, ngrams),
        )

    def _local_day(self, ts: int) -> str:
        return datetime.fromtimestamp(ts, tz=self.timezone).strftime("%Y-%m-%d")

    def now_day(self) -> str:
        return self._local_day(_utc_now())

    def upsert_rule(self, session_key: str, key: str, value: str, *, priority: int = 100, source: str = "brain", enabled: bool = True, updated_at: int | None = None) -> None:
        ts = updated_at or _utc_now()
        self.conn.execute(
            """
            INSERT INTO rules(session_key, key, value, priority, enabled, source, updated_at)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(session_key, key)
            DO UPDATE SET value=excluded.value, priority=excluded.priority, enabled=excluded.enabled, source=excluded.source, updated_at=excluded.updated_at
            """,
            (session_key, key, value, priority, 1 if enabled else 0, source, ts),
        )
        self.conn.commit()

    def list_rules(self, session_key: str) -> dict[str, str]:
        self.repair_rules(session_key)
        rows = self.conn.execute(
            "SELECT key, value FROM rules WHERE enabled=1 AND session_key IN (?, 'global') ORDER BY session_key='global', priority ASC, updated_at DESC",
            (session_key,),
        ).fetchall()
        out: dict[str, str] = {}
        for row in rows:
            key = str(row["key"])
            if key in out:
                continue
            out[key] = str(row["value"])
        return out

    def upsert_style(self, session_key: str, key: str, value: str, *, updated_at: int | None = None) -> None:
        ts = updated_at or _utc_now()
        self.conn.execute(
            """
            INSERT INTO style_profile(session_key, key, value, updated_at)
            VALUES(?,?,?,?)
            ON CONFLICT(session_key, key)
            DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (session_key, key, value, ts),
        )
        self.conn.commit()

    def list_style(self, session_key: str) -> dict[str, str]:
        self.repair_style_profile(session_key)
        rows = self.conn.execute(
            "SELECT key, value FROM style_profile WHERE session_key IN (?, 'global') ORDER BY session_key='global', updated_at DESC",
            (session_key,),
        ).fetchall()
        out: dict[str, str] = {}
        for row in rows:
            key = str(row["key"])
            if key in out:
                continue
            out[key] = str(row["value"])
        return out

    def repair_style_profile(self, session_key: str) -> int:
        rows = self.conn.execute(
            "SELECT session_key, key, value FROM style_profile WHERE session_key IN (?, 'global')",
            (session_key,),
        ).fetchall()
        dirty = [(str(row["session_key"]), str(row["key"])) for row in rows if _dirty_style_value(str(row["key"] or ""), str(row["value"] or ""))]
        if not dirty:
            return 0
        self.conn.executemany("DELETE FROM style_profile WHERE session_key=? AND key=?", dirty)
        self.conn.commit()
        return len(dirty)

    def repair_rules(self, session_key: str) -> int:
        rows = self.conn.execute(
            "SELECT session_key, key, value FROM rules WHERE session_key IN (?, 'global')",
            (session_key,),
        ).fetchall()
        dirty = [(str(row["session_key"]), str(row["key"])) for row in rows if _dirty_rule_value(str(row["key"] or ""), str(row["value"] or ""))]
        if not dirty:
            return 0
        self.conn.executemany("DELETE FROM rules WHERE session_key=? AND key=?", dirty)
        self.conn.commit()
        return len(dirty)

    def repair_style_and_rules_all(self) -> dict[str, int]:
        sessions = {"global"}
        for row in self.conn.execute("SELECT DISTINCT session_key FROM style_profile").fetchall():
            sessions.add(str(row["session_key"]))
        for row in self.conn.execute("SELECT DISTINCT session_key FROM rules").fetchall():
            sessions.add(str(row["session_key"]))
        style_removed = 0
        rules_removed = 0
        for session_key in sessions:
            style_removed += self.repair_style_profile(session_key)
            rules_removed += self.repair_rules(session_key)
        return {"style_removed": style_removed, "rules_removed": rules_removed}

    def repair_public_labels_all(self) -> int:
        rows = self.conn.execute(
            "SELECT id, value, text, summary FROM memory_items WHERE tombstoned=0"
        ).fetchall()
        changed = 0
        for row in rows:
            value = _rewrite_public_labels(str(row["value"] or ""))
            text = _rewrite_public_labels(str(row["text"] or ""))
            summary = _rewrite_public_labels(str(row["summary"] or ""))
            if (
                value == str(row["value"] or "")
                and text == str(row["text"] or "")
                and summary == str(row["summary"] or "")
            ):
                continue
            item_id = int(row["id"])
            keywords = " ".join(dict.fromkeys(_slug_tokens(" ".join([summary, value]))))
            ngrams = _ngrams(" ".join([summary, value]))
            self.conn.execute(
                "UPDATE memory_items SET value=?, text=?, summary=?, keywords=?, ngrams=?, updated_at=? WHERE id=?",
                (value, text, summary, keywords, ngrams, _utc_now(), item_id),
            )
            self._upsert_memory_fts(item_id, summary or text or value, keywords, ngrams)
            changed += 1
        if changed:
            self.conn.commit()
        return changed

    def insert_memory(
        self,
        *,
        session_key: str,
        layer: str,
        kind: str,
        fact_key: str,
        value: str,
        text: str,
        summary: str,
        confidence: float,
        importance: float,
        strength: float,
        tags: dict[str, Any] | None = None,
        source_quote: str = "",
        ttl_days: int | None = None,
        created_at: int | None = None,
    ) -> int:
        ts = created_at or _utc_now()
        ttl_expires_at = ts + ttl_days * 86400 if ttl_days else None
        summary = _normalize_text(summary or text or value)
        text = _normalize_text(text or summary or value)
        fact_key = str(fact_key or "").strip()
        value = _clean_fact_value(fact_key, value)
        keywords = " ".join(dict.fromkeys(_slug_tokens(" ".join([summary, value, fact_key]))))
        ngrams = _ngrams(" ".join([summary, value, fact_key]))
        cur = self.conn.execute(
            """
            INSERT INTO memory_items(
              session_key, layer, kind, fact_key, value, text, summary, keywords, ngrams,
              confidence, importance, strength, tags_json, source_quote, ttl_expires_at, created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                session_key,
                layer,
                kind,
                fact_key,
                value,
                text,
                summary,
                keywords,
                ngrams,
                float(confidence),
                float(importance),
                float(strength),
                json.dumps(tags or {}, ensure_ascii=False),
                _normalize_text(source_quote)[:160],
                ttl_expires_at,
                ts,
                ts,
            ),
        )
        item_id = int(cur.lastrowid)
        self._upsert_memory_fts(item_id, summary, keywords, ngrams)
        if fact_key:
            self.conn.execute(
                """
                INSERT INTO fact_index(session_key, fact_key, item_id, updated_at)
                VALUES(?,?,?,?)
                ON CONFLICT(session_key, fact_key)
                DO UPDATE SET item_id=excluded.item_id, updated_at=excluded.updated_at
                """,
                (session_key, fact_key, item_id, ts),
            )
        self.conn.commit()
        return item_id

    def insert_event(
        self,
        *,
        session_key: str,
        ts: int,
        actor: str,
        kind: str,
        summary: str,
        salience: float,
        keywords: Iterable[str] | None = None,
        ttl_days: int | None = None,
    ) -> int:
        day_key = self._local_day(ts)
        ttl_expires_at = ts + ttl_days * 86400 if ttl_days else None
        summary = _normalize_text(summary)
        key_text = " ".join(dict.fromkeys((keywords or []) or _slug_tokens(summary)))
        ngrams = _ngrams(summary)
        cur = self.conn.execute(
            "INSERT INTO events(session_key, ts, day_key, actor, kind, summary, keywords, salience, ttl_expires_at, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (session_key, ts, day_key, actor, kind, summary, key_text, float(salience), ttl_expires_at, ts),
        )
        event_id = int(cur.lastrowid)
        self._upsert_event_fts(event_id, summary, key_text, ngrams)
        self.conn.commit()
        return event_id

    def insert_quarantine(self, session_key: str, raw_text: str, reason: str, risk: float = 1.0) -> None:
        self.conn.execute(
            "INSERT INTO quarantine(session_key, raw_text, reason, risk, created_at) VALUES(?,?,?,?,?)",
            (session_key, raw_text[:400], reason[:120], float(risk), _utc_now()),
        )
        self.conn.commit()

    def list_quarantine(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id, session_key, raw_text, reason, risk, created_at FROM quarantine ORDER BY created_at DESC LIMIT ?",
            (max(1, min(limit, 500)),),
        ).fetchall()
        return [dict(row) for row in rows]

    def purge_expired(self) -> dict[str, int]:
        now = _utc_now()
        removed_memory = self.conn.execute(
            "DELETE FROM memory_items WHERE ttl_expires_at IS NOT NULL AND ttl_expires_at <= ?",
            (now,),
        ).rowcount
        removed_events = self.conn.execute(
            "DELETE FROM events WHERE ttl_expires_at IS NOT NULL AND ttl_expires_at <= ?",
            (now,),
        ).rowcount
        self.conn.commit()
        return {"memory": int(removed_memory or 0), "events": int(removed_events or 0)}

    def decay_ephemera(self, session_key: str, *, factor: float = 0.92, min_strength: float = 0.08) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT id, strength, importance FROM memory_items WHERE session_key IN (?, 'global') AND layer='ephemeral' AND tombstoned=0",
            (session_key,),
        ).fetchall()
        updated = 0
        pruned = 0
        for row in rows:
            strength = float(row["strength"] or 0.0) * factor
            importance = float(row["importance"] or 0.0) * factor
            if max(strength, importance) < min_strength:
                self.conn.execute("UPDATE memory_items SET tombstoned=1, updated_at=? WHERE id=?", (_utc_now(), int(row["id"])))
                pruned += 1
            else:
                self.conn.execute(
                    "UPDATE memory_items SET strength=?, importance=?, updated_at=? WHERE id=?",
                    (strength, importance, _utc_now(), int(row["id"])),
                )
                updated += 1
        if updated or pruned:
            self.conn.commit()
        return {"updated": updated, "pruned": pruned}

    def search_memory(
        self,
        *,
        session_key: str,
        queries: list[str],
        fact_keys: list[str],
        layers: tuple[str, ...],
        limit: int,
        include_global: bool = True,
    ) -> list[SearchResult]:
        now = _utc_now()
        layer_placeholders = ",".join("?" for _ in layers)
        sessions = [session_key] + (["global"] if include_global else [])
        session_placeholders = ",".join("?" for _ in sessions)
        candidates: dict[int, float] = {}
        params_common: list[Any] = list(layers) + sessions + [now]
        base_where = (
            f"m.layer IN ({layer_placeholders}) AND m.session_key IN ({session_placeholders}) "
            "AND m.tombstoned=0 AND (m.ttl_expires_at IS NULL OR m.ttl_expires_at > ?)"
        )
        for q in [q for q in queries if q.strip()][:6]:
            match = _fts_match_query(q)
            sql = (
                "SELECT m.id, (-bm25(memory_fts)) AS score FROM memory_fts "
                "JOIN memory_items m ON m.id = memory_fts.rowid "
                f"WHERE {base_where} AND memory_fts MATCH ? ORDER BY bm25(memory_fts) LIMIT ?"
            )
            for row in self.conn.execute(sql, params_common + [match, max(4, limit * 4)]).fetchall():
                candidates[int(row["id"])] = max(candidates.get(int(row["id"]), 0.0), float(row["score"] or 0.0))
        if fact_keys:
            fact_rows = self.conn.execute(
                f"""
                SELECT m.id, 4.0 as score
                FROM fact_index fi
                JOIN memory_items m ON m.id = fi.item_id
                WHERE {base_where} AND fi.session_key IN ({session_placeholders}) AND fi.fact_key IN ({','.join('?' for _ in fact_keys)})
                ORDER BY m.updated_at DESC
                LIMIT ?
                """,
                params_common + sessions + fact_keys + [max(4, limit * 4)],
            ).fetchall()
            for row in fact_rows:
                candidates[int(row["id"])] = max(candidates.get(int(row["id"]), 0.0), float(row["score"] or 0.0))
        if not candidates:
            return []
        ids = sorted(candidates, key=lambda item_id: candidates[item_id], reverse=True)[: max(limit * 4, limit)]
        rows = self.conn.execute(
            f"SELECT * FROM memory_items WHERE id IN ({','.join('?' for _ in ids)})",
            ids,
        ).fetchall()
        mapped = {int(row["id"]): row for row in rows}
        results: list[SearchResult] = []
        for item_id in ids:
            row = mapped.get(item_id)
            if not row:
                continue
            if _dirty_profile_fact(str(row["fact_key"] or ""), str(row["value"] or ""), str(row["summary"] or "")):
                continue
            recency_bonus = 0.15 if int(row["updated_at"] or 0) >= now - 86400 * 14 else 0.0
            score = candidates[item_id] + float(row["confidence"] or 0.0) * 0.3 + float(row["importance"] or 0.0) * 0.2 + recency_bonus
            results.append(self._row_to_search_result(row, score))
        dedup: dict[tuple[str, str, str], SearchResult] = {}
        for result in results:
            key = (result.layer, result.fact_key, result.value or result.summary)
            if key not in dedup or dedup[key].score < result.score:
                dedup[key] = result
        ordered = sorted(dedup.values(), key=lambda item: item.score, reverse=True)
        return ordered[:limit]

    def search_events(
        self,
        *,
        session_key: str,
        queries: list[str],
        start_day: str,
        end_day: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        now = _utc_now()
        candidates: dict[int, float] = {}
        for q in [q for q in queries if q.strip()][:6]:
            match = _fts_match_query(q)
            sql = (
                "SELECT e.id, (-bm25(events_fts)) AS score FROM events_fts "
                "JOIN events e ON e.id = events_fts.rowid "
                "WHERE e.session_key=? AND e.day_key BETWEEN ? AND ? AND (e.ttl_expires_at IS NULL OR e.ttl_expires_at > ?) AND events_fts MATCH ? "
                "ORDER BY bm25(events_fts) LIMIT ?"
            )
            for row in self.conn.execute(sql, (session_key, start_day, end_day, now, match, max(4, limit * 4))).fetchall():
                candidates[int(row["id"])] = max(candidates.get(int(row["id"]), 0.0), float(row["score"] or 0.0))
        if not candidates:
            rows = self.conn.execute(
                "SELECT id, summary, ts, day_key, actor, kind, salience FROM events WHERE session_key=? AND day_key BETWEEN ? AND ? ORDER BY salience DESC, ts DESC LIMIT ?",
                (session_key, start_day, end_day, max(4, limit)),
            ).fetchall()
            return [dict(row) for row in rows]
        ids = sorted(candidates, key=lambda item_id: candidates[item_id], reverse=True)[: max(limit * 4, limit)]
        rows = self.conn.execute(
            f"SELECT id, summary, ts, day_key, actor, kind, salience FROM events WHERE id IN ({','.join('?' for _ in ids)})",
            ids,
        ).fetchall()
        mapped = {int(row["id"]): dict(row) for row in rows}
        ordered: list[dict[str, Any]] = []
        for event_id in ids:
            row = mapped.get(event_id)
            if row:
                ordered.append(row)
        return ordered[:limit]

    def refresh_daily_digest(self, session_key: str, day_key: str) -> None:
        rows = self.conn.execute(
            "SELECT summary, kind, salience FROM events WHERE session_key=? AND day_key=? ORDER BY salience DESC, ts DESC LIMIT 12",
            (session_key, day_key),
        ).fetchall()
        deduped_rows = _dedupe_consecutive_rows_by_summary(rows)
        bullets = [f"- [{row['kind']}] {str(row['summary'])[:140]}" for row in deduped_rows]
        digest_micro = " | ".join(bullets[:4])
        digest_meso = "\n".join(bullets[:10])
        self.conn.execute(
            """
            INSERT INTO daily_digests(day_key, session_key, digest_micro, digest_meso, updated_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(day_key, session_key)
            DO UPDATE SET digest_micro=excluded.digest_micro, digest_meso=excluded.digest_meso, updated_at=excluded.updated_at
            """,
            (day_key, session_key, digest_micro, digest_meso, _utc_now()),
        )
        self.conn.commit()

    def refresh_recent_digests(self, session_key: str, days: int = 7) -> None:
        today = datetime.now(self.timezone).date()
        for i in range(max(1, days)):
            day_key = (today - timedelta(days=i)).strftime("%Y-%m-%d")
            self.refresh_daily_digest(session_key, day_key)

    def recent_digest(self, session_key: str, days: int = 2, max_items: int = 3) -> str:
        today = datetime.now(self.timezone).date()
        earliest = (today - timedelta(days=max(1, days) - 1)).strftime("%Y-%m-%d")
        latest = today.strftime("%Y-%m-%d")
        now = _utc_now()
        recent_events = self.conn.execute(
            """
            SELECT day_key, kind, summary, ts, id
            FROM events
            WHERE session_key=? AND day_key BETWEEN ? AND ? AND (ttl_expires_at IS NULL OR ttl_expires_at > ?)
            ORDER BY ts DESC, id DESC
            LIMIT ?
            """,
            (session_key, earliest, latest, now, max(6, max_items * 4)),
        ).fetchall()
        event_entries: list[str] = []
        for row in _dedupe_consecutive_rows_by_summary(recent_events):
            summary = _normalize_text(str(row["summary"] or ""))[:140]
            if not summary:
                continue
            event_entries.append(f"{row['day_key']}:- [{row['kind']}] {summary}")
            if len(event_entries) >= max(1, max_items):
                break
        if event_entries:
            return " | ".join(event_entries)

        rows: list[str] = []
        for i in range(max(1, days)):
            day_key = (today - timedelta(days=i)).strftime("%Y-%m-%d")
            row = self.conn.execute(
                "SELECT digest_micro FROM daily_digests WHERE day_key=? AND session_key=?",
                (day_key, session_key),
            ).fetchone()
            if row and row["digest_micro"]:
                segments = _dedupe_consecutive_strings(str(row["digest_micro"]).split("|"))
                for segment in segments:
                    rows.append(f"{day_key}:{segment}")
                    if len(rows) >= max(1, max_items):
                        return " | ".join(rows)
        return " | ".join(rows[: max(1, max_items)])

    def export_recent_digests(self, session_key: str, days: int = 7) -> list[dict[str, Any]]:
        today = datetime.now(self.timezone).date()
        rows: list[dict[str, Any]] = []
        for i in range(max(1, days)):
            day_key = (today - timedelta(days=i)).strftime("%Y-%m-%d")
            row = self.conn.execute(
                "SELECT digest_micro, digest_meso, updated_at FROM daily_digests WHERE day_key=? AND session_key=?",
                (day_key, session_key),
            ).fetchone()
            if not row or not row["digest_micro"]:
                continue
            rows.append(
                {
                    "day_key": day_key,
                    "digest_micro": str(row["digest_micro"] or ""),
                    "digest_meso": str(row["digest_meso"] or ""),
                    "updated_at": int(row["updated_at"] or _utc_now()),
                }
            )
        return rows

    def surface_anchor(self, session_key: str) -> str:
        row = self.conn.execute(
            "SELECT summary FROM memory_items WHERE session_key=? AND layer='surface' AND kind <> 'snapshot' AND tombstoned=0 ORDER BY updated_at DESC LIMIT 1",
            (session_key,),
        ).fetchone()
        return str(row["summary"]) if row else ""

    def deep_anchor(self, session_key: str) -> str:
        rows = self.conn.execute(
            """
            SELECT fact_key, value, summary, text
            FROM memory_items
            WHERE session_key IN (?, 'global')
              AND layer='deep'
              AND tombstoned=0
              AND (ttl_expires_at IS NULL OR ttl_expires_at > ?)
            ORDER BY updated_at DESC
            LIMIT 24
            """,
            (session_key, _utc_now()),
        ).fetchall()
        if not rows:
            return ""
        ranked: list[tuple[float, str]] = []
        for row in rows:
            fact_key = str(row["fact_key"] or "")
            value = str(row["value"] or "")
            summary = str(row["summary"] or "")
            text = str(row["text"] or "")
            candidate = _prefer_human_anchor_text(fact_key, value, summary, text)
            if not candidate:
                continue
            score = _anchor_candidate_score(fact_key, value, summary, text)
            if score <= 0:
                continue
            ranked.append((score, candidate))
        if not ranked:
            return ""
        ranked.sort(key=lambda item: item[0], reverse=True)
        return ranked[0][1]

    def profile_snapshot(self, session_key: str) -> str:
        self.repair_profile_facts(session_key)
        return self.compute_profile_snapshot(session_key)

    def compute_profile_snapshot(self, session_key: str) -> str:
        style = self.list_style(session_key)
        return self.compute_public_profile_snapshot(session_key, style)

    def compute_public_profile_snapshot(self, session_key: str, style: dict[str, str] | None = None) -> str:
        style = dict(style or {})
        facts = self.conn.execute(
            """
            SELECT fact_key, value FROM memory_items
            WHERE session_key IN (?, 'global') AND layer='deep' AND tombstoned=0 AND fact_key GLOB 'profile.*'
            ORDER BY updated_at DESC LIMIT 30
            """,
            (session_key,),
        ).fetchall()
        parts: list[str] = []
        ordered_style = ["callUser", "firstPerson", "persona", "tone", "speaking_style", "verbosity"]
        for key in ordered_style:
            val = style.get(key)
            if val:
                parts.append(f"{key}:{val}")
        preferred = [
            "profile.identity.card",
            "profile.name",
            "profile.display_name",
            "profile.alias",
            "profile.nickname",
            "profile.role",
            "profile.user_name",
        ]
        excluded_prefixes = (
            "profile.spouse",
            "profile.child",
            "profile.pet",
            "profile.family",
            "profile.relationship",
            "profile.task_",
            "profile.timeline",
        )
        latest: dict[str, str] = {}
        for row in facts:
            fk = str(row["fact_key"] or "")
            value = _clean_fact_value(fk, str(row["value"] or ""))
            if _dirty_profile_fact(fk, value, ""):
                continue
            if fk in latest:
                continue
            if value.strip():
                latest[fk] = value
        for fk in preferred:
            value = latest.get(fk)
            if value:
                parts.append(f"{fk}:{value}")
        return _rewrite_public_labels(" | ".join(parts[:8]))

    def refresh_profile_snapshot(self, session_key: str) -> str:
        self.repair_profile_facts(session_key)
        snapshot = self.compute_profile_snapshot(session_key)
        existing_rows = self.conn.execute(
            """
            SELECT id FROM memory_items
            WHERE session_key=? AND fact_key='profile.snapshot' AND tombstoned=0
            ORDER BY updated_at DESC
            """,
            (session_key,),
        ).fetchall()
        ts = _utc_now()
        if not snapshot:
            if existing_rows:
                ids = [int(row["id"]) for row in existing_rows]
                placeholders = ",".join("?" for _ in ids)
                self.conn.execute(f"UPDATE memory_items SET tombstoned=1, updated_at=? WHERE id IN ({placeholders})", [ts] + ids)
                self.conn.commit()
            return ""
        if existing_rows:
            item_id = int(existing_rows[0]["id"])
            keywords = " ".join(dict.fromkeys(_slug_tokens(snapshot)))
            ngrams = _ngrams(snapshot)
            self.conn.execute(
                "UPDATE memory_items SET value=?, summary=?, text=?, keywords=?, ngrams=?, updated_at=? WHERE id=?",
                (snapshot, snapshot, snapshot, keywords, ngrams, ts, item_id),
            )
            self._upsert_memory_fts(item_id, snapshot, keywords, ngrams)
            if len(existing_rows) > 1:
                ids = [int(row["id"]) for row in existing_rows[1:]]
                placeholders = ",".join("?" for _ in ids)
                self.conn.execute(f"UPDATE memory_items SET tombstoned=1, updated_at=? WHERE id IN ({placeholders})", [ts] + ids)
        else:
            self.insert_memory(
                session_key=session_key,
                layer="surface",
                kind="snapshot",
                fact_key="profile.snapshot",
                value=snapshot,
                text=snapshot,
                summary=snapshot,
                confidence=1.0,
                importance=0.9,
                strength=0.9,
                created_at=ts,
            )
            return snapshot
        self.conn.commit()
        return snapshot

    def repair_profile_facts(self, session_key: str) -> int:
        rows = self.conn.execute(
            """
            SELECT id, fact_key, value, summary FROM memory_items
            WHERE session_key IN (?, 'global') AND fact_key GLOB 'profile.*' AND tombstoned=0
            """,
            (session_key,),
        ).fetchall()
        dirty_ids = [int(row["id"]) for row in rows if _dirty_profile_fact(str(row["fact_key"] or ""), str(row["value"] or ""), str(row["summary"] or ""))]
        if not dirty_ids:
            return 0
        ts = _utc_now()
        placeholders = ",".join("?" for _ in dirty_ids)
        self.conn.execute(f"UPDATE memory_items SET tombstoned=1, updated_at=? WHERE id IN ({placeholders})", [ts] + dirty_ids)
        self.conn.execute(f"DELETE FROM fact_index WHERE item_id IN ({placeholders})", dirty_ids)
        self.conn.commit()
        return len(dirty_ids)

    def refresh_fact_index(self, session_key: str) -> int:
        rows = self.conn.execute(
            """
            SELECT id, session_key, fact_key, updated_at
            FROM memory_items
            WHERE session_key IN (?, 'global') AND layer='deep' AND tombstoned=0 AND fact_key <> ''
            ORDER BY updated_at DESC
            """,
            (session_key,),
        ).fetchall()
        seen: set[tuple[str, str]] = set()
        written = 0
        for row in rows:
            key = (str(row["session_key"]), str(row["fact_key"]))
            if key in seen:
                continue
            seen.add(key)
            self.conn.execute(
                """
                INSERT INTO fact_index(session_key, fact_key, item_id, updated_at)
                VALUES(?,?,?,?)
                ON CONFLICT(session_key, fact_key)
                DO UPDATE SET item_id=excluded.item_id, updated_at=excluded.updated_at
                """,
                (key[0], key[1], int(row["id"]), int(row["updated_at"] or _utc_now())),
            )
            written += 1
        self.conn.commit()
        return written

    def refresh_fts(self, session_key: str) -> dict[str, int]:
        memory_rows = self.conn.execute(
            "SELECT id, summary, keywords, ngrams FROM memory_items WHERE session_key IN (?, 'global') AND tombstoned=0",
            (session_key,),
        ).fetchall()
        event_rows = self.conn.execute(
            "SELECT id, summary, keywords FROM events WHERE session_key=?",
            (session_key,),
        ).fetchall()
        for row in memory_rows:
            self._upsert_memory_fts(int(row["id"]), str(row["summary"] or ""), str(row["keywords"] or ""), str(row["ngrams"] or ""))
        for row in event_rows:
            self._upsert_event_fts(int(row["id"]), str(row["summary"] or ""), str(row["keywords"] or ""), _ngrams(str(row["summary"] or "")))
        self.conn.commit()
        return {"memory": len(memory_rows), "events": len(event_rows)}

    def recent_surface_messages(self, session_key: str, limit: int = 3) -> list[str]:
        rows = self.conn.execute(
            "SELECT summary FROM memory_items WHERE session_key=? AND layer='surface' AND kind <> 'snapshot' AND tombstoned=0 ORDER BY updated_at DESC LIMIT ?",
            (session_key, limit),
        ).fetchall()
        return [str(row["summary"]) for row in rows]

    def duplicate_groups(self, session_key: str, limit: int = 24) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id, fact_key, value, summary FROM memory_items WHERE session_key IN (?, 'global') AND tombstoned=0 AND layer='deep' ORDER BY updated_at DESC LIMIT 300",
            (session_key,),
        ).fetchall()
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            fact_key = str(row["fact_key"] or "")
            summary = str(row["summary"] or "")
            fingerprint = fact_key or summary[:48].lower()
            if not fingerprint:
                continue
            groups.setdefault(fingerprint, []).append(dict(row))
        out = []
        for fingerprint, items in groups.items():
            if len(items) < 2:
                continue
            out.append({"fingerprint": fingerprint, "items": items})
        return out[:limit]

    def apply_merge(self, target_id: int, source_ids: list[int], merged_summary: str, merged_value: str | None = None) -> None:
        ts = _utc_now()
        row = self.conn.execute(
            "SELECT fact_key, value, text FROM memory_items WHERE id=?",
            (target_id,),
        ).fetchone()
        fact_key = str(row["fact_key"] or "") if row else ""
        current_value = str(row["value"] or "") if row else ""
        value = _normalize_text(merged_value if merged_value is not None else current_value)
        summary = _normalize_text(merged_summary)
        text = _normalize_text(str(row["text"] or summary) if row else summary)
        keywords = " ".join(dict.fromkeys(_slug_tokens(" ".join([summary, value, fact_key]))))
        ngrams = _ngrams(" ".join([summary, value, fact_key]))
        self.conn.execute(
            "UPDATE memory_items SET summary=?, value=?, text=?, keywords=?, ngrams=?, updated_at=? WHERE id=?",
            (summary, value, text, keywords, ngrams, ts, target_id),
        )
        self._upsert_memory_fts(target_id, summary, keywords, ngrams)
        if fact_key:
            session_row = self.conn.execute("SELECT session_key FROM memory_items WHERE id=?", (target_id,)).fetchone()
            if session_row:
                self.conn.execute(
                    """
                    INSERT INTO fact_index(session_key, fact_key, item_id, updated_at)
                    VALUES(?,?,?,?)
                    ON CONFLICT(session_key, fact_key)
                    DO UPDATE SET item_id=excluded.item_id, updated_at=excluded.updated_at
                    """,
                    (str(session_row["session_key"]), fact_key, target_id, ts),
                )
        if source_ids:
            placeholders = ",".join("?" for _ in source_ids)
            self.conn.execute(
                f"UPDATE memory_items SET tombstoned=1, updated_at=? WHERE id IN ({placeholders})",
                (ts, *source_ids),
            )
        self.conn.commit()

    def recent_brain_context(self, session_key: str) -> str:
        surface = self.recent_surface_messages(session_key, limit=3)
        events = self.conn.execute(
            "SELECT summary FROM events WHERE session_key=? ORDER BY ts DESC LIMIT 3",
            (session_key,),
        ).fetchall()
        parts = [f"surf:{txt}" for txt in surface]
        parts.extend(f"evt:{str(row['summary'])}" for row in events)
        return " | ".join(parts[:6])
