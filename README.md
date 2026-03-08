# Hybrid MEMQ v3

Hybrid MEMQ v3 is a full rebuild of the OpenClaw memory plugin around one rule:

- `QBRAIN` decides what to remember, what to recall, and what to merge.
- deterministic code applies those plans.
- OpenClaw remains the final answer model.

## Core Model

Each turn uses only three channels, in fixed order:

1. `QRULE`
2. `QSTYLE`
3. `QCTX`

Rules:
- `QRULE`: strict safety, language, procedure, hard constraints
- `QSTYLE`: persona, tone, speaking style, user naming, first person
- `QCTX`: contextual memory only

Cross-channel contamination is rejected.

## Architecture

- `plugin/openclaw-memory-memq`
  - thin OpenClaw plugin
  - computes total input budget
  - trims recent history dynamically
  - calls sidecar
  - injects `QRULE -> QSTYLE -> QCTX`
- `sidecar`
  - single source of truth
  - SQLite persistence
  - FTS/BM25 retrieval
  - Brain plan generation via Ollama
  - deterministic apply layer
- `bench`
  - proof scripts for required runtime, recall, timeline, budget, sleep consolidation, audit

## Runtime Profiles

Two explicit profiles exist:

- `brain-required`
  - Brain must succeed for ingest / recall / idle merge
  - no fallback
  - no degraded continuation
  - fail-closed before API LLM call
- `brain-optional`
  - Brain may be absent
  - deterministic low-quality fallback is allowed
  - for OSS distribution/debugging only

## QBRAIN Responsibilities

QBRAIN produces plans only:

- `IngestPlan`
- `RecallPlan`
- `MergePlan`
- `AuditPatchPlan`

It does not write the DB directly.
It does not answer the user directly.
It does not bypass validation.

## Deterministic Apply Layer

The sidecar applies Brain plans deterministically:

- ingest
  - facts -> `memory_items`
  - events -> `events`
  - style updates -> `style_profile` (explicit only)
  - rule updates -> `rules` (explicit only)
  - suspicious content -> `quarantine`
- recall
  - FTS/BM25 + `fact_index` + timeline range
  - rerank with confidence / recency / importance / strength / redundancy penalty
  - pack bounded `QCTX`
- idle merge
  - merge duplicates
  - prune obsolete items
  - refresh `daily_digests`
  - refresh `fact_index`
  - refresh profile snapshot

## Storage Model

Core SQLite tables:

- `memory_items`
- `events`
- `daily_digests`
- `rules`
- `style_profile`
- `fact_index`
- `quarantine`

## Retrieval Model

No embeddings.

Retrieval uses:

- SQLite FTS5 / BM25
- plain text + Japanese n-gram fields
- `fact_index`
- timeline range filters

This keeps recall scalable without embedding dependencies.

## Local Overrides

Users can override `QSTYLE` and `QRULE` locally without editing SQLite:

- `QSTYLE.local.json`
- `QRULE.local.json`

These files are merged on top of QBRAIN-managed values at injection time.
Allowed `QSTYLE` keys:

- `tone`
- `persona`
- `verbosity`
- `speaking_style`
- `callUser`
- `firstPerson`
- `prefix`

Allowed `QRULE` prefixes:

- `security.`
- `language.`
- `procedure.`
- `compliance.`
- `output.`
- `operation.`

## Budget Model

`QSTYLE` budget is fixed to `120`.

Every turn uses a total cap:

- `tokens.system`
- `tokens.rules`
- `tokens.style`
- `tokens.ctx`
- `tokens.recent`
- `tokens.total`

Recent history is not fixed at 5000 forever.
It gets the remaining budget after the fixed channels.

## QCTX Rules

`QCTX` may be null.

When present, packing starts from:

- `wm.surf`
- `p.snapshot`
- `t.recent`

Then query-specific additions are packed according to the Brain recall plan:

- timeline intent -> `t.range`, `t.digest`, `t.ev*`
- profile intent -> profile facts
- state/overview intent -> working summaries
- ephemeral hints only if budget remains

## Security

Primary audit stays deterministic:

- secret detection
- token-like strings
- private key markers
- prompt override / exfil phrases

Secondary audit may use Brain patch planning.
Redaction is applied even when `block=false`.

## Runtime Proof

The sidecar exposes:

- `GET /brain/stats`
- `GET /brain/trace/recent`

Each turn records:

- `trace_id`
- `op`
- `model`
- `latency_ms`
- `ps_seen`
- `prompt_sha256`
- `apply_summary`

The plugin logs:

- `[memq][qbrain-proof] trace_id=... op=... model=... ps_seen=...`

## Quick Start

```bash
cd /Users/hiroyukimiyake/Documents/New\ project
scripts/memq-openclaw.sh setup
scripts/memq-openclaw.sh status
scripts/memq-openclaw.sh brain-proof
```

## Main Commands

- `scripts/memq-openclaw.sh install`
- `scripts/memq-openclaw.sh setup`
- `scripts/memq-openclaw.sh enable`
- `scripts/memq-openclaw.sh disable`
- `scripts/memq-openclaw.sh start-sidecar`
- `scripts/memq-openclaw.sh stop-sidecar`
- `scripts/memq-openclaw.sh restart-sidecar`
- `scripts/memq-openclaw.sh brain-required-on`
- `scripts/memq-openclaw.sh brain-optional-on`
- `scripts/memq-openclaw.sh status`
- `scripts/memq-openclaw.sh brain-proof`

## Verification Targets

Proof benches:

- `bench/src/brain_required_proof.py`
- `bench/src/generic_memory_recall.py`
- `bench/src/token_budget_proof.py`
- `bench/src/timeline_recall_proof.py`
- `bench/src/sleep_consolidation_proof.py`
- `bench/src/audit_proof.py`

Acceptance:

1. required mode records Brain trace + `ps_seen=true` for ingest / recall / merge
2. Brain failure fails closed before API LLM call
3. `昨日何した？` and `最近どうだった？` resolve from timeline memory
4. `君は誰？` and `家族構成は？` resolve from stored profile memory
5. total input tokens stay bounded
6. QSTYLE budget never exceeds `120`
7. no secret leakage
