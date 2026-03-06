export const defaults = {
  "memq.sidecarUrl": "http://127.0.0.1:7781",
  "memq.workspaceRoot": process.cwd(),
  "memq.brain.mode": "brain-optional",
  "memq.brain.provider": "ollama",
  "memq.brain.baseUrl": "http://127.0.0.1:11434",
  "memq.brain.model": "gpt-oss:20b",
  "memq.brain.keepAlive": "30m",
  "memq.brain.timeoutMs": 60000,
  "memq.budgets.memctxTokens": 120,
  "memq.budgets.rulesTokens": 80,
  "memq.budgets.styleTokens": 120,
  "memq.total.maxInputTokens": 4200,
  "memq.total.reserveTokens": 1800,
  "memq.recent.maxTokens": 2600,
  "memq.recent.minKeepMessages": 4,
  "memq.retrieval.topK": 5,
  "memq.degraded.enabled": true,
  "memq.style.enabled": true,
  "memq.idle.enabled": true,
  "memq.security.primaryRulesEnabled": true,
  "memq.security.llmAuditEnabled": false,
} as const;

export function getCfg<T>(api: any, key: string, fallback: T): T {
  const pluginConfig = api?.pluginConfig?.[key];
  if (pluginConfig !== undefined) return pluginConfig as T;
  let cursor = api?.config;
  for (const part of key.split(".")) {
    if (cursor && typeof cursor === "object" && part in cursor) cursor = cursor[part];
    else return fallback;
  }
  return cursor as T;
}

export function logInfo(api: any, message: string): void {
  if (api?.logger?.info) api.logger.info(message);
  else console.log(message);
}
