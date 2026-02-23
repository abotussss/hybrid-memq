export interface FallbackInput {
  topScores: number[];
  maxScoreMin: number;
  entropyMax: number;
  unresolvedCriticalConflict: boolean;
}

function entropy(scores: number[]): number {
  if (!scores.length) return 0;
  const shifted = scores.map((s) => Math.exp(s));
  const z = shifted.reduce((a, b) => a + b, 0);
  if (z === 0) return 0;
  const p = shifted.map((x) => x / z);
  return -p.reduce((acc, x) => (x <= 0 ? acc : acc + x * Math.log(x)), 0);
}

export function shouldFallback(i: FallbackInput): boolean {
  const max = i.topScores.length ? Math.max(...i.topScores) : 0;
  const h = entropy(i.topScores);
  return max < i.maxScoreMin || h > i.entropyMax || i.unresolvedCriticalConflict;
}
