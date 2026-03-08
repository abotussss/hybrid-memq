import { existsSync } from "node:fs";
import { join } from "node:path";
import { spawn } from "node:child_process";
import type { QctxQueryRequest, QctxQueryResponse } from "../types.js";

export class SidecarClient {
  constructor(private readonly baseUrl: string) {}

  private async req<T>(path: string, method = "POST", body?: unknown, timeoutMs = 15000): Promise<T> {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
      const response = await fetch(`${this.baseUrl}${path}`, {
        method,
        headers: { "content-type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: ctrl.signal,
      });
      if (!response.ok) {
        const detail = await response.text().catch(() => "");
        throw new Error(`sidecar ${path} status=${response.status} body=${detail.slice(0, 400)}`);
      }
      return (await response.json()) as T;
    } finally {
      clearTimeout(timer);
    }
  }

  async health(): Promise<any | null> {
    try {
      return await this.req<any>("/health", "GET", undefined, 3000);
    } catch {
      return null;
    }
  }

  async ensureUp(workspaceRoot: string, env: Record<string, string>): Promise<boolean> {
    const healthy = await this.health();
    if (healthy?.ok) return true;
    const appPkg = join(workspaceRoot, "sidecar", "minisidecar.py");
    if (!existsSync(appPkg)) return false;
    const py = existsSync(join(workspaceRoot, "sidecar", ".venv", "bin", "python"))
      ? join(workspaceRoot, "sidecar", ".venv", "bin", "python")
      : "python3";
    const child = spawn(py, ["-m", "sidecar.minisidecar"], {
      cwd: workspaceRoot,
      detached: true,
      stdio: "ignore",
      env: { ...process.env, ...env, MEMQ_ROOT: workspaceRoot },
    });
    child.unref();
    for (let i = 0; i < 20; i += 1) {
      await new Promise((resolve) => setTimeout(resolve, 300));
      const probe = await this.health();
      if (probe?.ok) return true;
    }
    return false;
  }

  async bootstrapImportMd(workspaceRoot: string): Promise<void> {
    await this.req("/bootstrap/import_md", "POST", { workspaceRoot }, 30000);
  }

  async idleTick(nowSec: number): Promise<void> {
    await this.req("/idle_tick", "POST", { nowSec }, 3000);
  }

  async qctxQuery(req: QctxQueryRequest, timeoutMs = 70000): Promise<QctxQueryResponse> {
    return await this.req<QctxQueryResponse>("/qctx/query", "POST", req, timeoutMs);
  }

  async ingestTurn(payload: Record<string, unknown>, timeoutMs = 70000): Promise<any> {
    return await this.req<any>("/memory/ingest_turn", "POST", payload, timeoutMs);
  }

  async previewPrompt(payload: Record<string, unknown>, timeoutMs = 70000): Promise<any> {
    return await this.req<any>("/memory/preview_prompt", "POST", payload, timeoutMs);
  }

  async idleRunOnce(payload: Record<string, unknown>, timeoutMs = 70000): Promise<any> {
    return await this.req<any>("/idle/run_once", "POST", payload, timeoutMs);
  }

  async auditOutput(payload: Record<string, unknown>, timeoutMs = 30000): Promise<any> {
    return await this.req<any>("/audit/output", "POST", payload, timeoutMs);
  }
}
