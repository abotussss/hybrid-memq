import { randomUUID } from "node:crypto";
import { existsSync, accessSync, constants, mkdirSync, realpathSync, lstatSync } from "node:fs";
import { dirname } from "node:path";
import * as lancedb from "@lancedb/lancedb";
import { HashEmbedder, buildSearchDocument, normalizeText } from "./embedder.mjs";

function clampInt(value, min, max) {
  if (!Number.isFinite(value)) return min;
  return Math.min(max, Math.max(min, Math.floor(value)));
}

function escapeSqlLiteral(value) {
  return String(value || "").replace(/'/g, "''");
}

export function validateStoragePath(dbPath) {
  let resolvedPath = dbPath;
  try {
    const stats = lstatSync(dbPath);
    if (stats.isSymbolicLink()) resolvedPath = realpathSync(dbPath);
  } catch (err) {
    if (err?.code !== "ENOENT") throw err;
  }
  if (!existsSync(resolvedPath)) mkdirSync(resolvedPath, { recursive: true });
  accessSync(resolvedPath, constants.W_OK);
  return resolvedPath;
}

export class MemoryStore {
  constructor(config) {
    this.config = config;
    this.db = null;
    this.table = null;
    this.initPromise = null;
    this.ftsIndexCreated = false;
    this.embedder = new HashEmbedder(config.vectorDim || 64);
  }

  get hasFtsSupport() {
    return this.ftsIndexCreated;
  }

  async ensureInitialized() {
    if (this.table) return;
    if (!this.initPromise) this.initPromise = this.doInitialize();
    return this.initPromise;
  }

  async doInitialize() {
    validateStoragePath(this.config.dbPath);
    this.db = await lancedb.connect(this.config.dbPath);
    try {
      this.table = await this.db.openTable(this.config.tableName || "memq_memories");
    } catch {
      this.table = await this.db.createTable(this.config.tableName || "memq_memories", [
        {
          id: "__schema__",
          session_key: "global",
          layer: "deep",
          kind: "fact",
          fact_key: "",
          value: "",
          text: "",
          summary: "",
          searchable_text: "",
          importance: 0,
          confidence: 0,
          strength: 0,
          timestamp: 0,
          vector: new Array(this.config.vectorDim || 64).fill(0),
        },
      ]);
      await this.table.delete(`id = "__schema__"`);
    }
    try {
      const indices = await this.table.listIndices();
      const hasFts = indices?.some((idx) => idx.indexType === "FTS" || idx.columns?.includes("searchable_text"));
      if (!hasFts) {
        await this.table.createIndex("searchable_text", { config: lancedb.Index.fts() });
      }
      this.ftsIndexCreated = true;
    } catch {
      this.ftsIndexCreated = false;
    }
  }

  toRow(entry) {
    const text = normalizeText(entry.text || "");
    const summary = normalizeText(entry.summary || entry.text || entry.value || "");
    const searchable_text = normalizeText(buildSearchDocument({ ...entry, text, summary }));
    return {
      id: entry.id || randomUUID(),
      session_key: entry.session_key || "global",
      layer: entry.layer || "deep",
      kind: entry.kind || "fact",
      fact_key: entry.fact_key || "",
      value: entry.value || "",
      text,
      summary,
      searchable_text,
      importance: Number(entry.importance || 0),
      confidence: Number(entry.confidence || 0),
      strength: Number(entry.strength || 0),
      timestamp: Number(entry.updated_at || entry.created_at || Date.now() / 1000),
      vector: this.embedder.embedDocument(searchable_text),
    };
  }

  async storeMany(entries) {
    await this.ensureInitialized();
    const rows = (entries || []).map((entry) => this.toRow(entry));
    if (rows.length) await this.table.add(rows);
    return rows;
  }

  async vectorSearch(vector, limit = 5, scopeFilter, layer) {
    await this.ensureInitialized();
    const safeLimit = clampInt(limit, 1, 50);
    let query = this.table.vectorSearch(vector).limit(Math.max(safeLimit * 6, 24));
    const clauses = [];
    if (scopeFilter?.length) {
      clauses.push(`(${scopeFilter.map((scope) => `session_key = '${escapeSqlLiteral(scope)}'`).join(" OR ")})`);
    }
    if (layer) clauses.push(`layer = '${escapeSqlLiteral(layer)}'`);
    if (clauses.length) query = query.where(clauses.join(" AND "));
    const results = await query.toArray().catch(() => []);
    return results.map((row) => ({
          entry: {
            id: String(row.id),
            session_key: String(row.session_key || ""),
            layer: String(row.layer || ""),
            kind: String(row.kind || ""),
            fact_key: String(row.fact_key || ""),
            value: String(row.value || ""),
            text: String(row.text || ""),
            summary: String(row.summary || ""),
            keywords: String(row.searchable_text || row.keywords || ""),
            importance: Number(row.importance || 0),
            confidence: Number(row.confidence || 0),
            strength: Number(row.strength || 0),
        timestamp: Number(row.timestamp || 0),
        vector: Array.from(row.vector || []),
      },
      score: 1 / (1 + Number(row._distance ?? 0)),
    }));
  }

  async bm25Search(queryText, limit = 5, scopeFilter, layer) {
    await this.ensureInitialized();
    if (!this.ftsIndexCreated) return [];
    const safeLimit = clampInt(limit, 1, 50);
      let query = this.table.search(queryText, "fts").limit(Math.max(safeLimit * 6, 24));
    const clauses = [];
    if (scopeFilter?.length) {
      clauses.push(`(${scopeFilter.map((scope) => `session_key = '${escapeSqlLiteral(scope)}'`).join(" OR ")})`);
    }
    if (layer) clauses.push(`layer = '${escapeSqlLiteral(layer)}'`);
    if (clauses.length) query = query.where(clauses.join(" AND "));
    const results = await query.toArray().catch(() => []);
    return results.map((row) => ({
          entry: {
            id: String(row.id),
            session_key: String(row.session_key || ""),
            layer: String(row.layer || ""),
            kind: String(row.kind || ""),
            fact_key: String(row.fact_key || ""),
            value: String(row.value || ""),
            text: String(row.text || ""),
            summary: String(row.summary || ""),
            keywords: String(row.searchable_text || row.keywords || ""),
            importance: Number(row.importance || 0),
            confidence: Number(row.confidence || 0),
            strength: Number(row.strength || 0),
        timestamp: Number(row.timestamp || 0),
        vector: Array.from(row.vector || []),
      },
      score: Number.isFinite(Number(row._score)) ? 1 / (1 + Math.exp(-Number(row._score) / 5)) : 0.5,
    }));
  }
}
