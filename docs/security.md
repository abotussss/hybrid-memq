# Security Notes

## Memory safety

- Secrets are never promoted into memory channels.
- Suspected prompt-injection or exfiltration-like content is quarantined.
- Quarantined items are excluded from MEMCTX recall.

## Output audit

Two layers are supported:

1. Primary deterministic audit (always local)
2. Secondary LLM audit (optional, high-risk gated)

Secondary audit is only called when risk score exceeds configured threshold.

## Rules/style separation

- `MEMRULES`: strict operational/safety constraints only
- `MEMSTYLE`: persona/tone/verbosity only
- `MEMCTX`: memory context only

This separation prevents cross-channel policy pollution.

## Restart behavior

- Gateway restart does not clear sidecar DB.
- Sidecar restart reloads persisted state.
- Current local proof is stored in `docs/runtime_persistence_latest_20260226.json`.
