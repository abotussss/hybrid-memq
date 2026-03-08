# Security Notes

## Memory safety

- Secrets are never promoted into memory channels.
- Suspected prompt-injection or exfiltration-like content is quarantined.
- Quarantined items are excluded from QCTX recall.

## Output audit

Two layers are supported:

1. Primary deterministic audit (always local)
2. Secondary LLM audit (optional, high-risk gated)

Secondary audit is only called when risk score exceeds configured threshold.

## Rules/style separation

- `QRULE`: strict operational/safety constraints only
- `QSTYLE`: persona/tone/verbosity only
- `QCTX`: memory context only

This separation prevents cross-channel policy pollution.

## Restart behavior

- Gateway restart does not clear sidecar DB.
- Sidecar restart reloads persisted state.
- Runtime persistence should be verified with the generic proof scripts in `bench/src/` rather than checked-in local snapshots.
