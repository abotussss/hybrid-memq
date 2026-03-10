import { existsSync, mkdirSync, symlinkSync } from "node:fs";
import { dirname, resolve } from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";
import createJiti from "jiti";

const HERE = fileURLToPath(new URL(".", import.meta.url));
const ROOT = resolve(HERE, "../../../../");
const PLUGIN_ROOT = resolve(HERE, "../../");
const PLUGIN_NODE_MODULES = resolve(PLUGIN_ROOT, "node_modules");
const VENDOR_ROOT = resolve(ROOT, "vendor/memory-lancedb-pro");
const VENDOR_SRC = resolve(ROOT, "vendor/memory-lancedb-pro/src");
const DEFAULT_DB_PATH = resolve(ROOT, ".memq/lancedb");

function ensureVendorNodeModules() {
  const target = resolve(VENDOR_ROOT, "node_modules");
  if (existsSync(target)) return;
  mkdirSync(dirname(target), { recursive: true });
  symlinkSync(PLUGIN_NODE_MODULES, target, "dir");
}

ensureVendorNodeModules();

const jiti = createJiti(import.meta.url, { interopDefault: true });

const { MemoryStore, validateStoragePath } = jiti(resolve(VENDOR_SRC, "store.ts"));
const { createEmbedder } = jiti(resolve(VENDOR_SRC, "embedder.ts"));
const { createRetriever, DEFAULT_RETRIEVAL_CONFIG } = jiti(resolve(VENDOR_SRC, "retriever.ts"));
const { AccessTracker } = jiti(resolve(VENDOR_SRC, "access-tracker.ts"));

const NOOP_LOGGER = {
  warn: () => {},
  info: () => {},
};

const VECTOR_DIM = Number(process.env.MEMQ_LANCEDB_EMBED_DIMENSIONS || 768);
const EMBED_BASE_URL = process.env.MEMQ_LANCEDB_EMBED_BASE_URL || "http://127.0.0.1:11434/v1";
const EMBED_MODEL = process.env.MEMQ_LANCEDB_EMBED_MODEL || "nomic-embed-text";
const EMBED_API_KEY = process.env.MEMQ_LANCEDB_EMBED_API_KEY || "ollama";

function stdinJson() {
  return new Promise((resolveJson, reject) => {
    let body = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => {
      body += chunk;
    });
    process.stdin.on("end", () => {
      try {
        resolveJson(JSON.parse(body || "{}"));
      } catch (error) {
        reject(error);
      }
    });
    process.stdin.on("error", reject);
  });
}

function ok(payload = {}) {
  process.stdout.write(JSON.stringify({ ok: true, backend: "memory-lancedb-pro", ...payload }));
}

function fail(error) {
  process.stdout.write(
    JSON.stringify({
      ok: false,
      backend: "memory-lancedb-pro",
      error: error instanceof Error ? error.message : String(error),
    }),
  );
  process.exitCode = 1;
}

function cleanString(value) {
  return String(value || "").trim();
}

function makeText(entry) {
  return cleanString(entry.text) || cleanString(entry.summary) || [cleanString(entry.fact_key), cleanString(entry.value)].filter(Boolean).join(":");
}

function mapKindToCategory(kind) {
  switch (String(kind || "").toLowerCase()) {
    case "style":
      return "preference";
    case "rule":
      return "decision";
    case "fact":
      return "fact";
    case "event":
      return "other";
    case "digest":
      return "reflection";
    default:
      return "other";
  }
}

function mapKindsToCategory(kinds) {
  const wanted = Array.from(new Set((Array.isArray(kinds) ? kinds : []).map((item) => String(item || "").toLowerCase()).filter(Boolean)));
  if (!wanted.length) return undefined;
  const mapped = Array.from(new Set(wanted.map((kind) => mapKindToCategory(kind))));
  return mapped.length === 1 ? mapped[0] : undefined;
}

function parseMetadata(metadata) {
  if (!metadata) return {};
  if (typeof metadata === "object") return metadata;
  try {
    return JSON.parse(String(metadata));
  } catch {
    return {};
  }
}

function makeRow(entry, score = 0) {
  const meta = parseMetadata(entry.metadata);
  return {
    id: entry.id,
    numeric_id: 0,
    session_key: String(meta.session_key || entry.scope || "global"),
    layer: String(meta.layer || ""),
    kind: String(meta.kind || ""),
    fact_key: String(meta.fact_key || ""),
    value: String(meta.value || ""),
    text: cleanString(meta.text) || cleanString(entry.text),
    summary: cleanString(meta.summary),
    confidence: Number(meta.confidence || 0),
    importance: Number(meta.importance || entry.importance || 0),
    strength: Number(meta.strength || 0),
    updated_at: Number(meta.updated_at || entry.timestamp || 0),
    timestamp: Number(entry.timestamp || meta.updated_at || 0),
    score: Number(score || 0),
  };
}

function rowAllowed(row, { sessionKey, includeGlobal, layer, kinds, factKeys, factKeyPrefixes }) {
  if (row.session_key !== sessionKey && !(includeGlobal && row.session_key === "global")) return false;
  if (layer && row.layer && row.layer !== layer) return false;
  if (Array.isArray(kinds) && kinds.length && row.kind && !kinds.includes(row.kind)) return false;
  if (Array.isArray(factKeys) && factKeys.length && row.fact_key && !factKeys.includes(row.fact_key)) return false;
  if (Array.isArray(factKeyPrefixes) && factKeyPrefixes.length && row.fact_key) {
    if (!factKeyPrefixes.some((prefix) => row.fact_key.startsWith(prefix))) return false;
  }
  return true;
}

