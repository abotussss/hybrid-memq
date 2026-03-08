# MEMQ

English follows first. Japanese follows after that.

## Overview

MEMQ is an OpenClaw memory plugin system with four clear roles:

- **memory-lancedb-pro** stores and retrieves memory
- **QBRAIN** decides what to save, what to recall, and what to update
- **QCTX** bridges selected memory into the final prompt
- **OpenClaw** remains the final answer model

MEMQ is not a raw memory dump. Its job is to reduce prompt tokens while preserving identity, rules, and relevant context.

## Naming

Public names are unified as follows:

- `QRULE`: hard rules and operating constraints
- `QSTYLE`: persona, tone, address style, first person
- `QCTX`: contextual hints selected from memory
- `QBRAIN`: orchestration model that produces plans


## Architecture

### 1. OpenClaw plugin

The plugin is intentionally thin.

It only does four things:

1. trim recent history under a total token cap
2. call the sidecar for `QCTX` recall
3. inject `QRULE -> QSTYLE -> QCTX`
4. send the final turn back for memory ingest

The plugin does **not** infer intent, timeline, or style updates on its own.

### 2. Sidecar

The sidecar is the runtime control plane.

It is responsible for:

- reading and writing memory state
- calling QBRAIN
- validating QBRAIN plans
- applying those plans deterministically
- returning bounded `qrule`, `qstyle`, `qctx`

### 3. memory-lancedb-pro

`memory-lancedb-pro` is the memory authority for fresh sessions.

It stores:

- long-term facts
- rule entries
- style entries
- event entries
- digest entries

MEMQ then uses `QCTX` to retrieve only the subset of memory that is relevant to the current turn.

### 4. QBRAIN

QBRAIN does not write the database directly.

QBRAIN only creates plans:

- `IngestPlan`
- `RecallPlan`
- `MergePlan`
- `AuditPatchPlan`

Deterministic code applies those plans. This keeps orchestration flexible while keeping persistence safe.

## Memory Flow

### Ingest

1. user turn arrives
2. QBRAIN reads the turn and current state
3. QBRAIN emits facts, events, and explicit style/rule updates
4. sidecar validates them
5. memory-lancedb-pro stores them

### Recall

1. current prompt arrives
2. QBRAIN emits a `RecallPlan`
3. sidecar queries memory-lancedb-pro using that plan
4. results are reranked and compacted
5. `QCTX` is packed under a strict token budget
6. plugin injects `QRULE -> QSTYLE -> QCTX`

### Why QCTX exists

QCTX is not the memory store.

QCTX is the **bridge** between long-term memory and the final OpenClaw prompt.
It exists to reduce token usage while preserving the minimum useful context.

## Storage and Retrieval

### Primary memory authority

Current target design:

- memory authority: `memory-lancedb-pro`
- bridge layer: `QCTX`
- orchestration: `QBRAIN`
- final answer: `OpenClaw`

### Retrieval method

The current retrieval path uses:

- LanceDB-backed memory access
- lexical search and filtering
- fact-key preference
- timeline range filtering
- duplicate suppression
- compact packing into `QCTX`

This is intentionally optimized for practical prompt reduction, not for dumping full memory back into the model.

## Q Channels

### QRULE

Contains only:

- safety
- language
- procedure
- operation constraints
- output constraints

### QSTYLE

Contains only:

- persona
- tone
- speaking style
- first person
- how to address the user

### QCTX

Contains only:

- working memory anchor
- profile snapshot
- timeline digest
- selected deep or surface memory hints

Cross-channel contamination is treated as a bug.

## Local user overrides

These files are **local-only** and **user-specific**:

- `QSTYLE.local.json`
- `QRULE.local.json`
- `QSTYLE.current.json`
- `QRULE.current.json`
- `QCTX.current.txt`

`QSTYLE.local.json` and `QRULE.local.json` are optional user overrides.

`QSTYLE.current.json`, `QRULE.current.json`, and `QCTX.current.txt` are visible runtime snapshots.
They are mirrors of the effective state for inspection, not the source of truth used for orchestration.
QBRAIN still reads and writes the effective state through LanceDB-backed memory plus local overrides.

All of these files are gitignored.

Tracked examples for OSS:

- `QSTYLE.local.example.json`
- `QRULE.local.example.json`

Use the local files only when a specific machine or user needs an override.

## Runtime profiles

### `brain-required`

- QBRAIN must succeed
- no degraded continuation
- no fallback before the final model call

### `brain-optional`

- intended for general OSS/debug environments
- lower quality fallback may be allowed

## Main endpoints

### Public runtime endpoints

- `GET /health`
- `POST /qctx/query`
- `POST /memory/preview_prompt`
- `POST /memory/ingest_turn`
- `GET /qstyle/current`
- `GET /qrule/current`
- `GET /profile`
- `GET /brain/stats`
- `GET /brain/trace/recent`

## Verification

Main verification scripts:

- `bench/src/brain_required_proof.py`
- `bench/src/generic_memory_recall.py`
- `bench/src/token_budget_proof.py`
- `bench/src/timeline_recall_proof.py`
- `bench/src/sleep_consolidation_proof.py`
- `bench/src/audit_proof.py`

