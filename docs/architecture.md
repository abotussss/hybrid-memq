# Architecture

## Mode A
- `api_text`: `MEMCTX v1` を token budget 内で prepend 注入

## Core formulae
- Activation score:
  - `A_i = w_s*sim + w_r*recency + w_f*frequency + w_p*priority - w_c*conflict_penalty`
- Ephemeral decay:
  - `strength(t)=strength(t0)*exp(-lambda*dt)+beta*use_event`
- Write gate:
  - `W=a*utility+b*novelty+c*stability+d*explicitness-e*redundancy`

## Runtime pipeline
1. query embed
2. surface hit attempt
3. deep search if needed
4. conflict detection + fallback判定
5. MEMCTX compile (budgeted)
6. agent_endでtouch/update
7. background consolidation

## Sidecar storage
- SQLite `memory_trace` tableに量子化コードとメタを保存
- `/index/consolidate` が削除/重複統合/強度更新を実施
