# MEMQ Reconstruction Validation (2026-02-25)

## Scope
- Mode A (API text injection) only.
- Verify MEMQ-centered context reconstruction:
  - old conversation turns summarized into MEMQ
  - active OpenClaw history pruned to minimal recent messages
  - raw pruned history archived locally as insurance
  - MEMCTX budget still fixed and injected each turn

## Local Runtime Evidence
- Sidecar endpoint test (`/conversation/summarize`) succeeded:
  - `ok=true`
  - created `convsurf:*` (`retentionScope=surface_only`)
  - created `convdeep:*` (`retentionScope=deep`)
- OpenClaw turn logs showed pruning in `before_prompt_build`:
  - turn4: `pruned=2 archive=1`
  - turn5: `pruned=4 archive=1`
  - turn6: `pruned=6 archive=1`
  - turn7: `pruned=8 archive=1`
  - turn8: `pruned=10 archive=1`
  - turn13: `pruned=20 archive=1`
- OpenClaw context diagnostics confirmed bounded active history:
  - `[context-diag] ... messages=6 ... sessionKey=memq-recon-test`
- Archive file exists and contains normalized user/assistant text:
  - `~/.openclaw/workspace/.memq/conversation_archive/memq-recon-test.jsonl`
- Sidecar DB contains conversation summary traces:
  - `convsurf:*` rows with `retention_scope='surface_only'`
  - `convdeep:*` rows with `retention_scope='deep'`
- Hook duplication removed:
  - one `before_prompt_build` log per turn after `memq.compat.enableLegacyBeforeAgentStart=false`

## Regression Guard Added
- Disabled duplicate hook execution by default:
  - `memq.compat.enableLegacyBeforeAgentStart=false`
  - verified turn8 has a single `before_prompt_build` log line
- Sidecar outage fallback:
  - if sidecar embed/search fails, hook no longer crashes
  - plugin emits degraded-mode MEMCTX (`sidecar_retrieval_degraded_surface_only`) and continues the turn
  - validated with OpenClaw turn log line:
    - `sidecar embed failed ... fallback to surface-only memctx`
    - followed by successful `agent_end` completion

## Result
- PASS: MEMQ now reconstructs context from Surface/Deep summaries while pruning long active history.
- PASS: Raw history is retained out-of-band (JSONL archive) for recovery/audit.
- PASS: Fixed-budget MEMCTX injection remains active during reconstructed turns.
- PASS: before_prompt hook is resilient to temporary sidecar unavailability.
