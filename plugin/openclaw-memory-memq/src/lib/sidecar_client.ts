import type { MemqMessage, MemqQueryRequest, MemqQueryResponse } from "../types.js";
import { existsSync } from "node:fs";
import { join } from "node:path";
import { spawn } from "node:child_process";

export class SidecarClient {
  private runtimeBrainTimeoutMs?: number;

  constructor(private readonly baseUrl: string) {}

  private normalizeBrainMode(v: string | undefined): "off" | "best_effort" | "required" {
    const s = String(v || "").trim().toLowerCase();
    if (s === "off") return "off";
    if (s === "required") return "required";
    return "best_effort";
  }

  private parseTimeout(v: string | undefined, fallbackMs: number): number {
    const n = Number(v || "");
    if (!Number.isFinite(n) || n <= 0) return fallbackMs;
    return Math.max(5000, Math.floor(n));
  }

  private brainTimeoutMs(): number {
    if (this.runtimeBrainTimeoutMs && Number.isFinite(this.runtimeBrainTimeoutMs)) {
      return Math.max(5000, Math.floor(this.runtimeBrainTimeoutMs));
    }
    return this.parseTimeout(process.env.MEMQ_BRAIN_TIMEOUT_MS, 60000);
  }

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
      if (!r.ok) {
        let detail = "";
        try {
          const t = await r.text();
          if (t) detail = t.slice(0, 400);
        } catch {
          // ignore
        }
        throw new Error(`sidecar ${path} status=${r.status}${detail ? ` body=${detail}` : ""}`);
      }
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

  async ensureUp(
    workspaceRoot: string,
    opts?: {
      brainMode?: string;
      brainProvider?: string;
      brainBaseUrl?: string;
      brainModel?: string;
      brainKeepAlive?: string;
      brainTimeoutMs?: string | number;
      brainMaxTokens?: string | number;
      brainAutoRestart?: string | number | boolean;
      brainRestartCooldownSec?: string | number;
      brainRestartWaitMs?: string | number;
    }
  ): Promise<boolean> {
    const configuredTimeout = this.parseTimeout(
      opts?.brainTimeoutMs !== undefined ? String(opts.brainTimeoutMs) : process.env.MEMQ_BRAIN_TIMEOUT_MS,
      60000
    );
    this.runtimeBrainTimeoutMs = configuredTimeout;
    if (await this.health()) return true;
    const root = String(workspaceRoot || "").trim();
    if (!root) return false;
    const app = join(root, "sidecar", "minisidecar.py");
    if (!existsSync(app)) return false;
    const venvPy = join(root, "sidecar", ".venv", "bin", "python");
    const py = existsSync(venvPy) ? venvPy : "python3";
    try {
      const brainMode = this.normalizeBrainMode(opts?.brainMode || process.env.MEMQ_BRAIN_MODE);
      const env = {
        ...process.env,
        MEMQ_ROOT: root,
        MEMQ_DB_PATH: ".memq/sidecar.sqlite3",
        MEMQ_BRAIN_ENABLED: process.env.MEMQ_BRAIN_ENABLED || "1",
        MEMQ_BRAIN_MODE: brainMode,
        MEMQ_BRAIN_PROVIDER: opts?.brainProvider || process.env.MEMQ_BRAIN_PROVIDER || "ollama",
        MEMQ_BRAIN_BASE_URL: opts?.brainBaseUrl || process.env.MEMQ_BRAIN_BASE_URL || "http://127.0.0.1:11434",
        MEMQ_BRAIN_MODEL: opts?.brainModel || process.env.MEMQ_BRAIN_MODEL || "gpt-oss:20b",
        MEMQ_BRAIN_KEEP_ALIVE: opts?.brainKeepAlive || process.env.MEMQ_BRAIN_KEEP_ALIVE || "30m",
        MEMQ_BRAIN_TIMEOUT_MS: String(opts?.brainTimeoutMs || process.env.MEMQ_BRAIN_TIMEOUT_MS || "60000"),
        MEMQ_BRAIN_MAX_TOKENS: String(opts?.brainMaxTokens || process.env.MEMQ_BRAIN_MAX_TOKENS || "256"),
        MEMQ_BRAIN_AUTO_RESTART: String(
          opts?.brainAutoRestart ?? process.env.MEMQ_BRAIN_AUTO_RESTART ?? "1"
        ),
        MEMQ_BRAIN_RESTART_COOLDOWN_SEC: String(
          opts?.brainRestartCooldownSec ?? process.env.MEMQ_BRAIN_RESTART_COOLDOWN_SEC ?? "30"
        ),
        MEMQ_BRAIN_RESTART_WAIT_MS: String(
          opts?.brainRestartWaitMs ?? process.env.MEMQ_BRAIN_RESTART_WAIT_MS ?? "2000"
        ),
      };
      const child = spawn(py, [app], {
        cwd: root,
        detached: true,
        stdio: "ignore",
        env,
      });
      child.unref();
    } catch {
      return false;
    }

    for (let i = 0; i < 12; i += 1) {
      await new Promise((r) => setTimeout(r, 300));
      if (await this.health()) return true;
    }
    return false;
  }

  async bootstrapImportMd(workspaceRoot: string): Promise<void> {
    await this.req<{ ok: boolean }>("/bootstrap/import_md", { workspaceRoot });
  }

  async idleTick(nowSec: number): Promise<void> {
    await this.req<{ ok: boolean }>("/idle_tick", { nowSec });
  }

  async memctxQuery(req: MemqQueryRequest): Promise<MemqQueryResponse> {
    return await this.req<MemqQueryResponse>("/memctx/query", req, "POST", this.brainTimeoutMs());
  }

  async summarizeConversation(sessionKey: string, prunedMessages: MemqMessage[], retentionScope: "surface_only" | "deep"): Promise<void> {
    await this.req<{ ok: boolean }>(
      "/conversation/summarize",
      { sessionKey, prunedMessages, retentionScope },
      "POST",
      this.brainTimeoutMs()
    );
  }

  async ingestTurn(payload: { sessionKey: string; userText: string; assistantText: string; ts: number; metadata?: Record<string, unknown> }): Promise<{ ok: boolean; wrote?: Record<string, number>; traceId?: string }> {
    return await this.req<{ ok: boolean; wrote?: Record<string, number>; traceId?: string }>(
      "/memory/ingest_turn",
      payload,
      "POST",
      this.brainTimeoutMs()
    );
  }

  async idleRunOnce(payload?: { nowTs?: number; maxWorkMs?: number }): Promise<{ ok: boolean; did?: string[]; stats?: Record<string, unknown>; traceId?: string }> {
    return await this.req<{ ok: boolean; did?: string[]; stats?: Record<string, unknown>; traceId?: string }>(
      "/idle/run_once",
      payload ?? {},
      "POST",
      this.brainTimeoutMs()
    );
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
