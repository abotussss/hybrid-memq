import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";

interface SanitizeResult {
  filesScanned: number;
  filesChanged: number;
  idsNormalized: number;
  staleToolMessagesRemoved: number;
  brokenBranchesRemoved: number;
}

function normalizeToolCallId(value: unknown): string {
  const raw = String(value || "");
  if (!raw) return "";
  const pipe = raw.indexOf("|");
  return pipe >= 0 ? raw.slice(0, pipe) : raw;
}

async function collectSessionFiles(root: string, maxAgeMs?: number): Promise<string[]> {
  const entries = await fs.readdir(root, { withFileTypes: true }).catch(() => []);
  const out: string[] = [];
  for (const entry of entries) {
    if (entry.name === "backup") continue;
    const full = path.join(root, entry.name);
    if (entry.isDirectory()) {
      out.push(...await collectSessionFiles(full, maxAgeMs));
      continue;
    }
    if (!entry.isFile() || !entry.name.endsWith(".jsonl")) continue;
    if (typeof maxAgeMs === "number") {
      const stat = await fs.stat(full).catch(() => null);
      if (!stat) continue;
      if (Date.now() - stat.mtimeMs > maxAgeMs) continue;
    }
    out.push(full);
  }
  return out;
}

function isToolCallMessage(parsed: any): boolean {
  return Array.isArray(parsed?.message?.content) && parsed.message.content.some((part: any) => part?.type === "toolCall");
}

function isToolResultMessage(parsed: any): boolean {
  return parsed?.message?.role === "toolResult" && typeof parsed?.message?.toolCallId === "string";
}

function isMissingToolCallError(parsed: any): boolean {
  return parsed?.message?.role === "assistant"
    && parsed?.message?.stopReason === "error"
    && typeof parsed?.message?.errorMessage === "string"
    && parsed.message.errorMessage.includes("No tool call found for function call output");
}

function sanitizeLine(line: string): { line: string; changed: boolean; idsNormalized: number; parsed?: any } {
  if (!line.includes("toolCall") && !line.includes("toolCallId")) {
    try {
      return { line, changed: false, idsNormalized: 0, parsed: JSON.parse(line) };
    } catch {
      return { line, changed: false, idsNormalized: 0 };
    }
  }
  let parsed: any;
  try {
    parsed = JSON.parse(line);
  } catch {
    return { line, changed: false, idsNormalized: 0 };
  }
  let changed = false;
  let idsNormalized = 0;
  const content = parsed?.message?.content;
  if (Array.isArray(content)) {
    for (const part of content) {
      if (part?.type !== "toolCall" || typeof part?.id !== "string") continue;
      const normalized = normalizeToolCallId(part.id);
      if (normalized && normalized !== part.id) {
        part.id = normalized;
        changed = true;
        idsNormalized += 1;
      }
    }
  }
  if (parsed?.message?.role === "toolResult" && typeof parsed?.message?.toolCallId === "string") {
    const normalized = normalizeToolCallId(parsed.message.toolCallId);
    if (normalized && normalized !== parsed.message.toolCallId) {
      parsed.message.toolCallId = normalized;
      changed = true;
      idsNormalized += 1;
    }
  }
  return changed
    ? { line: JSON.stringify(parsed), changed: true, idsNormalized, parsed }
    : { line, changed: false, idsNormalized: 0, parsed };
}

async function sanitizeSessionFile(file: string): Promise<{ changed: boolean; idsNormalized: number; staleToolMessagesRemoved: number; brokenBranchesRemoved: number }> {
  const original = await fs.readFile(file, "utf8").catch(() => "");
  if (!original) return { changed: false, idsNormalized: 0, staleToolMessagesRemoved: 0, brokenBranchesRemoved: 0 };
  let changed = false;
  let idsNormalized = 0;
  const parsedLines = original.split("\n").map((line) => {
    const result = sanitizeLine(line);
    changed = changed || result.changed;
    idsNormalized += result.idsNormalized;
    return result;
  });
  const staleCutoff = Date.now() - 12 * 60 * 60 * 1000;
  let staleToolMessagesRemoved = 0;
  let brokenBranchesRemoved = 0;
  const removedBranchIds = new Set<string>();
  const lines = parsedLines.flatMap((item) => {
    const parsed = item.parsed;
    const messageId = typeof parsed?.id === "string" ? parsed.id : "";
    const parentId = typeof parsed?.parentId === "string" ? parsed.parentId : "";
    if (parentId && removedBranchIds.has(parentId)) {
      if (messageId) removedBranchIds.add(messageId);
      changed = true;
      brokenBranchesRemoved += 1;
      return [];
    }
    if (isMissingToolCallError(parsed)) {
      if (messageId) removedBranchIds.add(messageId);
      changed = true;
      brokenBranchesRemoved += 1;
      return [];
    }
    const rawTs = parsed?.timestamp || parsed?.message?.timestamp;
    const ts = rawTs ? Date.parse(String(rawTs)) : NaN;
    const isStale = Number.isFinite(ts) && ts < staleCutoff;
    if (isStale && (isToolCallMessage(parsed) || isToolResultMessage(parsed))) {
      changed = true;
      staleToolMessagesRemoved += 1;
      return [];
    }
    return [item.changed && parsed ? JSON.stringify(parsed) : item.line];
  });
  if (!changed) return { changed: false, idsNormalized: 0, staleToolMessagesRemoved: 0, brokenBranchesRemoved: 0 };
  const tmp = `${file}.memqtmp`;
  await fs.writeFile(tmp, lines.join("\n"), "utf8");
  await fs.rename(tmp, file);
  return { changed: true, idsNormalized, staleToolMessagesRemoved, brokenBranchesRemoved };
}

export async function sanitizeOpenClawSessions(opts?: { maxAgeHours?: number }): Promise<SanitizeResult> {
  const openclawHome = path.join(os.homedir(), ".openclaw", "agents");
  const agentDirs = await fs.readdir(openclawHome, { withFileTypes: true }).catch(() => []);
  let filesScanned = 0;
  let filesChanged = 0;
  let idsNormalized = 0;
  let staleToolMessagesRemoved = 0;
  let brokenBranchesRemoved = 0;
  const maxAgeMs = typeof opts?.maxAgeHours === "number" ? opts.maxAgeHours * 60 * 60 * 1000 : undefined;
  for (const agentDir of agentDirs) {
    if (!agentDir.isDirectory()) continue;
    const sessionsDir = path.join(openclawHome, agentDir.name, "sessions");
    const files = await collectSessionFiles(sessionsDir, maxAgeMs).catch(() => []);
    for (const file of files) {
      filesScanned += 1;
      const result = await sanitizeSessionFile(file);
      if (result.changed) filesChanged += 1;
      idsNormalized += result.idsNormalized;
      staleToolMessagesRemoved += result.staleToolMessagesRemoved;
      brokenBranchesRemoved += result.brokenBranchesRemoved;
    }
  }
  return { filesScanned, filesChanged, idsNormalized, staleToolMessagesRemoved, brokenBranchesRemoved };
}
