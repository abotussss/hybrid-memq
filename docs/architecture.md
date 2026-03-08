# Architecture

## English

MEMQ is split into four layers:

- **OpenClaw plugin**: token budgeting and prompt injection
- **sidecar**: runtime controller and deterministic apply layer
- **memory-lancedb-pro**: memory authority
- **QBRAIN**: planning layer for ingest, recall, and updates

### Flow

1. OpenClaw receives a turn
2. plugin trims recent context under a total cap
3. plugin calls sidecar `POST /qctx/query`
4. sidecar asks QBRAIN for a `RecallPlan`
5. sidecar queries memory-lancedb-pro
6. sidecar packs `QRULE`, `QSTYLE`, `QCTX`
7. plugin injects them into OpenClaw
8. after response, plugin sends the turn to `POST /memory/ingest_turn`

### Design invariant

- memory is stored in memory-lancedb-pro
- QBRAIN decides what matters
- QCTX carries only the prompt-time bridge
- OpenClaw still answers using recent context plus compact hints

## 日本語

MEMQ は 4 層に分かれます。

- **OpenClaw plugin**: token 予算管理と注入
- **sidecar**: 実行制御と deterministic apply
- **memory-lancedb-pro**: 記憶 authority
- **QBRAIN**: 保存・想起・更新計画の作成

### 流れ

1. OpenClaw がターンを受ける
2. plugin が recent context を総上限内に切る
3. plugin が sidecar の `POST /qctx/query` を呼ぶ
4. sidecar が QBRAIN に `RecallPlan` を作らせる
5. sidecar が memory-lancedb-pro を検索する
6. sidecar が `QRULE / QSTYLE / QCTX` を pack する
7. plugin がそれを OpenClaw に注入する
8. 応答後、plugin が `POST /memory/ingest_turn` へ返す

### 設計上の不変条件

- 記憶は memory-lancedb-pro に置く
- 何が重要かは QBRAIN が決める
- QCTX は prompt-time bridge だけを担う
- OpenClaw は recent context と compact hint を使って最終回答する
