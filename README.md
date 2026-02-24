# hybrid-memq

A production-focused memory plugin for OpenClaw with a **Surface / Deep / Ephemeral** memory model and fixed-budget **MEMCTX** injection.

`hybrid-memq` replaces the default memory slot with a compact retrieval-and-injection pipeline designed to improve long-session memory quality while reducing input-token cost.

## Features
- Surface / Deep / Ephemeral memory architecture
- Fixed-budget MEMCTX compilation (`k=v` fact DSL)
- OpenClaw hook integration (`before_prompt_build`, `agent_end`, `before_compaction`, `gateway_start`)
- Local sidecar (SQLite + embedding/retrieval + consolidation + audit)
- Preference/profile learning (non-LLM, local rules + decay aggregation)
- Memory quarantine for suspicious/polluting facts
- Optional high-risk dual output audit (rule-based + secondary LLM audit)
- Seamless enable/disable switch for OpenClaw memory slot

## Repository Layout
```text
core/                         Shared memory logic (scoring, memctx, gates, decay)
plugin/openclaw-memory-memq/  OpenClaw memory plugin (TypeScript)
sidecar/                      Local sidecar (Python)
docs/                         Design and operations docs
examples/                     Example OpenClaw config
scripts/                      One-command setup/switch helpers
memq.yaml                     Reference configuration
```

## Requirements
- OpenClaw installed locally
- Node.js 20+ and pnpm
- Python 3.10+

## Quick Start
### 1) Build plugin
```bash
cd ~/hybrid-memq/plugin/openclaw-memory-memq
pnpm install
pnpm build
```

### 2) Install plugin into OpenClaw
```bash
openclaw plugins install -l ~/hybrid-memq/plugin/openclaw-memory-memq
```

### 3) Start sidecar
Minimal mode (no extra deps):
```bash
cd ~/hybrid-memq/sidecar
python3 minisidecar.py
```

Or FastAPI mode:
```bash
cd ~/hybrid-memq/sidecar
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn memq_sidecar.app:app --host 127.0.0.1 --port 7781
```

### 4) Enable hybrid-memq in OpenClaw
```bash
scripts/memq-openclaw.sh setup
```

### 5) Verify runtime
```bash
scripts/memq-openclaw.sh status
curl -sS http://127.0.0.1:7781/health
```

## CLI Commands
`scripts/memq-openclaw.sh`

| Command | Purpose |
|---|---|
| `install` | Install/link plugin into OpenClaw |
| `setup` | Interactive first-run wizard (recommended) |
| `configure` | Interactive command menu for ongoing operations |
| `enable` | Enable `openclaw-memory-memq` memory slot (backup existing config) |
| `disable` | Restore previous OpenClaw config from backup |
| `on` | Shortcut for `quickstart` |
| `off` | Disable MEMQ and stop sidecar |
| `start-sidecar` | Start local sidecar supervisor (auto-restart) |
| `stop-sidecar` | Stop local sidecar |
| `restart-sidecar` | Restart sidecar and clean stale listener on `127.0.0.1:7781` |
| `status` | Show plugin/slot/sidecar status |
| `quickstart` | `install + start-sidecar + enable + status` (non-interactive) |
| `audit-on <url> <model> [risk_threshold] [block_threshold]` | Enable secondary LLM audit for high-risk outputs |
| `audit-off` | Disable secondary LLM audit (MEMQ itself stays enabled) |
| `audit-primary-on` | Enable primary output audit (rule-based) |
| `audit-primary-off` | Disable primary output audit (MEMCTX/MEMRULES injection stays enabled) |
| `audit-status` | Show current audit env values |
| `memstyle-on` | Enable MEMSTYLE v1 injection |
| `memstyle-off` | Disable MEMSTYLE v1 injection |
| `memstyle-status` | Show MEMSTYLE v1 enable state |

### CLI UX
- `setup`: first-run wizard (ASCII banner + guided prompts)
- `configure`: interactive operation menu for day-to-day switching
- `quickstart`: non-interactive path for scripts/CI

