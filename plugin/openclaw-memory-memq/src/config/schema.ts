import { appendFileSync, mkdirSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";

export const defaults = {
  "memq.sidecarUrl": "http://127.0.0.1:7781",
  "memq.workspaceRoot": process.cwd(),
  "memq.brain.mode": "required",
  "memq.brain.provider": "ollama",
  "memq.brain.baseUrl": "http://127.0.0.1:11434",
  "memq.brain.model": "gpt-oss:20b",
  "memq.brain.keepAlive": "30m",
  "memq.brain.timeoutMs": 60000,
  "memq.brain.maxTokens": 256,
  "memq.brain.autoRestart": true,
  "memq.brain.restartCooldownSec": 30,
  "memq.brain.restartWaitMs": 2000,
  "memq.budgets.memctxTokens": 120,
  "memq.budgets.rulesTokens": 80,
  "memq.budgets.styleTokens": 120,
  "memq.total.maxInputTokens": 4200,
  "memq.total.reserveTokens": 1800,
  "memq.total.capSafetyRatio": 0.72,
  "memq.recent.maxTokens": 2600,
  "memq.recent.minKeepMessages": 4,
  "memq.retrieval.topK": 5,
  "memq.retrieval.surfaceFirst": true,
  "memq.retrieval.surfaceThreshold": 0.85,
  "memq.retrieval.deepEnabled": true,
  "memq.archive.enabled": true,
  "memq.archive.maxFileBytes": 8_000_000,
  "memq.archive.maxFiles": 30,
  "memq.degraded.enabled": false,
  "memq.security.primaryRulesEnabled": true,
  "memq.security.llmAuditEnabled": false,
  "memq.security.llmAuditThreshold": 0.2,
  "memq.security.blockThreshold": 0.85,
  "memq.style.enabled": true,
  "memq.style.maxBudgetTokens": 120,
  "memq.idle.enabled": true,
  "memq.idle.idleSeconds": 120,
} as const;

export function getCfg<T>(api: any, key: string, fallback: T): T {
  const pc = api?.pluginConfig?.[key];
  if (pc !== undefined) return pc as T;
  let root = api?.config;
  for (const part of key.split(".")) {
    if (root && typeof root === "object" && part in root) root = root[part];
    else {
      root = undefined;
      break;
    }
  }
  if (root !== undefined) return root as T;
  return fallback;
}

export function logInfo(api: any, msg: string): void {
  const logger = api?.logger;
  if (logger?.info) logger.info(msg);
  else console.log(msg);
  if (String(msg).includes("[memq][brain-proof]")) {
    try {
      const p = join(homedir(), ".openclaw", "logs", "gateway.log");
      mkdirSync(dirname(p), { recursive: true });
      appendFileSync(p, `${new Date().toISOString()} ${msg}\n`, "utf-8");
    } catch {
      // ignore file logging failures
    }
  }
}
