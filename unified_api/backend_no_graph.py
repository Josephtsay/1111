"""Adapter for no-graph-search backend (OpenSearch hybrid)."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Sequence

# Allow importing from no-graph-search
_NO_GRAPH_ROOT = Path(__file__).resolve().parent.parent / "no-graph-search"
if str(_NO_GRAPH_ROOT) not in sys.path:
    sys.path.insert(0, str(_NO_GRAPH_ROOT))

from src.search import Filters, JobSearch  # noqa: E402

from .schema import BackendResult, HitResponse  # noqa: E402


class NoGraphBackend:
    """Thin wrapper around no-graph-search's JobSearch."""

    def __init__(self) -> None:
        self._searcher: JobSearch | None = None

    def _get_searcher(self) -> JobSearch:
        if self._searcher is None:
            self._searcher = JobSearch()
        return self._searcher

    def search(
        self,
        ks: str,
        *,
        c0: list[str] | None = None,
        d0: list[str] | None = None,
        top_k: int = 10,
        mode: str = "hybrid",
        salary_min: int | None = None,
        job_types: list[str] | None = None,
        work_hours: list[str] | None = None,
    ) -> BackendResult:
        searcher = self._get_searcher()
        filters = Filters(
            location_codes=c0 or [],
            job_types=job_types or [],
            work_hours=work_hours or [],
            salary_min=salary_min,
        )

        start = time.perf_counter_ns()
        result = searcher.search(ks, filters=filters, top_k=top_k, mode=mode)
        took_ms = (time.perf_counter_ns() - start) // 1_000_000

        hits = [
            HitResponse(
                score=h["score"],
                job_id=h["job_id"],
                title=h.get("title"),
                city=h.get("city"),
                district=h.get("district"),
                job_category_l3=h.get("job_category_l3"),
                job_category_l2=h.get("job_category_l2"),
                job_category_l1=h.get("job_category_l1"),
                industry_l3=h.get("industry_l3"),
                salary_text=h.get("salary_text"),
                salary_type=h.get("salary_type"),
                salary_min=h.get("salary_min"),
                salary_max=h.get("salary_max"),
                is_negotiable=h.get("is_negotiable"),
                job_type=h.get("job_type"),
                work_hours=h.get("work_hours"),
                education=h.get("education"),
                experience=h.get("experience"),
                company_id=h.get("company_id"),
                updated_at=h.get("updated_at"),
                content=h.get("content"),
            )
            for h in result.get("hits", [])
        ]

        return BackendResult(
            backend="no-graph",
            mode=result.get("mode", mode),
            took_ms=result.get("took_ms", took_ms),
            total=result.get("total", len(hits)),
            filter_notes=result.get("filter_notes", {}),
            hits=hits,
        )