## How It Works
### Runtime (per turn)
1. Build query embedding from current user turn.
2. Retrieve from Surface first.
3. Retrieve from Deep only when needed.
4. Re-rank candidates and compile MEMCTX facts under strict token budget.
5. Inject MEMCTX into OpenClaw prompt context.
6. Update access stats and refresh Surface after response.

### Sleep Consolidation (idle)
The sidecar monitors activity and runs consolidation when idle:
- strength decay
- low-value pruning
- dedup/merge
- conflict refresh
- preference/profile refresh
- reindex (when needed)

No API LLM call is required for this idle consolidation loop.

## MEMCTX, MEMRULES, MEMSTYLE
- **MEMCTX**: compact memory recall channel (`k=v` facts), fixed budget.
- **MEMRULES**: strict policy channel for rules that should be applied every turn.
- **MEMSTYLE**: compact style/persona channel for stable voice and response feel.

All three channels are budgeted separately to prevent prompt growth outliers.

### Why MEMRULES matters
MEMORY-style retrieval alone is relevance-based, so critical rules can be omitted on low-relevance turns.  
MEMRULES exists to keep hard constraints consistently present with a dedicated budget.

What this gives you:
- stronger enforcement of owner/user-defined policies (language policy, safety constraints, output boundaries)
- less prompt-injection persistence (only whitelisted structured rules are carried)
- lower risk of "forgotten rules" in long sessions
- predictable token ceiling for policy instructions

### MEMRULES Enforcement
- Budget isolation:
  - MEMCTX uses `memq.budgetTokens`
  - MEMRULES uses `memq.rules.budgetTokens`
- Deterministic rule sources:
  - static hard rules (`memq.rules.hard`)
  - critical profile rules (language/tone/policy)
- Language policy defaults:
  - `en` is always allowed as baseline setting language
  - additional habitual languages are inferred from recent user input patterns
  - explicit user request (e.g. "reply in Chinese") updates allowed/preferred languages for that turn, and output audit still runs
- Structured compact format is used to prevent long prompt inflation.
- Quarantined/suspicious facts are never promoted into rules.
- Enforcement is allowlist-based: any language outside configured/explicitly allowed set is a repair/block target.

### Why MEMSTYLE matters
Many models drift in tone/persona across long runs, and some multilingual models can produce awkward translated style artifacts.  
MEMSTYLE provides a tiny, explicit style anchor to preserve consistency without long prompt prose.

What this gives you:
- stable tone/persona across sessions
- less "style reset" after many turns
- lower need for repeated user reminders ("speak politely", "be concise")
- compact style control with strict upper token limit

MEMSTYLE is optional and can be toggled per environment.
MEMQ does not include any fixed character preset; persona/style is always user-defined.

### MEMRULES vs MEMSTYLE (conflict handling)
- Channel order is fixed: `MEMRULES -> MEMSTYLE -> MEMCTX`.
- Hard constraints belong to MEMRULES; style/persona/tone belong to MEMSTYLE only.
- The plugin normalizes overlapping style keys (`tone`, `verbosity`) so MEMRULES and MEMSTYLE do not drift to different values in the same turn.
- If you need strict behavior, set `memq.rules.strict=true` and keep MEMSTYLE focused on expression, not safety/policy.
- MEMQ precedence mode can keep runtime behavior aligned to MEMQ channels even when workspace files (`AGENTS.md`, `SOUL.md`, `IDENTITY.md`, `MEMORY.md`) contain conflicting hints.

### Output Audit Flow
1. Sidecar runs primary policy audit and computes `riskScore`.
2. If `riskScore` is below threshold, primary decision is used.
3. If `riskScore` is high and secondary audit is enabled, sidecar calls the configured LLM auditor.
4. Language-policy violations can force secondary audit even when not high-risk.
5. For language-only violations, sidecar can request repaired text in allowed/preferred language.
6. Final decision applies block/redact policy before returning output.
7. If LLM repair is unavailable, deterministic repair strips disallowed-language segments.

Typical high-risk signals:
- secret/token patterns (`sk-`, JWT-like blobs, private-key markers)
- override/exfiltration patterns (`ignore previous`, `reveal system prompt`, etc.)
- language policy violations when `memq.rules.allowedLanguages` is configured

