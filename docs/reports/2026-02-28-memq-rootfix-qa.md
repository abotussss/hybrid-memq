# Hybrid MEMQ v2 Root-Fix QA Report (2026-02-28)

## 1. Scope
- Goal: fix the observed failure mode where OpenClaw appeared to only recall tiny fragments (`s1/d1`) and long-term memory felt non-persistent.
- Scope covered in this pass:
  - long-memory retrieval robustness
  - tool-call integrity (prevents `No tool call found ...`)
  - sidecar outage durability for ingest/style
  - MEMCTX budget enforcement
  - Ephemeral decay/prune
  - quarantine exclusion path

## 2. Root Causes Found
1. Embedding/tokenization quality was weak for Japanese text (space-split bias), reducing deep recall quality.
2. Deep retrieval gating could skip deep too often when surface sim was high but lexical coverage was poor.
3. Long-term durability was underpowered across session-key churn (too little global deep promotion).
4. Legacy memory pollution (`[MEMRULES v1]`, runtime metadata lines) contaminated retrievable summaries.
5. Sidecar outage degraded to empty MEMSTYLE (style drift after restart/outage windows).
6. History pruning could orphan tool results (provider-side `No tool call found` path).

## 3. Implemented Fixes
- Targeted hardening for the three open issues:
  - duplicate consolidation strengthened (fact-key merge + fuzzy dedup + conflict expiry)
  - injection minimization hardened (query-conditioned strict filtering and tighter line limits)
  - update/expiry behavior hardened (new fact overrides older conflicting facts; stale user rule overrides auto-disabled)

- Retrieval/Memory core:
  - Improved deterministic embedding tokenizer with CJK bi/tri-grams and char n-grams:
    - `/Users/hiroyukimiyake/Documents/New project/sidecar/memq/quant.py`
  - Deep gating now considers lexical coverage, not only cosine sim:
    - `/Users/hiroyukimiyake/Documents/New project/sidecar/memq/retrieval.py`
  - Reduced noisy global deep bias (`convdeep_global`) and kept durable bonus:
    - `/Users/hiroyukimiyake/Documents/New project/sidecar/memq/retrieval_deep.py`
  - Added cross-session deep fallback search when session-local deep pool is small:
    - `/Users/hiroyukimiyake/Documents/New project/sidecar/memq/db.py`
    - `/Users/hiroyukimiyake/Documents/New project/sidecar/memq/retrieval_deep.py`
  - Auto deep promotion for stable facts + global mirror for session-churn durability:
    - `/Users/hiroyukimiyake/Documents/New project/sidecar/memq/ingest.py`
  - Added fact-key extraction + conflict expiry to enforce deterministic overwrite semantics:
    - `/Users/hiroyukimiyake/Documents/New project/sidecar/memq/ingest.py`
    - `/Users/hiroyukimiyake/Documents/New project/sidecar/memq/db.py`

- Pollution control:
  - Added bracket-format MEM block stripping (`[MEMRULES v1]` etc):
    - `/Users/hiroyukimiyake/Documents/New project/sidecar/memq/ingest.py`
    - `/Users/hiroyukimiyake/Documents/New project/sidecar/memq/conv_summarize.py`
    - `/Users/hiroyukimiyake/Documents/New project/sidecar/memq/memctx_pack.py`
    - `/Users/hiroyukimiyake/Documents/New project/sidecar/memq/db.py`
  - Runtime meta lines (`HEARTBEAT/AGENTS/...`) filtered from memory summaries.
  - Added retrieval-time noise exclusion and MEMCTX dedupe/normalization (`reply_to_current`, markdown markers, near-duplicate lines):
    - `/Users/hiroyukimiyake/Documents/New project/sidecar/memq/retrieval_surface.py`
    - `/Users/hiroyukimiyake/Documents/New project/sidecar/memq/retrieval_deep.py`
    - `/Users/hiroyukimiyake/Documents/New project/sidecar/memq/memctx_pack.py`
  - Deep-candidate generation changed to compact fact-style summaries (user-grounded) instead of storing long assistant prose:
    - `/Users/hiroyukimiyake/Documents/New project/sidecar/memq/ingest.py`
  - Added stale user-rule expiry in idle consolidation:
    - `/Users/hiroyukimiyake/Documents/New project/sidecar/memq/rules.py`
    - `/Users/hiroyukimiyake/Documents/New project/sidecar/memq/idle_consolidation.py`

- Budget and observability:
  - MEMCTX now includes tiny pool hints (`meta.surface_pool`, `meta.deep_pool`):
    - `/Users/hiroyukimiyake/Documents/New project/sidecar/memq/memctx_pack.py`

