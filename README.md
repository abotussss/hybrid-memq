# Hybrid MEMQ v2 (Mode A)

Hybrid MEMQ v2 is an OpenClaw memory plugin focused on persistent memory with bounded prompt growth.
It injects three fixed-budget blocks every turn:

- `MEMRULES v1` (strict rules / safety)
- `MEMSTYLE v1` (persona / tone consistency)
- `MEMCTX v1` (Surface / Deep / Ephemeral memory context)

## Key Capabilities

- Persistent local memory in SQLite via sidecar.
- Surface-first retrieval, deep fallback, ephemeral decay/prune.
- Deep vector quantization (6/7/8-bit; default 8-bit).
- Fixed token budgets per channel.
- Conversation pruning + archive + reconstruction summary.
- Local learning of preference/policy/style from user messages.
- Quarantine of suspicious memory candidates.
- Primary output audit (rule-based) and optional secondary LLM audit.
- Degraded mode when sidecar is unavailable.

## Repository Layout

- `plugin/openclaw-memory-memq`: OpenClaw plugin (TypeScript)
- `sidecar`: local API service (FastAPI + SQLite)
- `scripts/memq-openclaw.sh`: setup/enable/disable/status CLI
- `docs`: architecture/setup/security and latest runtime persistence proof
- `examples/openclaw.json`: example OpenClaw config

## Requirements

- OpenClaw installed
- Node.js + pnpm
- Python 3.10+
- `openclaw`, `pnpm`, `python3`, `curl`, `lsof`

## Quick Start

```bash
cd /path/to/hybrid-memq
scripts/memq-openclaw.sh setup
scripts/memq-openclaw.sh status
curl -sS http://127.0.0.1:7781/health
```

`setup` performs:

1. Plugin build + install/link
2. MEMQ default config reset
3. Sidecar startup
4. Memory slot switch to `openclaw-memory-memq`

## CLI Commands

| Command | Description |
|---|---|
| `scripts/memq-openclaw.sh setup` | Build/install plugin + start sidecar + enable MEMQ |
| `scripts/memq-openclaw.sh quickstart` | Same as setup |
| `scripts/memq-openclaw.sh install` | Build and install/link plugin only |
| `scripts/memq-openclaw.sh reset-config` | Reset plugin config to MEMQ defaults |
| `scripts/memq-openclaw.sh enable` | Switch OpenClaw memory slot to MEMQ (backup previous state) |
| `scripts/memq-openclaw.sh disable` | Restore previous memory slot/config |
| `scripts/memq-openclaw.sh start-sidecar` | Start sidecar |
| `scripts/memq-openclaw.sh stop-sidecar` | Stop sidecar |
| `scripts/memq-openclaw.sh restart-sidecar` | Restart sidecar |
| `scripts/memq-openclaw.sh status` | Show plugin/slot/sidecar status |
| `scripts/memq-openclaw.sh audit-on <url> <model> [risk] [block]` | Enable secondary LLM audit |
| `scripts/memq-openclaw.sh audit-off` | Disable secondary LLM audit |
| `scripts/memq-openclaw.sh audit-primary-on` | Enable primary rule-based audit |
| `scripts/memq-openclaw.sh audit-primary-off` | Disable primary rule-based audit |
| `scripts/memq-openclaw.sh audit-status` | Show audit settings |
| `scripts/memq-openclaw.sh memstyle-on` | Enable MEMSTYLE injection |
| `scripts/memq-openclaw.sh memstyle-off` | Disable MEMSTYLE injection |
| `scripts/memq-openclaw.sh memstyle-status` | Show MEMSTYLE status |

## Runtime Flow

1. `before_prompt_build`
- split keep/prune by `memq.recent.maxTokens`
- archive pruned history
- summarize pruned history in sidecar
- query sidecar for `MEMRULES/MEMSTYLE/MEMCTX`
- inject in fixed order: `MEMRULES -> MEMSTYLE -> MEMCTX`

2. `agent_end`
- ingest turn into sidecar and update memory/profile

3. `before_compaction`
- request sidecar idle consolidation (best effort)

4. `gateway_start`
- sidecar health check and markdown bootstrap import

## Verification

```bash
python3 -m py_compile sidecar/minisidecar.py sidecar/memq/*.py
python3 -m unittest -v
python3 bench/src/text_sanitization_regression.py
```

## Persistence and Restart Behavior

- Memory, style, rules, and profiles are persisted in sidecar SQLite.
- Gateway restart does not reset MEMQ data.
- Sidecar restart reloads the same SQLite state.
- Latest local proof artifact: `docs/runtime_persistence_latest_20260226.json`

## Security Notes

- No secret values are injected into MEMCTX.
- Suspected injection content is quarantined and excluded from recall.
- Secondary audit is called only on high-risk outputs when enabled.

---

# Hybrid MEMQ v2（Mode A）

Hybrid MEMQ v2 は、OpenClaw向けの永続記憶プラグインです。
毎ターン、以下の3ブロックを固定予算で注入します。

