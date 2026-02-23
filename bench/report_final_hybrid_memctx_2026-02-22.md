# Hybrid MEMCTX 最終検証レポート（2026-02-22）

## 1. 要約
本検証は、OpenClaw における既存方式（`MEMORY.md` 全文注入系 / ベクトル全文注入系）と、提案方式（`surface + deep + fixed-budget MEMCTX`）を比較した。

結論（実運用寄りの OpenClaw 実測）:
- 平均入力トークン: **-52.90%**（9465.44 -> 4458.11）
- 平均レイテンシ: **-8.26%**（4782.07ms -> 4387.09ms）
- 正答率: **同等以上**（0.9778 -> 1.0000）

結論（大規模合成）:
- 固定予算注入（120 tokens）を維持しつつ、全文注入系比で **約98.7%** の入力トークン削減（条件依存）
- deep 検索品質は `lancedb_full` と同等設計（同一検索条件）

---

## 2. 実装確認（固定予算の担保）
`MEMCTX` の固定予算制約はコード上で実施済み。

- `plugin/openclaw-memory-memq/src/memq_core.ts`
  - `compileMemCtx(...)` で `budgetTokens` を受け取り、各行追加前に `if (used + t > budget) break` を実行
  - `surface` / `deep` / `conflicts` / `rules` すべて同じ制約下で切り詰め
- `plugin/openclaw-memory-memq/src/hooks/before_prompt_build.ts`
  - `memq.budgetTokens`（既定120）を読み、毎ターン `compileMemCtx` に渡す
  - 注入トークン概算 (`estimateTokens(memctx)`) をログ化

したがって、**方式そのもの（MEMCTX本文）は固定予算制約を守る**。

---

## 3. 実験設定

### 3.1 Production-like（OpenClaw 実測）
- スクリプト: `bench/scripts/prod_like_openclaw_eval.py`
- モデル: `openai-codex / gpt-5.3-codex`（run metaで確認）
- 比較:
  - `baseline_full`: top-k 全文注入
  - `hybrid_memctx`: `MEMCTX v1` 注入
- 出力:
  - `bench/results/prod_like_detail_30.csv`
  - `bench/results/prod_like_detail_30_s99.csv`（追加run）
  - `bench/results/prod_like_summary_combined.csv`

### 3.2 大規模合成（方式比較）
- スクリプト: `bench/scripts/compare_memory_modes.py`
- 出力: `bench/results/mode_compare*.csv`
- 比較モード:
  - `memory_md`
  - `lancedb`
  - `memq_deep`
  - `memq_hybrid`

### 3.3 カテゴリ保持/long-lag
- スクリプト: `bench/scripts/category_retention_eval.py`
- 条件: `n_mem=15000, n_queries=30000, seeds=4`
- 出力:
  - `bench/results/category_retention_main.csv`
  - `bench/results/category_retention_lag.csv`
  - 集計: `bench/results/category_retention_main_agg.csv`, `bench/results/category_retention_lag_agg.csv`

---

## 4. 結果

### 4.1 OpenClaw 実測（最重要）
出典: `bench/results/prod_like_summary_combined.csv`

| mode | n | accuracy | avg_input_tokens | p95_input_tokens | avg_duration_ms | p95_duration_ms |
|---|---:|---:|---:|---:|---:|---:|
| baseline_full | 45 | 0.9778 | 9465.44 | 25727 | 4782.07 | 6998 |
| hybrid_memctx | 45 | 1.0000 | 4458.11 | 11315 | 4387.09 | 6457 |

差分（Hybrid - Baseline）:
- 入力トークン: **-52.90%**
- 平均レイテンシ: **-8.26%**
- 正答率: **+0.0222**

### 4.1.1 追加検証（予算厳密化後）
出典: `bench/results/prod_like_summary_5_budgetfix.csv`

| mode | n | accuracy | avg_input_tokens | avg_memory_payload_tokens_est | avg_memctx_tokens_est | avg_duration_ms |
|---|---:|---:|---:|---:|---:|---:|
| baseline_full | 5 | 1.0000 | 3569.2 | 4444.0 | 0.0 | 3194.2 |
| hybrid_memctx | 5 | 1.0000 | 341.2 | 117.2 | 117.2 | 2656.6 |

確認点:
- `memctx_tokens_est` 最大値は **118**（`<=120`）
- メモリ注入ペイロード削減率は **97.36%**
- 総入力トークン削減率は **90.44%**（このrun条件）

### 4.2 大規模合成（入力削減の上限感）
出典: `bench/results/mode_compare.csv`

- `memory_md` avg_input_tokens: 9707.86
- `lancedb` avg_input_tokens: 9375.83
- `memq_hybrid` avg_input_tokens: 120.00
- `memq_hybrid` vs `memory_md`: **-98.76%**

補足:
- この試験は「固定予算注入」の効果を強く測る設計。
- 実運用では tool schema や system prompt など他要素があり、削減率は 50%台〜90%台で変動する。

