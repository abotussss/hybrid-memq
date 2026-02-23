# Long-memory benchmark (Mode A)

## Purpose
超長大メモリ（1件あたり数千文字）と大量問い合わせで、以下を検証する。
- token削減
- latency
- deep retrieval hit@k
- surface/deep呼び出し率

## Command
```bash
cd /Users/hiroyukimiyake/Documents/New project
python3 bench/scripts/long_memory_eval.py \
  --n-mem 8000 \
  --n-queries 1000 \
  --long-chars 8000 \
  --surface-max 0 \
  --budget 120 \
  --output bench/results/long_memory_eval_8k_deep_only.csv
```

reuseを含む評価:
```bash
python3 bench/scripts/long_memory_eval.py \
  --n-mem 8000 \
  --n-queries 1000 \
  --long-chars 8000 \
  --surface-max 120 \
  --budget 120 \
  --reuse-prob 0.6 \
  --output bench/results/long_memory_eval_8k_reuse60.csv
```

## Outputs
- `/Users/hiroyukimiyake/Documents/New project/bench/results/long_memory_eval_8k_deep_only.csv`
- `/Users/hiroyukimiyake/Documents/New project/bench/results/long_memory_eval_8k_reuse60.csv`

## Note
この評価はローカル再現を優先した合成ベンチ。実運用の品質評価は `bench/scripts/replay_api_mode.py` で実APIを併用して確認すること。
