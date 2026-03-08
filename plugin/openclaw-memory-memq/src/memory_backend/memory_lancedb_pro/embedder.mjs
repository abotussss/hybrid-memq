import { createHash } from "node:crypto";

export function normalizeText(text) {
  return String(text || "").replace(/\0/g, " ").replace(/\s+/g, " ").trim();
}

export function tokenizeText(text) {
  return (
    normalizeText(text)
      .toLowerCase()
      .match(/[0-9a-z\u3040-\u30ff\u3400-\u9fff_-]+/g) || []
  );
}

export function characterNgrams(text, n = 3) {
  const clean = normalizeText(text).replace(/\s+/g, "");
  if (!clean) return [];
  if (clean.length <= n) return [clean];
  const out = [];
  for (let i = 0; i <= clean.length - n; i += 1) out.push(clean.slice(i, i + n));
  return out;
}

export function buildSearchDocument(entry) {
  return normalizeText(
    [
      entry.summary,
      entry.text,
      entry.fact_key ? `${entry.fact_key}:${entry.value || ""}` : "",
      ...(entry.keywords || []),
      ...characterNgrams(entry.summary || entry.text || "", 3),
    ].join(" "),
  );
}

export class HashEmbedder {
  constructor(dim = 64) {
    this.dim = dim;
  }

  embed(text) {
    const vec = new Array(this.dim).fill(0);
    const source = [...tokenizeText(text), ...characterNgrams(text, 3)];
    if (!source.length) return vec;
    for (const tok of source) {
      const digest = createHash("sha256").update(tok).digest();
      const idx = digest[0] % this.dim;
      const sign = digest[1] % 2 === 0 ? 1 : -1;
      vec[idx] += sign;
    }
    const norm = Math.sqrt(vec.reduce((acc, value) => acc + value * value, 0)) || 1;
    return vec.map((value) => value / norm);
  }

  embedQuery(text) {
    return this.embed(text);
  }

  embedDocument(text) {
    return this.embed(text);
  }
}
