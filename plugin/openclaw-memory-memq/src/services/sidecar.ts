import type { MemoryType, VolatilityClass } from "../memq_core.js";
import type { SidecarSearchResult } from "../types.js";

export class SidecarClient {
  constructor(private readonly baseUrl: string) {}

  async health(): Promise<boolean> {
    try {
      const r = await fetch(`${this.baseUrl}/health`);
      return r.ok;
    } catch {
      return false;
    }
  }

  async embed(text: string): Promise<number[]> {
    const r = await fetch(`${this.baseUrl}/embed`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ text })
    });
    if (!r.ok) throw new Error("embed failed");
    const j = (await r.json()) as { vector: number[] };
    return j.vector;
  }

  async search(vector: number[], k: number): Promise<SidecarSearchResult[]> {
    const r = await fetch(`${this.baseUrl}/index/search`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ vector, k })
    });
    if (!r.ok) throw new Error("search failed");
    const j = (await r.json()) as { items: SidecarSearchResult[] };
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
  }): Promise<void> {
    const r = await fetch(`${this.baseUrl}/index/add`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(item)
    });
    if (!r.ok) throw new Error("add failed");
  }

  async touch(ids: string[]): Promise<void> {
    if (!ids.length) return;
    const r = await fetch(`${this.baseUrl}/index/touch`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ ids })
    });
    if (!r.ok) throw new Error("touch failed");
  }

  async consolidate(nowSec: number): Promise<void> {
    const r = await fetch(`${this.baseUrl}/consolidate`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ nowSec })
    });
    if (!r.ok) throw new Error("consolidate failed");
  }

  async idleTick(nowSec: number): Promise<void> {
    const r = await fetch(`${this.baseUrl}/idle_tick`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ nowSec })
    });
    if (!r.ok) throw new Error("idle_tick failed");
  }

  async profile(): Promise<{
    preferences: Array<{ key: string; value: string; confidence: number; updatedAt: number }>;
    memoryPolicies: Array<{ key: string; value: string; confidence: number; updatedAt: number }>;
  }> {
    const r = await fetch(`${this.baseUrl}/profile`);
    if (!r.ok) throw new Error("profile failed");
    return (await r.json()) as {
      preferences: Array<{ key: string; value: string; confidence: number; updatedAt: number }>;
      memoryPolicies: Array<{ key: string; value: string; confidence: number; updatedAt: number }>;
    };
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
    const r = await fetch(`${this.baseUrl}/preference/event`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ events })
    });
    if (!r.ok) throw new Error("push preference events failed");
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
    const r = await fetch(`${this.baseUrl}/audit/output`, {
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
    if (!r.ok) throw new Error("audit output failed");
    return (await r.json()) as {
      ok: boolean;
      passed: boolean;
      riskScore: number;
      reasons: string[];
      repairedApplied?: boolean;
      repairedText?: string;
      secondary?: { enabled?: boolean; called?: boolean; ok?: boolean; block?: boolean; risk?: number; reasons?: string[] };
    };
  }

  async stats(): Promise<Record<string, unknown>> {
    const r = await fetch(`${this.baseUrl}/stats`);
    if (!r.ok) throw new Error("stats failed");
    return (await r.json()) as Record<string, unknown>;
  }

  async rebuild(): Promise<void> {
    const r = await fetch(`${this.baseUrl}/index/rebuild`, { method: "POST" });
    if (!r.ok) throw new Error("rebuild failed");
  }
}
