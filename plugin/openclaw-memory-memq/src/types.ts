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
}

export interface MemqQueryResponse {
  ok: boolean;
  memrules: string;
  memstyle: string;
  memctx: string;
  meta: {
    surfaceHit: boolean;
    deepCalled: boolean;
    usedMemoryIds: number[];
    debug?: Record<string, unknown>;
  };
}

export interface RuntimeState {
  lastUserBySession: Map<string, string>;
  lastPromptBySession: Map<string, string>;
  lastMemstyleBySession: Map<string, string>;
}
