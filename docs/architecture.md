# Hybrid MEMQ Architecture

Hybrid MEMQ is built around one invariant:

- the plugin owns prompt-time token budgeting
- the sidecar owns memory truth and memory selection
- QBRAIN proposes plans, but deterministic code decides what is persisted and what is injected

## Runtime Shape

The system has two processes:

- `plugin/openclaw-memory-memq`
  - thin OpenClaw adapter
  - trims recent messages against a total input cap
  - calls the sidecar for preview ingest, recall planning, audit, and idle work
  - injects memory blocks in strict order: `QRULE -> QSTYLE -> QCTX`
- `sidecar`
  - SQLite-backed memory engine
  - Brain orchestration for ingest / recall / merge
  - deterministic apply layer
  - prompt blueprint assembly

## Prompt Blueprint

Prompt-time recall is assembled as a versioned blueprint, not as ad-hoc strings scattered across the route:

1. load current `rules` and `style_profile`
2. ask QBRAIN for a `BrainRecallPlan`
3. retrieve candidate memory with lexical search plus fact-index lookups
4. rerank candidates with intent-aware scoring
5. pack bounded `QRULE`, `QSTYLE`, and `QCTX`
6. return debug metadata for proof and regression testing

The blueprint currently exposes:

- `qrule`
- `qstyle`
- `qctx`
- `meta.surfaceHit`
- `meta.deepCalled`
- `meta.usedMemoryIds`
- `meta.debug.trace_id`
- `meta.debug.intent`
- `meta.debug.time_range`
- `meta.debug.qctx_keys`
- `meta.debug.retrieval`

## Retrieval Model

Embeddings are not required.

Retrieval is hybrid and deterministic:

- SQLite FTS5 / BM25 for lexical recall
- `fact_index` for exact fact-key recall
- timeline window queries for event recall
- intent-aware reranking for `profile`, `timeline`, `state`, and `fact`
- request-level `topK` override applied consistently across deep, surface, and timeline search

Reranking prefers:

- `profile.*` facts when the query is profile-heavy
- `timeline.*` and recent events when the query is timeline-heavy
- `surface` items for state / overview prompts
- session-local memory over global carry, except where global profile facts are the right fallback

## Channel Contract

Each turn has three bounded channels:

- `QRULE`
  - policy, safety, procedure, language constraints
- `QSTYLE`
  - persona, first person, how to address the user, tone
- `QCTX`
  - contextual memory only

Cross-channel leakage is treated as a bug. `QCTX` must never carry policy or style budget noise.

## Persistence Model

SQLite remains the single source of truth.

Core tables:

- `memory_items`
- `events`
- `daily_digests`
- `rules`
- `style_profile`
- `fact_index`
- `quarantine`

The deterministic apply layer is responsible for:

- writing facts and events
- updating explicit style/rule changes
- quarantining suspicious content
- refreshing digests, fact index, and profile snapshots
- merging duplicate deep memories during idle consolidation

## Failure Modes

Two runtime profiles exist:

- `brain-required`
  - fail closed for ingest / recall / merge
  - no degraded continuation before the final answer model runs
- `brain-optional`
  - recall and ingest may fall back to deterministic low-quality behavior
  - intended for OSS debugging and constrained local environments

## Why This Shape

If rebuilt from scratch, this is the boundary worth preserving:

- the plugin should stay thin and budget-oriented
- the sidecar should expose a stable prompt blueprint contract
- retrieval quality should improve by better ranking, not by stuffing more raw memory into prompt context
