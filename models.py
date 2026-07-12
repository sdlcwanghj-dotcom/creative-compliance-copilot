from typing import Literal

from pydantic import BaseModel, Field


class ReviewViolation(BaseModel):
    category: str
    evidence: str
    reason: str
    rule_id: str
    severity: Literal["high", "medium", "low"]
    confidence: float = Field(ge=0, le=1)


class StructuredReviewReport(BaseModel):
    decision: Literal["pass", "modify", "escalate"]
    risk_level: Literal["high", "medium", "low"]
    violations: list[ReviewViolation]
    suggested_rewrite: str
    human_review_required: bool
    report_id: str
    created_at: str
    scope_notice: str
