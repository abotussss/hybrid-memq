import type { MemoryTrace } from "./types.js";

export interface ActivationWeights {
  wS: number;
  wR: number;
  wF: number;
  wP: number;
  wC: number;
  tauRecencySec: number;
}

export interface ActivationInput {
  similarity: number;
  nowSec: number;
  trace: MemoryTrace;
  unresolvedConflict: boolean;
}

export const DEFAULT_WEIGHTS: ActivationWeights = {
  wS: 1.0,
  wR: 0.4,
  wF: 0.2,
  wP: 0.6,
  wC: 0.3,
  tauRecencySec: 2 * 24 * 60 * 60
};

export function alphaType(type: MemoryTrace["type"]): number {
  switch (type) {
    case "constraint":
    case "identity":
      return 1.0;
    case "preference":
    case "decision":
      return 0.9;
    case "plan":
      return 0.8;
    case "definition":
      return 0.7;
    case "episode":
      return 0.5;
    default:
      return 0.4;
  }
}

export function recency(nowSec: number, lastAccessSec: number, tauSec: number): number {
  const delta = Math.max(0, nowSec - lastAccessSec);
  return Math.exp(-delta / tauSec);
}

export function frequency(accessCount: number): number {
  return Math.log(1 + Math.max(0, accessCount));
}

export function activationScore(input: ActivationInput, w: ActivationWeights = DEFAULT_WEIGHTS): number {
  const r = recency(input.nowSec, input.trace.lastAccessAtSec ?? input.trace.tsSec, w.tauRecencySec);
  const f = frequency(input.trace.accessCount ?? 0);
  const p = alphaType(input.trace.type) * input.trace.importance;
  const c = input.unresolvedConflict ? 1 : 0;
  return w.wS * input.similarity + w.wR * r + w.wF * f + w.wP * p - w.wC * c;
}
