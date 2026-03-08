from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sidecar.memq.db import MemqDB, _dirty_rule_value, _dirty_style_value
from sidecar.memq.lancedb_bridge import LanceDbMemoryBackend


STYLE_ALLOWED_KEYS = {"tone", "persona", "verbosity", "speaking_style", "callUser", "firstPerson", "prefix"}
RULE_PREFIXES = ("security.", "language.", "procedure.", "compliance.", "output.", "operation.")
QCTX_PROFILE_ALLOWED_PREFIXES = ("profile.", "project.", "pref.", "relationship.", "timeline.")


def _sort_rows(rows: list[dict[str, Any]], session_key: str) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda row: (
            0 if str(row.get("session_key") or "") == session_key else 1,
            -int(row.get("timestamp") or 0),
        ),
    )


def list_qstyle(db: MemqDB, memory_backend: LanceDbMemoryBackend | None, session_key: str) -> dict[str, str]:
    if memory_backend is None or not memory_backend.enabled():
        return db.list_style(session_key)
    rows = memory_backend.list_entries(session_key=session_key, kinds=["style"], include_global=True, limit=128)
    out: dict[str, str] = {}
    for row in _sort_rows(rows, session_key):
        key = str(row.get("fact_key") or "").replace("qstyle.", "", 1)
        value = str(row.get("value") or "")
        if key not in STYLE_ALLOWED_KEYS or key in out or _dirty_style_value(key, value):
            continue
        out[key] = value
    return out


def list_qrule(db: MemqDB, memory_backend: LanceDbMemoryBackend | None, session_key: str) -> dict[str, str]:
    if memory_backend is None or not memory_backend.enabled():
        return db.list_rules(session_key)
    rows = memory_backend.list_entries(session_key=session_key, kinds=["rule"], include_global=True, limit=256)
    out: dict[str, str] = {}
    for row in _sort_rows(rows, session_key):
        key = str(row.get("fact_key") or "").replace("qrule.", "", 1)
        value = str(row.get("value") or "")
        if not any(key.startswith(prefix) for prefix in RULE_PREFIXES) or key in out or _dirty_rule_value(key, value):
            continue
        out[key] = value
    return out


def profile_snapshot(db: MemqDB, memory_backend: LanceDbMemoryBackend | None, session_key: str, style: dict[str, str]) -> str:
    if memory_backend is None or not memory_backend.enabled():
        return db.compute_public_profile_snapshot(session_key, style)
    parts: list[str] = []
    for key in ("callUser", "firstPerson", "persona", "tone", "speaking_style", "verbosity"):
        value = str(style.get(key) or "").strip()
        if value:
            parts.append(f"{key}:{value}")
    rows = memory_backend.list_entries(
        session_key=session_key,
        kinds=["fact"],
        include_global=True,
        limit=64,
        fact_key_prefixes=["profile."],
    )
    seen = {part.split(":", 1)[0] for part in parts if ":" in part}
    for row in _sort_rows(rows, session_key):
        fact_key = str(row.get("fact_key") or "")
        if not fact_key.startswith("profile."):
            continue
        value = str(row.get("value") or row.get("summary") or row.get("text") or "").strip()
        if not value or fact_key in seen:
            continue
        parts.append(f"{fact_key}:{value}")
        seen.add(fact_key)
        if len(parts) >= 8:
            break
    return " | ".join(parts)


def qctx_profile_snapshot(db: MemqDB, memory_backend: LanceDbMemoryBackend | None, session_key: str) -> str:
    if memory_backend is None or not memory_backend.enabled():
        snapshot = db.compute_public_profile_snapshot(session_key, {})
        parts = [segment.strip() for segment in str(snapshot or "").split("|")]
        kept = [segment for segment in parts if segment.startswith(QCTX_PROFILE_ALLOWED_PREFIXES)]
        return " | ".join(kept[:6])
    rows = memory_backend.list_entries(
        session_key=session_key,
        kinds=["fact"],
        include_global=True,
        limit=96,
        fact_key_prefixes=["profile.", "project.", "pref.", "relationship.", "timeline."],
    )
    parts: list[str] = []
    seen: set[str] = set()
    for row in _sort_rows(rows, session_key):
        fact_key = str(row.get("fact_key") or "")
        if not fact_key.startswith(QCTX_PROFILE_ALLOWED_PREFIXES):
            continue
        if fact_key.startswith(("qstyle.", "qrule.")):
            continue
        value = str(row.get("value") or row.get("text") or row.get("summary") or "").strip()
        if not value:
            continue
        line = f"{fact_key}:{value}"
        marker = line.lower()
        if marker in seen:
            continue
        seen.add(marker)
        parts.append(line)
        if len(parts) >= 6:
            break
    return " | ".join(parts)