- `MEMRULES v1`（厳格ルール/安全）
- `MEMSTYLE v1`（口調/人格の一貫性）
- `MEMCTX v1`（表層/深層/揮発 記憶文脈）

## 主な機能

- sidecar(SQLite)によるローカル永続記憶。
- 表層優先検索、必要時のみ深層検索、揮発記憶の減衰/剪定。
- 深層ベクトル量子化（6/7/8bit、既定8bit）。
- チャネル別固定トークン予算。
- 会話の剪定・アーカイブ・再構成サマリ。
- 会話からの嗜好/方針/スタイル学習（ローカル）。
- 汚染疑いデータの隔離（quarantine）。
- 一次出力監査（ルールベース）+ 任意の二次LLM監査。
- sidecar障害時のdegraded継続。

## リポジトリ構成

- `plugin/openclaw-memory-memq`: OpenClawプラグイン（TypeScript）
- `sidecar`: ローカルAPI（FastAPI + SQLite）
- `scripts/memq-openclaw.sh`: 導入/切替/状態確認CLI
- `docs`: 設計/導入/セキュリティ/最新永続性証跡
- `examples/openclaw.json`: OpenClaw設定例

## 必要環境

- OpenClaw
- Node.js + pnpm
- Python 3.10+
- `openclaw`, `pnpm`, `python3`, `curl`, `lsof`

## クイックスタート

```bash
cd /path/to/hybrid-memq
scripts/memq-openclaw.sh setup
scripts/memq-openclaw.sh status
curl -sS http://127.0.0.1:7781/health
```

`setup` で実行される内容:

1. プラグインのビルド + インストール/リンク
2. MEMQ既定設定の反映
3. sidecar起動
4. memory slotを `openclaw-memory-memq` に切替

## CLIコマンド

| コマンド | 説明 |
|---|---|
| `scripts/memq-openclaw.sh setup` | ビルド/導入 + sidecar起動 + MEMQ有効化 |
| `scripts/memq-openclaw.sh quickstart` | setupと同じ |
| `scripts/memq-openclaw.sh install` | プラグインのみビルド/導入 |
| `scripts/memq-openclaw.sh reset-config` | MEMQ既定設定に戻す |
| `scripts/memq-openclaw.sh enable` | memory slotをMEMQへ切替（元設定退避） |
| `scripts/memq-openclaw.sh disable` | 元のmemory slot/configへ復元 |
| `scripts/memq-openclaw.sh start-sidecar` | sidecar起動 |
| `scripts/memq-openclaw.sh stop-sidecar` | sidecar停止 |
| `scripts/memq-openclaw.sh restart-sidecar` | sidecar再起動 |
| `scripts/memq-openclaw.sh status` | plugin/slot/sidecar状態確認 |
| `scripts/memq-openclaw.sh audit-on <url> <model> [risk] [block]` | 二次LLM監査を有効化 |
| `scripts/memq-openclaw.sh audit-off` | 二次LLM監査を無効化 |
| `scripts/memq-openclaw.sh audit-primary-on` | 一次監査を有効化 |
| `scripts/memq-openclaw.sh audit-primary-off` | 一次監査を無効化 |
| `scripts/memq-openclaw.sh audit-status` | 監査設定を表示 |
| `scripts/memq-openclaw.sh memstyle-on` | MEMSTYLE注入を有効化 |
| `scripts/memq-openclaw.sh memstyle-off` | MEMSTYLE注入を無効化 |
| `scripts/memq-openclaw.sh memstyle-status` | MEMSTYLE状態を表示 |

## 実行フロー

1. `before_prompt_build`
- `memq.recent.maxTokens` で keep/prune 分割
- prune会話をアーカイブ
- prune会話をsidecarで要約
- `MEMRULES/MEMSTYLE/MEMCTX` を取得
- `MEMRULES -> MEMSTYLE -> MEMCTX` の順で注入

2. `agent_end`
- ターンをsidecarへ取り込み、記憶/プロファイル更新

3. `before_compaction`
- sidecarへ睡眠整理を要求（best effort）

4. `gateway_start`
- sidecarヘルス確認 + Markdown初期取り込み

## 検証コマンド

```bash
python3 -m py_compile sidecar/minisidecar.py sidecar/memq/*.py
python3 -m unittest -v
python3 bench/src/text_sanitization_regression.py
```

## 永続性と再起動

- 記憶/スタイル/ルール/プロファイルはsidecar SQLiteに永続化。
- Gateway再起動ではMEMQデータは消えません。
- sidecar再起動でも同じSQLiteを再読込します。
- 最新のローカル証跡: `docs/runtime_persistence_latest_20260226.json`

## セキュリティ

- 秘密情報をMEMCTXへ注入しません。
- 汚染疑いはquarantineへ隔離し、想起対象から除外。
- 二次監査は有効時かつ高リスク出力時のみ実行。
