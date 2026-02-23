#!/usr/bin/env node

const cmd = process.argv[2];
const sidecar = process.env.MEMQ_SIDECAR_URL ?? "http://127.0.0.1:7781";

async function call(path: string, init?: RequestInit): Promise<unknown> {
  const r = await fetch(`${sidecar}${path}`, init);
  if (!r.ok) throw new Error(`${path} failed (${r.status})`);
  return await r.json();
}

async function main() {
  if (cmd === "status") {
    const health = await call("/health");
    const stats = await call("/stats");
    console.log(JSON.stringify({ ok: true, sidecar, health, stats }, null, 2));
    return;
  }

  if (cmd === "reindex") {
    const out = await call("/index/rebuild", { method: "POST" });
    console.log(JSON.stringify(out, null, 2));
    return;
  }

  if (cmd === "consolidate") {
    const out = await call("/index/consolidate", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ nowSec: Math.floor(Date.now() / 1000) })
    });
    console.log(JSON.stringify(out, null, 2));
    return;
  }

  console.log("usage: memq <status|reindex|consolidate>");
}

main().catch((e) => {
  console.error(e.message);
  process.exit(1);
});
