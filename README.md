# Hybrid MEMQ

Hybrid MEMQ is an OpenClaw memory plugin that keeps prompt growth bounded while preserving long-running memory behavior.

Each turn injects three bounded channels in fixed order:

- `MEMRULES` (strict rules / safety constraints)
- `MEMSTYLE` (persona / tone consistency)
- `MEMCTX` (Surface / Deep / Ephemeral memory context)

## Current Runtime Reality

- API text injection architecture.
- Retrieval is embedding-free at runtime (lexical + fact-key + recency/importance scoring).
- Structured Deep facts are persisted in SQLite with conflict handling and TTL support.
- Ephemeral is not fixed 1-day expiry anymore; it decays and is pruned by low value.
- Output audit redaction is applied even when `block=false` (if `redactedText` is returned).
- `MEMSTYLE` budget default is fixed to `120` tokens (`styleTokens=120`, `maxBudgetTokens=120`).
- Timeline memory is persisted as `events` + `daily_digests` for time-scoped prompts like “yesterday/recent”.

## Why This Plugin Exists

- Stop unbounded token growth from raw-history/memory-file injection.
- Keep long-horizon continuity via compact structured recall.
- Separate concerns into deterministic channels:
  - rules (`MEMRULES`)
  - style/persona (`MEMSTYLE`)
  - memory/context (`MEMCTX`)

## Core Features

- Local sidecar (`FastAPI + SQLite`) with persistent memory state.
- Surface-first retrieval with Deep fallback gating.
- Embedding-free scalable candidate generation via SQLite FTS5/BM25 (with lexical/fact-key reranking).
- Deep fact indexing (`fact_key`) to improve long-lag recall beyond recent-item limits.
- Conversation pruning by token budget, archive of pruned history, and sidecar reconstruction summaries.
- Episodic timeline pipeline: turn/action events -> idle daily digest -> compact `t.*` MEMCTX injection.
- Utility-based MEMCTX packing under fixed budget (intent-weighted, redundancy-penalized, hierarchical fallback).
- Idle sleep-style consolidation (decay, dedup, promotion, profile refresh, cleanup).
- Quarantine for suspicious memory candidates (prompt-injection-like inputs).
- Primary rule-based output audit + optional secondary LLM audit.
- Degraded mode if sidecar is unavailable.

## Repository Layout

- `plugin/openclaw-memory-memq`: OpenClaw plugin (TypeScript)
- `sidecar`: local sidecar service (FastAPI + SQLite)
- `scripts/memq-openclaw.sh`: install/setup/enable/disable/status CLI
- `bench/src`: regression scripts
- `sidecar/tests`: unit/regression tests
- `docs`: architecture, setup, security, benchmark/persistence notes

## Requirements

- OpenClaw
- Node.js + pnpm
- Python 3.10+
- CLI tools: `openclaw`, `pnpm`, `python3`, `curl`, `lsof`

## Quick Start

```bash
cd ~/hybrid-memq
scripts/memq-openclaw.sh setup
scripts/memq-openclaw.sh status
curl -sS http://127.0.0.1:7781/health
```

`setup` runs:

1. build/install plugin
2. reset MEMQ defaults
3. start sidecar
4. switch OpenClaw memory slot to `openclaw-memory-memq`

## CLI Commands

