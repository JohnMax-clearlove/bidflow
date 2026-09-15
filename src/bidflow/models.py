"""公开的 Agent 结果模型；未知业务扩展保留，关键字段严格校验。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Record(BaseModel):
    model_config = ConfigDict(extra="allow")


class SourceRef(Record):
    file_id: str
    block_id: str
    quote: str = Field(min_length=1)
    page: int | None = Field(default=None, ge=1)


class Rule(Record):
    id: str | None = None
    kind: Literal["qualification", "invalid", "business", "technical", "requirement", "price"]
    title: str = Field(min_length=1)
    text: str = Field(min_length=1)
    sources: list[SourceRef] = Field(min_length=1)
    max_score: float | None = Field(default=None, ge=0)
    status: Literal["draft", "confirmed", "retired"] = "draft"
    revision: int = Field(default=1, ge=1)
    subrequirements: list[str] = Field(default_factory=list)
    criteria: dict[str, Any] = Field(default_factory=dict)
    conflict: bool = False


class AnalyzeResult(Record):
    rules: list[Rule]
    covered_block_ids: list[str]
    facts: dict[str, Any] = Field(default_factory=dict)
    settings: dict[str, Any] = Field(
        default_factory=dict,
        description="仅提议从招标原文提取的项目设置，例如anonymous、allow_indices、expected_total_score；接收后仍需rules确认。",
    )
    conflicts: list[dict[str, Any]] = Field(default_factory=list)
    retire_rule_ids: list[str] = Field(default_factory=list)


class Evidence(Record):
    id: str | None = None
    file_id: str
    title: str
    pages: list[int] = Field(default_factory=list)
    facts: dict[str, Any] = Field(default_factory=dict)
    verification: Literal["candidate", "verified", "insufficient", "rejected"] = "candidate"
    notes: str = ""
    verified_by: str = ""
    file_sha256: str = ""


class Response(Record):
    id: str | None = None
    rule_id: str
    evidence_ids: list[str] = Field(default_factory=list)
    section_ids: list[str] = Field(default_factory=list)
    status: Literal["candidate", "supported", "insufficient", "rejected"] = "candidate"
    rationale: str = Field(min_length=1)
    score: float | None = Field(default=None, ge=0)
    missing: list[str] = Field(default_factory=list)
    rule_revision: int = Field(default=1, ge=1)
    covered_subrequirements: list[str] = Field(default_factory=list)


class EvidenceResult(Record):
    evidence: list[Evidence] = Field(default_factory=list)
    responses: list[Response] = Field(default_factory=list)


class SectionPlan(Record):
    id: str | None = None
    title: str
    rule_ids: list[str] = Field(min_length=1)
    content_points: list[str] = Field(min_length=1)
    fact_refs: list[str] = Field(default_factory=list)
    method_refs: list[str] = Field(default_factory=list)
    visuals: list[str] = Field(default_factory=list)
    volume: str = "技术部分"
    status: Literal["draft", "confirmed"] = "draft"


class PlanResult(Record):
    plans: list[SectionPlan] = Field(min_length=1)


class WriteResult(Record):
    section_id: str
    markdown: str = Field(min_length=1)
    writer_id: str = Field(min_length=1)
    covered_subrequirements: dict[str, list[str]] = Field(default_factory=dict)


class ReviewFinding(Record):
    rule_id: str
    severity: Literal["error", "warning", "info"]
    message: str
    suggestion: str
    location: str


class ReviewResult(Record):
    section_id: str
    reviewer_id: str = Field(min_length=1)
    writer_id: str = Field(min_length=1)
    findings: list[ReviewFinding]
    scores: list[dict[str, Any]]
    covered_subrequirements: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Reviewer逐条确认正文实际覆盖的评分子要求；不能照抄Writer自报结果。",
    )
    status: Literal["pass", "revise"]

    @model_validator(mode="after")
    def independent(self):
        if self.reviewer_id == self.writer_id:
            raise ValueError("独立审核人与写作人不能相同")
        if self.status == "pass" and any(item.severity == "error" for item in self.findings):
            raise ValueError("存在错误项时不能标记审核通过")
        return self


RESULT_MODELS = {"analyze": AnalyzeResult, "amendment": AnalyzeResult, "evidence": EvidenceResult, "plan": PlanResult, "write": WriteResult, "review": ReviewResult}
