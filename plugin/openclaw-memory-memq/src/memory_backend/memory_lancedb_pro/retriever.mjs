import { filterNoise } from "./noise-filter.mjs";
import { normalizeQuery, shouldSkipRetrieval } from "./adaptive-retrieval.mjs";
import { selectFinalTopKSetwise } from "./final-selection.mjs";
import { expandQuery } from "./query-expander.mjs";

function clamp01(value, fallback = 0) {
  if (!Number.isFinite(value)) return fallback;
  return Math.min(1, Math.max(0, value));
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
      timeDecayHalfLifeDays: 60,
      sameKeyMultiplier: 0.12,
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

  applyTimeDecay(score, entry) {
    const halfLifeDays = Math.max(0, Number(this.config.timeDecayHalfLifeDays || 0));
    if (!halfLifeDays) return score;
    const ageDays = Math.max(0, Date.now() / 1000 - Number(entry.timestamp || 0)) / 86400;
    const multiplier = 0.5 + 0.5 * Math.exp(-ageDays / Math.max(1, halfLifeDays));
    return score * multiplier;
  }

  selectResults(results, limit) {
    const final = selectFinalTopKSetwise(
      results.map((result) => {
        const entry = result.entry || {};
        const text = String(entry.summary || entry.text || entry.value || "");
        return {
          id: String(entry.id || ""),
          text,
          baseScore: Number(result.score || 0),
          ts: Number(entry.timestamp || 0) * 1000,
          normalizedKey: String(entry.fact_key || ""),
          softKey: String(entry.fact_key || entry.summary || entry.text || ""),
          category: String(entry.kind || ""),
          scope: String(entry.session_key || ""),
          sourceType: String(entry.layer || ""),
          embedding: Array.isArray(entry.vector) ? entry.vector : [],
          raw: result,
        };
      }),
      {
        finalLimit: limit,
        shortlistLimit: Math.max(limit * 5, Number(this.config.candidatePoolSize || 20)),
        freshnessHalfLifeMs: Math.max(1, Number(this.config.recencyHalfLifeDays || 14)) * 86400000,
        penalties: {
          sameKeyMultiplier: Number(this.config.sameKeyMultiplier || 0.12),
        },
      },
    );
    return final.map((item) => item.raw);
  }

  async retrieve({ query, limit, scopeFilter, layer, kinds, factKeys }) {
    const normalizedQuery = normalizeQuery(query || "");
    const expandedQuery = expandQuery(normalizedQuery);
    if (!factKeys?.length && shouldSkipRetrieval(normalizedQuery)) return [];
    const safeLimit = Math.max(1, Math.min(Number(limit || 5), 20));
    const candidatePool = Math.max(safeLimit * 4, Number(this.config.candidatePoolSize || 20));
    const vector = expandedQuery ? this.embedder.embedQuery(expandedQuery) : [];
    const [vectorResults, bm25Results] = await Promise.all([
      expandedQuery ? this.store.vectorSearch(vector, candidatePool, scopeFilter, layer, kinds) : [],
      expandedQuery ? this.store.bm25Search(expandedQuery, candidatePool, scopeFilter, layer, kinds) : [],
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
        score: this.applyTimeDecay(
          this.applyMetadata(this.applyRecency(result.score, result.entry), result.entry, factKeys),
          result.entry,
        ),
      }))
      .filter((result) => result.score >= Number(this.config.hardMinScore || 0.2))
      .sort((a, b) => b.score - a.score);

    return this.selectResults(filtered, safeLimit);
  }
}