| Command | Description |
|---|---|
| `scripts/memq-openclaw.sh setup` | install + reset-config + start-sidecar + enable + status |
| `scripts/memq-openclaw.sh quickstart` | same as `setup` |
| `scripts/memq-openclaw.sh install` | build and link/install plugin only |
| `scripts/memq-openclaw.sh reset-config` | reset plugin config to MEMQ defaults |
| `scripts/memq-openclaw.sh enable` | switch memory slot to MEMQ (backs up previous plugins config) |
| `scripts/memq-openclaw.sh disable` | restore previous plugins config / memory slot |
| `scripts/memq-openclaw.sh start-sidecar` | start local sidecar supervisor |
| `scripts/memq-openclaw.sh stop-sidecar` | stop sidecar supervisor/app |
| `scripts/memq-openclaw.sh restart-sidecar` | restart sidecar and clean stale listener on `127.0.0.1:7781` |
| `scripts/memq-openclaw.sh status` | print plugin/slot/sidecar status |
| `scripts/memq-openclaw.sh audit-on <url> <model> [risk] [block]` | enable secondary LLM audit path |
| `scripts/memq-openclaw.sh audit-off` | disable secondary LLM audit path |
| `scripts/memq-openclaw.sh audit-primary-on` | enable primary rule-based output audit |
| `scripts/memq-openclaw.sh audit-primary-off` | disable primary rule-based output audit |
| `scripts/memq-openclaw.sh audit-status` | show current audit settings |
| `scripts/memq-openclaw.sh memstyle-on` | enable MEMSTYLE injection |
| `scripts/memq-openclaw.sh memstyle-off` | disable MEMSTYLE injection |
| `scripts/memq-openclaw.sh memstyle-status` | show MEMSTYLE enabled state |

## Default Budgets

- `MEMCTX`: `120`
- `MEMRULES`: `80`
- `MEMSTYLE`: `120`
- `TOTAL INPUT CAP` (estimated): `5200`
- `TOTAL RESERVE` (system/tools margin): `1100`

These are configured in:

- `plugin/openclaw-memory-memq/src/config/schema.ts`
- `scripts/memq-openclaw.sh` (`reset-config`)
- `memq.yaml`

## Runtime Flow

1. `before_prompt_build`
- compute dynamic recent budget from total cap (`memq.total.maxInputTokens`) and reserve
- split messages by dynamic recent budget (bounded by `memq.recent.maxTokens`)
- archive pruned messages (`.memq/conversation_archive/*.jsonl`)
- summarize pruned window into sidecar (`surface_only` + `deep`)
- repair orphan tool-result integrity in kept window
- query sidecar for `MEMRULES/MEMSTYLE/MEMCTX`
- inject in fixed order: `MEMRULES -> MEMSTYLE -> MEMCTX`
- for time-scoped prompts (`昨日/先週/recent`), timeline route is prioritized and injects `t.range/t.digest/t.ev*`
- MEMCTX payload is packed by utility-per-token (intent-weighted with redundancy penalty), not static first-N ordering

2. `agent_end`
- ingest current turn into sidecar
- update memory/profile/rule/style state

3. `before_compaction`
- trigger sidecar idle consolidation (`best effort`)

4. `gateway_start`
- sidecar health probe
- bootstrap import from `MEMORY.md`, `IDENTITY.md`, `SOUL.md`, `HEARTBEAT.md`

## Security and Audit

- MEMQ never injects raw secrets into MEMCTX.
- Suspicious inputs are quarantined and excluded from recall.
- Primary output audit checks risk patterns locally.
- Secondary LLM audit is called only when configured and high-risk threshold is reached.
- If audit returns `redactedText`, plugin applies it even when `block=false`.

## Persistence and Restart Behavior

- Memory/rules/style/profiles are persisted in sidecar SQLite (`.memq/sidecar.sqlite3`).
- OpenClaw gateway restart does not clear MEMQ DB.
- Sidecar restart reloads the same DB and resumes from persisted state.

## Verification

```bash
python3 -m py_compile sidecar/minisidecar.py sidecar/memq/*.py
python3 -m unittest -v
python3 -m unittest -v sidecar.tests.test_regressions
python3 bench/src/text_sanitization_regression.py
python3 bench/src/timeline_scale_check.py
node bench/src/plugin_token_budget_regression.mjs
pnpm -C plugin/openclaw-memory-memq build
```

## Notes

- Runtime retrieval currently does not require external embedding APIs.
- Retrieval uses FTS5/BM25 candidates + lexical/fact-key/recency reranking (no embedding runtime dependency).
- `sidecar/memq/quant.py` remains in the repository, while current retrieval is lexical/fact-key driven.
- timeline range parsing is based on `Asia/Tokyo` day boundaries.

---

# Hybrid MEMQ

Hybrid MEMQ は、OpenClaw向けの記憶プラグインです。  
長い会話でも入力トークンの肥大を抑えながら、記憶の継続性を保つことを目的にしています。

