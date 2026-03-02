import type { MemqMessage } from "../types.js";

export function estimateTokens(text: string): number {
  const s = String(text || "");
  if (!s) return 1;
  const cjk = (s.match(/[\u3040-\u30ff\u3400-\u9fff]/g) || []).length;
  const ascii = (s.match(/[A-Za-z0-9_]/g) || []).length;
  const other = Math.max(0, s.length - cjk - ascii);
  // Heuristic: CJK tends to tokenize denser than ascii chunks.
  const est = Math.ceil(cjk * 1.05 + ascii / 3.6 + other / 2.4);
  return Math.max(1, est);
}

export function messageText(m: any): string {
  if (!m || typeof m !== "object") return "";
  if (typeof m.content === "string") return m.content.trim();
  if (Array.isArray(m.content)) {
    return m.content
      .map((b: any) => (b && typeof b === "object" && b.type === "text" ? String(b.text ?? "") : ""))
      .join(" ")
      .trim();
  }
  if (typeof m.text === "string") return m.text.trim();
  return "";
}

export function normalizeMessages(input: any[]): MemqMessage[] {
  return input
    .map((m) => ({
      role: String(m?.role ?? "unknown"),
      text: messageText(m),
      ts: undefined,
    }))
    .filter((m) => m.text.length > 0);
}

export function estimateMessageTokens(m: MemqMessage): number {
  return 4 + estimateTokens(m.role) + estimateTokens(m.text);
}

export function splitRecentByTokenBudget(messages: any[], maxTokens: number, minKeepMessages: number): {
  keepStart: number;
  kept: any[];
  pruned: any[];
  keptTokens: number;
} {
  const n = messages.length;
  if (!n) return { keepStart: 0, kept: [], pruned: [], keptTokens: 0 };
  const normalized = normalizeMessages(messages);
  const tokenByIndex = messages.map((m, i) => estimateMessageTokens(normalized[i] ?? { role: String(m?.role ?? "x"), text: messageText(m) }));

  let keepStart = n;
  let sum = 0;
  for (let i = n - 1; i >= 0; i -= 1) {
    const mustKeep = n - i <= Math.max(1, minKeepMessages);
    const t = tokenByIndex[i] ?? 1;
    if (mustKeep || sum + t <= Math.max(200, maxTokens)) {
      keepStart = i;
      sum += t;
      continue;
    }
    break;
  }

  return {
    keepStart,
    kept: messages.slice(keepStart),
    pruned: messages.slice(0, keepStart),
    keptTokens: sum,
  };
}
