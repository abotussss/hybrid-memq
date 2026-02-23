import type { MemoryTrace, VolatilityClass } from "./types.js";

export interface ForgettingParams {
  lambdaHigh: number;
  lambdaMedium: number;
  lambdaLow: number;
  betaUseEvent: number;
}

export const DEFAULT_FORGETTING: ForgettingParams = {
  lambdaHigh: 1.6e-5,
  lambdaMedium: 8e-6,
  lambdaLow: 2.5e-6,
  betaUseEvent: 0.15
};

export function lambdaOf(v: VolatilityClass, p: ForgettingParams = DEFAULT_FORGETTING): number {
  if (v === "high") return p.lambdaHigh;
  if (v === "medium") return p.lambdaMedium;
  return p.lambdaLow;
}

export function decayStrength(
  trace: MemoryTrace,
  nowSec: number,
  useEvent: 0 | 1,
  p: ForgettingParams = DEFAULT_FORGETTING
): number {
  const l = lambdaOf(trace.volatilityClass, p);
  const elapsed = Math.max(0, nowSec - (trace.updatedAtSec ?? trace.tsSec));
  const decayed = trace.strength * Math.exp(-l * elapsed);
  return Math.max(0, Math.min(1, decayed + p.betaUseEvent * useEvent));
}
