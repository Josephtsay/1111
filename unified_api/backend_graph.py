"""Adapter for graph-enhanced search backend.

Uses OpenSearch for everything — same hybrid search as no-graph, but adds a
`graph_skills` boost clause. No in-memory graph, no CSV loading.
Cold start identical to no-graph backend (~3-5s).

Architecture:
    query → embed (Bedrock) → hybrid query (BM25 + kNN + graph_skills boost) → results
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

_NO_GRAPH_ROOT = Path(__file__).resolve().parent.parent / "no-graph-search"
if str(_NO_GRAPH_ROOT) not in sys.path:
    sys.path.insert(0, str(_NO_GRAPH_ROOT))

from config.settings import settings  # noqa: E402
from src.clients import opensearch_client  # noqa: E402
from src.create_index import SEARCH_PIPELINE_ID, VECTOR_FIELD  # noqa: E402
from src.lookup import Lookup  # noqa: E402
from src.search import (  # noqa: E402
    BM25_FIELDS,
    SOURCE_FIELDS,
    Filters,
    bm25_query,
    knn_query,
    normalize_location_codes,
)

from .schema import BackendResult, HitResponse  # noqa: E402

# Graph skill boost weight relative to BM25+kNN
SKILL_BOOST_WEIGHT = 1.5


class GraphBackend:
    """OpenSearch hybrid + graph_skills boost.

    Same cold start as no-graph. Adds a `should` clause that boosts jobs
    whose `graph_skills` keyword field matches skills extracted from the query.
    """

    def __init__(self) -> None:
        self._client = None
        self._lookup: Lookup | None = None
        self._bedrock = None

    def _get_client(self):
        if self._client is None:
            self._client = opensearch_client()
        return self._client

    def _get_lookup(self) -> Lookup:
        if self._lookup is None:
            self._lookup = Lookup()
        return self._lookup

    def _embed_query(self, text: str) -> list[float]:
        from src.clients import bedrock_runtime_client, embed_texts, INPUT_TYPE_QUERY
        if self._bedrock is None:
            self._bedrock = bedrock_runtime_client()
        return embed_texts([text], input_type=INPUT_TYPE_QUERY, client=self._bedrock)[0]

    @property
    def available(self) -> bool:
        return True

    @property
    def error(self) -> str | None:
        return None

    def _find_matching_skills(self, query: str, top_n: int = 10) -> list[str]:
        """Find graph_skills that are relevant to this query.

        Strategy: search the `skills` text field (which has BM25 analyzer)
        for the query, then aggregate the top graph_skills from matching jobs.
        This effectively does: "what skills do jobs matching this query have?"
        """
        client = self._get_client()
        result = client.search(
            index=settings.opensearch_index,
            body={
                "size": 0,
                "query": {
                    "multi_match": {
                        "query": query,
                        "fields": ["skills^2", "title^3", "job_category_l3^2"],
                        "type": "best_fields",
                    }
                },
                "aggs": {
                    "top_skills": {
                        "terms": {"field": "graph_skills", "size": top_n}
                    }
                },
            },
        )
        buckets = result.get("aggregations", {}).get("top_skills", {}).get("buckets", [])
        return [b["key"] for b in buckets]

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
        start = time.perf_counter_ns()
        client = self._get_client()
        lookup = self._get_lookup()

        # Step 1: Find matching skills
        matched_skills = self._find_matching_skills(ks)

        # Step 2: Build filters (reuse no-graph-search logic)
        filters = Filters(
            location_codes=c0 or [],
            job_types=job_types or [],
            work_hours=work_hours or [],
            salary_min=salary_min,
        )
        filter_clauses, notes = filters.build(lookup)

        # Step 3: Build hybrid query with graph_skills boost
        vector = self._embed_query(ks)

        # BM25 sub-query with skill boost
        bm25 = bm25_query(ks)
        bm25_with_boost: dict[str, Any] = {
            "bool": {
                "must": [bm25],
            }
        }
        if filter_clauses:
            bm25_with_boost["bool"]["filter"] = filter_clauses

        # Add graph_skills boost as a should clause
        if matched_skills:
            bm25_with_boost["bool"]["should"] = [
                {
                    "terms": {
                        "graph_skills": matched_skills,
                        "boost": SKILL_BOOST_WEIGHT,
                    }
                }
            ]

        # kNN sub-query
        knn = knn_query(vector, top_k, filter_clauses)

        body = {
            "size": top_k,
            "_source": SOURCE_FIELDS + ["graph_skills"],
            "query": {"hybrid": {"queries": [bm25_with_boost, knn]}},
        }

        response = client.search(
            index=settings.opensearch_index,
            body=body,
            params={"search_pipeline": SEARCH_PIPELINE_ID},
        )

        took_ms = (time.perf_counter_ns() - start) // 1_000_000

        hits = []
        for hit in response["hits"]["hits"]:
            src = hit["_source"]
            job_skills = src.get("graph_skills", [])
            # Which of our matched skills does this job have?
            job_matched = [s for s in matched_skills if s in job_skills] if job_skills else []

            hits.append(
                HitResponse(
                    score=hit["_score"],
                    job_id=src.get("job_id", hit["_id"]),
                    title=src.get("title"),
                    city=src.get("city"),
                    district=src.get("district"),
                    job_category_l3=src.get("job_category_l3"),
                    job_category_l2=src.get("job_category_l2"),
                    job_category_l1=src.get("job_category_l1"),
                    industry_l3=src.get("industry_l3"),
                    salary_text=src.get("salary_text"),
                    salary_type=src.get("salary_type"),
                    salary_min=src.get("salary_min"),
                    salary_max=src.get("salary_max"),
                    is_negotiable=src.get("is_negotiable"),
                    job_type=src.get("job_type"),
                    work_hours=src.get("work_hours"),
                    education=src.get("education"),
                    experience=src.get("experience"),
                    company_id=src.get("company_id"),
                    updated_at=src.get("updated_at"),
                    content=src.get("content"),
                    matched_skills=job_matched if job_matched else None,
                )
            )

        return BackendResult(
            backend="graph",
            mode="hybrid+skill-boost",
            took_ms=took_ms,
            total=response["hits"]["total"]["value"],
            filter_notes={
                "skills_matched": ", ".join(matched_skills) if matched_skills else "(none)",
                **notes,
            },
            hits=hits,
        )
