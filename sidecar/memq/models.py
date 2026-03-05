from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class Message(BaseModel):
    role: str
    text: str
    ts: Optional[int] = None


class QueryBudgets(BaseModel):
    memctxTokens: int = 120
    rulesTokens: int = 80
    styleTokens: int = 120


class MemctxQueryRequest(BaseModel):
    sessionKey: str
    prompt: str
    recentMessages: List[Message] = Field(default_factory=list)
    budgets: QueryBudgets = Field(default_factory=QueryBudgets)
    topK: int = 5
    surfaceThreshold: Optional[float] = None
    deepEnabled: Optional[bool] = None


class MemctxMeta(BaseModel):
    surfaceHit: bool
    deepCalled: bool
    usedMemoryIds: List[str] = Field(default_factory=list)
    debug: Dict[str, Any] = Field(default_factory=dict)
    traceId: Optional[str] = None


class MemctxQueryResponse(BaseModel):
    ok: bool = True
    memrules: str = ""
    memstyle: str = ""
    memctx: str = ""
    meta: MemctxMeta
    traceId: Optional[str] = None


class SummarizeRequest(BaseModel):
    sessionKey: str
    prunedMessages: List[Message] = Field(default_factory=list)
    retentionScope: Literal["surface_only", "deep"]


class SummarizeResponse(BaseModel):
    ok: bool = True
    convsurfId: Optional[str] = None
    convdeepId: Optional[str] = None
    stats: Dict[str, Any] = Field(default_factory=dict)


class IngestTurnRequest(BaseModel):
    sessionKey: str
    userText: str = ""
    assistantText: str = ""
    ts: int
    metadata: Optional[Dict[str, Any]] = None


class IngestTurnResponse(BaseModel):
    ok: bool = True
    wrote: Dict[str, int] = Field(default_factory=dict)
    traceId: Optional[str] = None


class IdleRunRequest(BaseModel):
    nowTs: Optional[int] = None
    maxWorkMs: Optional[int] = None


class IdleRunResponse(BaseModel):
    ok: bool = True
    did: List[str] = Field(default_factory=list)
    stats: Dict[str, Any] = Field(default_factory=dict)
    traceId: Optional[str] = None


class IdleTickRequest(BaseModel):
    nowSec: Optional[int] = None


class AuditThresholds(BaseModel):
    llmAuditThreshold: float = 0.2
    blockThreshold: float = 0.85


class AuditRequest(BaseModel):
    sessionKey: str
    text: str
    mode: Literal["primary", "dual"] = "primary"
    thresholds: Optional[AuditThresholds] = None


class AuditResponse(BaseModel):
    ok: bool = True
    risk: float
    block: bool
    redactedText: Optional[str] = None
    reasons: List[str] = Field(default_factory=list)


class BootstrapImportRequest(BaseModel):
    workspaceRoot: str


class ProfileResponse(BaseModel):
    ok: bool = True
    style_profile: Dict[str, str] = Field(default_factory=dict)
    preference_profile: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    memory_policy_profile: Dict[str, Dict[str, Any]] = Field(default_factory=dict)


class QuarantineResponse(BaseModel):
    ok: bool = True
    items: List[Dict[str, Any]] = Field(default_factory=list)
