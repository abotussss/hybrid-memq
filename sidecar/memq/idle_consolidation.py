from __future__ import annotations

import time
from typing import Dict, List

from .db import MemqDB
from .rules import refresh_preference_profiles


def run_idle_consolidation(db: MemqDB, session_key: str | None = None) -> Dict[str, object]:
    now = int(time.time())
    did: List[str] = []
    stats: Dict[str, int | float] = {}

    did.append("decay")
    ep = db.decay_and_prune_ephemeral()
    stats.update(ep)

    if session_key:
        did.append("dedup_surface")
        stats["dedup_surface_removed"] = db.dedup_layer("surface", session_key)
        did.append("dedup_deep")
        stats["dedup_deep_removed"] = db.dedup_layer("deep", session_key)

    did.append("profile_refresh")
    updated = refresh_preference_profiles(db, now)
    stats["profile_keys_updated"] = len(updated)

    return {"did": did, "stats": stats}
