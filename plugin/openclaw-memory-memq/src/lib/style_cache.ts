import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";

function cachePath(workspaceRoot: string): string {
  return join(workspaceRoot, ".memq", "style_cache.json");
}

export function readStyleCache(workspaceRoot: string, sessionKey: string): string {
  try {
    const p = cachePath(workspaceRoot);
    const raw = readFileSync(p, "utf-8");
    const obj = JSON.parse(raw) as Record<string, string>;
    return String(obj?.[sessionKey] ?? obj?.["__last__"] ?? "");
  } catch {
    return "";
  }
}

export function writeStyleCache(workspaceRoot: string, sessionKey: string, memstyle: string): void {
  const style = String(memstyle || "").trim();
  if (!style) return;
  try {
    const p = cachePath(workspaceRoot);
    mkdirSync(dirname(p), { recursive: true });
    let obj: Record<string, string> = {};
    try {
      obj = JSON.parse(readFileSync(p, "utf-8")) as Record<string, string>;
    } catch {
      obj = {};
    }
    obj[sessionKey] = style;
    obj["__last__"] = style;
    writeFileSync(p, JSON.stringify(obj, null, 2), "utf-8");
  } catch {
    // best effort
  }
}

