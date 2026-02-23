import type { MemoryTrace } from "./types.js";

export interface WriteGateWeights {
  a: number;
  b: number;
  c: number;
  d: number;
  e: number;
}

export interface CandidateFeatures {
  utility: number;
  novelty: number;
  stability: number;
  explicitness: number;
  redundancy: number;
  type: MemoryTrace["type"];
}

export const DEFAULT_WRITE_WEIGHTS: WriteGateWeights = {
  a: 0.9,
  b: 0.8,
  c: 0.5,
  d: 1.0,
  e: 0.9
};

export function writeScore(f: CandidateFeatures, w: WriteGateWeights = DEFAULT_WRITE_WEIGHTS): number {
  return w.a * f.utility + w.b * f.novelty + w.c * f.stability + w.d * f.explicitness - w.e * f.redundancy;
}

export function shouldWriteDeep(
  f: CandidateFeatures,
  thresholdLow: number,
  thresholdHigh: number,
  w: WriteGateWeights = DEFAULT_WRITE_WEIGHTS
): boolean {
  const s = writeScore(f, w);
  const lowTypes: Array<MemoryTrace["type"]> = ["preference", "constraint", "identity"];
  return lowTypes.includes(f.type) ? s > thresholdLow : s > thresholdHigh;
}
