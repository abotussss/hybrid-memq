from __future__ import annotations

import json
import time
from datetime import timedelta
from typing import Dict, List

from .db import MemqDB
from .rules import prune_stale_rule_overrides, refresh_preference_profiles
from .structured_facts import (
    extract_structured_facts_from_text,
    parse_fact_signature_from_row,
    plausible_fact_value,
    structured_fact_summary,
)
from .timeline import date_to_day_key, day_key_to_date, today_day_key


def _promote_structured_from_conv(db: MemqDB, *, dim: int, bits_per_dim: int, max_rows: int = 200) -> int:
    _ = dim
    _ = bits_per_dim
    rows = db.conn.execute(
        "SELECT session_key,summary,updated_at FROM conv_summaries WHERE retention_scope='deep' ORDER BY updated_at DESC LIMIT ?",
        (int(max_rows),),
    ).fetchall()
    if not rows:
        return 0

    existing = set()
    for r in db.list_memory_items_any("deep", limit=20000):
        fk, fv = parse_fact_signature_from_row(dict(r))
        if fk and fv:
            existing.add((str(r["session_key"]), fk, fv))

    wrote = 0
    now = int(time.time())
    for row in rows:
        session_key = str(row["session_key"] or "default")
        ts = int(row["updated_at"] or now)
        lines = str(row["summary"] or "").split("\n")
        for ln in lines:
            facts = extract_structured_facts_from_text(
                ln,
                ts=ts,
                source="idle_consolidation",
                confidence_scale=0.88,
                strip_prefix=True,
            )
            for fact in facts:
                fk = str(fact.get("fact_key") or "")
                fv = str(fact.get("value") or "").strip().lower()
                if not fk or not fv:
                    continue
                if not plausible_fact_value(fk, str(fact.get("value") or "")):
                    continue
                if (session_key, fk, fv) in existing:
                    continue
                summary = structured_fact_summary(fact)
                mid = db.add_memory_item(
                    session_key=session_key,
                    layer="deep",
                    text=summary,
                    summary=summary,
                    importance=0.70,
                    tags={"kind": "structured_fact", "from": "idle_promote", "ts": ts, "fact_keys": [fk], "fact": fact},
                    emb_f16=None,
                    emb_q=None,
                    emb_dim=0,
                    source="idle_consolidation",
                )
                wrote += db.expire_conflicting_fact_keys("deep", session_key, [fk], mid)
                wrote += 1
                existing.add((session_key, fk, fv))

                if ("global", fk, fv) not in existing:
                    gid = db.add_memory_item(
                        session_key="global",
                        layer="deep",
                        text=summary,
                        summary=summary,
                        importance=0.76,
                        tags={"kind": "durable_global_fact", "from": "idle_promote", "ts": ts, "fact_keys": [fk], "fact": fact},
                        emb_f16=None,
                        emb_q=None,
                        emb_dim=0,
                        source="idle_consolidation",
                    )
                    wrote += db.expire_conflicting_fact_keys("deep", "global", [fk], gid)
                    wrote += 1
                    existing.add(("global", fk, fv))
    return wrote


def _prune_invalid_profile_facts(db: MemqDB) -> int:
    rows = db.list_memory_items_any("deep", limit=30000)
    removed = 0
    for r in rows:
        try:
            tags = json.loads(str(r["tags"] or "{}"))
        except Exception:
            tags = {}
        fact = tags.get("fact") if isinstance(tags, dict) else {}
        if not isinstance(fact, dict):
            continue
        fk = str(fact.get("fact_key") or "")
        if fk not in {
            "profile.family.spouse",
            "profile.family.pet",
            "profile.family.child",
            "profile.identity.call_user",
            "profile.identity.first_person",
        }:
            continue
        fv = str(fact.get("value") or "")
        if plausible_fact_value(fk, fv):
            continue
        db.conn.execute("DELETE FROM memory_items WHERE id=?", (str(r["id"]),))
        removed += 1
    if removed > 0:
        db.conn.commit()
    return removed


def _promote_profile_facts(db: MemqDB, *, dim: int, bits_per_dim: int) -> int:
    _ = dim
    _ = bits_per_dim
    style = db.get_style_profile()
    if not style:
        return 0

    now = int(time.time())
    rows = db.list_memory_items("deep", "global", limit=10000)
    existing = set()
    for r in rows:
        try:
            tags = json.loads(str(r["tags"] or "{}"))
        except Exception:
            tags = {}
        fact = tags.get("fact") if isinstance(tags, dict) else {}
        if not isinstance(fact, dict):
            continue
        fk = str(fact.get("fact_key") or "")
        fv = str(fact.get("value") or "").strip().lower()
        if fk and fv:
            existing.add((fk, fv))

    defs: List[Dict[str, object]] = []
    persona = str(style.get("persona") or "").strip()
    tone = str(style.get("tone") or "").strip()
    call_user = str(style.get("callUser") or "").strip()
    first_person = str(style.get("firstPerson") or "").strip()
    if persona:
        defs.append(
            {"subject": "assistant", "relation": "persona.role", "value": persona[:48], "fact_key": "profile.persona.role", "confidence": 0.96}
        )
    if tone:
        defs.append(
            {"subject": "assistant", "relation": "persona.tone", "value": tone[:48], "fact_key": "profile.persona.tone", "confidence": 0.90}
        )
    if call_user:
        defs.append(
            {"subject": "assistant", "relation": "identity.call_user", "value": call_user[:48], "fact_key": "profile.identity.call_user", "confidence": 0.96}
        )
    if first_person:
        defs.append(
            {"subject": "assistant", "relation": "identity.first_person", "value": first_person[:48], "fact_key": "profile.identity.first_person", "confidence": 0.94}
        )

    wrote = 0
    for f in defs:
        fk = str(f["fact_key"])
        fv = str(f["value"]).strip().lower()
        if (fk, fv) in existing:
            continue
        fact = {
            **f,
            "source": "profile_sync",
            "stable": True,
            "ttl_days": 3650,
            "explicit": False,
            "ts": now,
        }
        summary = structured_fact_summary(fact)
        gid = db.add_memory_item(
            session_key="global",
            layer="deep",
            text=summary,
            summary=summary,
            importance=0.90,
            tags={"kind": "durable_global_fact", "from": "profile_sync", "ts": now, "fact_keys": [fk], "fact": fact},
            emb_f16=None,
            emb_q=None,
            emb_dim=0,
            source="idle_consolidation",
        )
        wrote += db.expire_conflicting_fact_keys("deep", "global", [fk], gid)
        wrote += 1
        existing.add((fk, fv))
    return wrote


