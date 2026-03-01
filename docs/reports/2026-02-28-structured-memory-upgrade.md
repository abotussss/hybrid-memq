# Hybrid MEMQ v2 Structured-Memory Upgrade (2026-02-28)

## Requested gaps addressed
- Long-term extraction too strict and leaking facts into short-term-only behavior.
- Free-text deep storage reducing recall stability.
- Retrieval should check long-term store first for memory questions.

## Implemented changes

### 0) Generic refactor (anti-hotfix)
- Removed character-specific branches from style extraction and style defaults.
- Introduced centralized fact-key taxonomy (`fact_keys.py`) and reused it across:
  - DB key inference
  - retrieval keyization
  - MEMCTX packing key intent detection
- Shifted from ad-hoc per-file regex duplication to shared key inference utilities.

Files:
- `sidecar/memq/style.py`
- `sidecar/memq/fact_keys.py`
- `sidecar/memq/db.py`
- `sidecar/memq/retrieval.py`
- `sidecar/memq/retrieval_deep.py`
- `sidecar/memq/memctx_pack.py`

### 1) Structured long-term storage (subject/relation/value)
- Added structured fact extraction in `ingest_turn`:
  - family: spouse/pet/child
  - identity: call user / first person
  - persona role
  - preference: search engine
  - rule facts
- Stored with metadata in `tags.fact` and specific `fact_keys`.
- Added write-gate scoring (`utility/novelty/stability/explicitness/redundancy`) before deep insert.

Files:
- `sidecar/memq/ingest.py`

### 2) Conflict/overwrite behavior improved
- Fact keys were refined to specific keys:
  - `profile.family.spouse`, `profile.family.pet`, `profile.identity.call_user`, etc.
- Existing conflict expiry now applies per specific key, reducing accidental broad overwrites.

Files:
- `sidecar/memq/ingest.py`
- `sidecar/memq/db.py`

### 3) Retrieval changed to long-term-first for memory intents
- Added `long_term_required` detection in retrieval pipeline.
- For memory-like queries, deep lookup is forced even if surface hit exists.
- Deep debug includes `long_term_required` and `deep_key_hits`.

Files:
- `sidecar/memq/retrieval.py`

### 4) Deep ranking now key-first + structured-first
- Query/key matching expanded to specific keys.
- Key-matched rows are prioritized before non-key rows.
- `structured_fact` / `durable_global_fact` kinds receive bonus.
- Diagnostic/noise rows filtered from deep retrieval.

Files:
- `sidecar/memq/retrieval_deep.py`

### 5) MEMCTX packing now de-duplicates by fact key overlap
- For intented queries, repeated lines with same matched key are skipped.
- Multi-intent queries can include more deep rows (`d_limit` adaptive).

Files:
- `sidecar/memq/memctx_pack.py`

### 6) Pre-response verification (source / timestamp / confidence)
- Deep retrieval now computes verification metadata per candidate:
  - `fact_confidence`
  - `fact_ts`
  - `source`
  - `verification_score`
  - `verification_ok`
- For intent-heavy factual prompts, low-verified rows are down-ranked or filtered before MEMCTX packing.
- MEMCTX packing additionally enforces verification gate for deep lines.

Files:
- `sidecar/memq/retrieval_deep.py`
- `sidecar/memq/memctx_pack.py`

### 7) Conflict resolution upgraded from blind overwrite to ranked winner
- Conflict handling now resolves per fact-key by ranking:
  - confidence
  - freshness
  - importance
  - source trust
- Winner is kept; non-winners are removed.
- `conflict_group` is updated with members and policy (`prefer_recent_x_confidence`).

Files:
- `sidecar/memq/db.py`

### 8) Structured summary now retains provenance fields
- Structured deep summaries now include:
  - `subject`
  - `conf`
  - `src`
  - `ttl`
- Fact tags include timestamp (`fact.ts`) for verification.

Files:
- `sidecar/memq/ingest.py`

### 9) Query keyization for retrieval embedding
- Retrieval embedding input now appends canonical key hints (e.g. `profile.family`, `profile.identity`) derived from prompt intent.
- This improves candidate recall for memory-intent prompts without increasing injection tokens.

Files:
- `sidecar/memq/retrieval.py`

### 10) Conversation-summary promotion noise filter
- Added exclusion for imperative/style-command lines so they are not promoted to deep memory from pruned logs.
- Added cleanup filter for known persona-command residue in historical deep rows.

Files:
- `sidecar/memq/conv_summarize.py`
- `sidecar/memq/db.py`