## Quick start

```bash
cd /Users/hiroyukimiyake/Documents/New\ project
scripts/memq-openclaw.sh setup
scripts/memq-openclaw.sh status
curl -sS http://127.0.0.1:7781/health
```

---

# MEMQ（日本語）

## 概要

MEMQ は OpenClaw 用のメモリプラグイン構成です。役割は明確に分かれています。

- **memory-lancedb-pro** が記憶を保持・検索する
- **QBRAIN** が何を保存し、何を引き出し、何を更新するかを決める
- **QCTX** が必要な記憶だけを最終プロンプトへ橋渡しする
- **OpenClaw** が最終回答を行う

重要なのは、QCTX が記憶そのものではないことです。
QCTX は、長期記憶から必要なヒントだけを小さく渡すための橋渡し層です。

## 名称

公開名は次で統一しています。

- `QRULE`：ルール、制約、安全、言語、手順
- `QSTYLE`：人格、口調、呼称、一人称
- `QCTX`：会話用の文脈ヒント
- `QBRAIN`：保存・検索・更新の計画を作る脳


## 全体構造

### 1. OpenClaw plugin

plugin は薄く保っています。役割は 4 つだけです。

1. recent history を総トークン上限内に切る
2. sidecar に問い合わせて `QCTX` を組む
3. `QRULE -> QSTYLE -> QCTX` の順で注入する
4. 最終ターンを sidecar に返して記憶化する

plugin 自体は、意図判定や style 更新判断をしません。

### 2. sidecar

sidecar は実行時の制御面です。

- 記憶状態の読み書き
- QBRAIN 呼び出し
- plan の検証
- deterministic apply
- `qrule / qstyle / qctx` の返却

### 3. memory-lancedb-pro

`memory-lancedb-pro` は fresh session における記憶 authority です。

保持対象:

- 長期 fact
- rule entry
- style entry
- event
- digest

QCTX はこの記憶から、そのターンに必要なものだけを引きます。

### 4. QBRAIN

QBRAIN は直接 DB を触りません。

QBRAIN が作るのは plan だけです。

- `IngestPlan`
- `RecallPlan`
- `MergePlan`
- `AuditPatchPlan`

実際の保存・検索・注入は deterministic code が行います。

## メモリの流れ

### 保存

1. ユーザー発話が来る
2. QBRAIN が現在状態を見て plan を作る
3. fact / event / 明示 style update / 明示 rule update を返す
4. sidecar が検証する
5. memory-lancedb-pro に保存する

### 想起

1. 現在の prompt を受ける
2. QBRAIN が `RecallPlan` を作る
3. sidecar が memory-lancedb-pro を検索する
4. 候補を絞る
5. `QCTX` を予算内で pack する
6. plugin が `QRULE -> QSTYLE -> QCTX` を注入する

## Q チャネルの役割

### QRULE

含めるもの:

- safety
- language
- procedure
- operation constraint
- output constraint

### QSTYLE

含めるもの:

- persona
- tone
- speaking style
- first person
- user の呼び方

### QCTX

含めるもの:

- working memory anchor
- profile snapshot
- timeline digest
- 必要な deep / surface memory のヒント

チャネル混入は不具合として扱います。

## ローカル上書き

この 2 つは **ユーザーごとのローカルファイル**です。

- `QSTYLE.local.json`
- `QRULE.local.json`
- `QSTYLE.current.json`
- `QRULE.current.json`
- `QCTX.current.txt`

`QSTYLE.local.json` と `QRULE.local.json` は任意のローカル上書きです。

`QSTYLE.current.json` / `QRULE.current.json` / `QCTX.current.txt` は、
現在有効な状態を確認するための visible mirror です。
実際の source of truth は QBRAIN が扱う記憶ストアであり、
これらの current ファイルは inspection 用のスナップショットです。

これらは gitignore 対象です。
OSS 配布用には example を使います。

- `QSTYLE.local.example.json`
- `QRULE.local.example.json`

## 実行プロファイル

### `brain-required`

- QBRAIN 必須
- degraded continuation なし
- final model 呼び出し前に fallback しない

### `brain-optional`

- OSS 配布や debug 向け
- 品質は落ちるが fallback を許可できる

## 主な endpoint

- `GET /health`
- `POST /qctx/query`
- `POST /memory/preview_prompt`
- `POST /memory/ingest_turn`
- `GET /qstyle/current`
- `GET /qrule/current`
- `GET /profile`
- `GET /brain/stats`
- `GET /brain/trace/recent`

## 検証

主な proof script:

- `bench/src/brain_required_proof.py`
- `bench/src/generic_memory_recall.py`
- `bench/src/token_budget_proof.py`
- `bench/src/timeline_recall_proof.py`
- `bench/src/sleep_consolidation_proof.py`
- `bench/src/audit_proof.py`

## クイックスタート

```bash
cd /Users/hiroyukimiyake/Documents/New\ project
scripts/memq-openclaw.sh setup
scripts/memq-openclaw.sh status
curl -sS http://127.0.0.1:7781/health
```
