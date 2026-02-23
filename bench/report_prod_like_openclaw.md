# Production-like OpenClaw Evaluation (gpt-5.3-codex)

## Goal
Compare:
- `baseline_full`: existing full-memory style (top-k full memory text injected)
- `hybrid_memctx`: proposed Hybrid (compressed `MEMCTX v1` injection)

on actual OpenClaw agent runs using `openai-codex/gpt-5.3-codex`.

## Environment
- Runtime: OpenClaw local agent (`openclaw agent --local --agent main --json`)
- Model observed in run meta: `provider=openai-codex`, `model=gpt-5.3-codex`
- Query format: deterministic key->value recall task
- Accuracy metric: exact inclusion of expected `value_xxxxxx`

## Dataset / Sample size
- Memory items: `n_mem=300` (long paragraph style)
- Runs:
  - Run A: `n_queries=30` (completed)
  - Run B: `n_queries=30`, partial captured `15 queries` due long-run interruption
- Combined evaluated turns:
  - `baseline_full`: 45
  - `hybrid_memctx`: 45

## Results (combined)
Source: `bench/results/prod_like_summary_combined.csv`

| mode | n | accuracy | avg_input_tokens | p95_input_tokens | avg_duration_ms | p95_duration_ms |
|---|---:|---:|---:|---:|---:|---:|
| baseline_full | 45 | 0.9778 | 9465.4444 | 25727 | 4782.0667 | 6998 |
| hybrid_memctx | 45 | 1.0000 | 4458.1111 | 11315 | 4387.0889 | 6457 |

### Delta (Hybrid vs Baseline)
- Input token reduction: **52.90%**
- Avg latency improvement: **8.26%**
- Accuracy delta: **+0.0222**

## Interpretation
- Hybrid materially reduces prompt size under real OpenClaw execution with gpt-5.3-codex.
- Response time improved in average and p95.
- Accuracy did not degrade in this recall-focused workload; it improved slightly.

## Artifacts
- Detail A: `bench/results/prod_like_detail_30.csv`
- Detail B (partial): `bench/results/prod_like_detail_30_s99.csv`
- Summary A: `bench/results/prod_like_summary_30.csv`
- Summary combined: `bench/results/prod_like_summary_combined.csv`
- Runner: `bench/scripts/prod_like_openclaw_eval.py`
