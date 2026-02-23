import type { Fact, MemCtxInput, MemoryTrace } from "./types.js";
import { inferIntent, requiredSlots } from "./intent.js";

const tok = (s: string) => Math.ceil(s.length / 4);

function lineForTrace(trace: MemoryTrace, facts: Fact[]): string {
  const tags = (trace.tags ?? []).join(",");
  const factLine = facts.map((f) => `${f.k}=${f.v}`).join(",");
  return `  - id=${trace.id} type=${trace.type} conf=${trace.confidence.toFixed(2)} imp=${trace.importance.toFixed(2)} t=${trace.tsSec} tags=[${tags}] facts=[${factLine}]`;
}

export function compileMemCtx(input: MemCtxInput): string {
  const now = input.nowSec ?? Math.floor(Date.now() / 1000);
  const intent = inferIntent(input.userText ?? "");
  const need = new Set(requiredSlots(intent));

  const out: string[] = ["[MEMCTX v1]", `budget_tokens=${input.budgetTokens}`, "surface:"];
  let used = out.reduce((a, b) => a + tok(b), 0);

  const emit = (label: "surface" | "deep", traces: MemoryTrace[], factBudget: number) => {
    for (const trace of traces) {
      const sortedFacts = [...trace.facts].sort((a, b) => {
        const aNeed = need.has(a.k) ? 1 : 0;
        const bNeed = need.has(b.k) ? 1 : 0;
        const aConf = a.conf ?? trace.confidence;
        const bConf = b.conf ?? trace.confidence;
        return bNeed - aNeed || bConf - aConf;
      });

      const selected: Fact[] = [];
      let factTok = 0;
      for (const f of sortedFacts) {
        const c = tok(`${f.k}=${f.v}`) + 1;
        if (factTok + c > factBudget) break;
        selected.push(f);
        factTok += c;
      }

      const line = lineForTrace(trace, selected);
      const t = tok(line);
      if (used + t > input.budgetTokens) break;
      out.push(line);
      used += t;
    }
  };

  emit("surface", input.surface, 24);
  out.push("deep:");
  used += tok("deep:");
  emit("deep", input.deep, 34);

  if (input.conflicts?.length) {
    out.push("conflicts:");
    used += tok("conflicts:");
    for (const c of input.conflicts) {
      const line = `  - key=${c.key} policy=${c.policy} members=[${c.members.join(",")}]`;
      const t = tok(line);
      if (used + t > input.budgetTokens) break;
      out.push(line);
      used += t;
    }
  }

  out.push("rules:");
  for (const r of input.rules) {
    const line = `  - do=${r}`;
    const t = tok(line);
    if (used + t > input.budgetTokens) break;
    out.push(line);
    used += t;
  }

  out.push("notes:");
  out.push(`  - generated_at=${now}`);
  out.push("  - if_conflict=prefer_higher_imp_then_recent");
  return out.join("\n");
}

export const estimateTokens = tok;