毎ターン、以下の3チャネルを固定順で注入します。

- `MEMRULES`（厳格ルール / 安全制約）
- `MEMSTYLE`（人格・口調の一貫性）
- `MEMCTX`（Surface / Deep / Ephemeral 記憶文脈）

## 現在の実装実態

- API text injectionベースの構成です。
- 実行時検索はEmbedding非依存（語彙一致 + fact-key + 新しさ/重要度スコア）。
- Deepは構造化factをSQLiteに永続保存し、TTL・競合解決を適用。
- Ephemeralは「固定1日削除」ではなく、価値減衰と低価値剪定で整理。
- 出力監査の`redactedText`は`block=false`でも反映。
- `MEMSTYLE`予算は既定で`120`固定（`styleTokens=120`、`maxBudgetTokens=120`）。
- 「昨日/最近」系の問い合わせ向けに、`events`と`daily_digests`の時系列記憶を保持。

## このプラグインの狙い

- 生ログや巨大なmemoryファイルの毎ターン注入を止める。
- 文脈を短い構造化記憶として再構成して維持する。
- ルール・スタイル・記憶を分離し、衝突や取りこぼしを減らす。

## 主な機能

- `FastAPI + SQLite` sidecarによるローカル永続記憶。
- Surface優先、必要時のみDeep検索のゲーティング。
- SQLite FTS5/BM25 を使った embedding 非依存の候補生成（その後に語彙/fact_key/新しさで再ランク）。
- `fact_key`索引による長期想起強化（古い重要記憶の取りこぼし抑制）。
- 会話のトークン予算剪定、剪定履歴アーカイブ、要約再構成。
- エピソード時系列パイプライン（turn/actionイベント -> アイドル時の日次ダイジェスト -> `t.*`としてMEMCTX注入）。
- 固定予算下での効用ベースMEMCTXパッキング（意図重み + 冗長性ペナルティ + 階層フォールバック）。
- アイドル時の睡眠整理（減衰、重複統合、昇格、プロファイル更新、クリーンアップ）。
- 汚染疑い入力の隔離（quarantine）。
- 一次監査（ルールベース）+ 任意の二次LLM監査。
- sidecar障害時のdegraded継続。

## リポジトリ構成

- `plugin/openclaw-memory-memq`: OpenClawプラグイン（TypeScript）
- `sidecar`: ローカルsidecar（FastAPI + SQLite）
- `scripts/memq-openclaw.sh`: 導入/切替/監査設定CLI
- `bench/src`: 回帰スクリプト
- `sidecar/tests`: ユニット/回帰テスト
- `docs`: 設計、導入、セキュリティ、検証メモ

## 必要環境

- OpenClaw
- Node.js + pnpm
- Python 3.10+
- `openclaw`, `pnpm`, `python3`, `curl`, `lsof`

## クイックスタート

```bash
cd ~/hybrid-memq
scripts/memq-openclaw.sh setup
scripts/memq-openclaw.sh status
curl -sS http://127.0.0.1:7781/health
```

`setup`で実行される内容:

1. プラグインをビルド/導入
2. MEMQ既定設定にリセット
3. sidecar起動
4. OpenClawのmemory slotを`openclaw-memory-memq`へ切替

## CLIコマンド

