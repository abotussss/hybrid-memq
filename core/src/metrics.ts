export interface TurnMetrics {
  ts: number;
  mode: "api_text";
  injectedTokens: number;
  deepCalled: boolean;
  surfaceHits: number;
  latencyMs: number;
  fallbackUsed: boolean;
}

export class MetricsCollector {
  private turns: TurnMetrics[] = [];

  add(turn: TurnMetrics): void {
    this.turns.push(turn);
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
      surfaceHitRate: avg(this.turns.map((t) => Math.min(1, t.surfaceHits > 0 ? 1 : 0))),
      fallbackRate: avg(this.turns.map((t) => (t.fallbackUsed ? 1 : 0)))
    };
  }
}
