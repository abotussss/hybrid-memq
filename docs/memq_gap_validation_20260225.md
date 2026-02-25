# MEMQ Gap Validation (A-D) - 2026-02-25

This validation addresses the four missing verification points raised in review.

## A) Reconstruction semantic fidelity
Target:
- measure whether conversation summarization preserves requested directive facts
- measure whether retrieved deep memory can support expected directives

Method:
- script: `/Users/hiroyukimiyake/Documents/New project/bench/scripts/verify_reconstruction_fidelity_20260225.py`
- synthetic cases: `N=220` sessions
- evaluates directive fact precision/recall/F1 for:
  - `tone`, `avoid_suggestions`, `language`, `format`, `verbosity`, `persona`, `speaking_style`, `style_avoid`, `retention.default`
- retrieval support proxy:
  - query with latest user request
  - top20 deep retrieval must contain at least one expected directive fact

Result:
- output: `/Users/hiroyukimiyake/Documents/New project/bench/results/reconstruction_fidelity_20260225.json`
- metrics:
  - precision: `0.8721`
  - recall: `0.8148`
  - F1: `0.8425`
  - retrieval support rate (top20): `1.0000`

Interpretation:
- summarization is useful and mostly faithful on directive extraction
- remaining misses are concentrated in:
  - `verbosity=low` under-trigger
  - occasional extra `persona=calm_pragmatic` when concise/pragmatic phrasing is present

## B) Archive safety and growth
Target:
- verify archive does not leak secrets
- verify archive growth remains bounded

Code changes:
- plugin archive path now supports:
  - secret redaction before write
  - per-file byte cap
  - file count cap
  - retention-day cleanup
- implementation:
  - `/Users/hiroyukimiyake/Documents/New project/plugin/openclaw-memory-memq/src/hooks/before_prompt_build.ts`

Method:
- script: `/Users/hiroyukimiyake/Documents/New project/bench/scripts/verify_archive_safety_growth_20260225.mjs`
- config for test:
  - `maxFileBytes=1400`
  - `maxFiles=3`
  - `redactSecrets=true`
- injects archive inputs containing `sk-proj-*` style strings

Result:
- output: `/Users/hiroyukimiyake/Documents/New project/bench/results/archive_safety_growth_20260225.json`
- observed:
  - `file_count=3` (bounded)
  - `max_file_bytes_observed=1327` (bounded)
  - `secrets_found_files=0` (redaction effective)

## C) Pruning policy quality
Target:
- verify bounded history keeps important instructions, not only newest turns

Code changes:
- added keep strategy:
  - `last_n` (baseline)
  - `importance_recency` (new default)
- implementation:
  - `/Users/hiroyukimiyake/Documents/New project/plugin/openclaw-memory-memq/src/hooks/before_prompt_build.ts`

Method:
- script: `/Users/hiroyukimiyake/Documents/New project/bench/scripts/verify_pruning_policy_quality_20260225.mjs`
- scenario:
  - critical rule appears early
  - long filler conversation after that
  - `keepRecentMessages=6`

Result:
- output: `/Users/hiroyukimiyake/Documents/New project/bench/results/pruning_policy_quality_20260225.json`
- `last_n`: critical rule dropped
- `importance_recency`: critical rule retained while keeping `6` messages

## D) Degraded mode quality and recovery
Target:
- quantify quality drop when sidecar retrieval path fails
- verify recovery behavior

Method:
- script: `/Users/hiroyukimiyake/Documents/New project/bench/scripts/verify_degraded_mode_quality_20260225.mjs`
- phases:
  - normal -> degraded(embed/search failure) -> recovered
- metrics:
  - deep lines in MEMCTX
  - slot coverage (`goal/deadline/owner/status`)
  - recovery turns

Result:
- output: `/Users/hiroyukimiyake/Documents/New project/bench/results/degraded_mode_quality_20260225.json`
- normal:
  - deep lines: `1`
  - slot coverage: `0.5`
- degraded:
  - deep lines: `0`
  - slot coverage: `0.0`
- recovered:
  - deep lines: `1`
  - slot coverage: `0.5`
- recovery turns: `1`

Interpretation:
- degraded mode continues safely with reduced memory quality (no crash)
- quality recovers in the next turn once sidecar path is restored

## Summary
- A-D are now covered with executable checks and artifacts.
- Remaining known gap is not instrumentation but model-side heuristic quality tuning:
  - improve recall for `verbosity=low`
  - reduce over-eager `persona=calm_pragmatic` inference in summarization.
