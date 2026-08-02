"""Unified search API — shared request/response schema."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class SearchRequest(BaseModel):
    """Unified search request accepted by all backends."""

    ks: str = Field(..., description="搜尋關鍵字", examples=["水電"])
    c0: str | list[str] | None = Field(
        default=None,
        description="地區代碼，逗號分隔字串或陣列",
        examples=["100221,100100"],
    )
    d0: str | list[str] | None = Field(
        default=None,
        description="職務分類代碼，逗號分隔字串或陣列",
        examples=["110102,110103"],
    )
    top_k: int = Field(default=10, ge=1, le=100, description="回傳筆數")
    mode: str = Field(default="hybrid", description="搜尋模式 (hybrid/bm25/knn)")
    salary_min: int | None = Field(default=None, description="月薪等值下限")
    job_types: list[str] | None = Field(default=None, description="職缺屬性 filter")
    work_hours: list[str] | None = Field(default=None, description="工時 filter")
    backend: Literal["no-graph", "graph", "both"] = Field(
        default="both",
        description="路由到哪個 backend: no-graph / graph / both",
    )


class HitResponse(BaseModel):
    """Single search hit — superset of fields from both backends."""

    score: float
    job_id: str
    title: str | None = None
    city: str | None = None
    district: str | None = None
    job_category_l3: str | None = None
    job_category_l2: str | None = None
    job_category_l1: str | None = None
    industry_l3: str | None = None
    salary_text: str | None = None
    salary_type: str | None = None
    salary_min: int | None = None
    salary_max: int | None = None
    is_negotiable: bool | None = None
    job_type: str | None = None
    work_hours: list[str] | None = None
    education: str | None = None
    experience: str | None = None
    company_id: str | None = None
    updated_at: str | None = None
    content: str | None = None
    # Graph-specific fields
    lexical_score: float | None = None
    location_match: float | None = None
    duty_match: float | None = None
    matched_skills: list[str] | None = None


class BackendResult(BaseModel):
    """Result from a single backend."""

    backend: str
    mode: str
    took_ms: int
    total: int
    filter_notes: dict[str, str] = Field(default_factory=dict)
    hits: list[HitResponse]


class SearchResponse(BaseModel):
    """Unified response — one or two backend results."""

    query: str
    results: list[BackendResult]
