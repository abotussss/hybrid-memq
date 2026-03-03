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
  const toText = (v: any, maxLen = 3200): string => {
    if (v == null) return "";
    if (typeof v === "string") return v.trim().slice(0, maxLen);
    try {
      return JSON.stringify(v).slice(0, maxLen);
    } catch {
      return String(v).slice(0, maxLen);
    }
  };
  if (typeof m.content === "string") return m.content.trim();
  if (Array.isArray(m.content)) {
    return m.content
      .map((b: any) => {
        if (!b || typeof b !== "object") return "";
        const t = String((b as any).type ?? "");
        if (t === "text") return toText((b as any).text, 3200);
        if (t === "toolResult" || t === "tool_result" || t === "function_call_output" || t === "tool-output" || t === "toolOutput") {
          return toText((b as any).output ?? (b as any).result ?? (b as any).content ?? b, 3600);
        }
        if (t === "toolCall" || t === "tool_call" || t === "tool-use" || t === "function_call") {
          const name = toText((b as any).name ?? (b as any).function?.name ?? "", 120);
          const args = toText((b as any).args ?? (b as any).arguments ?? (b as any).function?.arguments ?? "", 480);
          return `tool_call:${name} args:${args}`.trim();
        }
        return "";
      })
      .join(" ")
      .trim();
  }
  if (Array.isArray((m as any).tool_calls)) {
    return (m as any).tool_calls
      .map((tc: any) => {
        const name = toText(tc?.function?.name ?? tc?.name ?? "", 120);
        const args = toText(tc?.function?.arguments ?? tc?.arguments ?? "", 480);
        return `tool_call:${name} args:${args}`.trim();
      })
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
  const isHumanRole = (role: string): boolean => role === "user" || role === "assistant";

  let sum = 0;
  let keptHuman = 0;
  const keepFlags = new Array<boolean>(n).fill(false);
  for (let i = n - 1; i >= 0; i -= 1) {
    const role = String(messages[i]?.role ?? "").toLowerCase();
    const human = isHumanRole(role);
    const mustKeep = human && keptHuman < Math.max(1, minKeepMessages);
    const t = tokenByIndex[i] ?? 1;
    if (mustKeep || sum + t <= Math.max(200, maxTokens)) {
      keepFlags[i] = true;
      sum += t;
      if (human) keptHuman += 1;
      continue;
    }
    // Oversized non-human payloads (tool outputs etc.) should be pruned first;
    // do not stop walking, otherwise we may fail to keep required recent human turns.
    if (!human) {
      continue;
    }
    break;
  }

  const kept: any[] = [];
  const pruned: any[] = [];
  let keepStart = n;
  for (let i = 0; i < n; i += 1) {
    if (keepFlags[i]) {
      if (keepStart > i) keepStart = i;
      kept.push(messages[i]);
    } else {
      pruned.push(messages[i]);
    }
  }
  if (keepStart === n && kept.length > 0) keepStart = 0;

  return {
    keepStart,
    kept,
    pruned,
    keptTokens: sum,
  };
}
