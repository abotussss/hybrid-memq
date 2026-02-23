import type { MemoryTrace } from "./types.js";
import { activationScore, type ActivationWeights, DEFAULT_WEIGHTS } from "./scoring.js";

export interface ScoredTrace {
  trace: MemoryTrace;
  similarity: number;
  score: number;
}

export function rankByActivation(
  traces: Array<{ trace: MemoryTrace; similarity: number; unresolvedConflict: boolean }>,
  nowSec: number,
  w: ActivationWeights = DEFAULT_WEIGHTS
): ScoredTrace[] {
  return traces
    .map((x) => ({
      trace: x.trace,
      similarity: x.similarity,
      score: activationScore({ similarity: x.similarity, nowSec, trace: x.trace, unresolvedConflict: x.unresolvedConflict }, w)
    }))
    .sort((a, b) => b.score - a.score);
}
