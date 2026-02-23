# MEMQ vs Baselines Report

## 1. Objective
- Compare three families under the same synthetic long-memory workload:
  - `memory_md`: traditional MEMORY.md style full-text injection
  - `lancedb`: vector recall + full-text injection (OpenClaw memory-lancedb equivalent behavior)
  - `memq`: compressed DSL injection with Surface/Deep split (`memq_surface`, `memq_deep`, `memq_hybrid`)

## 2. Experimental Setup
- Main run: `n_mem=12000`, `n_queries=6000`, `seeds=5`, `reuse_prob=0.35`
- Cold run: same but `reuse_prob=0.0`
- Scale runs: `n_mem=5000` and `n_mem=20000`
- Injection policy:
  - `memory_md/lancedb`: top-k full chunk injection (variable tokens)
  - `memq_*`: fixed budget `120` tokens (`MEMCTX v1`)

## 3. Main Results (12k/6k, reuse=0.35, mean over 5 seeds)

| mode | avg_input_tokens | avg_latency_ms | deep_call_rate | surface_hit_rate | hit@1 | hit@3 | hit@5 | context_efficiency |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| memory_md | 9707.8639 | 0.1124 | 0.0000 | 0.0000 | 0.4509 | 0.4515 | 0.4521 | 0.0466 |
| lancedb | 9375.8263 | 0.2356 | 1.0000 | 0.0000 | 0.8131 | 0.8716 | 0.8921 | 0.0952 |
| memq_surface | 0.0000 | 0.0001 | 1.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| memq_deep | 120.0000 | 0.2362 | 1.0000 | 0.0000 | 0.8123 | 0.8711 | 0.8912 | 7.4269 |
| memq_hybrid | 120.0000 | 0.2267 | 0.8536 | 0.1464 | 0.8123 | 0.8711 | 0.8912 | 7.4269 |

### 3.1 Key deltas
- `memq_hybrid` token reduction vs `memory_md`: **98.76%**
- `memq_deep` token reduction vs `lancedb`: **98.72%**
- `memq_deep` hit@5 delta vs `lancedb`: **-0.0009**
- `memq_hybrid` deep_call_rate: **0.8536** (surface absorbs ~14.64% turns)

## 4. Cold-start Results (12k/6k, reuse=0.0)
- Surface benefit naturally drops in cold streams:
  - `memq_hybrid` deep_call_rate: 0.9960
  - `memq_hybrid` surface_hit_rate: 0.0040
- Even in cold streams, fixed-budget compression remains:
  - token reduction (`memq_hybrid` vs `memory_md`): **98.76%**

## 5. Scale Robustness

| setting | memory_md tokens | memq_hybrid tokens | reduction | memory_md hit@5 | memq_hybrid hit@5 |
|---|---:|---:|---:|---:|---:|
| 5k/4k | 9699.3124 | 120.0000 | 98.76% | 0.4482 | 0.9168 |
| 20k/4k | 9573.6592 | 120.0000 | 98.75% | 0.4532 | 0.8686 |

## 6. Interpretation
- The strongest consistent gain is **token compression**: MEMQ fixed budget prevents prompt growth with memory size.
- Deep-only MEMQ preserves retrieval quality close to LanceDB-like recall while dramatically reducing injection size.
- Hybrid MEMQ adds turn-level compute savings in reuse-heavy conversations by skipping deep search when surface is confident.

## 7. Threats to Validity / Limitations
- This report uses a synthetic benchmark harness (controlled, reproducible), not a full human-labeled task set.
- LanceDB behavior is approximated as vector recall + full-text injection; production plugin internals may differ.
- End-to-end quality with real API models should be confirmed with replay logs (`bench/scripts/replay_api_mode.py`).

## 8. Reproducibility
```bash
python3 bench/scripts/compare_memory_modes.py --n-mem 12000 --n-queries 6000 --seeds 5 --reuse-prob 0.35 --out bench/results/mode_compare.csv --out-raw bench/results/mode_compare_raw.csv
python3 bench/scripts/compare_memory_modes.py --n-mem 12000 --n-queries 6000 --seeds 5 --reuse-prob 0.0 --out bench/results/mode_compare_cold.csv --out-raw bench/results/mode_compare_cold_raw.csv
python3 bench/scripts/compare_memory_modes.py --n-mem 5000 --n-queries 4000 --seeds 5 --reuse-prob 0.35 --out bench/results/mode_compare_5k.csv --out-raw bench/results/mode_compare_5k_raw.csv
python3 bench/scripts/compare_memory_modes.py --n-mem 20000 --n-queries 4000 --seeds 3 --reuse-prob 0.35 --out bench/results/mode_compare_20k.csv --out-raw bench/results/mode_compare_20k_raw.csv
```