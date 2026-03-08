from __future__ import annotations

from typing import Any

from sidecar.memq.brain.service import BrainService
from sidecar.memq.config import Config
from sidecar.memq.db import MemqDB


async def run_idle_consolidation(
    *,
    cfg: Config,
    db: MemqDB,
    brain: BrainService,
    session_key: str,
) -> tuple[dict[str, Any], str | None]:
    if cfg.qctx_backend == "memory-lancedb-pro":
        return {"did": ["disabled"]}, None
    stats: dict[str, Any] = {"did": []}
    purge = db.purge_expired()
    if purge["memory"] or purge["events"]:
        stats["did"].append("purge_expired")
        stats["purged"] = purge
    decay = db.decay_ephemera(session_key)
    if decay["updated"] or decay["pruned"]:
        stats["did"].append("ephemera_decay")
        stats["ephemera"] = decay
    db.refresh_recent_digests(session_key, days=7)
    stats["did"].append("refresh_digests")
    snapshot = db.refresh_profile_snapshot(session_key)
    if snapshot:
        stats["did"].append("refresh_profile_snapshot")
    index_rows = db.refresh_fact_index(session_key)
    if index_rows:
        stats["did"].append("refresh_fact_index")

    trace_id: str | None = None
    groups = db.duplicate_groups(session_key, limit=24)
    if groups or cfg.brain_required:
        plan, trace_id, _ = await brain.build_merge_plan(session_key=session_key, candidate_groups=groups)
        applied = brain.apply_merge_plan(db, session_key=session_key, plan=plan)
        stats["did"].append("brain_merge_plan")
        stats["merge"] = applied
        if applied["merged"] or applied["pruned"]:
            fts = db.refresh_fts(session_key)
            stats["did"].append("refresh_fts")
            stats["fts"] = fts
    return stats, trace_id
