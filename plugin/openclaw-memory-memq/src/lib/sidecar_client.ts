import type { MemqMessage, MemqQueryRequest, MemqQueryResponse } from "../types.js";

export class SidecarClient {
  constructor(private readonly baseUrl: string) {}

  private async req<T>(path: string, body?: unknown, method = "POST", timeoutMs = 6000): Promise<T> {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
      const r = await fetch(`${this.baseUrl}${path}`, {
        method,
        headers: { "content-type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: ctrl.signal,
      });
      if (!r.ok) throw new Error(`sidecar ${path} status=${r.status}`);
      return (await r.json()) as T;
    } finally {
      clearTimeout(timer);
    }
  }

  async health(): Promise<boolean> {
    try {
      const j = await this.req<{ ok?: boolean }>("/health", undefined, "GET", 1500);
      return Boolean(j.ok);
    } catch {
      return false;
    }
  }

  async bootstrapImportMd(workspaceRoot: string): Promise<void> {
    await this.req<{ ok: boolean }>("/bootstrap/import_md", { workspaceRoot });
  }

  async idleTick(nowSec: number): Promise<void> {
    await this.req<{ ok: boolean }>("/idle_tick", { nowSec });
  }

  async memctxQuery(req: MemqQueryRequest): Promise<MemqQueryResponse> {
    return await this.req<MemqQueryResponse>("/memctx/query", req);
  }

  async summarizeConversation(sessionKey: string, prunedMessages: MemqMessage[], retentionScope: "surface_only" | "deep"): Promise<void> {
    await this.req<{ ok: boolean }>("/conversation/summarize", { sessionKey, prunedMessages, retentionScope });
  }

  async ingestTurn(payload: { sessionKey: string; userText: string; assistantText: string; ts: number; metadata?: Record<string, unknown> }): Promise<void> {
    await this.req<{ ok: boolean }>("/memory/ingest_turn", payload);
  }

  async idleRunOnce(payload?: { nowTs?: number; maxWorkMs?: number }): Promise<void> {
    await this.req<{ ok: boolean }>("/idle/run_once", payload ?? {});
  }

  async auditOutput(payload: { sessionKey: string; text: string; mode: "primary" | "dual"; thresholds?: Record<string, number> }): Promise<{
    ok: boolean;
    risk: number;
    block: boolean;
    redactedText?: string;
    reasons: string[];
  }> {
    return await this.req("/audit/output", payload);
  }

  async profile(): Promise<{
    ok: boolean;
    preference_profile: Record<string, { value: string; confidence: number; updated_at: number }>;
    memory_policy_profile: Record<string, { value: string; confidence: number; updated_at: number }>;
  }> {
    return await this.req("/profile", undefined, "GET");
  }

  async quarantine(limit = 50): Promise<{
    ok: boolean;
    items: Array<Record<string, unknown>>;
  }> {
    return await this.req(`/quarantine?limit=${Math.max(1, Math.min(500, limit))}`, undefined, "GET");
  }
}