def _compact_digest_lines(rows: List[Dict[str, object]], max_lines: int = 6) -> str:
    out: List[str] = []
    seen = set()
    for r in rows:
        actor = str(r.get("actor") or "assistant")
        kind = str(r.get("kind") or "chat")
        summary = " ".join(str(r.get("summary") or "").split()).strip()
        if not summary:
            continue
        sig = summary.lower()
        if sig in seen:
            continue
        seen.add(sig)
        line = f"- [{actor}/{kind}] {summary[:160]}"
        out.append(line)
        if len(out) >= max_lines:
            break
    return "\n".join(out)


def _refresh_daily_digests(db: MemqDB, session_key: str, lookback_days: int = 14) -> int:
    today = day_key_to_date(today_day_key())
    start = today - timedelta(days=max(1, int(lookback_days)))
    start_key = date_to_day_key(start)
    end_key = date_to_day_key(today)

    rows = db.conn.execute(
        """
        SELECT DISTINCT day_key
        FROM events
        WHERE day_key>=? AND day_key<=?
          AND (session_key=? OR session_key='global')
        ORDER BY day_key DESC
        """,
        (start_key, end_key, session_key),
    ).fetchall()
    updated = 0
    for r in rows:
        day_key = str(r["day_key"])
        ev = db.list_events_range(
            session_key=session_key,
            start_day=day_key,
            end_day=day_key,
            limit=240,
            include_global=True,
        )
        compact = _compact_digest_lines([dict(x) for x in ev], max_lines=6)
        if not compact:
            continue
        db.upsert_daily_digest(
            day_key=day_key,
            scope="session",
            session_key=session_key,
            compact_text=compact,
            updated_at=int(time.time()),
        )
        updated += 1
    return updated


def run_idle_consolidation(db: MemqDB, session_key: str | None = None, *, dim: int = 256, bits_per_dim: int = 8) -> Dict[str, object]:
    now = int(time.time())
    did: List[str] = []
    stats: Dict[str, int | float] = {}

    did.append("decay")
    ep = db.decay_and_prune_ephemeral()
    stats.update(ep)
    did.append("prune_expired_events")
    stats["expired_events_deleted"] = db.prune_expired_events()

    if session_key:
        did.append("dedup_surface")
        stats["dedup_surface_removed"] = db.dedup_layer("surface", session_key)
        did.append("dedup_surface_fuzzy")
        stats["dedup_surface_fuzzy_removed"] = db.dedup_layer_fuzzy("surface", session_key)
        did.append("dedup_deep")
        stats["dedup_deep_removed"] = db.dedup_layer("deep", session_key)
        did.append("dedup_deep_fuzzy")
        stats["dedup_deep_fuzzy_removed"] = db.dedup_layer_fuzzy("deep", session_key)

    did.append("cleanup_noisy_memory")
    stats.update(db.cleanup_noisy_memory())

    did.append("promote_structured_from_conv")
    stats["structured_promoted_from_conv"] = _promote_structured_from_conv(db, dim=dim, bits_per_dim=bits_per_dim)

    did.append("promote_profile_facts")
    stats["profile_facts_promoted"] = _promote_profile_facts(db, dim=dim, bits_per_dim=bits_per_dim)

    did.append("prune_invalid_profile_facts")
    stats["invalid_profile_facts_removed"] = _prune_invalid_profile_facts(db)

    did.append("backfill_fact_keys")
    stats["fact_keys_backfilled"] = db.backfill_fact_keys(layer="deep", limit=10000)
    did.append("backfill_fact_index")
    stats["fact_index_backfilled"] = db.backfill_fact_index(layer="deep", limit=20000)
    did.append("cleanup_stale_fact_index")
    stats["fact_index_stale_removed"] = db.cleanup_stale_fact_index()

    did.append("profile_refresh")
    updated = refresh_preference_profiles(db, now)
    stats["profile_keys_updated"] = len(updated)

    did.append("rule_override_prune")
    stats["stale_rule_overrides_disabled"] = prune_stale_rule_overrides(db, now)
    if session_key:
        did.append("daily_digest_refresh")
        stats["daily_digests_updated"] = _refresh_daily_digests(db, session_key=session_key, lookback_days=21)

    return {"did": did, "stats": stats}
