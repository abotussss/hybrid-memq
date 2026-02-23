import type { SidecarSearchResult } from "../types.js";

interface SurfaceEntry {
  id: string;
  at: number;
  data: SidecarSearchResult;
}

export class SurfaceCache {
  private readonly max: number;
  private readonly bySession = new Map<string, Map<string, SurfaceEntry>>();

  constructor(max: number) {
    this.max = max;
  }

  getTop(sessionId: string, limit: number): SidecarSearchResult[] {
    const m = this.bySession.get(sessionId);
    if (!m) return [];
    return [...m.values()]
      .sort((a, b) => b.at - a.at)
      .slice(0, limit)
      .map((x) => x.data);
  }

  touch(sessionId: string, items: SidecarSearchResult[]): void {
    const now = Date.now();
    const m = this.bySession.get(sessionId) ?? new Map<string, SurfaceEntry>();
    for (const item of items) {
      m.set(item.id, { id: item.id, at: now, data: item });
    }

    while (m.size > this.max) {
      let oldest: SurfaceEntry | undefined;
      for (const v of m.values()) {
        if (!oldest || v.at < oldest.at) oldest = v;
      }
      if (!oldest) break;
      m.delete(oldest.id);
    }

    this.bySession.set(sessionId, m);
  }
}

export class RuntimeMetrics {
  private readonly turns: Array<{
    ts: number;
    mode: "api_text";
    injectedTokens: number;
    deepCalled: boolean;
    surfaceHits: number;
    latencyMs: number;
    fallbackUsed: boolean;
  }> = [];

  add(turn: {
    mode: "api_text";
    injectedTokens: number;
    deepCalled: boolean;
    surfaceHits: number;
    latencyMs: number;
    fallbackUsed: boolean;
  }): void {
    this.turns.push({ ts: Date.now(), ...turn });
  }

  summary() {
    const n = this.turns.length || 1;
    const avg = (xs: number[]) => xs.reduce((a, b) => a + b, 0) / n;
    const sorted = [...this.turns].sort((a, b) => a.latencyMs - b.latencyMs);
    const p95 = sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * 0.95))]?.latencyMs ?? 0;
    return {
      turns: this.turns.length,
      avgInjectedTokens: avg(this.turns.map((t) => t.injectedTokens)),
      p95LatencyMs: p95,
      deepCallRate: avg(this.turns.map((t) => (t.deepCalled ? 1 : 0))),
      surfaceHitRate: avg(this.turns.map((t) => (t.surfaceHits > 0 ? 1 : 0))),
      fallbackRate: avg(this.turns.map((t) => (t.fallbackUsed ? 1 : 0)))
    };
  }
}
