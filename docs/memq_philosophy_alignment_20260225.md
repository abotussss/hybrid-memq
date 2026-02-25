# MEMQ Philosophy Alignment Validation (2026-02-25)

## Scope
Validated the requested behavior:
- avoid full-context prompt growth
- Surface -> Deep cue-based recall
- Ephemeral behavior for Surface
- quantized Deep memory
- strict runtime channels (MEMRULES/MEMSTYLE/MEMCTX) preserved
- idle "sleep consolidation" execution evidence

## Code changes in this validation
- `/Users/hiroyukimiyake/Documents/New project/plugin/openclaw-memory-memq/src/hooks/before_prompt_build.ts`
  - hard-cap pruning for old messages (`memq.history.hardCap.enabled`)
  - cue-based deep recall (query cue + surface-derived cue)
  - topM retrieval control (`memq.retrieval.topM`)
- `/Users/hiroyukimiyake/Documents/New project/plugin/openclaw-memory-memq/src/services/state.ts`
  - Surface cache TTL expiration (`memq.surface.ttlSec`) for Ephemeral behavior
- `/Users/hiroyukimiyake/Documents/New project/plugin/openclaw-memory-memq/src/services/sidecar.ts`
  - retry + timeout for sidecar calls (stability)
  - consolidate endpoint compatibility (`/consolidate` and `/index/consolidate`)
- `/Users/hiroyukimiyake/Documents/New project/sidecar/memq_sidecar/app.py`
  - added `/consolidate` alias on consolidate handler

## Runtime checks

### 1) Plugin hook behavior (local mock execution)
Command result:
```json
{"pruned_messages":6,"search_calls":[{"k":12},{"k":6}],"embed_calls":2,"has_memctx_rule":false,"has_hardcap_rule":false,"prepend_tokens_est":215}
```

Interpretation:
- `pruned_messages=6`: old messages were pruned to recent window (hard-cap working)
- `search_calls=[12,6]`: deep retrieval executed as two-step cue recall
  - first: query embedding with `topM=12`
  - second: surface cue embedding with `topM/2=6`
- `embed_calls=2`: query embed + surface-cue embed

### 2) Sidecar memory/rule behavior (HTTP runtime)
Command result:
```json
{"add_deep_ok": true, "add_surface_ok": true, "search_ids": ["deep:test:1", "..."], "surface_excluded_from_deep_search": true, "summarized": 3, "profile_keys": ["avoid_suggestions", "language", "persona", "speaking_style", "suggestion_policy", "tone", "verbosity"], "bad_add_quarantined": true, "quarantine_count": 20, "manual_consolidate_ok": true, "lastConsolidateAt_before": 1771979922, "lastConsolidateAt_after": 1771979922, "idle_auto_consolidated": false}
```

Interpretation:
- `surface_excluded_from_deep_search=true`: Deep search excludes `retention_scope=surface_only`
- `bad_add_quarantined=true`: injection-like text is quarantined, not accepted
- `profile_keys` updated: local non-LLM preference learning is active

Note:
- in this run, `idle_auto_consolidated=false` because manual consolidate ran just before waiting and the interval guard blocked immediate next run (expected behavior).

### 3) Idle sleep consolidation (automatic trigger evidence)
Command result:
```json
{"lastActivityAt_before": 1771980038, "lastConsolidateAt_before": 0, "lastConsolidateAt_after": 1771980128, "idle_auto_consolidated": true}
```

Interpretation:
- no manual call used in this run
- sidecar background loop auto-triggered consolidate after idle threshold

## Outcome
- Full-context injection style is now explicitly constrained by hard-cap + reconstruction.
- Surface/Deep/Ephemeral behavior is active:
  - Surface: LRU + TTL (ephemeral)
  - Deep: quantized int8 vectors + ANN-like topK search
  - Surface cue can trigger deeper recall
- Rule/style channels remain separate and always budgeted independently.
- Sleep consolidation auto-runs during idle and is observable via `lastConsolidateAt`.
