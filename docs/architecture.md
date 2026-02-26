# Hybrid MEMQ v2 Architecture (Mode A)

## Components

- OpenClaw plugin (`plugin/openclaw-memory-memq`)
- Sidecar service (`sidecar/minisidecar.py` + `sidecar/memq/*`)
- Shared config (`memq.yaml`)

## OpenClaw hooks

- `before_prompt_build`
  - split conversation by recent token budget
  - archive pruned messages
  - summarize pruned messages to sidecar
  - fetch and inject `MEMRULES`, `MEMSTYLE`, `MEMCTX`
- `agent_end`
  - ingest turn into sidecar
- `before_compaction`
  - trigger idle consolidation (best effort)
- `gateway_start`
  - sidecar health check and markdown bootstrap import
- `message_sending`
  - output audit (primary + optional secondary)

## Memory model

- `surface`: fast, recent context
- `deep`: long-term memory with quantized embeddings
- `ephemeral`: low-value short-lived memory with decay/prune
- `quarantine`: suspected unsafe memory excluded from recall

## Injection model

Per-turn fixed budgets:

- `MEMRULES` (strict policy)
- `MEMSTYLE` (tone/persona)
- `MEMCTX` (memory context)

Injection order is fixed:

1. MEMRULES
2. MEMSTYLE
3. MEMCTX

## Sidecar persistence

- SQLite stores memory, rules, style profile, preference/policy profiles, quarantine, and summaries.
- Sidecar restarts reload the same DB file.
- Gateway restarts do not reset sidecar data.

## Degraded mode

If sidecar is unavailable, plugin injects minimal safe blocks and continues run execution.