### 11) Structured re-promotion from archived conversation summaries (idle)
- Idle consolidation now re-parses `conv_summaries(deep)` and promotes only structured facts into deep/global.
- This repairs legacy sessions where only free-text convdeep rows existed.
- Promotion records are tagged with `source=idle_consolidation`, and conflict resolution is applied per fact-key.

Files:
- `sidecar/memq/idle_consolidation.py`
- `sidecar/minisidecar.py`

### 12) Factual retrieval strict mode + MEMCTX provenance
- For factual prompts (`q_fact_keys` detected), deep retrieval now requires tag-backed fact-key matches.
- Non-structured convdeep/durable text rows are excluded from factual recall.
- MEMCTX deep lines now carry provenance (`src`, `ts`, `conf`) for matched factual rows.

Files:
- `sidecar/memq/retrieval_deep.py`
- `sidecar/memq/memctx_pack.py`

### 13) Strict structured promotion from conversation summaries
- `conversation/summarize` and idle consolidation now promote only structured facts from summarized lines.
- Free-text convdeep rows are no longer used as factual ground truth.
- Added profile/style synchronization into durable structured facts (`profile_sync`) so persona/identity factual queries remain retrievable after restart.

Files:
- `sidecar/minisidecar.py`
- `sidecar/memq/idle_consolidation.py`

### 14) Value plausibility checks + invalid fact pruning
- Added plausibility checks for profile-like fields (spouse/pet/callUser/firstPerson) to prevent accidental extraction of descriptive words.
- Added idle pruning pass for invalid profile facts.

Files:
- `sidecar/minisidecar.py`
- `sidecar/memq/idle_consolidation.py`

## Evidence (local run)

### A. Structured storage proof
After ingesting:
- `覚えて。妻はともこ。犬はおこげ。`
- `俺のことはヒロって呼んで。`
- `検索はBrave優先で。`

`/memory/list?layer=deep&sessionKey=qa:live-structured-check2` shows:
- `家族: 妻=ともこ | subject=user | conf=0.93`
- `家族: ペット=おこげ | subject=user | conf=0.92`
- `呼称: ユーザー呼称=ヒロ | subject=assistant | conf=0.96`
- `設定: 検索エンジン=brave | subject=user | conf=0.90`

### B. Retrieval output proof (same session)
`/memctx/query` for prompt `家族構成と呼称と検索設定を要約して` returns:
- `d1=家族: ペット=おこげ ...`
- `d2=家族: 妻=ともこ ...`
- `d3=呼称: ユーザー呼称=ヒロ ...`
- `d4=設定: 検索エンジン=brave ...`

### C. Regression suites
- `python3 scripts/qa_memq_v2.py` => `ok=true`
  - includes `structured_memory_storage=true`
  - includes `conflict_resolver_ranked=true`
  - includes `pre_response_verification=true`
- `python3 scripts/qa_live_probe.py` => `ok=true`
- `node scripts/qa_tool_integrity.mjs` => `ok=true`
- `node scripts/qa_plugin_degraded_style.mjs` => `ok=true`

## Current status
- Long-term memory now persists as structured facts and is retrievable by intent-specific keys.
- Memory intent queries now enforce deep check before fallback behavior.
- Free-text-only deep reliance is reduced; structured facts are primary.

## Latest validation snapshot (local)
- `pnpm --dir /Users/hiroyukimiyake/Documents/New project/plugin/openclaw-memory-memq build` => PASS
- `python3 /Users/hiroyukimiyake/Documents/New project/scripts/qa_memq_v2.py` => PASS
  - `memctx_tokens=47`
  - `story_hit_terms=3`
  - `conflict_resolver_ranked=true`
  - `pre_response_verification=true`
- `python3 /Users/hiroyukimiyake/Documents/New project/scripts/qa_live_probe.py` => PASS
  - `/idle/run_once` executed (`did=[decay,dedup,cleanup,promote_structured_from_conv,promote_profile_facts,prune_invalid_profile_facts,profile_refresh,...]`)
  - `structured_promoted_from_conv` and `profile_facts_promoted` confirmed
  - `family` probe returns structured deep fact
  - `persona` probe returns structured deep fact with provenance
  - `meta.debug.deep_verified=5`
  - no MEM block contamination in generated MEMCTX
- `node /Users/hiroyukimiyake/Documents/New project/scripts/qa_tool_integrity.mjs` => PASS
- `node /Users/hiroyukimiyake/Documents/New project/scripts/qa_plugin_degraded_style.mjs` => PASS
