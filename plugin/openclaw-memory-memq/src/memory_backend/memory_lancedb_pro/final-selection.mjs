function normalizeLimit(value, fallback) {
  if (!Number.isFinite(value)) return Math.max(1, Math.floor(fallback || 1));
  return Math.max(1, Math.floor(value));
}

function clampMultiplier(value) {
  if (!Number.isFinite(value)) return 1;
  return Math.max(0.02, Math.min(1.5, value));
}

function sanitizeTimestamp(ts, now) {
  const n = Number(ts || 0);
  if (!Number.isFinite(n) || n <= 0) return now;
  return n;
}

function normalizeKey(candidate) {
  return String(
    candidate.normalizedKey ||
    candidate.softKey ||
    candidate.id ||
    candidate.text ||
    ""
  ).trim().toLowerCase();
}

function tokenizeForOverlap(text, minLength = 3) {
  const tokens = String(text || "")
    .toLowerCase()
    .match(/[0-9a-z\u3040-\u30ff\u3400-\u9fff_-]+/g);
  if (!tokens) return [];
  return tokens.filter((token) => token.length >= minLength);
}

function jaccardSimilarity(left, right) {
  if (!left?.size || !right?.size) return 0;
  let overlap = 0;
  for (const token of left) if (right.has(token)) overlap += 1;
  return overlap / Math.max(1, left.size + right.size - overlap);
}

function cosineSimilarity(left, right) {
  if (!Array.isArray(left) || !Array.isArray(right) || !left.length || !right.length || left.length !== right.length) {
    return null;
  }
  let dot = 0;
  let leftNorm = 0;
  let rightNorm = 0;
  for (let index = 0; index < left.length; index += 1) {
    const a = Number(left[index] || 0);
    const b = Number(right[index] || 0);
    dot += a * b;
    leftNorm += a * a;
    rightNorm += b * b;
  }
  if (!leftNorm || !rightNorm) return null;
  return dot / Math.sqrt(leftNorm * rightNorm);
}

function computeFreshnessScore(ts, now, halfLifeMs) {
  if (!Number.isFinite(ts) || !Number.isFinite(now)) return 0;
  const halfLife = Number.isFinite(halfLifeMs) && halfLifeMs > 0 ? halfLifeMs : 14 * 86400000;
  const age = Math.max(0, now - ts);
  return Math.exp(-age / halfLife);
}

function buildShortlist(candidates, { shortlistLimit, finalLimit }) {
  const finalCap = Math.min(candidates.length, normalizeLimit(finalLimit, candidates.length));
  const shortlistCap = Math.min(
    candidates.length,
    normalizeLimit(shortlistLimit, Math.max(finalCap, finalCap * 4)),
  );
  return [...candidates]
    .sort((left, right) => {
      const leftScore = Number(left.baseScore || 0);
      const rightScore = Number(right.baseScore || 0);
      if (rightScore !== leftScore) return rightScore - leftScore;
      return Number(right.ts || 0) - Number(left.ts || 0);
    })
    .slice(0, shortlistCap);
}

export function selectFinalTopKSetwise(candidates, config = {}) {
  if (!Array.isArray(candidates) || !candidates.length) return [];
  const finalLimit = Math.min(candidates.length, normalizeLimit(config.finalLimit, candidates.length));
  const weights = {
    relevance: 1,
    freshness: 0.15,
    categoryCoverage: 0.06,
    scopeCoverage: 0.04,
    ...(config.weights || {}),
  };
  const penalties = {
    sameKeyMultiplier: 0.12,
    overlapThresholds: [
      { minOverlap: 0.92, multiplier: 0.08 },
      { minOverlap: 0.80, multiplier: 0.24 },
      { minOverlap: 0.68, multiplier: 0.5 },
    ],
    semanticThresholds: [
      { minSimilarity: 0.97, multiplier: 0.08 },
      { minSimilarity: 0.92, multiplier: 0.2 },
    ],
    ...(config.penalties || {}),
  };
  const now = Number.isFinite(config.now) ? Number(config.now) : Date.now();
  const prepared = buildShortlist(candidates, config).map((candidate, stableRank) => ({
    candidate,
    stableRank,
    ts: sanitizeTimestamp(candidate.ts, now),
    key: normalizeKey(candidate),
    overlapTokens: tokenizeForOverlap(candidate.softKey || candidate.text || candidate.normalizedKey || ""),
    embedding: Array.isArray(candidate.embedding) ? candidate.embedding : [],
  }));

  const selected = [];
  const selectedKeys = new Set();
  const selectedCategories = new Set();
  const selectedScopes = new Set();
  const selectedTokenSets = [];
  const selectedEmbeddings = [];

  while (prepared.length && selected.length < finalLimit) {
    let bestIndex = 0;
    let bestScore = Number.NEGATIVE_INFINITY;
    for (let index = 0; index < prepared.length; index += 1) {
      const row = prepared[index];
      const baseScore = Number(row.candidate.baseScore || 0);
      const freshness = computeFreshnessScore(row.ts, now, config.freshnessHalfLifeMs);
      let utility = baseScore * weights.relevance + freshness * weights.freshness;
      if (row.candidate.category && !selectedCategories.has(row.candidate.category)) utility += weights.categoryCoverage;
      if (row.candidate.scope && !selectedScopes.has(row.candidate.scope)) utility += weights.scopeCoverage;

      let multiplier = 1;
      if (row.key && selectedKeys.has(row.key)) multiplier *= clampMultiplier(penalties.sameKeyMultiplier);
      if (row.overlapTokens.length && selectedTokenSets.length) {
        const candidateSet = new Set(row.overlapTokens);
        let maxOverlap = 0;
        for (const selectedSet of selectedTokenSets) {
          maxOverlap = Math.max(maxOverlap, jaccardSimilarity(candidateSet, selectedSet));
        }
        for (const threshold of penalties.overlapThresholds || []) {
          if (maxOverlap >= Number(threshold.minOverlap || 1)) {
            multiplier *= clampMultiplier(threshold.multiplier);
            break;
          }
        }
      }
      if (row.embedding.length && selectedEmbeddings.length) {
        let maxSimilarity = -1;
        for (const embedding of selectedEmbeddings) {
          const similarity = cosineSimilarity(row.embedding, embedding);
          if (similarity !== null) maxSimilarity = Math.max(maxSimilarity, similarity);
        }
        if (maxSimilarity >= 0) {
          for (const threshold of penalties.semanticThresholds || []) {
            if (maxSimilarity >= Number(threshold.minSimilarity || 1)) {
              multiplier *= clampMultiplier(threshold.multiplier);
              break;
            }
          }
        }
      }

      const adjusted = utility * multiplier;
      if (adjusted > bestScore || (Math.abs(adjusted - bestScore) <= 1e-12 && row.stableRank < prepared[bestIndex].stableRank)) {
        bestScore = adjusted;
        bestIndex = index;
      }
    }

    const [chosen] = prepared.splice(bestIndex, 1);
    selected.push(chosen.candidate);
    if (chosen.key) selectedKeys.add(chosen.key);
    if (chosen.candidate.category) selectedCategories.add(chosen.candidate.category);
    if (chosen.candidate.scope) selectedScopes.add(chosen.candidate.scope);
    if (chosen.overlapTokens.length) selectedTokenSets.push(new Set(chosen.overlapTokens));
    if (chosen.embedding.length) selectedEmbeddings.push(chosen.embedding);
  }

  return selected;
}