async function createContext(dbPath) {
  const resolved = validateStoragePath(dbPath || DEFAULT_DB_PATH);
  const store = new MemoryStore({ dbPath: resolved, vectorDim: VECTOR_DIM });
  const embedder = createEmbedder({
    provider: "openai-compatible",
    apiKey: EMBED_API_KEY,
    model: EMBED_MODEL,
    baseURL: EMBED_BASE_URL,
    dimensions: VECTOR_DIM,
  });
  const retriever = createRetriever(store, embedder, {
    ...DEFAULT_RETRIEVAL_CONFIG,
    rerank: process.env.MEMQ_LANCEDB_RERANK || "lightweight",
    rerankApiKey: process.env.MEMQ_LANCEDB_RERANK_API_KEY || undefined,
    rerankEndpoint: process.env.MEMQ_LANCEDB_RERANK_ENDPOINT || undefined,
    rerankProvider: process.env.MEMQ_LANCEDB_RERANK_PROVIDER || undefined,
  });
  retriever.setAccessTracker(
    new AccessTracker({
      store,
      logger: NOOP_LOGGER,
    }),
  );
  return { store, embedder, retriever };
}

async function ingestCommand(payload) {
  const { store, embedder } = await createContext(payload.dbPath);
  const entries = Array.isArray(payload.entries) ? payload.entries : [];
  let stored = 0;
  for (const entry of entries) {
    const text = makeText(entry);
    if (!text) continue;
    const metadata = {
      session_key: cleanString(entry.session_key) || "global",
      layer: cleanString(entry.layer),
      kind: cleanString(entry.kind),
      fact_key: cleanString(entry.fact_key),
      value: cleanString(entry.value),
      text: cleanString(entry.text),
      summary: cleanString(entry.summary),
      confidence: Number(entry.confidence || 0),
      importance: Number(entry.importance || 0),
      strength: Number(entry.strength || 0),
      updated_at: Number(entry.updated_at || Date.now() / 1000),
    };
    const vector = await embedder.embedPassage(text);
    await store.store({
      text,
      vector,
      category: mapKindToCategory(entry.kind),
      scope: metadata.session_key || "global",
      importance: Number.isFinite(metadata.importance) ? metadata.importance : 0.5,
      metadata: JSON.stringify(metadata),
    });
    stored += 1;
  }
  ok({ stored });
}

async function queryCommand(payload) {
  const { retriever } = await createContext(payload.dbPath);
  const sessionKey = cleanString(payload.sessionKey) || "global";
  const queries = Array.isArray(payload.queries) ? payload.queries.map(cleanString).filter(Boolean) : [];
  const factKeys = Array.isArray(payload.factKeys) ? payload.factKeys.map(cleanString).filter(Boolean) : [];
  const limit = Math.max(1, Number(payload.limit || 5));
  const query = [...queries, ...factKeys].join(" ").trim() || "memory";
  const scopeFilter = payload.includeGlobal === false ? [sessionKey] : Array.from(new Set([sessionKey, "global"]));
  const category = mapKindsToCategory(payload.kinds);
  const results = await retriever.retrieve({
    query,
    limit: Math.max(limit * 4, limit),
    scopeFilter,
    category,
    source: "manual",
  });
  const items = [];
  for (const result of results) {
    const row = makeRow(result.entry, result.score);
    if (!rowAllowed(row, {
      sessionKey,
      includeGlobal: payload.includeGlobal !== false,
      layer: cleanString(payload.layer),
      kinds: Array.isArray(payload.kinds) ? payload.kinds.map(cleanString).filter(Boolean) : [],
      factKeys,
      factKeyPrefixes: [],
    })) {
      continue;
    }
    items.push(row);
    if (items.length >= limit) break;
  }
  ok({ items });
}

async function listCommand(payload) {
  const { store } = await createContext(payload.dbPath);
  const sessionKey = cleanString(payload.sessionKey) || "global";
  const includeGlobal = payload.includeGlobal !== false;
  const limit = Math.max(1, Number(payload.limit || 50));
  const category = mapKindsToCategory(payload.kinds);
  const scopeFilter = includeGlobal ? [sessionKey, "global"] : [sessionKey];
  const results = await store.list(scopeFilter, category, Math.max(limit * 4, limit), 0);
  const items = [];
  for (const entry of results) {
    const row = makeRow(entry, 0);
    if (!rowAllowed(row, {
      sessionKey,
      includeGlobal,
      layer: cleanString(payload.layer),
      kinds: Array.isArray(payload.kinds) ? payload.kinds.map(cleanString).filter(Boolean) : [],
      factKeys: [],
      factKeyPrefixes: Array.isArray(payload.factKeyPrefixes) ? payload.factKeyPrefixes.map(cleanString).filter(Boolean) : [],
    })) {
      continue;
    }
    items.push(row);
    if (items.length >= limit) break;
  }
  ok({ items });
}

async function main() {
  try {
    const command = process.argv[2];
    const payload = await stdinJson();
    if (command === "ingest") return await ingestCommand(payload);
    if (command === "query") return await queryCommand(payload);
    if (command === "list") return await listCommand(payload);
    throw new Error(`unknown command: ${command}`);
  } catch (error) {
    fail(error);
  }
}

await main();
