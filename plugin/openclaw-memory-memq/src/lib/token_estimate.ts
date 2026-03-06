export interface NormalizedMessage {
  role: string;
  text: string;
  ts?: number;
  raw?: any;
}

function flattenContent(value: any): string[] {
  if (typeof value === "string") return [value];
  if (Array.isArray(value)) {
    const out: string[] = [];
    for (const item of value) out.push(...flattenContent(item));
    return out;
  }
  if (!value || typeof value !== "object") return [];
  const type = String((value as any).type ?? "");
  const parts: string[] = [];
  if ((value as any).text) parts.push(String((value as any).text));
  if ((value as any).content) parts.push(...flattenContent((value as any).content));
  if ((value as any).output) parts.push(String((value as any).output));
  if ((value as any).result) parts.push(String((value as any).result));
  if ((value as any).arguments) parts.push(JSON.stringify((value as any).arguments));
  if ((value as any).name && type.includes("tool")) parts.push(`tool:${String((value as any).name)}`);
  if ((value as any).function?.name) parts.push(`fn:${String((value as any).function.name)}`);
  if (parts.length === 0) parts.push(JSON.stringify(value));
  return parts;
}

export function messageText(message: any): string {
  const parts: string[] = [];
  if (!message || typeof message !== "object") return "";
  if (typeof message.text === "string") parts.push(message.text);
  if (typeof message.content === "string") parts.push(message.content);
  if (Array.isArray(message.content)) parts.push(...flattenContent(message.content));
  if (Array.isArray((message as any).tool_calls)) parts.push(JSON.stringify((message as any).tool_calls));
  if ((message as any).toolCallId) parts.push(String((message as any).toolCallId));
  const joined = parts.join(" ").replace(/\s+/g, " ").trim();
  return joined.slice(0, 6000);
}

export function estimateTokens(text: string): number {
  const clean = String(text || "").replace(/\s+/g, " ").trim();
  if (!clean) return 0;
  return Math.max(1, Math.ceil(clean.length / 4));
}

export function normalizeMessages(messages: any[]): NormalizedMessage[] {
  if (!Array.isArray(messages)) return [];
  return messages
    .map((message) => ({
      role: String(message?.role ?? "unknown"),
      text: messageText(message),
      ts: typeof message?.ts === "number" ? message.ts : undefined,
      raw: message,
    }))
    .filter((message) => message.text.trim());
}
