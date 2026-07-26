from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SkillType(str, Enum):
    PROGRAMMING_LANGUAGE = "ProgrammingLanguage"
    FRAMEWORK = "Framework"
    LIBRARY = "Library"
    DATABASE = "Database"
    CLOUD_PLATFORM = "CloudPlatform"
    DEVOPS_TOOL = "DevOpsTool"
    OPERATING_SYSTEM = "OperatingSystem"
    DATA_TOOL = "DataTool"
    AI_ML = "AI_ML"
    BUSINESS_SKILL = "BusinessSkill"
    SOFT_SKILL = "SoftSkill"
    CERTIFICATION = "Certification"
    DOMAIN_KNOWLEDGE = "DomainKnowledge"
    OTHER = "Other"


class Requirement(str, Enum):
    REQUIRED = "required"
    PREFERRED = "preferred"
    MENTIONED = "mentioned"


class SkillMention(BaseModel):
    model_config = ConfigDict(extra="forbid")

    raw_mention: str = Field(min_length=1)
    canonical_candidate: str = Field(min_length=1)
    skill_type: SkillType
    requirement: Requirement
    importance: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: str = Field(min_length=1)
    start_char: int | None = Field(default=None, ge=0)
    end_char: int | None = Field(default=None, ge=0)

    @field_validator("raw_mention", "canonical_candidate", "evidence")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class JobSkillExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(min_length=1)
    occupation_candidate: str | None = None
    skills: list[SkillMention]
    extraction_prompt_version: str = Field(min_length=1)


class VerificationDecision(str, Enum):
    SAME_SKILL = "same_skill"
    RELATED_BUT_DISTINCT = "related_but_distinct"
    NOT_RELATED = "not_related"
    AMBIGUOUS = "ambiguous"


class VerificationResult(BaseModel):
    decision: VerificationDecision
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str


class CanonicalizationAudit(BaseModel):
    raw_mention: str
    normalized_text: str
    selected_skill: str | None
    candidate_list: list[str]
    similarity_scores: list[float]
    verifier_decision: VerificationDecision | None
    verifier_confidence: float | None
    rule_used: str
    source_job_id: str


class QueryParseResult(BaseModel):
    raw_query: str
    skill_mentions: list[str]
    canonical_skills: list[str]
    occupation_candidate: str | None = None
    location_filter: str | None = None
    unresolved_terms: list[str] = Field(default_factory=list)


class TraversalTrace(BaseModel):
    query_term: str
    canonical_anchor: str
    path: list[str]
    edge_types: list[str]
    individual_edge_weights: list[float]
    final_path_score: float
    matched_job: str | None = None
    explanation: str


class GraphNode(BaseModel):
    node_id: str
    label: str
    properties: dict[str, Any]


class GraphEdge(BaseModel):
    edge_id: str
    from_id: str
    to_id: str
    label: str
    properties: dict[str, Any]


class RetryPolicy(BaseModel):
    max_attempts: int = Field(default=3, ge=1)
    initial_delay_seconds: float = Field(default=1.0, ge=0)
    max_delay_seconds: float = Field(default=30.0, ge=0)
    retryable_error_names: tuple[str, ...] = (
        "ThrottlingException",
        "TooManyRequestsException",
        "ServiceUnavailableException",
        "ModelTimeoutException",
    )


class ValidationFinding(BaseModel):
    check: str
    severity: Literal["fail", "quarantine", "warn", "auto_fix"]
    message: str
    record_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)

