# QBRAIN Required Proof

This document describes how to verify fail-closed Brain orchestration for Hybrid MEMQ.

## Preconditions
- Ollama is running on `http://127.0.0.1:11434`
- `gpt-oss:20b` is pulled and available in Ollama
- Sidecar is running with `MEMQ_BRAIN_MODE=required`

## Run
```bash
python3 bench/src/brain_required_proof.py
```

## What the proof checks
1. Global Brain trace for `gpt-oss:20b` has:
   - `ingest_plan >= 10`
   - `recall_plan >= 10`
   - `merge_plan >= 1`
2. The script computes missing counts and only executes the required top-up calls.
4. Trace lines include `model=gpt-oss:20b`.
5. Trace lines include `/api/ps` proof (`ps_snapshot.seen=true`).
6. `GET /api/ps` includes `gpt-oss:20b`.
7. Fail-closed check: a temporary required sidecar with broken Ollama URL returns HTTP 503 for `/qctx/query`.

## Useful env overrides
- `MEMQ_PROOF_TARGET_INGEST` (default: `10`)
- `MEMQ_PROOF_TARGET_RECALL` (default: `10`)
- `MEMQ_PROOF_TARGET_MERGE` (default: `1`)
- `MEMQ_PROOF_TIMEOUT_SEC` (default: `150`)

## Output
- JSON result file:
  - `bench/results/brain_required_proof.json`
- Key endpoints used:
  - `/brain/stats`
  - `/brain/trace/recent?n=240`
  - `http://127.0.0.1:11434/api/ps`

## Manual quick proof
```bash
scripts/memq-openclaw.sh brain-proof
```

Additional generic proof scripts:

- `python3 bench/src/generic_memory_recall.py`
- `python3 bench/src/timeline_recall_proof.py`
- `python3 bench/src/sleep_consolidation_proof.py`
- `python3 bench/src/token_budget_proof.py`
- `python3 bench/src/audit_proof.py`
