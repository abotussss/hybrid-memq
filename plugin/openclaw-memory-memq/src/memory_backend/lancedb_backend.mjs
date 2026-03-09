import { createHash } from "node:crypto";
import { HashEmbedder, normalizeText } from "./memory_lancedb_pro/embedder.mjs";
import { MemoryStore } from "./memory_lancedb_pro/store.mjs";
import { MemoryRetriever } from "./memory_lancedb_pro/retriever.mjs";

function readStdin() {
  return new Promise((resolve, reject) => {
    let data = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => (data += chunk));
    process.stdin.on("end", () => resolve(data));
    process.stdin.on("error", reject);
  });
}

function numericId(value) {
  const digest = createHash("sha256").update(String(value || "")).digest("hex");
  return parseInt(digest.slice(0, 8), 16);
}

function escapeSqlLiteral(value) {
  return String(value || "").replace(/'/g, "''");
}

function createBackend(dbPath) {
  const embedder = new HashEmbedder(64);
  const store = new MemoryStore({ dbPath, vectorDim: 64, tableName: "memq_memories" });
  const retriever = new MemoryRetriever(store, embedder);
  return { store, retriever };
}

async function ingest(payload) {
  const { store } = createBackend(payload.dbPath);
  const rows = await store.storeMany(payload.entries || []);
  return { ok: true, stored: rows.length, backend: "memory-lancedb-pro" };
}

async function query(payload) {
  const sessionKey = String(payload.sessionKey || "default");
  const scopes = payload.includeGlobal === false ? [sessionKey] : [sessionKey, "global"];
  const queryText = normalizeText([...(payload.queries || []), ...(payload.factKeys || [])].join(" "));
  const limit = Math.max(1, Number(payload.limit || 5));
  const kinds = Array.isArray(payload.kinds) ? payload.kinds.map((item) => String(item || "")) : [];
  const { retriever } = createBackend(payload.dbPath);
  const results = await retriever.retrieve({
    query: queryText,
    limit,
    scopeFilter: scopes,
    layer: payload.layer,
    kinds,
    factKeys: payload.factKeys || [],
  });
  const items = results.map((result) => ({
    numeric_id: numericId(result.entry.id),
    id: Number(result.entry.id) || 0,
    session_key: String(result.entry.session_key || ""),
    layer: String(result.entry.layer || ""),
    kind: String(result.entry.kind || ""),
    fact_key: String(result.entry.fact_key || ""),
    value: String(result.entry.value || ""),
    text: String(result.entry.text || ""),
    summary: String(result.entry.summary || ""),
    confidence: Number(result.entry.confidence || 0),
    importance: Number(result.entry.importance || 0),
    strength: Number(result.entry.strength || 0),
    updated_at: Number(result.entry.timestamp || 0),
    score: Number(result.score || 0),
  }));
  return { ok: true, items, backend: "memory-lancedb-pro" };
}

async function list(payload) {
  const sessionKey = String(payload.sessionKey || "default");
  const scopes = payload.includeGlobal === false ? [sessionKey] : [sessionKey, "global"];
  const limit = Math.max(1, Number(payload.limit || 50));
  const kinds = Array.isArray(payload.kinds) ? payload.kinds.map((item) => String(item || "")) : [];
  const layer = payload.layer ? String(payload.layer) : "";
  const factKeyPrefixes = Array.isArray(payload.factKeyPrefixes)
    ? payload.factKeyPrefixes.map((item) => String(item || ""))
    : [];
  const { store } = createBackend(payload.dbPath);
  await store.ensureInitialized();
  let query = store.table.query().limit(Math.max(limit * 4, limit));
  const clauses = [];
  if (scopes.length) {
    clauses.push(`(${scopes.map((scope) => `session_key = '${escapeSqlLiteral(scope)}'`).join(" OR ")})`);
  }
  if (layer) clauses.push(`layer = '${escapeSqlLiteral(layer)}'`);
  if (kinds.length) clauses.push(`(${kinds.map((kind) => `kind = '${escapeSqlLiteral(kind)}'`).join(" OR ")})`);
  if (factKeyPrefixes.length) {
    clauses.push(`(${factKeyPrefixes.map((prefix) => `fact_key LIKE '${escapeSqlLiteral(prefix)}%'`).join(" OR ")})`);
  }
  if (clauses.length) query = query.where(clauses.join(" AND "));
  const rows = await query.toArray().catch(() => []);
  rows.sort((left, right) => Number(right.timestamp || 0) - Number(left.timestamp || 0));
  const items = rows.slice(0, limit).map((row) => ({
    id: String(row.id || ""),
    session_key: String(row.session_key || ""),
    layer: String(row.layer || ""),
    kind: String(row.kind || ""),
    fact_key: String(row.fact_key || ""),
    value: String(row.value || ""),
    text: String(row.text || ""),
    summary: String(row.summary || ""),
    importance: Number(row.importance || 0),
    confidence: Number(row.confidence || 0),
    strength: Number(row.strength || 0),
    timestamp: Number(row.timestamp || 0),
  }));
  return { ok: true, items, backend: "memory-lancedb-pro" };
}

async function main() {
  const command = process.argv[2];
  const raw = await readStdin();
  const payload = raw ? JSON.parse(raw) : {};
  if (command === "ingest") {
    console.log(JSON.stringify(await ingest(payload)));
    return;
  }
  if (command === "query") {
    console.log(JSON.stringify(await query(payload)));
    return;
  }
  if (command === "list") {
    console.log(JSON.stringify(await list(payload)));
    return;
  }
  console.error(`unknown command: ${command || ""}`);
  process.exit(2);
}

main().catch((error) => {
  console.error(error?.stack || String(error));
  process.exit(1);
});
