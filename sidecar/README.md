# Sidecar

## English

The sidecar is the runtime controller for MEMQ.

Responsibilities:

- keep the effective state for `QRULE`, `QSTYLE`, and `QCTX`
- ask QBRAIN for ingest and recall plans
- validate and apply those plans deterministically
- query memory-lancedb-pro for memory recall
- expose proof/debug endpoints

Main endpoints:

- `GET /health`
- `POST /qctx/query`
- `POST /memory/preview_prompt`
- `POST /memory/ingest_turn`
- `GET /qstyle/current`
- `GET /qrule/current`
- `GET /profile`
- `GET /brain/stats`
- `GET /brain/trace/recent`

## 日本語

sidecar は MEMQ の runtime controller です。

役割:

- `QRULE / QSTYLE / QCTX` の実効状態を管理する
- QBRAIN に ingest / recall plan を作らせる
- plan を検証して deterministic に適用する
- memory-lancedb-pro を検索して記憶を引く
- proof/debug endpoint を提供する

主な endpoint:

- `GET /health`
- `POST /qctx/query`
- `POST /memory/preview_prompt`
- `POST /memory/ingest_turn`
- `GET /qstyle/current`
- `GET /qrule/current`
- `GET /profile`
- `GET /brain/stats`
- `GET /brain/trace/recent`