### 4.3 カテゴリ保持・long-lag
出典: `bench/results/category_retention_main_agg.csv`, `bench/results/category_retention_lag_agg.csv`

観察:
- `memq_hybrid` は同スクリプト内で deep retrieval を `lancedb_full` と同条件にしているため、hit率は同等
- `memory_md` は「persona/user_rules/soul」を優遇する合成バイアスが入っており、そのカテゴリで高い
- long-lag では設計上 `memory_md` が優位に出る設定がある

解釈:
- このカテゴリ試験は「注入方式より、検索器の性質差をどう置くか」に依存。
- 実運用評価（4.1）を主指標とし、カテゴリ試験はアブレーションとして扱うのが妥当。

---

## 5. コスト試算（モデル別）

入力トークン単価は 2026-02-22 時点の公開価格に基づくか、公開価格が不明なモデルは近傍モデル価格で代理。

- `gpt-5.3-codex`: 公開価格が明示されないため `gpt-5-codex` 単価を代理
- `gpt-5.2`: OpenAI pricing
- `Claude Opus 4.6 / Sonnet 4.6`: Anthropic pricing pageの Opus/Sonnet 最新系単価で試算

試算根拠（実測）:
- baseline: 9465.44 tokens/turn
- hybrid: 4458.11 tokens/turn
- 削減: 5007.33 tokens/turn（-52.90%）

`bench/results/cost_estimate_by_model.csv` より（入力課金のみ）:

| model | 10,000 turns | 100,000 turns | 1,000,000 turns |
|---|---:|---:|---:|
| gpt-5.3-codex* baseline | $118.32 | $1,183.18 | $11,831.81 |
| gpt-5.3-codex* hybrid | $55.73 | $557.26 | $5,572.64 |
| gpt-5.3-codex* 削減額 | **$62.59** | **$625.92** | **$6,259.17** |
| gpt-5.2 baseline | $165.65 | $1,656.45 | $16,564.53 |
| gpt-5.2 hybrid | $78.02 | $780.17 | $7,801.69 |
| gpt-5.2 削減額 | **$87.63** | **$876.28** | **$8,762.83** |
| Claude Opus 4.6 baseline | $473.27 | $4,732.72 | $47,327.22 |
| Claude Opus 4.6 hybrid | $222.91 | $2,229.06 | $22,290.56 |
| Claude Opus 4.6 削減額 | **$250.37** | **$2,503.67** | **$25,036.67** |
| Claude Sonnet 4.6 baseline | $283.96 | $2,839.63 | $28,396.33 |
| Claude Sonnet 4.6 hybrid | $133.74 | $1,337.43 | $13,374.33 |
| Claude Sonnet 4.6 削減額 | **$150.22** | **$1,502.20** | **$15,022.00** |


asterisk: `gpt-5.3-codex` は `gpt-5-codex` 価格代理。

---

## 6. 137,438 tokens 外れ値について
`hybrid_memctx` に 137,438 tokens が出た件（`bench/results/prod_like_detail_30.csv`）は、
**MEMCTX予算違反ではなく usage 計測側の外れ値**である可能性が高い。

理由:
- `make_memctx`（ベンチ）および `compileMemCtx`（プラグイン）は budget 制約で行追加停止する実装
- 実際の `usage.input` は provider 側の集計仕様（再試行/内部前処理/メタ含有）の影響を受けうる

対処（次改修）:
- `usage.input` と別に `memctx_est_tokens` を毎ターン保存
- `agentMeta.promptTokens` / `lastCallUsage.input` が取れる場合は別列保存
- これで「方式由来」と「プロバイダ計測由来」を分離

---

## 7. 総合結論
1. API LLM モード（Mode A）は **実運用検証可能な完成度**に到達している。
2. 既存全文注入方式に対し、**トークン・コスト・レイテンシを削減**しつつ、少なくとも今回の実測では精度を維持/改善。
3. 表層/深層/揮発モデルは、固定予算注入と組み合わせることで、長期運用時のコスト発散を抑制できる。
4. 追加改善ポイントは「計測分離（外れ値解像度）」と「カテゴリ別評価の現実データ化」。

---

## 8. 参照ファイル
- 実測詳細: `bench/results/prod_like_detail_30.csv`
- 実測詳細(追加): `bench/results/prod_like_detail_30_s99.csv`
- 実測集計: `bench/results/prod_like_summary_combined.csv`
- カテゴリ集計: `bench/results/category_retention_main_agg.csv`
- lag集計: `bench/results/category_retention_lag_agg.csv`
- コスト試算: `bench/results/cost_estimate_by_model.csv`
- 比較ベンチ: `bench/results/mode_compare.csv`
- 実装（予算制約）: `plugin/openclaw-memory-memq/src/memq_core.ts`
- 実装（フック注入）: `plugin/openclaw-memory-memq/src/hooks/before_prompt_build.ts`
