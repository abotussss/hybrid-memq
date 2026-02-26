import { appendFileSync, existsSync, mkdirSync, readdirSync, readFileSync, statSync, unlinkSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { messageText } from "./token_estimate.js";

const SECRET_PATTERNS: RegExp[] = [
  /\bsk-proj-[A-Za-z0-9_-]{10,}\b/g,
  /\bsk-[A-Za-z0-9]{16,}\b/g,
  /\bghp_[A-Za-z0-9]{20,}\b/g,
  /\bxox[baprs]-[A-Za-z0-9-]{20,}\b/g,
  /\bAKIA[0-9A-Z]{16}\b/g,
  /\bASIA[0-9A-Z]{16}\b/g,
  /\b(api[_ -]?key|secret|token|password|private[_ -]?key)\b\s*[:=]\s*\S{4,}/gi,
];

function redactSecrets(s: string): string {
  let out = String(s || "");
  for (const p of SECRET_PATTERNS) out = out.replace(p, "[REDACTED_SECRET]");
  return out;
}

function capFile(path: string, maxBytes: number): void {
  if (!existsSync(path)) return;
  const st = statSync(path);
  if (st.size <= maxBytes) return;
  const buf = readFileSync(path);
  const start = Math.max(0, buf.length - maxBytes);
  let tail = buf.subarray(start);
  const lf = tail.indexOf(0x0a);
  if (lf >= 0 && lf + 1 < tail.length) tail = tail.subarray(lf + 1);
  writeFileSync(path, tail);
}

export function archivePrunedMessages(input: {
  workspaceRoot: string;
  sessionKey: string;
  pruned: any[];
  maxFileBytes: number;
  maxFiles: number;
}): string | null {
  if (!input.pruned.length) return null;
  const dir = join(input.workspaceRoot, ".memq", "conversation_archive");
  mkdirSync(dir, { recursive: true });
  const sessionSafe = input.sessionKey.replace(/[^a-zA-Z0-9._-]+/g, "_").slice(0, 120) || "default";
  const path = join(dir, `${sessionSafe}.jsonl`);
  const now = Math.floor(Date.now() / 1000);
  const lines: string[] = [];
  for (const m of input.pruned) {
    const role = String(m?.role ?? "unknown");
    const text = redactSecrets(messageText(m)).slice(0, 1600);
    if (!text) continue;
    lines.push(JSON.stringify({ ts: now, role, text }, null, 0));
  }
  if (!lines.length) return null;
  appendFileSync(path, `${lines.join("\n")}\n`, "utf8");
  capFile(path, Math.max(4096, input.maxFileBytes));

  const files = readdirSync(dir)
    .filter((x) => x.endsWith(".jsonl"))
    .map((name) => ({ name, path: join(dir, name), mtime: statSync(join(dir, name)).mtimeMs }))
    .sort((a, b) => b.mtime - a.mtime);
  for (const old of files.slice(Math.max(1, input.maxFiles))) {
    try {
      unlinkSync(old.path);
    } catch {
      // best effort
    }
  }
  return path;
}