def recent_digest(db: MemqDB, memory_backend: LanceDbMemoryBackend | None, session_key: str, *, days: int = 2, max_items: int = 3) -> str:
    if memory_backend is None or not memory_backend.enabled():
        return db.recent_digest(session_key, days=days, max_items=max_items)
    today = datetime.now(db.timezone).date()
    earliest = today - timedelta(days=max(1, days) - 1)
    rows = memory_backend.list_entries(session_key=session_key, kinds=["digest", "event"], include_global=False, limit=64)
    entries: list[str] = []
    for row in _sort_rows(rows, session_key):
        ts = int(row.get("timestamp") or 0)
        if not ts:
            continue
        day = datetime.fromtimestamp(ts, tz=db.timezone).date()
        if day < earliest:
            continue
        summary = str(row.get("summary") or row.get("text") or "").strip()
        if not summary:
            continue
        entries.append(f"{day.isoformat()}:- [{row.get('kind')}] {summary[:140]}")
        if len(entries) >= max_items:
            break
    return " | ".join(entries)


def recent_brain_context(db: MemqDB, memory_backend: LanceDbMemoryBackend | None, session_key: str, *, max_items: int = 6) -> str:
    if memory_backend is None or not memory_backend.enabled():
        return db.recent_brain_context(session_key)
    rows = memory_backend.list_entries(session_key=session_key, kinds=["event", "digest", "fact", "style", "rule"], include_global=True, limit=96)
    entries: list[str] = []
    seen: set[str] = set()
    for row in _sort_rows(rows, session_key):
        kind = str(row.get("kind") or "")
        fact_key = str(row.get("fact_key") or "")
        if kind == "style":
            key = fact_key.replace("qstyle.", "", 1)
            val = str(row.get("value") or "").strip()
            if key in STYLE_ALLOWED_KEYS and val:
                line = f"style:{key}={val}"
            else:
                continue
        elif kind == "rule":
            key = fact_key.replace("qrule.", "", 1)
            val = str(row.get("value") or "").strip()
            if any(key.startswith(prefix) for prefix in RULE_PREFIXES) and val:
                line = f"rule:{key}={val}"
            else:
                continue
        else:
            summary = str(row.get("summary") or row.get("text") or row.get("value") or "").strip()
            if not summary:
                continue
            line = f"{kind}:{summary[:180]}"
        marker = line.lower()
        if marker in seen:
            continue
        seen.add(marker)
        entries.append(line)
        if len(entries) >= max_items:
            break
    return " | ".join(entries)


def surface_anchor(db: MemqDB, memory_backend: LanceDbMemoryBackend | None, session_key: str) -> str:
    if memory_backend is None or not memory_backend.enabled():
        return db.surface_anchor(session_key)
    rows = memory_backend.list_entries(session_key=session_key, kinds=["fact"], include_global=False, layer="surface", limit=8)
    for row in _sort_rows(rows, session_key):
        summary = str(row.get("summary") or row.get("text") or "").strip()
        if summary:
            return summary
    return ""


def deep_anchor(db: MemqDB, memory_backend: LanceDbMemoryBackend | None, session_key: str) -> str:
    if memory_backend is None or not memory_backend.enabled():
        return db.deep_anchor(session_key)
    rows = memory_backend.list_entries(session_key=session_key, kinds=["fact"], include_global=True, layer="deep", limit=24)
    for row in _sort_rows(rows, session_key):
        fact_key = str(row.get("fact_key") or "")
        if fact_key.startswith(("qstyle.", "qrule.")) or fact_key.startswith(RULE_PREFIXES):
            continue
        text = str(row.get("text") or row.get("summary") or row.get("value") or "").strip()
        lowered = text.lower()
        if lowered.startswith(("persona=", "calluser=", "firstperson=", "tone=", "speaking_style=")) or lowered.startswith(RULE_PREFIXES):
            continue
        if text and len(text) >= 12:
            return text
    return ""
