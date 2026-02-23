# Mode A validation (API LLM)

## Goal
`baseline(full text memory injection)` と `memq(MEMCTX v1 budgeted injection)` を同じ問い合わせセットで比較し、入力tokens/latencyを定量化する。

## A) 依存なしスモーク（このリポジトリだけで再現）
```bash
cd /Users/hiroyukimiyake/Documents/New project
pnpm --filter @memq/bench api:smoke
```

生成物:
- `/Users/hiroyukimiyake/Documents/New project/bench/results/api_replay_smoke.csv`

このスモークはモックSidecar/モックOpenAIを使うため、実APIキー不要。

## B) 実API検証（OpenAI互換）
1. Sidecar起動
```bash
cd /Users/hiroyukimiyake/Documents/New project/sidecar
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn memq_sidecar.app:app --host 127.0.0.1 --port 7781
```
2. メモリ投入
```bash
cd /Users/hiroyukimiyake/Documents/New project
python3 bench/scripts/ingest_memory.py --workspace . --sidecar http://127.0.0.1:7781
```
3. リプレイ実行
```bash
export OPENAI_API_KEY=...
export OPENAI_BASE_URL=https://api.openai.com/v1
python3 bench/scripts/replay_api_mode.py \
  --dataset bench/data/replay.jsonl \
  --output bench/results/api_replay_real.csv \
  --sidecar http://127.0.0.1:7781 \
  --model gpt-4.1-mini
```

生成物:
- `/Users/hiroyukimiyake/Documents/New project/bench/results/api_replay_real.csv`

## 判定目安
- `input_token_reduction_pct` が正（例: 40%以上）
- `avg_memq_latency_ms <= avg_baseline_latency_ms`
- 品質低下が懸念される場合は `budgetTokens` と `topK` を上げて再計測
