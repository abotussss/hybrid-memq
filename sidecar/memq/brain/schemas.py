from __future__ import annotations

from pydantic import AliasChoices, BaseModel, Field, field_validator, model_validator
from typing import Literal


class FactCandidate(BaseModel):
    entity_id: str = Field(default="ent:user", validation_alias=AliasChoices("entity_id", "entity", "entityId"))
    fact_key: str
    value: str = Field(default="", validation_alias=AliasChoices("value", "fact_value", "factValue", "text"))
    confidence: float = Field(default=0.6, ge=0.0, le=1.0, validation_alias=AliasChoices("confidence", "score", "probability"))
    layer: Literal["surface", "deep", "ephemeral"] = "deep"
    ttl_days: int | None = None
    keywords: list[str] = Field(default_factory=list)
    evidence_quote: str = Field(default="", validation_alias=AliasChoices("evidence_quote", "evidence", "quote"))
    importance: float = Field(default=0.6, ge=0.0, le=1.0)
    strength: float = Field(default=0.6, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _fill_missing_value(self) -> "FactCandidate":
        if not (self.value or "").strip():
            self.value = (self.evidence_quote or "").strip()
        return self


class EventCandidate(BaseModel):
    day: str | None = None
    ts: int | None = None
    actor: Literal["user", "assistant", "tool"] = Field(default="user", validation_alias=AliasChoices("actor", "role"))
    kind: str = Field(default="chat", validation_alias=AliasChoices("kind", "event_type"))
    summary: str = Field(validation_alias=AliasChoices("summary", "description"))
    salience: float = Field(default=0.4, ge=0.0, le=1.0)
    ttl_days: int | None = None
    keywords: list[str] = Field(default_factory=list)


class StyleUpdate(BaseModel):
    apply: bool = False
    explicit: bool = False
    keys: dict[str, str] = Field(default_factory=dict)

    @field_validator("keys", mode="before")
    @classmethod
    def _coerce_keys(cls, value):
        if isinstance(value, dict):
            return {str(k): str(v) for k, v in value.items()}
        if isinstance(value, list):
            out: dict[str, str] = {}
            for item in value:
                if isinstance(item, dict):
                    key = item.get("key") or item.get("name")
                    val = item.get("value") or item.get("text") or ""
                    if key:
                        out[str(key)] = str(val)
                elif isinstance(item, str):
                    out[str(item)] = ""
            return out
        return value


class RuleUpdate(BaseModel):
    apply: bool = False
    explicit: bool = False
    rules: dict[str, str] = Field(default_factory=dict)

    @field_validator("rules", mode="before")
    @classmethod
    def _coerce_rules(cls, value):
        if isinstance(value, dict):
            return {str(k): str(v) for k, v in value.items()}
        if isinstance(value, list):
            out: dict[str, str] = {}
            for item in value:
                if isinstance(item, dict):
                    key = item.get("key") or item.get("name")
                    val = item.get("value") or item.get("text") or ""
                    if key:
                        out[str(key)] = str(val)
                elif isinstance(item, str):
                    out[str(item)] = ""
            return out
        return value


class QuarantineItem(BaseModel):
    reason: str
    raw_snippet: str
    risk: float = Field(default=1.0, ge=0.0, le=1.0)


class BrainIngestPlan(BaseModel):
    version: str = "memq_brain_v3"
    facts: list[FactCandidate] = Field(default_factory=list)
    events: list[EventCandidate] = Field(default_factory=list)
    style_update: StyleUpdate | None = None
    rules_update: RuleUpdate | None = None
    quarantine: list[QuarantineItem] = Field(default_factory=list)

    @field_validator("events", mode="before")
    @classmethod
    def _coerce_events(cls, value):
        if not isinstance(value, list):
            return value
        out = []
        for item in value:
            if isinstance(item, str):
                out.append({"summary": item, "kind": "chat", "actor": "user"})
            else:
                out.append(item)
        return out


class BrainPreviewPlan(BaseModel):
    version: str = "memq_brain_v3"
    style_update: StyleUpdate | None = None
    rules_update: RuleUpdate | None = None


class IntentWeights(BaseModel):
    timeline: float = Field(default=0.0, ge=0.0, le=1.0)
    profile: float = Field(default=0.0, ge=0.0, le=1.0)
    state: float = Field(default=0.0, ge=0.0, le=1.0)
    fact: float = Field(default=0.0, ge=0.0, le=1.0)
    overview: float = Field(default=0.0, ge=0.0, le=1.0)


class TimeRange(BaseModel):
    start_day: str = Field(validation_alias=AliasChoices("start_day", "startDay", "start"))
    end_day: str = Field(validation_alias=AliasChoices("end_day", "endDay", "end"))
    label: str = "range"


class BudgetSplit(BaseModel):
    profile: int = 24
    timeline: int = 24
    surface: int = 24
    deep: int = 24
    ephemeral: int = 8


class RetrievalPlan(BaseModel):
    allow_surface: bool = True
    allow_deep: bool = True
    allow_timeline: bool = True
    topk_surface: int = 4
    topk_deep: int = 4
    topk_events: int = 4


class BrainRecallPlan(BaseModel):
    version: str = "memq_brain_v3"
    intent: IntentWeights = Field(default_factory=IntentWeights)
    time_range: TimeRange | None = None
    entity_hints: list[str] = Field(default_factory=list)
    fact_keys: list[str] = Field(default_factory=list)
    fts_queries: list[str] = Field(default_factory=list)
    budget_split: BudgetSplit = Field(default_factory=BudgetSplit)
    retrieval: RetrievalPlan = Field(default_factory=RetrievalPlan)


class MergeAction(BaseModel):
    target_id: int
    source_ids: list[int] = Field(default_factory=list)
    merged_summary: str
    merged_value: str | None = None


class PruneAction(BaseModel):
    id: int
    reason: str


class BrainMergePlan(BaseModel):
    version: str = "memq_brain_v3"
    merges: list[MergeAction] = Field(default_factory=list)
    prunes: list[PruneAction] = Field(default_factory=list)


class ChangedSpan(BaseModel):
    start: int
    end: int
    reason: str


class BrainAuditPatchPlan(BaseModel):
    version: str = "memq_brain_v3"
    patched_text: str
    changed_spans: list[ChangedSpan] = Field(default_factory=list)
