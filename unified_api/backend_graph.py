"""Adapter for graph-enhanced search backend.

Uses OpenSearch (via no-graph-search) for BM25/hybrid candidate retrieval,
then applies skill-graph rerank from job_skill_graph.
No SQLite FTS index needed.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# Allow importing job_skill_graph package and no-graph-search
_REPO_ROOT = Path(__file__).resolve().parent.parent
_NO_GRAPH_ROOT = _REPO_ROOT / "no-graph-search"
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if str(_NO_GRAPH_ROOT) not in sys.path:
    sys.path.insert(0, str(_NO_GRAPH_ROOT))

from src.search import Filters, JobSearch  # noqa: E402

from .schema import BackendResult, HitResponse  # noqa: E402


class GraphBackend:
    """OpenSearch candidates + skill-graph rerank.

    Retrieval: same OpenSearch hybrid as no-graph-search (shared instance).
    Rerank: InMemorySkillGraph from nodes.csv + edges.csv boosts candidates
    that match skill traversal paths.

    Requires:
      - OpenSearch connection (same .env as no-graph-search)
      - Graph artifacts: nodes.csv + edges.csv (env: GRAPH_DIR, default: ./graph/)

    If graph artifacts are missing, returns an error instead of crashing.
    """

    def __init__(self, searcher: JobSearch | None = None) -> None:
        self._searcher = searcher
        self._graph = None
        self._available: bool | None = None
        self._error: str | None = None

    def _get_searcher(self) -> JobSearch:
        if self._searcher is None:
            self._searcher = JobSearch()
        return self._searcher

    def _init_graph(self):
        """Lazy-load graph artifacts."""
        if self._available is not None:
            return

        try:
            import pandas as pd
            from job_skill_graph.retrieval import InMemorySkillGraph

            graph_dir = Path(os.environ.get("GRAPH_DIR", _REPO_ROOT / "graph"))
            nodes_path = graph_dir / "nodes.csv"
            edges_path = graph_dir / "edges.csv"

            if not nodes_path.is_file():
                raise FileNotFoundError(f"nodes.csv not found: {nodes_path}")
            if not edges_path.is_file():
                raise FileNotFoundError(f"edges.csv not found: {edges_path}")

            nodes = pd.read_csv(nodes_path, dtype=str, keep_default_na=False)
            edges = pd.read_csv(edges_path, dtype=str, keep_default_na=False)

            # Column mapping: actual CSV → InMemorySkillGraph expected
            if "node_type" in nodes.columns and "label" not in nodes.columns:
                nodes = nodes.rename(columns={"node_type": "label"})
            if "name" in nodes.columns and "canonical_name" not in nodes.columns:
                nodes["canonical_name"] = nodes["name"]
            if "source_id" in edges.columns and "from_id" not in edges.columns:
                edges = edges.rename(columns={
                    "source_id": "from_id",
                    "target_id": "to_id",
                    "edge_type": "label",
                })

            self._graph = InMemorySkillGraph(nodes, edges)
            self._available = True

        except Exception as e:
            self._available = False
            self._error = str(e)

    @property
    def available(self) -> bool:
        self._init_graph()
        return self._available

    @property
    def error(self) -> str | None:
        self._init_graph()
        return self._error

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
        self._init_graph()

        if not self._available:
            return BackendResult(
                backend="graph",
                mode=mode,
                took_ms=0,
                total=0,
                filter_notes={"error": self._error or "graph backend unavailable"},
                hits=[],
            )

        start = time.perf_counter_ns()

        # --- Step 1: Get candidates from OpenSearch (same as no-graph) ---
        searcher = self._get_searcher()
        filters = Filters(
            location_codes=c0 or [],
            job_types=job_types or [],
            work_hours=work_hours or [],
            salary_min=salary_min,
        )
        # Fetch a larger candidate pool for reranking
        candidate_pool = max(top_k * 5, 50)
        os_result = searcher.search(ks, filters=filters, top_k=candidate_pool, mode=mode)
        os_hits = os_result.get("hits", [])

        if not os_hits:
            took_ms = (time.perf_counter_ns() - start) // 1_000_000
            return BackendResult(
                backend="graph",
                mode="graph-rerank",
                took_ms=took_ms,
                total=0,
                filter_notes={},
                hits=[],
            )

        # --- Step 2: Graph traversal for skill matching ---
        parsed = self._graph.parse_query(ks)
        graph_candidates = self._graph.retrieve(
            parsed,
            top_k=candidate_pool,
            location_codes=c0 or [],
        )
        graph_scores = {c.job_id: c for c in graph_candidates}

        # --- Step 3: Combine OpenSearch score + graph score ---
        # Normalize OpenSearch scores to [0, 1]
        os_scores_raw = [h["score"] for h in os_hits]
        os_max = max(os_scores_raw) if os_scores_raw else 1.0
        os_min = min(os_scores_raw) if os_scores_raw else 0.0
        os_range = os_max - os_min if os_max > os_min else 1.0

        # Normalize graph scores to [0, 1]
        graph_scores_raw = [c.graph_score for c in graph_candidates] if graph_candidates else []
        g_max = max(graph_scores_raw) if graph_scores_raw else 1.0
        g_min = min(graph_scores_raw) if graph_scores_raw else 0.0
        g_range = g_max - g_min if g_max > g_min else 1.0

        # Merge: OpenSearch weight 0.6, Graph weight 0.4
        OS_WEIGHT = 0.6
        GRAPH_WEIGHT = 0.4

        reranked = []
        for h in os_hits:
            job_id = h["job_id"]
            os_norm = (h["score"] - os_min) / os_range

            gc = graph_scores.get(job_id)
            if gc:
                g_norm = (gc.graph_score - g_min) / g_range
                matched_skills = gc.matched_skills
            else:
                g_norm = 0.0
                matched_skills = None

            combined = os_norm * OS_WEIGHT + g_norm * GRAPH_WEIGHT

            reranked.append({
                **h,
                "combined_score": combined,
                "graph_score": g_norm,
                "matched_skills": matched_skills,
            })

        # Sort by combined score
        reranked.sort(key=lambda x: -x["combined_score"])
        reranked = reranked[:top_k]

        took_ms = (time.perf_counter_ns() - start) // 1_000_000

        hits = [
            HitResponse(
                score=r["combined_score"],
                job_id=r["job_id"],
                title=r.get("title"),
                city=r.get("city"),
                district=r.get("district"),
                job_category_l3=r.get("job_category_l3"),
                job_category_l2=r.get("job_category_l2"),
                job_category_l1=r.get("job_category_l1"),
                industry_l3=r.get("industry_l3"),
                salary_text=r.get("salary_text"),
                salary_type=r.get("salary_type"),
                salary_min=r.get("salary_min"),
                salary_max=r.get("salary_max"),
                is_negotiable=r.get("is_negotiable"),
                job_type=r.get("job_type"),
                work_hours=r.get("work_hours"),
                education=r.get("education"),
                experience=r.get("experience"),
                company_id=r.get("company_id"),
                updated_at=r.get("updated_at"),
                content=r.get("content"),
                lexical_score=r.get("score"),  # original OpenSearch score
                matched_skills=r.get("matched_skills"),
            )
            for r in reranked
        ]

        return BackendResult(
            backend="graph",
            mode="graph-rerank",
            took_ms=took_ms,
            total=os_result.get("total", len(hits)),
            filter_notes={
                "skills_parsed": ", ".join(parsed.canonical_skills) or "(none)",
                "graph_candidates": str(len(graph_candidates)),
            },
            hits=hits,
        )
