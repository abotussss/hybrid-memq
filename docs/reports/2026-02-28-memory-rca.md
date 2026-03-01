# Hybrid MEMQ v2 RCA / QA Report (2026-02-28)

## Scope
Root-cause analysis and remediation for:
- long-term memory not recalled (family/persona/recent)
- memory apparently lost across restart
- occasional tool-call pairing failure (`No tool call found for function call output`)

## Reproduced Symptoms
- Deep store contained relevant rows, but MEMCTX sometimes injected generic durable lines first.
- Family/persona queries were not consistently intent-focused.
- Runtime complaints about missing memory despite persisted DB rows.

## Root Causes
1. Retrieval ranking favored high-usage generic durable rows over intent-matched facts.
2. MEMCTX packer accepted deep rows with low intent relevance when durable-like text existed.
3. Conversation-deep promotion allowed diagnostic/meta lines into deep memory.
4. Sidecar path stability depended on runtime cwd if `MEMQ_ROOT` was not fixed.
5. Tool integrity repair covered common role-level tool results but needed broader block-level safety.

## Fixes Implemented
### Retrieval / Packing
- `sidecar/memq/retrieval_deep.py`
  - Added stronger intent-key bias and hard-gate for non-overlap rows on intented queries.
  - Added diagnostic-line suppression (`META_DIAGNOSTIC_RE`).
  - Added recent-query diagnostic suppression (`RECENT_DIAGNOSTIC_RE`).
- `sidecar/memq/memctx_pack.py`
  - Added query intent key inference.
  - Prioritized deep lines by key overlap and lexical relevance for intented prompts.
  - Restricted global durable inclusion when intent mismatch.

### Ingest / Consolidation hygiene
- `sidecar/memq/conv_summarize.py`
  - Added promotable-line filters for deep promotion.
  - Excluded diagnostic/meta/self-label lines from promotion.
- `sidecar/memq/db.py`
  - Expanded noisy-memory cleanup patterns.

### Runtime stability and persistence path
- `plugin/openclaw-memory-memq/src/lib/sidecar_client.ts`
  - `ensureUp()` now launches sidecar with explicit `MEMQ_ROOT` and `MEMQ_DB_PATH`.
- `scripts/memq-openclaw.sh`
  - start-sidecar now exports fixed `MEMQ_ROOT` / `MEMQ_DB_PATH`.
  - stop-sidecar now cleans stale supervisor/minisidecar processes (`pkill` fallback).

### Tool-call integrity hardening
- `plugin/openclaw-memory-memq/src/hooks/before_prompt_build.ts`
  - Extended repair to handle orphan tool-result blocks in content arrays.

## Verification Evidence

### 1) Core QA suite
Command:
```bash
python3 scripts/qa_memq_v2.py
```
Result: `ok=true`
- style/rule separation: PASS
- long-memory recall: PASS (`story_hit_terms=3`)
- MEMCTX budget guard: PASS (`memctx_tokens=29`)
- dedup/overwrite: PASS
- ephemeral decay/prune: PASS
- quarantine trigger: PASS

### 2) Live DB probe (workspace DB)
Command:
```bash
python3 scripts/qa_live_probe.py
```
Result: `ok=true`
- idle consolidation executed with expected stages (`decay`, `dedup`, `cleanup`, `backfill_fact_keys`, `profile_refresh`, `rule_override_prune`).
- family probe MEMCTX top line:
  - `d1=覚えて | 家族構成: 妻はともこ、犬はおこげ`
- persona probe includes persona-focused lines.
- recent probe reduced diagnostic noise (single recent fact line selected in this run).

### 3) Restart persistence proof
Single-run scripted restart test (start -> ingest/query -> stop -> restart -> query): PASS
- same family fact recovered before and after restart:
  - `d1=覚えて | 家族構成: 妻はともこ、犬はおこげ`

### 4) Plugin-level integrity/degraded tests
Commands:
```bash
node scripts/qa_tool_integrity.mjs
node scripts/qa_plugin_degraded_style.mjs
```
Result: both PASS
- orphan tool result is removed deterministically.
- degraded path still injects cached MEMSTYLE.

## Current Status
- Persistence path is fixed to workspace `.memq/sidecar.sqlite3`.
- Deep recall quality for intented queries is materially improved.
- MEMCTX remains within fixed budget in regression tests.
- Idle consolidation evidence is now captured in live probe output.

## Remaining Work (known, non-blocking)
- Persona query can still surface rough user-origin lines if they are semantically closest.
  - next step: add response-level style-normalized rewrite hint for persona-answer spans (without altering memory truth).

