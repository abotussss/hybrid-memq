# MEMQ Validation Report (MEMCTX + MEMRULES + MEMSTYLE v1)

Date: 2026-02-23

## Objective
Validate that:
1. Non-allowed language mixing is repaired (not only Chinese).
2. Explicit user-requested language is passed through.
3. MEMSTYLE v1 keeps style/persona consistency under strict mode.
4. Secret leakage is always blocked.
5. MEMCTX/MEMRULES/MEMSTYLE remain budgeted and non-conflicting by design.

## Runtime Mode
- Primary audit: ON (`MEMQ_OUTPUT_AUDIT_ENABLED=1`)
- Secondary LLM audit: ON (`MEMQ_LLM_AUDIT_ENABLED=1`, model `gpt-5.2`)
- Lang secondary trigger: ON
- Lang deterministic repair: ON
- MEMSTYLE: ON (`memq.style.enabled=true`)

## Test Matrix
Total cases: 67

Categories:
- clean: 20
- style_drift: 10
- mixed language/scripts: 24
- explicit language pass-through: 5
- secret leakage: 8

## Results
- pass expectation accuracy: **1.0000**
- repair expectation accuracy: **1.0000**
- false blocks on clean cases: **0**
- mixed-language repair rate: **1.0000**
- style-drift repair rate: **1.0000**
- secret block rate: **1.0000**
- explicit-language pass-through rate: **1.0000**

## Coverage Notes
Mixed-language repair covered:
- Chinese Han segment mixing in Japanese output
- Cyrillic mixing
- Korean mixing (including tokens like `대로`)
- Arabic/Hebrew/Devanagari/Thai/Greek script mixing

## Design Guarantees Implemented
- English is always allowed as baseline setting language.
- Allowed languages are extended by habitual user language inference.
- Explicit user request language can bypass language audit for that turn.
- Secrets are mandatory-block regardless of threshold.
- `memq.style.strict=true` enforces unresolved style violation blocking.
- Budgets are separated:
  - MEMCTX: `memq.budgetTokens`
  - MEMRULES: `memq.rules.budgetTokens`
  - MEMSTYLE: `memq.style.budgetTokens`

## Token Efficiency
- MEMSTYLE uses compact DSL and budget trimming.
- Example (`budget=24`) keeps high-priority style lines only (approximately 21 tokens used).
- This prevents MEMSTYLE from bloating MEMCTX/MEMRULES budgets.

## Artifacts
- Summary: `docs/memstyle_validation_summary.json`
- Detail: `docs/memstyle_validation_detail.json`
- This report: `docs/memstyle_validation_report.md`

## Limitation
- Absolute deterministic personality fidelity is impossible with non-deterministic LLMs.
- Secondary LLM audit may fail in restricted network environments; deterministic repair path remains active.
