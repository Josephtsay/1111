from __future__ import annotations

import os
import uuid
from typing import Annotated

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .search_service import (
    EmptySearchBackend,
    SearchBackend,
    SQLiteFTSSearchBackend,
)


class JobSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: Annotated[str, Field(min_length=1, max_length=500)]
    location_code: list[str] | None = None
    duty_code: list[str] | None = None

    @field_validator("query")
    @classmethod
    def nonempty_query(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("query must contain searchable text")
        return value

    @field_validator("location_code", "duty_code")
    @classmethod
    def normalize_codes(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))


class RankedJob(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(min_length=1)
    rank: int = Field(ge=1)


class JobSearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    result: list[RankedJob]


def validate_ranked_results(results: list[RankedJob]) -> None:
    job_ids = [item.job_id for item in results]
    ranks = [item.rank for item in results]
    if len(job_ids) != len(set(job_ids)):
        raise ValueError("Search response contains duplicate job_id")
    if ranks != list(range(1, len(results) + 1)):
        raise ValueError("Search response ranks must be continuous from 1")


def create_app(
    backend: SearchBackend | None = None,
    *,
    result_limit: int = 50,
) -> FastAPI:
    if backend is None:
        index_path = os.environ.get("JOB_SEARCH_INDEX")
        backend = (
            SQLiteFTSSearchBackend(index_path)
            if index_path
            else EmptySearchBackend()
        )
    app = FastAPI(
        title="1111 Job Search Ranking API",
        version="1.0.0",
        description="Hackathon request/response contract with pluggable retrieval.",
    )
    app.state.backend = backend

    @app.get("/health")
    def health() -> dict[str, object]:
        return {
            "status": "ok",
            "backend": backend.__class__.__name__,
            "result_limit": result_limit,
        }

    @app.post("/api/v1/jobs/search", response_model=JobSearchResponse)
    def search(request: JobSearchRequest) -> JobSearchResponse:
        candidates = backend.search(
            request.query,
            location_codes=request.location_code or (),
            duty_codes=request.duty_code or (),
            limit=result_limit,
        )
        result = [
            RankedJob(job_id=candidate.job_id, rank=rank)
            for rank, candidate in enumerate(candidates, start=1)
        ]
        validate_ranked_results(result)
        return JobSearchResponse(
            request_id=f"req_{uuid.uuid4().hex[:16]}",
            result=result,
        )

    return app


app = create_app()
