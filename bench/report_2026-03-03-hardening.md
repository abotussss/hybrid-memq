# MEMQ Hardening Report (2026-03-03)

## Scope

This pass targeted three runtime regressions seen in production-like runs:

1. Japanese recall weakness under embedding-free FTS.
2. Language-audit false positives in Japanese-first sessions.
3. Config drift causing token-heavy defaults in OpenClaw plugin manifest.

## Code Changes

- `/Users/hiroyukimiyake/Documents/New project/sidecar/memq/db.py`
  - Added lexical n-gram augmentation to FTS-indexed `content`.
  - Added CJK-aware fallback query path in `search_memory_fts`.
  - Added one-time FTS rebuild marker (`kv_state: fts_terms_version=v2`) to reindex existing rows with augmented terms.
- `/Users/hiroyukimiyake/Documents/New project/sidecar/memq/rules.py`
  - `extract_allowed_languages_from_rules` default changed to `["ja","en"]`.
  - Added `language.primary` preference fallback (confidence-gated).
- `/Users/hiroyukimiyake/Documents/New project/sidecar/memq/audit.py`
  - Defensive fallback changed from `["en"]` to `["ja","en"]`.
- `/Users/hiroyukimiyake/Documents/New project/plugin/openclaw-memory-memq/openclaw.plugin.json`
  - Aligned defaults with runtime schema:
    - `memq.total.maxInputTokens=4200`
    - `memq.total.reserveTokens=1800`
    - `memq.total.capSafetyRatio=0.72`
    - `memq.recent.maxTokens=2600`
    - `memq.recent.minKeepMessages=4`
- `/Users/hiroyukimiyake/Documents/New project/sidecar/tests/test_regressions.py`
  - Added regressions for:
    - Japanese n-gram FTS recall.
    - Default allowed languages (`ja,en`).
    - Primary-language preference influence.

## Verification Results

Executed on local repository state:

- `python3 -m py_compile sidecar/minisidecar.py sidecar/memq/*.py` -> PASS
- `python3 -m unittest -v sidecar.tests.test_regressions` -> PASS (43 tests, 1 skipped)
- `python3 bench/src/text_sanitization_regression.py` -> PASS
- `python3 bench/src/generic_recall_battery.py` -> PASS
  - success_rate = 1.000
  - empty_material_rate = 0.000
  - avg_memctx_tokens = 55.4
- `python3 bench/src/timeline_scale_check.py` -> PASS
  - events = 50000
  - yesterday_hits = 200
  - query_ms = 48.77
- `node bench/src/plugin_token_budget_regression.mjs` -> PASS
- `pnpm -C plugin/openclaw-memory-memq build` -> PASS

## Notes

- Retrieval remains embedding-free at runtime.
- This pass improves Japanese matching without introducing embedding dependencies.
- Language audit defaults are now compatible with Japanese+English sessions and reduce policy false positives.
