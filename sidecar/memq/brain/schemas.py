from __future__ import annotations

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class FactCandidate(BaseModel):
    entity_id: str = "ent:user"
    fact_key: str
    value: str
    confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    layer: Literal["surface", "deep", "ephemeral"] = "surface"
    ttl_days: int = Field(default=30, ge=1, le=3650)
    keywords: List[str] = Field(default_factory=list)
    evidence_quote: str = ""

    @field_validator("entity_id")
    @classmethod
    def _entity_norm(cls, v: str) -> str:
        t = (v or "").strip()
        if not t:
            return "ent:user"
        return t[:64]

    @field_validator("fact_key")
    @classmethod
    def _fact_key_norm(cls, v: str) -> str:
        t = (v or "").strip().lower()
        return t[:96]

    @field_validator("value")
    @classmethod
    def _value_norm(cls, v: str) -> str:
        return " ".join((v or "").split())[:160]

    @field_validator("keywords")
    @classmethod
    def _kw_norm(cls, v: List[str]) -> List[str]:
        out: List[str] = []
        for x in v or []:
            s = " ".join(str(x).split()).strip().lower()
            if not s or len(s) > 40:
                continue
            if s not in out:
                out.append(s)
            if len(out) >= 16:
                break
        return out


class EventCandidate(BaseModel):
    day: str = ""
    ts: int
    summary: str
    salience: float = Field(default=0.5, ge=0.0, le=1.0)
    ttl_days: int = Field(default=14, ge=1, le=3650)
    keywords: List[str] = Field(default_factory=list)
    kind: Literal["chat", "action", "decision", "progress", "error", "plan"] = "chat"
    actor: Literal["user", "assistant", "tool"] = "assistant"

    @field_validator("summary")
    @classmethod
    def _sum_norm(cls, v: str) -> str:
        return " ".join((v or "").split())[:320]


class StyleUpdatePlan(BaseModel):
    apply: bool = False
    explicit: bool = False
    keys: Dict[str, str] = Field(default_factory=dict)


class RulesUpdatePlan(BaseModel):
    apply: bool = False
    explicit: bool = False
    rules: List[str] = Field(default_factory=list)


class QuarantinePlan(BaseModel):
    reason: str = "brain_suspect"
    raw_snippet: str = ""


class BrainIngestPlan(BaseModel):
    version: str = "memq_brain_v1"
    facts: List[FactCandidate] = Field(default_factory=list, max_length=8)
    events: List[EventCandidate] = Field(default_factory=list, max_length=8)
    style_update: Optional[StyleUpdatePlan] = None
    rules_update: Optional[RulesUpdatePlan] = None
    quarantine: List[QuarantinePlan] = Field(default_factory=list, max_length=8)


class IntentWeights(BaseModel):
    timeline: float = Field(default=0.0, ge=0.0, le=1.0)
    profile: float = Field(default=0.0, ge=0.0, le=1.0)
    state: float = Field(default=0.0, ge=0.0, le=1.0)
    fact_lookup: float = Field(default=0.0, ge=0.0, le=1.0)
    meta: float = Field(default=0.0, ge=0.0, le=1.0)
    fact: float = Field(default=0.0, ge=0.0, le=1.0)
    procedure: float = Field(default=0.0, ge=0.0, le=1.0)
    overview: float = Field(default=0.0, ge=0.0, le=1.0)

    @field_validator("fact_lookup")
    @classmethod
    def _fact_lookup_norm(cls, v: float) -> float:
        return float(max(0.0, min(1.0, v)))

    @model_validator(mode="after")
    def _backfill_fact_lookup(self) -> "IntentWeights":
        if float(self.fact_lookup) <= 0.0 and float(self.fact) > 0.0:
            self.fact_lookup = float(max(0.0, min(1.0, self.fact)))
        return self


class TimeRangePlan(BaseModel):
    startDay: str
    endDay: str
    label: str = "recent"


class BudgetSplitPlan(BaseModel):
    profile: int = Field(default=20, ge=0, le=400)
    timeline: int = Field(default=30, ge=0, le=400)
    surface: int = Field(default=20, ge=0, le=400)
    deep: int = Field(default=40, ge=0, le=400)
    ephemeral: int = Field(default=10, ge=0, le=400)


class RetrievalPlan(BaseModel):
    topk_surface: int = Field(default=4, ge=1, le=50)
    topk_deep: int = Field(default=5, ge=1, le=50)
    topk_events: int = Field(default=4, ge=1, le=50)
    allow_deep: bool = True


class BrainRecallPlan(BaseModel):
    version: str = "memq_brain_v1"
    intent: IntentWeights = Field(default_factory=IntentWeights)
    time_range: Optional[TimeRangePlan] = None
    entity_hints: List[str] = Field(default_factory=list, max_length=12)
    fact_keys: List[str] = Field(default_factory=list, max_length=16)
    fts_queries: List[str] = Field(default_factory=list, max_length=6)
    budget_split: BudgetSplitPlan = Field(default_factory=BudgetSplitPlan)
    retrieval: RetrievalPlan = Field(default_factory=RetrievalPlan)


class BrainMergeItem(BaseModel):
    target_id: str
    source_ids: List[str] = Field(default_factory=list)
    merged_text: str = ""
    merged_summary: str = ""
    new_tags: Dict[str, str] = Field(default_factory=dict)
    drop_source: bool = True


class BrainPruneItem(BaseModel):
    id: str
    reason: str = "low_value"


class BrainMergePlan(BaseModel):
    version: str = "memq_brain_v1"
    merges: List[BrainMergeItem] = Field(default_factory=list, max_length=20)
    prunes: List[BrainPruneItem] = Field(default_factory=list, max_length=40)


class BrainSpanPatch(BaseModel):
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    reason: str = ""


class BrainAuditPatchPlan(BaseModel):
    version: str = "memq_brain_v1"
    patched_text: str = ""
    changed_spans: List[BrainSpanPatch] = Field(default_factory=list)
