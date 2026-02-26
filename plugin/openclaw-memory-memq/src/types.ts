export type Json = string | number | boolean | null | Json[] | { [k: string]: Json };

export interface MemqMessage {
  role: string;
  text: string;
  ts?: number;
}

export interface MemqBudgets {
  memctxTokens: number;
  rulesTokens: number;
  styleTokens: number;
}

export interface MemqQueryRequest {
  sessionKey: string;
  prompt: string;
  recentMessages: MemqMessage[];
  budgets: MemqBudgets;
  topK: number;
  surfaceThreshold?: number;
  deepEnabled?: boolean;
}

export interface MemqQueryResponse {
  ok: boolean;
  memrules: string;
  memstyle: string;
  memctx: string;
  meta: {
    surfaceHit: boolean;
    deepCalled: boolean;
    usedMemoryIds: string[];
    debug?: Record<string, Json>;
  };
}

export interface RuntimeState {
  lastUserBySession: Map<string, string>;
  lastPromptBySession: Map<string, string>;
  lastKeptBySession: Map<string, MemqMessage[]>;
}
