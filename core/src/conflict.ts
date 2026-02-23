import type { Conflict, MemoryTrace } from "./types.js";

export function detectConflicts(traces: MemoryTrace[]): Conflict[] {
  const byKey = new Map<string, Map<string, MemoryTrace[]>>();
  for (const t of traces) {
    for (const f of t.facts) {
      if (!byKey.has(f.k)) byKey.set(f.k, new Map());
      const values = byKey.get(f.k)!;
      if (!values.has(f.v)) values.set(f.v, []);
      values.get(f.v)!.push(t);
    }
  }

  const conflicts: Conflict[] = [];
  for (const [key, values] of byKey) {
    if (values.size <= 1) continue;
    const members = [...new Set([...values.values()].flat().map((t) => t.id))];
    conflicts.push({ key, policy: "prefer_user_explicit", members });
  }
  return conflicts;
}