Runtime checks:
```bash
scripts/memq-openclaw.sh audit-status
curl -sS http://127.0.0.1:7781/audit/stats
```

Sidecar env flags:
- `MEMQ_AUDIT_LANG_ALWAYS_SECONDARY=1`
- `MEMQ_AUDIT_LANG_REPAIR_ENABLED=1`

## Configuration
Main knobs (OpenClaw plugin config):
- `memq.sidecarUrl` (default `http://127.0.0.1:7781`)
- `memq.budgetTokens` (default `120`)
- `memq.topK` (default `5`)
- `memq.surface.max` (default `120`)
- `memq.rules.budgetTokens` (default `80`)
- `memq.rules.strict` (default `false`)
- `memq.rules.allowedLanguages` (default empty)
- `memq.rules.autoLanguageFromPrompt` (default `true`)
- `memq.rules.hard` (default empty, `|`-separated)
- `memq.style.enabled` (default `false`)
- `memq.style.budgetTokens` (default `24`)
- `memq.style.strict` (default `false`)
- `memq.style.tone` / `memq.style.persona` / `memq.style.speakingStyle` / `memq.style.verbosity`
- `memq.style.avoid` (`|`-separated)
- `memq.precedence.enabled` (default `true`)

Reference: `memq.yaml`

## OpenClaw Integration
Example config: `examples/openclaw.json`

Key points:
- plugin is loaded via `plugins.load.paths`
- memory slot is switched via `plugins.slots.memory = "openclaw-memory-memq"`

Rollback is one command:
```bash
scripts/memq-openclaw.sh disable
```

Interactive operations:
```bash
scripts/memq-openclaw.sh setup
scripts/memq-openclaw.sh configure
```

## Security Model
- Secrets are never stored in MEMCTX.
- Suspicious memory facts are quarantined and excluded from recall output.
- High-risk output can trigger secondary LLM audit (optional).
- MEMCTX and MEMRULES are budget-separated to prevent token blow-up.
- High-risk output can be blocked/redacted by sidecar policy.

## Documentation
- Setup: `docs/openclaw-setup.md`
- Architecture: `docs/architecture.md`
- Security: `docs/security.md`

## License
MIT (`LICENSE`)

---

## 日本語ガイド (Japanese)

### 概要
`hybrid-memq` は OpenClaw 向けのメモリプラグインです。  
**Surface / Deep / Ephemeral（表層 / 深層 / 揮発）** モデルと、固定予算の **MEMCTX** 注入により、長期運用での記憶品質を上げつつ入力トークンを抑えることを目的にしています。

### 主な機能
- 表層・深層・揮発の3層メモリ
- 固定トークン予算での MEMCTX (`k=v` 形式)
- OpenClaw フック連携（`before_prompt_build` / `agent_end` / `before_compaction` / `gateway_start`）
- ローカル sidecar（SQLite + 検索 + 睡眠整理 + 監査）
- 嗜好/方針プロファイルのローカル学習（非LLM）
- 汚染疑い情報の隔離（quarantine）
- 高リスク時のみ二次監査（ルール監査 + 任意LLM監査）
- OpenClaw 標準メモリとのシームレス切替

### クイックスタート
1) プラグインをビルド
```bash
cd ~/hybrid-memq/plugin/openclaw-memory-memq
pnpm install
pnpm build
```

2) OpenClaw にプラグインをインストール
```bash
openclaw plugins install -l ~/hybrid-memq/plugin/openclaw-memory-memq
```

3) sidecar を起動
```bash
cd ~/hybrid-memq/sidecar
python3 minisidecar.py
```

4) MEMQ を有効化
```bash
scripts/memq-openclaw.sh setup
```

5) 動作確認
```bash
scripts/memq-openclaw.sh status
curl -sS http://127.0.0.1:7781/health
```

### CLI コマンド
`scripts/memq-openclaw.sh`

