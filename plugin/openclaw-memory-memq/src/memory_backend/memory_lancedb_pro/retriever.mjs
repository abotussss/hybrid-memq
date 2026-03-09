import { filterNoise } from "./noise-filter.mjs";
import { normalizeQuery, shouldSkipRetrieval } from "./adaptive-retrieval.mjs";

function clamp01(value, fallback = 0) {
  if (!Number.isFinite(value)) return fallback;
  return Math.min(1, Math.max(0, value));
}

function jaccardFingerprint(text) {
  const clean = String(text || "").toLowerCase().replace(/\s+/g, " ").trim();
  if (!clean) return new Set();
  const grams = new Set();
  for (let i = 0; i < Math.max(1, clean.length - 2); i += 1) grams.add(clean.slice(i, i + 3));
  return grams;
}

function jaccard(a, b) {
  if (!a.size || !b.size) return 0;
  let intersection = 0;
  for (const item of a) if (b.has(item)) intersection += 1;
  return intersection / (a.size + b.size - intersection || 1);
}

export class MemoryRetriever {
  constructor(store, embedder, config = {}) {
    this.store = store;
    this.embedder = embedder;
    this.config = {
      candidatePoolSize: 20,
      minScore: 0.15,
      hardMinScore: 0.2,
      recencyHalfLifeDays: 14,
      recencyWeight: 0.1,
      importanceWeight: 0.15,
      confidenceWeight: 0.1,
      strengthWeight: 0.1,
      lengthNormAnchor: 500,
      ...config,
    };
  }

  applyRecency(score, entry) {
    const ageDays = Math.max(0, Date.now() / 1000 - Number(entry.timestamp || 0)) / 86400;
    const halfLife = Math.max(1, Number(this.config.recencyHalfLifeDays || 14));
    const boost = Math.exp(-ageDays / halfLife) * Number(this.config.recencyWeight || 0.1);
    return score + boost;
  }

  applyMetadata(score, entry, factKeys) {
    let out = score;
    out += clamp01(Number(entry.importance || 0), 0) * Number(this.config.importanceWeight || 0.15);
    out += clamp01(Number(entry.confidence || 0), 0) * Number(this.config.confidenceWeight || 0.1);
    out += clamp01(Number(entry.strength || 0), 0) * Number(this.config.strengthWeight || 0.1);
    if (factKeys?.length && entry.fact_key && factKeys.includes(entry.fact_key)) out += 0.25;
    const len = String(entry.summary || entry.text || "").length || 1;
    const anchor = Math.max(100, Number(this.config.lengthNormAnchor || 500));
    const norm = 1 / (1 + Math.max(0, Math.log2(len / anchor)));
    return out * norm;
  }

  dedupe(results, limit) {
    const out = [];
    const seen = [];
    for (const result of results) {
      const fp = jaccardFingerprint(result.entry.fact_key || result.entry.summary || result.entry.text);
      if (seen.some((other) => jaccard(other, fp) > 0.9)) continue;
      seen.push(fp);
      out.push(result);
      if (out.length >= limit) break;
    }
    return out;
  }

  async retrieve({ query, limit, scopeFilter, layer, kinds, factKeys }) {
    const normalizedQuery = normalizeQuery(query || "");
    if (!factKeys?.length && shouldSkipRetrieval(normalizedQuery)) return [];
    const safeLimit = Math.max(1, Math.min(Number(limit || 5), 20));
    const candidatePool = Math.max(safeLimit * 4, Number(this.config.candidatePoolSize || 20));
    const vector = normalizedQuery ? this.embedder.embedQuery(normalizedQuery) : [];
    const [vectorResults, bm25Results] = await Promise.all([
      normalizedQuery ? this.store.vectorSearch(vector, candidatePool, scopeFilter, layer, kinds) : [],
      normalizedQuery ? this.store.bm25Search(normalizedQuery, candidatePool, scopeFilter, layer, kinds) : [],
    ]);

    const map = new Map();
    const rrf = (rank) => 1 / (60 + rank);

    vectorResults.forEach((result, index) => {
      const prev = map.get(result.entry.id) || { ...result, score: 0, sources: {} };
      prev.score += rrf(index + 1) * 0.7;
      prev.sources.vector = { score: result.score, rank: index + 1 };
      map.set(result.entry.id, prev);
    });

    bm25Results.forEach((result, index) => {
      const prev = map.get(result.entry.id) || { ...result, score: 0, sources: {} };
      prev.score += rrf(index + 1) * 0.3;
      prev.sources.bm25 = { score: result.score, rank: index + 1 };
      map.set(result.entry.id, prev);
    });

    const filtered = filterNoise([...map.values()], (item) => item.entry.keywords || item.entry.summary || item.entry.text)
      .map((result) => ({
        ...result,
        score: this.applyMetadata(this.applyRecency(result.score, result.entry), result.entry, factKeys),
      }))
      .filter((result) => result.score >= Number(this.config.hardMinScore || 0.2))
      .sort((a, b) => b.score - a.score);

    return this.dedupe(filtered, safeLimit);
  }
}
