import { estimateTokens } from "./token_estimate.js";

export function wrapBlock(title: string, body: string): string {
  const b = String(body || "").trim();
  if (!b) return "";
  return `<${title}>\n${b}\n</${title}>`;
}

export function composeInjectedBlocks(memrules: string, memstyle: string, memctx: string): string {
  const parts = [
    wrapBlock("MEMRULES v1", memrules),
    wrapBlock("MEMSTYLE v1", memstyle),
    wrapBlock("MEMCTX v1", memctx),
  ].filter(Boolean);
  return parts.join("\n\n");
}

export function ensureBudget(text: string, budgetTokens: number): string {
  const lines = String(text || "")
    .split("\n")
    .map((x) => x.trimEnd())
    .filter((x) => x.length > 0);
  let out: string[] = [];
  let used = 0;
  for (const line of lines) {
    const t = estimateTokens(line);
    if (used + t > budgetTokens) break;
    out.push(line);
    used += t;
  }
  return out.join("\n");
}
