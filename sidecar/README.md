# Sidecar

## English

The sidecar is the runtime controller for MEMQ.

Responsibilities:

- keep the effective state for `QRULE`, `QSTYLE`, and `QCTX`
- ask QBRAIN for ingest and recall plans
- validate and apply those plans deterministically
- query memory-lancedb-pro for memory recall
- expose proof/debug endpoints

Upstream reference for the memory backend:

- [win4r/memory-lancedb-pro](https://github.com/win4r/memory-lancedb-pro)

Credit:

- This sidecar vendors and uses the upstream `memory-lancedb-pro` source directly as the memory backend.
- Thanks to the upstream author and contributors for the original work.

This repo vendors that upstream backend directly. Users normally do not install the upstream repository separately.

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

memory backend の上流リポジトリ:

- [win4r/memory-lancedb-pro](https://github.com/win4r/memory-lancedb-pro)

謝辞:

- この sidecar は、上流 `memory-lancedb-pro` source を vendor して、そのまま memory backend として使います。
- 元の実装を公開している作者と貢献者に感謝します。

この repo では、その backend の上流 source を vendor して含めています。
通常利用では、上流リポジトリを別途 install しません。

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
