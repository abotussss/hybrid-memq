import type { MemoryType, VolatilityClass } from "../memq_core.js";
import type { SidecarSearchResult } from "../types.js";

export class SidecarClient {
  private readonly retries = 2;
  private readonly timeoutMs = 4500;

  constructor(private readonly baseUrl: string) {}

  private async wait(ms: number): Promise<void> {
    await new Promise((resolve) => setTimeout(resolve, ms));
  }

  private async fetchWithTimeout(path: string, init?: RequestInit): Promise<Response> {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), this.timeoutMs);
    try {
      return await fetch(`${this.baseUrl}${path}`, { ...init, signal: ctrl.signal });
    } finally {
      clearTimeout(timer);
    }
  }

  private async requestJson<T>(path: string, init?: RequestInit, retries = this.retries): Promise<T> {
    let lastErr: Error | undefined;
    for (let i = 0; i <= retries; i += 1) {
      try {
        const r = await this.fetchWithTimeout(path, init);
        if (!r.ok) throw new Error(`sidecar request failed path=${path} status=${r.status}`);
        return (await r.json()) as T;
      } catch (err) {
        lastErr = err as Error;
        if (i >= retries) break;
        await this.wait(80 * (i + 1));
      }
    }
    throw lastErr ?? new Error(`sidecar request failed path=${path}`);
  }

  async health(): Promise<boolean> {
    try {
      const r = await this.fetchWithTimeout("/health", { method: "GET" });
      return r.ok;
    } catch {
      return false;
    }
  }

  async embed(text: string): Promise<number[]> {
    const j = await this.requestJson<{ vector: number[] }>("/embed", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ text })
    });
    return j.vector;
  }

  async search(vector: number[], k: number): Promise<SidecarSearchResult[]> {
    const j = await this.requestJson<{ items: SidecarSearchResult[] }>("/index/search", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ vector, k })
    });
    return j.items;
  }

  async add(item: {
    id: string;
    vector: number[];
    tsSec: number;
    type: MemoryType;
    importance: number;
    confidence: number;
    strength: number;
    volatilityClass: VolatilityClass;
    facts: Array<{ k: string; v: string; conf?: number }>;
    tags: string[];
    evidenceUri?: string;
    rawText?: string;
    retentionScope?: "deep" | "surface_only";
    ttlDays?: number;
    privacyScope?: "private" | "shareable";
  }): Promise<void> {
    await this.requestJson<Record<string, unknown>>("/index/add", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(item)
    });
  }

  async summarizeConversation(input: {
    sessionId: string;
    items: Array<{ role: string; text: string }>;
    nowSec?: number;
  }): Promise<{
    ok: boolean;
    summarized: number;
    bridgeNote?: string;
    surface?: SidecarSearchResult;
    deep?: SidecarSearchResult;
  }> {
    const j = await this.requestJson<{
      ok: boolean;
      summarized: number;
      bridgeNote?: string;
      surface?: any;
      deep?: any;
    }>("/conversation/summarize", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        sessionId: input.sessionId,
        items: input.items,
        nowSec: input.nowSec
      })
    });
    const toSearchLike = (x: any): SidecarSearchResult | undefined => {
      if (!x || typeof x !== "object" || typeof x.id !== "string") return undefined;
      return {
        id: x.id,
        score: 0,
        tsSec: Number(x.tsSec ?? Math.floor(Date.now() / 1000)),
        type: (x.type as MemoryType) ?? "note",
        importance: Number(x.importance ?? 0.5),
        confidence: Number(x.confidence ?? 0.7),
        strength: Number(x.strength ?? 0.6),
        volatilityClass: (x.volatilityClass as VolatilityClass) ?? "medium",
        facts: Array.isArray(x.facts) ? x.facts : [],
        tags: Array.isArray(x.tags) ? x.tags : [],
        rawText: typeof x.rawText === "string" ? x.rawText : undefined
      };
    };
    return {
      ok: Boolean(j.ok),
      summarized: Number(j.summarized ?? 0),
      bridgeNote: j.bridgeNote,
      surface: toSearchLike(j.surface),
      deep: toSearchLike(j.deep)
    };
  }

  async touch(ids: string[]): Promise<void> {
    if (!ids.length) return;
    await this.requestJson<Record<string, unknown>>("/index/touch", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ ids })
    });
  }

  async consolidate(nowSec: number): Promise<void> {
    try {
      await this.requestJson<Record<string, unknown>>("/consolidate", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ nowSec })
      }, 0);
      return;
    } catch {
      await this.requestJson<Record<string, unknown>>("/index/consolidate", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ nowSec })
      }, 0);
    }
  }

  async idleTick(nowSec: number): Promise<void> {
    await this.requestJson<Record<string, unknown>>("/idle_tick", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ nowSec })
    });
  }

  async profile(): Promise<{
    preferences: Array<{ key: string; value: string; confidence: number; updatedAt: number }>;
    memoryPolicies: Array<{ key: string; value: string; confidence: number; updatedAt: number }>;
  }> {
    return await this.requestJson<{
      preferences: Array<{ key: string; value: string; confidence: number; updatedAt: number }>;
      memoryPolicies: Array<{ key: string; value: string; confidence: number; updatedAt: number }>;
    }>("/profile");
  }

  async pushPreferenceEvents(
    events: Array<{
      id: string;
      key: string;
      value: string;
      weight: number;
      explicit: number;
      source: string;
      evidence_uri?: string;
      created_at: number;
    }>
  ): Promise<void> {
    if (!events.length) return;
    await this.requestJson<Record<string, unknown>>("/preference/event", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ events })
    });
  }

  async auditOutput(input: {
    sessionId: string;
    text: string;
    allowedLanguages: string[];
    preferredLanguage?: string;
    styleProfile?: {
      tone?: string;
      persona?: string;
      speakingStyle?: string;
      verbosity?: string;
      avoid?: string[];
      strict?: boolean;
    };
  }): Promise<{
    ok: boolean;
    passed: boolean;
    riskScore: number;
    reasons: string[];
    repairedApplied?: boolean;
    repairedText?: string;
    secondary?: { enabled?: boolean; called?: boolean; ok?: boolean; block?: boolean; risk?: number; reasons?: string[] };
  }> {
    return await this.requestJson<{
      ok: boolean;
      passed: boolean;
      riskScore: number;
      reasons: string[];
      repairedApplied?: boolean;
      repairedText?: string;
      secondary?: { enabled?: boolean; called?: boolean; ok?: boolean; block?: boolean; risk?: number; reasons?: string[] };
    }>("/audit/output", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        sessionId: input.sessionId,
        text: input.text,
        allowedLanguages: input.allowedLanguages,
        preferredLanguage: input.preferredLanguage ?? "",
        styleProfile: input.styleProfile ?? {}
      })
    });
  }

  async stats(): Promise<Record<string, unknown>> {
    return await this.requestJson<Record<string, unknown>>("/stats");
  }

  async rebuild(): Promise<void> {
    await this.requestJson<Record<string, unknown>>("/index/rebuild", { method: "POST" }, 0);
  }
}