| コマンド | 説明 |
|---|---|
| `install` | OpenClaw にプラグインをリンク/インストール |
| `setup` | 初回導入向けの対話ウィザード（推奨） |
| `configure` | 運用中の対話メニュー（CLIで設定変更） |
| `enable` | メモリスロットを `openclaw-memory-memq` に切替（既存設定を退避） |
| `disable` | 退避した設定を復元して元方式へ戻す |
| `on` | `quickstart` のショートカット |
| `off` | MEMQ を無効化し sidecar も停止 |
| `start-sidecar` | sidecar supervisor を起動（異常終了時に自動再起動） |
| `stop-sidecar` | sidecar を停止 |
| `restart-sidecar` | sidecar を再起動（`127.0.0.1:7781` の残留リスナーも整理） |
| `status` | 現在の設定・接続状態を表示 |
| `quickstart` | `install + start-sidecar + enable + status` を実行（非対話） |
| `audit-on <url> <model> [risk_threshold] [block_threshold]` | 高リスク時の二次LLM監査を有効化 |
| `audit-off` | 二次LLM監査のみ無効化（MEMQ本体は有効） |
| `audit-primary-on` | 一次出力監査（ルールベース）を有効化 |
| `audit-primary-off` | 一次出力監査のみ無効化（MEMCTX/MEMRULES注入は有効のまま） |
| `audit-status` | 監査設定の現在値を表示 |
| `memstyle-on` | MEMSTYLE v1 注入を有効化 |
| `memstyle-off` | MEMSTYLE v1 注入を無効化 |
| `memstyle-status` | MEMSTYLE v1 の有効状態を表示 |

### CLI UX
- `setup`: 初回導入向けウィザード（ASCIIバナー + 質問形式）
- `configure`: 運用中の設定切替メニュー
- `quickstart`: 自動化向けの非対話セットアップ

### 仕組み（実行時）
1. 現在ターンのクエリ埋め込みを生成  
2. 表層（Surface）を優先検索  
3. 必要時のみ深層（Deep）検索  
4. 候補を再ランクし、固定予算で MEMCTX を編成  
5. OpenClaw のプロンプト文脈へ注入  
6. 応答後にアクセス情報を更新して表層を再活性化

### 睡眠整理（Idle/Sleep Consolidation）
ユーザー操作が一定時間ないと sidecar が自動整理を実行します。
- 強度減衰（decay）
- 低価値記憶の剪定（prune）
- 重複統合（dedup/merge）
- 競合更新（conflict refresh）
- 嗜好/方針プロファイル更新
- 必要時の再インデックス

この整理は API LLM を呼ばず、ローカル処理のみで行います。

### MEMCTX / MEMRULES / MEMSTYLE
- **MEMCTX**: 想起情報（記憶）チャネル。`k=v` 形式で固定予算注入。
- **MEMRULES**: 毎ターン確実に効かせたい厳格ルールの専用チャネル。
- **MEMSTYLE**: 口調・人格・話し方の一貫性を保つための専用チャネル。

3チャネルを別予算で管理し、入力肥大化を防ぎます。

### なぜ MEMRULES が必要か
記憶検索だけだと「関連度が低いターン」で重要ルールが落ちることがあります。  
MEMRULES はそれを防ぐため、厳格ルールを常時・軽量に注入する仕組みです。

得られる効果:
- ユーザー定義ルール（言語方針・安全制約・出力境界）の安定適用
- 永続的プロンプト汚染の抑制（ホワイトリスト化された構造化ルールのみ昇格）
- 長大会話でも「ルール忘れ」を減らす
- ルール用トークン上限を予測可能に維持

### MEMRULES の厳格化
- 予算を分離して運用:
  - MEMCTX は `memq.budgetTokens`
  - MEMRULES は `memq.rules.budgetTokens`
- ルール生成元を限定:
  - 静的ハードルール（`memq.rules.hard`）
  - critical なプロファイルルール（言語/口調/方針）
- 言語ポリシーの既定:
  - `en` は設定言語として常時許可
  - 追加許可言語は、最近のユーザー入力傾向から自動推定
  - 「中国語で返して」のような明示要求ターンは、そのターンの許可/優先言語を更新し、監査自体は継続
- ルールは短い構造化形式で注入し、長文化を防止
- quarantine 対象や汚染疑い facts はルールへ昇格しない
- 言語制御は許可リスト方式で、設定済み/明示許可された言語以外は修復またはブロック対象になります