| コマンド | 説明 |
|---|---|
| `scripts/memq-openclaw.sh setup` | install + reset-config + start-sidecar + enable + status |
| `scripts/memq-openclaw.sh quickstart` | `setup`と同じ |
| `scripts/memq-openclaw.sh install` | プラグインのみビルド/導入 |
| `scripts/memq-openclaw.sh reset-config` | MEMQ既定設定に戻す |
| `scripts/memq-openclaw.sh enable` | MEMQを有効化（既存plugins設定を退避） |
| `scripts/memq-openclaw.sh disable` | 退避したplugins設定/slotを復元 |
| `scripts/memq-openclaw.sh start-sidecar` | sidecar supervisor起動 |
| `scripts/memq-openclaw.sh stop-sidecar` | sidecar停止 |
| `scripts/memq-openclaw.sh restart-sidecar` | sidecar再起動（`127.0.0.1:7781`残留リスナー掃除込み） |
| `scripts/memq-openclaw.sh status` | plugin/slot/sidecar状態表示 |
| `scripts/memq-openclaw.sh audit-on <url> <model> [risk] [block]` | 二次LLM監査を有効化 |
| `scripts/memq-openclaw.sh audit-off` | 二次LLM監査を無効化 |
| `scripts/memq-openclaw.sh audit-primary-on` | 一次監査を有効化 |
| `scripts/memq-openclaw.sh audit-primary-off` | 一次監査を無効化 |
| `scripts/memq-openclaw.sh audit-status` | 監査設定表示 |
| `scripts/memq-openclaw.sh memstyle-on` | MEMSTYLE注入を有効化 |
| `scripts/memq-openclaw.sh memstyle-off` | MEMSTYLE注入を無効化 |
| `scripts/memq-openclaw.sh memstyle-status` | MEMSTYLE有効状態を表示 |

## 既定予算

- `MEMCTX`: `120`
- `MEMRULES`: `80`
- `MEMSTYLE`: `120`
- `TOTAL INPUT CAP`（推定）: `5200`
- `TOTAL RESERVE`（system/tool余白）: `1100`

定義場所:

- `plugin/openclaw-memory-memq/src/config/schema.ts`
- `scripts/memq-openclaw.sh`（`reset-config`）
- `memq.yaml`

## 実行フロー

1. `before_prompt_build`
- `memq.total.maxInputTokens` と reserve から、ターンごとの動的 recent 予算を計算
- 動的 recent 予算（上限は `memq.recent.maxTokens`）で会話をkeep/prune分割
- pruneした履歴を`.memq/conversation_archive/*.jsonl`へ保存
- prune履歴をsidecarへ要約投入（`surface_only` + `deep`）
- kept側のtool-result整合を修復
- sidecarから`MEMRULES/MEMSTYLE/MEMCTX`を取得
- `MEMRULES -> MEMSTYLE -> MEMCTX`の順で注入
- `昨日/先週/recent`などの時間表現は時系列ルートを優先し、`t.range/t.digest/t.ev*`を注入
- MEMCTXは固定順序ではなく、トークン効率（utility/token）と冗長性抑制で詰める

2. `agent_end`
- 現在ターンをsidecarへingest
- 記憶/ルール/スタイル/プロファイルを更新

3. `before_compaction`
- sidecarへ睡眠整理を要求（best effort）

4. `gateway_start`
- sidecarヘルス確認
- `MEMORY.md`/`IDENTITY.md`/`SOUL.md`/`HEARTBEAT.md`を初期取り込み

## セキュリティと監査

- MEMCTXへ秘密値を生注入しません。
- 汚染疑い入力はquarantineへ隔離し、想起対象から除外。
- 一次監査はローカルでリスク判定。
- 二次監査LLMは有効化時かつ閾値超過時のみ呼び出し。
- `redactedText`が返った場合、`block=false`でも出力へ反映。
- 検索は FTS5/BM25 候補生成 + 語彙/fact_key/recency 再ランク（実行時 embedding 依存なし）。

## 永続性と再起動

- 記憶/ルール/スタイル/プロファイルは`.memq/sidecar.sqlite3`に永続化。
- OpenClaw Gateway再起動でMEMQデータは消えません。
- sidecar再起動後も同一DBを再読込して継続します。

## 検証コマンド

```bash
python3 -m py_compile sidecar/minisidecar.py sidecar/memq/*.py
python3 -m unittest -v
python3 -m unittest -v sidecar.tests.test_regressions
python3 bench/src/text_sanitization_regression.py
python3 bench/src/timeline_scale_check.py
node bench/src/plugin_token_budget_regression.mjs
pnpm -C plugin/openclaw-memory-memq build
```

## 備考

- 現行実装は外部Embedding APIに依存しません。
- `sidecar/memq/quant.py`はリポジトリに残っていますが、現行の実行経路は語彙/fact-key中心の検索です。
- 時系列の日付解釈は`Asia/Tokyo`境界で行います。
