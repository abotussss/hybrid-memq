export interface MemqMessage {
  role: string;
  text: string;
  ts?: number;
}

export interface QBudgets {
  qctxTokens: number;
  qruleTokens: number;
  qstyleTokens: number;
}

export interface QctxQueryRequest {
  sessionKey: string;
  prompt: string;
  recentMessages: MemqMessage[];
  budgets: QBudgets;
  topK: number;
}

export interface QctxQueryResponse {
  ok: boolean;
  qrule: string;
  qstyle: string;
  qctx: string;
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
  lastSessionSanitizeAtMs: number;
}