### なぜ MEMSTYLE が必要か
長い会話では口調・人格が揺れやすく、特に多言語系モデルでは翻訳調の違和感が出ることがあります。  
MEMSTYLE は短いスタイル固定情報を毎ターン注入し、文体ドリフトを抑えます。

得られる効果:
- 口調・人格・話し方の一貫性向上
- ターンが進んでもスタイル崩壊しにくい
- 「敬語で」「簡潔に」などの再指示回数を削減
- 小さい予算で安定化（`memq.style.budgetTokens` で上限固定）

MEMSTYLE は任意機能で、環境ごとにON/OFFできます。
MEMQ は特定キャラの固定プリセットを同梱せず、スタイルはユーザー任意で定義します。

### MEMRULES と MEMSTYLE の競合回避
- 注入順は固定です: `MEMRULES -> MEMSTYLE -> MEMCTX`
- 厳格制約は MEMRULES、口調・人格・キャラ設定は MEMSTYLE のみに分離します
- プラグイン側で重複しやすいスタイルキー（`tone`, `verbosity`）を正規化し、同一ターン内で値がぶつからないようにしています
- 厳格運用時は `memq.rules.strict=true` を有効にし、MEMSTYLE は表現面の指定に限定してください
- `memq.precedence.enabled=true` で、`AGENTS.md`/`SOUL.md`/`IDENTITY.md`/`MEMORY.md` と競合時に MEMQ チャネルを優先する実行方針を補強できます

### 出力監査フロー
1. sidecar が一次監査で `riskScore` を算出  
2. 閾値未満なら一次監査結果を採用  
3. 高リスクかつ二次監査有効時のみ、監査用LLMを呼び出し  
4. 言語ポリシー違反は高リスクでなくても二次監査を発火可能  
5. 言語違反のみの場合は、許可/優先言語での修正文を要求可能  
6. 最終判定として block/redact ポリシーを適用
7. 二次監査LLMが使えない場合は、ルールベース修正で非許可言語セグメントを除去

高リスク判定の主なシグナル:
- シークレット/トークン（`sk-`、JWT類似、private keyマーカー）
- 上書き/漏えい誘導（`ignore previous`、`reveal system prompt` など）
- `memq.rules.allowedLanguages` 設定時の言語ポリシー違反

動作確認:
```bash
scripts/memq-openclaw.sh audit-status
curl -sS http://127.0.0.1:7781/audit/stats
```

Sidecar環境変数:
- `MEMQ_AUDIT_LANG_ALWAYS_SECONDARY=1`
- `MEMQ_AUDIT_LANG_REPAIR_ENABLED=1`

対話式セットアップ:
```bash
scripts/memq-openclaw.sh setup
scripts/memq-openclaw.sh configure
```

### 設定項目（主要）
- `memq.sidecarUrl`（既定: `http://127.0.0.1:7781`）
- `memq.budgetTokens`（既定: `120`）
- `memq.topK`（既定: `5`）
- `memq.surface.max`（既定: `120`）
- `memq.rules.budgetTokens`（既定: `80`）
- `memq.rules.strict`（既定: `false`）
- `memq.rules.allowedLanguages`（既定: 空）
- `memq.rules.autoLanguageFromPrompt`（既定: `true`）
- `memq.rules.hard`（既定: 空、`|`区切り）
- `memq.style.enabled`（既定: `false`）
- `memq.style.budgetTokens`（既定: `24`）
- `memq.style.strict`（既定: `false`）
- `memq.style.tone` / `memq.style.persona` / `memq.style.speakingStyle` / `memq.style.verbosity`
- `memq.style.avoid`（`|`区切り）
- `memq.precedence.enabled`（既定: `true`）

参照: `memq.yaml` / `examples/openclaw.json`

### セキュリティ
- MEMCTX に秘密情報を保持しない
- 汚染疑いの facts は quarantine して想起対象から除外
- 必要に応じて高リスク出力のみ二次LLM監査を適用
- MEMCTX と MEMRULES は別予算で運用し、トークン暴騰を防止
- 高リスク応答は sidecar ポリシーで block/redact 可能

### 関連ドキュメント
- `docs/openclaw-setup.md`
- `docs/architecture.md`
- `docs/security.md`
