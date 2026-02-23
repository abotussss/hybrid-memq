export type MemoryType =
  | "preference"
  | "constraint"
  | "identity"
  | "plan"
  | "decision"
  | "definition"
  | "episode"
  | "note";

export type VolatilityClass = "high" | "medium" | "low";

export interface Fact {
  k: string;
  v: string;
  conf?: number;
}

export interface MemoryTrace {
  id: string;
  type: MemoryType;
  tsSec: number;
  updatedAtSec?: number;
  lastAccessAtSec?: number;
  accessCount?: number;
  strength: number;
  importance: number;
  confidence: number;
  volatilityClass: VolatilityClass;
  facts: Fact[];
  tags?: string[];
}

export interface Conflict {
  key: string;
  policy: "prefer_high_conf" | "prefer_recent" | "prefer_user_explicit";
  members: string[];
}

export interface MemCtxInput {
  budgetTokens: number;
  surface: MemoryTrace[];
  deep: MemoryTrace[];
  rules: string[];
  conflicts?: Conflict[];
  userText?: string;
  nowSec?: number;
}
