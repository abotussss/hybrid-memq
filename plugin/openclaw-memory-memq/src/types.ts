import type { MemoryType, VolatilityClass } from "./memq_core.js";

export interface PromptBuildInput {
  userText: string;
  recentMessages: string[];
  sessionId?: string;
}

export interface PromptBuildOutput {
  prependContext?: string;
  systemPrompt?: string;
}

export interface AgentEndInput {
  referencedMemoryIds?: string[];
  sessionId?: string;
}

export interface PluginApi {
  registerHook: (name: string, handler: (ctx: unknown) => Promise<unknown> | unknown) => void;
  config?: Record<string, unknown>;
  pluginConfig?: Record<string, unknown>;
}

export interface SidecarSearchResult {
  id: string;
  score: number;
  tsSec: number;
  updatedAtSec?: number;
  lastAccessAtSec?: number;
  accessCount?: number;
  type: MemoryType;
  importance: number;
  confidence: number;
  strength: number;
  volatilityClass: VolatilityClass;
  facts: Array<{ k: string; v: string; conf?: number }>;
  tags?: string[];
  rawText?: string;
}

export interface RuntimeState {
  lastCandidatesBySession: Map<string, SidecarSearchResult[]>;
  lastAllowedLanguagesBySession?: Map<string, string[]>;
}
