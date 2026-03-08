import { estimateTokens, type NormalizedMessage } from "./token_estimate.js";

export interface BudgetConfig {
  totalMaxInputTokens: number;
  totalReserveTokens: number;
  memctxTokens: number;
  rulesTokens: number;
  styleTokens: number;
  recentMaxTokens: number;
  recentMinKeepMessages: number;
}

export interface TrimResult {
  kept: NormalizedMessage[];
  pruned: NormalizedMessage[];
  keptTokens: number;
  prunedTokens: number;
  recentBudget: number;
}

export interface InputBreakdown {
  system: number;
  rules: number;
  style: number;
  ctx: number;
  recent: number;
  total: number;
  cap: number;
}

export interface EnforcedInput {
  recent: NormalizedMessage[];
  memctx: string;
  breakdown: InputBreakdown;
}

function estimateRecent(messages: NormalizedMessage[]): number {
  return messages.reduce((sum, message) => sum + estimateTokens(message.text) + 4, 0);
}

function trimMessagesToBudget(messages: NormalizedMessage[], budget: number, minKeepHuman: number): NormalizedMessage[] {
  const kept: NormalizedMessage[] = [];
  let used = 0;
  let keptHumans = 0;
  for (const message of [...messages].reverse()) {
    const cost = estimateTokens(message.text) + 4;
    const isHuman = message.role === "user" || message.role === "assistant";
    const mustKeep = isHuman && keptHumans < minKeepHuman;
    if (!mustKeep && used + cost > budget) continue;
    kept.push(message);
    used += cost;
    if (isHuman) keptHumans += 1;
  }
  kept.reverse();
  return kept;
}

function trimBlockToBudget(text: string, budget: number): string {
  const clean = String(text || "").trim();
  if (!clean) return "";
  if (budget <= 0) return "";
  const lines = clean.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  const kept: string[] = [];
  let used = 0;
  for (const line of lines) {
    const cost = estimateTokens(line) + 1;
    if (cost > budget) continue;
    if (used + cost > budget) continue;
    kept.push(line);
    used += cost;
  }
  return kept.join("\n");
}

export function trimRecentToBudget(messages: NormalizedMessage[], cfg: BudgetConfig, promptText: string): TrimResult {
  const promptTokens = estimateTokens(promptText);
  const fixed = cfg.memctxTokens + cfg.rulesTokens + cfg.styleTokens + promptTokens + cfg.totalReserveTokens;
  const recentBudget = Math.max(0, Math.min(cfg.recentMaxTokens, cfg.totalMaxInputTokens - fixed));

  const kept: NormalizedMessage[] = [];
  let keptTokens = 0;
  let keptHumans = 0;
  for (const message of [...messages].reverse()) {
    const cost = estimateTokens(message.text) + 4;
    const isHuman = message.role === "user" || message.role === "assistant";
    const mustKeep = isHuman && keptHumans < cfg.recentMinKeepMessages;
    if (!mustKeep && keptTokens + cost > recentBudget) continue;
    kept.push(message);
    keptTokens += cost;
    if (isHuman) keptHumans += 1;
  }
  kept.reverse();
  const keptSet = new Set(kept.map((message) => message));
  const pruned = messages.filter((message) => !keptSet.has(message));
  const prunedTokens = pruned.reduce((sum, message) => sum + estimateTokens(message.text) + 4, 0);
  return { kept, pruned, keptTokens, prunedTokens, recentBudget };
}

export function estimateInputBreakdown(parts: {
  prompt: string;
  recent: NormalizedMessage[];
  memrules: string;
  memstyle: string;
  memctx: string;
}, cfg: BudgetConfig): InputBreakdown {
  const system = estimateTokens(parts.prompt);
  const recent = estimateRecent(parts.recent);
  const rules = estimateTokens(parts.memrules);
  const style = estimateTokens(parts.memstyle);
  const ctx = estimateTokens(parts.memctx);
  const cap = Math.max(0, cfg.totalMaxInputTokens - cfg.totalReserveTokens);
  return { system, recent, rules, style, ctx, total: system + recent + rules + style + ctx, cap };
}

export function enforceTotalInputCap(parts: {
  prompt: string;
  recent: NormalizedMessage[];
  memrules: string;
  memstyle: string;
  memctx: string;
}, cfg: BudgetConfig): EnforcedInput {
  let recent = [...parts.recent];
  let memctx = parts.memctx;
  let breakdown = estimateInputBreakdown({ ...parts, recent, memctx }, cfg);
  if (breakdown.total > breakdown.cap) {
    const recentBudget = Math.max(0, breakdown.cap - breakdown.system - breakdown.rules - breakdown.style - breakdown.ctx);
    recent = trimMessagesToBudget(recent, recentBudget, cfg.recentMinKeepMessages);
    breakdown = estimateInputBreakdown({ ...parts, recent, memctx }, cfg);
  }
  if (breakdown.total > breakdown.cap) {
    const ctxBudget = Math.max(0, breakdown.cap - breakdown.system - breakdown.rules - breakdown.style - breakdown.recent);
    memctx = trimBlockToBudget(memctx, ctxBudget);
    breakdown = estimateInputBreakdown({ ...parts, recent, memctx }, cfg);
  }
  return { recent, memctx, breakdown };
}

export function estimateTotalInput(parts: {
  prompt: string;
  recent: NormalizedMessage[];
  memrules: string;
  memstyle: string;
  memctx: string;
}, cfg: BudgetConfig): number {
  return estimateInputBreakdown(parts, cfg).total;
}
