import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import type { MemoryType, VolatilityClass } from "../memq_core.js";
import { shouldWriteDeep } from "../memq_core.js";
import { SidecarClient } from "./sidecar.js";

interface IngestOptions {
  workspaceRoot: string;
  writeThresholdLow: number;
  writeThresholdHigh: number;
}

interface FileState {
  mtimeMs: number;
  digest: string;
}

interface IngestState {
  files: Record<string, FileState>;
}

function inferType(chunk: string): MemoryType {
  const s = chunk.toLowerCase();
  if (/(must|don't|do not|禁止|必須)/.test(s)) return "constraint";
  if (/(prefer|like|口調|好み)/.test(s)) return "preference";
  if (/(i am|my name|私は)/.test(s)) return "identity";
  if (/(plan|todo|deadline|期限)/.test(s)) return "plan";
  return "note";
}

function volatilityFor(type: MemoryType): VolatilityClass {
  if (type === "constraint" || type === "identity") return "low";
  if (type === "preference" || type === "plan") return "medium";
  return "high";
}

function chunkMarkdown(text: string): string[] {
  const parts = text
    .split(/\n\s*\n/g)
    .map((s) => s.trim())
    .filter(Boolean);

  return parts.flatMap((p) => {
    if (p.length <= 520) return [p];
    const out: string[] = [];
    for (let i = 0; i < p.length; i += 420) out.push(p.slice(i, i + 420));
    return out;
  });
}

function extractFacts(chunk: string): Array<{ k: string; v: string; conf?: number }> {
  const out: Array<{ k: string; v: string; conf?: number }> = [];
  const lines = chunk
    .split("\n")
    .map((x) => x.replace(/^[-*]\s+/, "").trim())
    .filter(Boolean)
    .slice(0, 6);

  for (const line of lines) {
    const m = line.match(/^([^:]{1,28})[:：]\s*(.{1,140})$/);
    if (m) {
      out.push({ k: m[1].toLowerCase().replace(/\s+/g, "_"), v: m[2], conf: 0.82 });
      continue;
    }
    if (line.length <= 80) {
      out.push({ k: "note", v: line, conf: 0.65 });
    }
  }
  return out;
}

function digestText(s: string): string {
  return createHash("sha256").update(s).digest("hex");
}

function loadState(path: string): IngestState {
  if (!existsSync(path)) return { files: {} };
  try {
    return JSON.parse(readFileSync(path, "utf8")) as IngestState;
  } catch {
    return { files: {} };
  }
}

function saveState(path: string, st: IngestState): void {
  writeFileSync(path, JSON.stringify(st, null, 2));
}

export async function ingestMarkdownMemory(sidecar: SidecarClient, opt: IngestOptions): Promise<number> {
  const dot = join(opt.workspaceRoot, ".memq");
  if (!existsSync(dot)) mkdirSync(dot, { recursive: true });
  const statePath = join(dot, "ingest_state.json");
  const state = loadState(statePath);

  const files = [join(opt.workspaceRoot, "MEMORY.md")];
  const memoryDir = join(opt.workspaceRoot, "memory");
  if (existsSync(memoryDir)) {
    const daily = readdirSync(memoryDir)
      .filter((n) => n.endsWith(".md"))
      .map((n) => join(memoryDir, n));
    files.push(...daily);
  }

  let added = 0;
  for (const f of files) {
    if (!existsSync(f)) continue;
    const raw = readFileSync(f, "utf8");
    const digest = digestText(raw);
    const prev = state.files[f];
    if (prev && prev.digest === digest) continue;

    const chunks = chunkMarkdown(raw);
    for (let i = 0; i < chunks.length; i++) {
      const chunk = chunks[i];
      const type = inferType(chunk);
      const facts = extractFacts(chunk);
      const emb = await sidecar.embed(chunk);
      const novelty = 0.7;
      const redundancy = 0.3;
      const explicit = /(remember|覚えて)/i.test(chunk) ? 1 : 0;
      const stability = type === "constraint" || type === "identity" ? 1 : 0.5;
      const utility = facts.length ? 0.8 : 0.3;
      const should = shouldWriteDeep(
        {
          utility,
          novelty,
          stability,
          explicitness: explicit,
          redundancy,
          type
        },
        opt.writeThresholdLow,
        opt.writeThresholdHigh
      );
      if (!should) continue;

      const id = createHash("sha256")
        .update(`${f}:${i}:${chunk}`)
        .digest("hex")
        .slice(0, 24);
      await sidecar.add({
        id,
        vector: emb,
        tsSec: Math.floor(Date.now() / 1000),
        type,
        importance: explicit ? 0.9 : 0.55,
        confidence: Math.min(0.95, 0.55 + facts.length * 0.06),
        strength: 0.55,
        volatilityClass: volatilityFor(type),
        facts,
        tags: ["memory_md", type],
        evidenceUri: f,
        rawText: chunk
      });
      added += 1;
    }

    state.files[f] = {
      mtimeMs: Date.now(),
      digest
    };
  }

  saveState(statePath, state);
  return added;
}
