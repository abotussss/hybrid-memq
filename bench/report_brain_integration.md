# MEMQ Brain Integration Verification (Local)

Date: 2026-03-03

## Scope

- Integrate local `MEMQ Brain` planning path into sidecar runtime without breaking deterministic fallback.
- Keep existing fixed-budget channels (`MEMRULES`/`MEMSTYLE`/`MEMCTX`) and existing OpenClaw plugin contract.
- Validate both:
  - Brain unavailable -> deterministic fallback continues.
  - Brain available -> plan-driven path is used.

## Implemented

- Brain config added/used from sidecar config:
  - `MEMQ_BRAIN_ENABLED`
  - `MEMQ_BRAIN_PROVIDER`
  - `MEMQ_BRAIN_BASE_URL`
  - `MEMQ_BRAIN_MODEL`
  - `MEMQ_BRAIN_TIMEOUT_MS`
  - `MEMQ_BRAIN_KEEP_ALIVE`
  - `MEMQ_BRAIN_TEMPERATURE`
  - `MEMQ_BRAIN_MAX_TOKENS`
  - `MEMQ_BRAIN_CONCURRENT`
- `minisidecar.py` wiring:
  - `/memory/ingest_turn`: Brain ingest plan first, deterministic fallback on failure.
  - `/memctx/query`: Brain recall plan first, deterministic fallback on failure.
  - `/health`: includes Brain status.
- Brain modules:
  - strict schema models (`BrainIngestPlan`, `BrainRecallPlan`, etc.)
  - Ollama client with JSON-schema response validation and cooldown on failure.
  - safe apply logic (fact-key whitelist, explicit gate for style/rules update, quarantine handling).
- Retrieval path:
  - `retrieve_candidates_with_plan(...)` for plan-driven search/boost.
- Prompt assets:
  - ingest/recall/merge/audit patch prompt templates added.

## Critical Fixes During Integration

- Brain timeout previously surfaced as HTTP 500 in `/memory/ingest_turn`.
- Fixed by normalizing transport errors to `BrainUnavailable` and forcing fallback path.
- Verified fallback now returns `ok=true` with `wrote.brain=0`.

## Verification Commands

```bash
python3 -m py_compile sidecar/minisidecar.py sidecar/memq/*.py sidecar/memq/brain/*.py
sidecar/.venv/bin/python -m unittest -v sidecar.tests.test_regressions
sidecar/.venv/bin/python bench/src/text_sanitization_regression.py
sidecar/.venv/bin/python bench/src/generic_recall_battery.py
sidecar/.venv/bin/python bench/src/timeline_scale_check.py
node bench/src/plugin_token_budget_regression.mjs
pnpm -C plugin/openclaw-memory-memq build
```

## Verification Results

- `unittest`: PASS (`40` tests, `0` fail)
- `text_sanitization_regression.py`: PASS
- `generic_recall_battery.py`: PASS
  - `success_rate=1.000`
  - `empty_material_rate=0.000`
  - `avg_memctx_tokens=57.2`
- `timeline_scale_check.py`: PASS
  - `events=50000`
  - `yesterday_hits=200`
  - query plan uses events index
- `plugin_token_budget_regression.mjs`: PASS
- plugin TypeScript build: PASS

## Live Path Checks

### 1) Brain unavailable fallback (real sidecar run)

- Sidecar started with:
  - `MEMQ_BRAIN_ENABLED=1`
  - no Ollama backend
- `/memory/ingest_turn` response:
  - `ok=true`
  - `wrote.brain=0`
- `/memctx/query` response:
  - `ok=true`
  - valid `MEMCTX` under budget (`budget_tokens=120`)

### 2) Brain plan path (mock Ollama run)

- Fake local Ollama endpoint returned schema-valid plans.
- `/memory/ingest_turn` response:
  - `wrote.brain=1`
- `/memctx/query` debug:
  - `brain_plan=1`
  - `brain_plan_used=1`

## Notes

- Brain is now optional and non-blocking for runtime continuity.
- Deterministic fallback remains the safety baseline.
- Existing MEMQ hard constraints (budgeted channels, quarantine, sleep consolidation, retrieval/index paths) remain active.