- Plugin resilience:
  - Added orphan tool-result repair in prompt hook:
    - `/Users/hiroyukimiyake/Documents/New project/plugin/openclaw-memory-memq/src/hooks/before_prompt_build.ts`
  - Added ingest durable queue and flush:
    - `/Users/hiroyukimiyake/Documents/New project/plugin/openclaw-memory-memq/src/lib/ingest_queue.ts`
    - `/Users/hiroyukimiyake/Documents/New project/plugin/openclaw-memory-memq/src/hooks/agent_end.ts`
    - `/Users/hiroyukimiyake/Documents/New project/plugin/openclaw-memory-memq/src/hooks/before_prompt_build.ts`
  - Added MEMSTYLE cache fallback for sidecar outage windows:
    - `/Users/hiroyukimiyake/Documents/New project/plugin/openclaw-memory-memq/src/lib/style_cache.ts`
    - `/Users/hiroyukimiyake/Documents/New project/plugin/openclaw-memory-memq/src/hooks/before_prompt_build.ts`

## 4. QA Artifacts
- `/Users/hiroyukimiyake/Documents/New project/docs/reports/artifacts/qa_memq_v2.json`
- `/Users/hiroyukimiyake/Documents/New project/docs/reports/artifacts/qa_tool_integrity.json`
- `/Users/hiroyukimiyake/Documents/New project/docs/reports/artifacts/qa_live_probe.json`
- `/Users/hiroyukimiyake/Documents/New project/docs/reports/artifacts/qa_plugin_degraded_style.json`
- `/Users/hiroyukimiyake/Documents/New project/docs/reports/artifacts/idle_run_once.json`
- `/Users/hiroyukimiyake/Documents/New project/docs/reports/artifacts/db_snapshot.txt`

## 5. Test Matrix and Results
### A) Core deterministic QA (`scripts/qa_memq_v2.py`)
- Command: `python3 scripts/qa_memq_v2.py`
- Result: PASS
- Checks passed:
  - style/rule separation
  - long-memory recall across session churn
  - MEMCTX budget guard
  - Ephemeral decay/prune
  - quarantine trigger
- Key metrics:
  - `memctx_tokens=29` (budget 120)
  - `story_hit_terms=3` (target terms recovered from deep recall)
  - `deep_called=true`
  - `dup_brave_rows=0` / `dup_google_rows=1` in overwrite test (new fact wins, old conflicting fact expired)

### B) Tool integrity guard (`scripts/qa_tool_integrity.mjs`)
- Command: `node scripts/qa_tool_integrity.mjs`
- Result: PASS
- Verified:
  - orphan tool-result removed
  - valid tool-result preserved

### C) Live sidecar probe on current local DB (`scripts/qa_live_probe.py`)
- Command: `python3 scripts/qa_live_probe.py`
- Result: PASS
- Observed:
  - memory stats present (`deep` and `surface` non-zero)
  - memstyle returns persisted persona/style keys
  - memrules returns strict rule channel
  - memctx generated within fixed budget
  - deep retrieval called when surface lexical coverage is low
  - forbidden noise markers are absent from MEMCTX (`[MEM*]`, `[[reply_to_current]]`, runtime metadata markers)

### D) Plugin degraded-mode style persistence (`scripts/qa_plugin_degraded_style.mjs`)
- Command: `node scripts/qa_plugin_degraded_style.mjs`
- Result: PASS
- Verified:
  - sidecar down path still injects MEMSTYLE from local cache
  - persona/callUser survive outage windows

## 6. Current Local Runtime Evidence (after cleanup run)
- Plugin slot:
  - `openclaw config get plugins.slots.memory` -> `openclaw-memory-memq`
- Memory DB snapshot:
  - see `/Users/hiroyukimiyake/Documents/New project/docs/reports/artifacts/db_snapshot.txt`
- Live probe:
  - see `/Users/hiroyukimiyake/Documents/New project/docs/reports/artifacts/qa_live_probe.json`

## 7. What This Fix Set Guarantees
- Long-memory is no longer constrained to tiny per-session fragments; deep/global durability is improved and retriever quality is improved for Japanese-heavy usage.
- MEMCTX remains fixed-budget bounded.
- Style survives sidecar outages better (cached fallback).
- Tool-call orphan errors are mitigated at prune time.

## 8. Remaining Limitations
- Deterministic local embedding is still heuristic (not equal to a trained semantic encoder).
- If global deep is polluted by user data quality, retrieval quality still depends on cleanup policy.
- Full end-to-end channel replay benchmark (real provider latency/cost over many runs) is outside this single QA pass and should be run as a separate benchmark job.
