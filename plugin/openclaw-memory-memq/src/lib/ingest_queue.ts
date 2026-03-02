import { appendFileSync, existsSync, mkdirSync, readFileSync, renameSync, unlinkSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import type { SidecarClient } from "./sidecar_client.js";

export interface IngestPayload {
  sessionKey: string;
  userText: string;
  assistantText: string;
  ts: number;
  metadata?: Record<string, unknown>;
}

function queuePath(workspaceRoot: string): string {
  return join(workspaceRoot, ".memq", "pending_ingest.jsonl");
}

export function enqueueIngest(workspaceRoot: string, payload: IngestPayload): void {
  const path = queuePath(workspaceRoot);
  mkdirSync(dirname(path), { recursive: true });
  const line = JSON.stringify(payload);
  appendFileSync(path, `${line}\n`, "utf8");
}

export async function flushIngestQueue(
  workspaceRoot: string,
  sidecar: SidecarClient,
  maxItems = 64
): Promise<{ sent: number; remain: number }> {
  const path = queuePath(workspaceRoot);
  if (!existsSync(path)) return { sent: 0, remain: 0 };

  const raw = readFileSync(path, "utf8");
  const lines = raw.split("\n").map((x) => x.trim()).filter(Boolean);
  if (!lines.length) {
    unlinkSync(path);
    return { sent: 0, remain: 0 };
  }

  const payloads: IngestPayload[] = [];
  for (const ln of lines) {
    try {
      const j = JSON.parse(ln);
      const p: IngestPayload = {
        sessionKey: String(j.sessionKey ?? "default"),
        userText: String(j.userText ?? ""),
        assistantText: String(j.assistantText ?? ""),
        ts: Number(j.ts ?? 0) || Math.floor(Date.now() / 1000),
        metadata: j && typeof j.metadata === "object" ? (j.metadata as Record<string, unknown>) : undefined,
      };
      payloads.push(p);
    } catch {
      // drop malformed line
    }
  }

  let sent = 0;
  const keep: IngestPayload[] = [];
  const batch = payloads.slice(0, Math.max(1, maxItems));
  const tail = payloads.slice(batch.length);

  for (let i = 0; i < batch.length; i += 1) {
    const p = batch[i];
    try {
      await sidecar.ingestTurn(p);
      sent += 1;
    } catch {
      keep.push(p);
      keep.push(...batch.slice(i + 1));
      break;
    }
  }
  keep.push(...tail);

  if (!keep.length) {
    unlinkSync(path);
    return { sent, remain: 0 };
  }

  const tmp = `${path}.tmp`;
  writeFileSync(tmp, `${keep.map((p) => JSON.stringify(p)).join("\n")}\n`, "utf8");
  renameSync(tmp, path);
  return { sent, remain: keep.length };
}
