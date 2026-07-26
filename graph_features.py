from __future__ import annotations

import math
import re
from collections import Counter

import numpy as np
import pandas as pd

from .models import QueryParseResult
from .retrieval import Candidate, InMemorySkillGraph


NON_GRAPH_FEATURE_NAMES = [
    "bm25_score",
    "dense_score",
    "title_overlap",
    "description_overlap",
    "location_match",
    "duty_code_match",
    "freshness_score",
    "salary_min_log",
    "salary_max_log",
]
BIAS_DIAGNOSTIC_FEATURE_NAMES = ["original_rank_reciprocal"]
GRAPH_FEATURE_NAMES = [
    "graph_score",
    "exact_skill_count",
    "one_hop_skill_count",
    "required_match_weight",
    "preferred_match_weight",
    "mentioned_match_weight",
    "occupation_match",
    "best_path_score",
    "mean_path_score",
]
ALL_FEATURE_NAMES = NON_GRAPH_FEATURE_NAMES + GRAPH_FEATURE_NAMES


def tokenize(text: str) -> list[str]:
    latin = re.findall(r"[a-z0-9+#.]+", text.casefold())
    han = re.findall(r"[\u3400-\u9fff]", text)
    return latin + han


def lexical_overlap(query: str, text: str) -> float:
    query_tokens = set(tokenize(query))
    if not query_tokens:
        return 0.0
    return len(query_tokens & set(tokenize(text))) / len(query_tokens)


def _deterministic_dense_score(query: str, text: str) -> float:
    query_counts = Counter(tokenize(query))
    text_counts = Counter(tokenize(text))
    vocabulary = sorted(set(query_counts) | set(text_counts))
    if not vocabulary:
        return 0.0
    q = np.array([query_counts[token] for token in vocabulary], dtype=float)
    d = np.array([text_counts[token] for token in vocabulary], dtype=float)
    denominator = np.linalg.norm(q) * np.linalg.norm(d)
    return float(np.dot(q, d) / denominator) if denominator else 0.0


def compute_pair_features(
    *,
    query_id: str,
    query: str,
    job_id: str,
    title: str,
    description: str,
    requirements: str,
    graph: InMemorySkillGraph,
    parsed_query: QueryParseResult | None = None,
    retrieved_candidates: dict[str, Candidate] | None = None,
    bm25_score: float = 0.0,
) -> dict[str, object]:
    parsed = parsed_query or graph.parse_query(query)
    candidate = (
        retrieved_candidates.get(job_id)
        if retrieved_candidates is not None
        else next(
            (
                item
                for item in graph.retrieve(parsed, top_k=len(graph.nodes))
                if item.job_id == job_id
            ),
            None,
        )
    )
    traces = candidate.traces if candidate else []
    exact = [
        trace
        for trace in traces
        if len(trace.edge_types) == 1
        and trace.edge_types[0] in {"REQUIRES", "PREFERS", "MENTIONS"}
    ]
    expanded = [trace for trace in traces if len(trace.edge_types) == 2]
    relation_sums = {
        relation: sum(
            trace.final_path_score
            for trace in exact
            if trace.edge_types[-1] == relation
        )
        for relation in ("REQUIRES", "PREFERS", "MENTIONS")
    }
    path_scores = [trace.final_path_score for trace in traces]
    full_text = f"{title} {requirements} {description}"
    return {
        "query_id": str(query_id),
        "job_id": str(job_id),
        "bm25_score": float(bm25_score),
        "dense_score": _deterministic_dense_score(query, full_text),
        "title_overlap": lexical_overlap(query, title),
        "description_overlap": lexical_overlap(query, f"{requirements} {description}"),
        "location_match": 0.0,
        "duty_code_match": 0.0,
        "freshness_score": 0.0,
        "salary_min_log": 0.0,
        "salary_max_log": 0.0,
        "original_rank_reciprocal": 0.0,
        "graph_score": float(candidate.graph_score if candidate else 0.0),
        "exact_skill_count": len({trace.canonical_anchor for trace in exact}),
        "one_hop_skill_count": len({trace.canonical_anchor for trace in expanded}),
        "required_match_weight": relation_sums["REQUIRES"],
        "preferred_match_weight": relation_sums["PREFERS"],
        "mentioned_match_weight": relation_sums["MENTIONS"],
        "occupation_match": float(
            any(trace.edge_types == ["INSTANCE_OF"] for trace in traces)
        ),
        "best_path_score": max(path_scores, default=0.0),
        "mean_path_score": float(np.mean(path_scores)) if path_scores else 0.0,
    }


def compute_feature_frame(
    queries: pd.DataFrame,
    jobs: pd.DataFrame,
    graph: InMemorySkillGraph,
    *,
    labels: pd.DataFrame | None = None,
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    label_lookup: dict[tuple[str, str], float] = {}
    split_lookup: dict[tuple[str, str], str] = {}
    if labels is not None:
        for _, label in labels.iterrows():
            key = (str(label["query_id"]), str(label["job_id"]))
            label_lookup[key] = float(label["relevance"])
            split_lookup[key] = str(label.get("data_split", "train"))
    for _, query_row in queries.sort_values("query_id").iterrows():
        query_id = str(query_row["query_id"])
        query_text = str(query_row["query"])
        parsed = graph.parse_query(query_text)
        candidates = {
            item.job_id: item
            for item in graph.retrieve(parsed, top_k=len(graph.nodes))
        }
        for _, job_row in jobs.sort_values("job_id").iterrows():
            job_id = str(job_row["job_id"])
            pair = compute_pair_features(
                query_id=query_id,
                query=query_text,
                job_id=job_id,
                title=str(job_row["title"]),
                description=str(job_row["description"]),
                requirements=str(job_row["requirements"]),
                graph=graph,
                parsed_query=parsed,
                retrieved_candidates=candidates,
            )
            add_structured_pair_features(pair, query_row, job_row, None)
            key = (query_id, job_id)
            if labels is not None:
                pair["relevance"] = label_lookup.get(key, 0.0)
                pair["data_split"] = split_lookup.get(
                    key, str(query_row.get("data_split", "train"))
                )
            rows.append(pair)
    frame = pd.DataFrame(rows)
    numeric = ALL_FEATURE_NAMES + (["relevance"] if "relevance" in frame else [])
    for column in numeric:
        frame[column] = pd.to_numeric(frame[column], errors="raise")
        if not np.isfinite(frame[column]).all():
            raise ValueError(f"Non-finite feature values in {column}")
    return frame.sort_values(["query_id", "job_id"]).reset_index(drop=True)


def _multi_values(value: object) -> set[str]:
    if value is None or pd.isna(value):
        return set()
    return {
        item.strip()
        for item in re.split(r"[,|]", str(value))
        if item.strip()
    }


def add_structured_pair_features(
    pair: dict[str, object],
    query_row: pd.Series,
    job_row: pd.Series,
    label_row: pd.Series | None,
) -> None:
    query_locations = _multi_values(query_row.get("location_code"))
    query_duties = _multi_values(
        query_row.get("occupation_code", query_row.get("duty_code"))
    )
    job_location = str(job_row.get("location_code", "") or "")
    job_duty = str(job_row.get("occupation_code", "") or "")
    pair["location_match"] = float(
        bool(query_locations) and job_location in query_locations
    )
    pair["duty_code_match"] = float(
        bool(query_duties) and job_duty in query_duties
    )
    query_time = pd.to_datetime(
        query_row.get("query_time"), errors="coerce", utc=True, format="mixed"
    )
    posted_at = pd.to_datetime(
        job_row.get("posted_at"), errors="coerce", utc=True, format="mixed"
    )
    if pd.notna(query_time) and pd.notna(posted_at) and query_time >= posted_at:
        age_days = (query_time - posted_at).total_seconds() / 86400
        pair["freshness_score"] = 1.0 / (1.0 + max(age_days, 0.0) / 30.0)
    pair["salary_min_log"] = float(
        np.log1p(max(0.0, float(job_row.get("salary_min", 0.0) or 0.0)))
    )
    pair["salary_max_log"] = float(
        np.log1p(max(0.0, float(job_row.get("salary_max", 0.0) or 0.0)))
    )
    original_rank = (
        float(label_row.get("original_rank", 0.0) or 0.0)
        if label_row is not None
        else 0.0
    )
    pair["original_rank_reciprocal"] = (
        1.0 / original_rank if original_rank > 0 else 0.0
    )


def compute_labeled_feature_frame(
    queries: pd.DataFrame,
    jobs: pd.DataFrame,
    labels: pd.DataFrame,
    graph: InMemorySkillGraph,
) -> pd.DataFrame:
    """Compute only observed query-job candidates, never a full cartesian product."""

    required_labels = {
        "query_id",
        "job_id",
        "relevance",
        "original_rank",
        "data_split",
    }
    if missing := required_labels - set(labels.columns):
        raise ValueError(f"labels missing columns: {sorted(missing)}")
    query_lookup = {
        str(row["query_id"]): row for _, row in queries.iterrows()
    }
    job_lookup = {str(row["job_id"]): row for _, row in jobs.iterrows()}
    unknown_queries = sorted(set(labels["query_id"].astype(str)) - set(query_lookup))
    unknown_jobs = sorted(set(labels["job_id"].astype(str)) - set(job_lookup))
    if unknown_queries:
        raise ValueError(f"labels reference unknown queries: {unknown_queries[:10]}")
    if unknown_jobs:
        raise ValueError(f"labels reference unknown jobs: {unknown_jobs[:10]}")

    rows: list[dict[str, object]] = []
    for query_id, label_group in labels.groupby("query_id", sort=True):
        query_row = query_lookup[str(query_id)]
        query_text = str(query_row["query"])
        parsed = graph.parse_query(query_text)
        duties = sorted(_multi_values(query_row.get("occupation_code")))
        if duties:
            parsed.occupation_candidate = duties[0]
        candidates = {
            item.job_id: item
            for item in graph.retrieve(parsed, top_k=len(graph.nodes))
        }
        for _, label_row in label_group.sort_values(
            ["original_rank", "job_id"]
        ).iterrows():
            job_row = job_lookup[str(label_row["job_id"])]
            pair = compute_pair_features(
                query_id=str(query_id),
                query=query_text,
                job_id=str(label_row["job_id"]),
                title=str(job_row["title"]),
                description=str(job_row["description"]),
                requirements=str(job_row["requirements"]),
                graph=graph,
                parsed_query=parsed,
                retrieved_candidates=candidates,
            )
            add_structured_pair_features(pair, query_row, job_row, label_row)
            pair["relevance"] = float(label_row["relevance"])
            pair["data_split"] = str(label_row["data_split"])
            pair["original_rank"] = int(label_row["original_rank"])
            rows.append(pair)
    frame = pd.DataFrame(rows)
    numeric = (
        ALL_FEATURE_NAMES
        + BIAS_DIAGNOSTIC_FEATURE_NAMES
        + ["relevance", "original_rank"]
    )
    for column in numeric:
        frame[column] = pd.to_numeric(frame[column], errors="raise")
        if not np.isfinite(frame[column]).all():
            raise ValueError(f"Non-finite feature values in {column}")
    return frame.sort_values(
        ["query_id", "original_rank", "job_id"]
    ).reset_index(drop=True)


def select_feature_set(name: str) -> list[str]:
    normalized = name.upper()
    if normalized == "A":
        return ["bm25_score"]
    if normalized == "B":
        return ["bm25_score", "dense_score"]
    if normalized == "C":
        return NON_GRAPH_FEATURE_NAMES
    if normalized == "D":
        return ALL_FEATURE_NAMES
    raise ValueError("Feature set must be one of A, B, C, D")


def weighted_baseline_score(frame: pd.DataFrame, feature_set: str) -> pd.Series:
    columns = select_feature_set(feature_set)
    weights = {
        "bm25_score": 1.0,
        "dense_score": 0.8,
        "title_overlap": 0.4,
        "description_overlap": 0.2,
        "location_match": 0.3,
        "duty_code_match": 0.4,
        "freshness_score": 0.15,
        "salary_min_log": 0.02,
        "salary_max_log": 0.02,
        "graph_score": 1.0,
        "exact_skill_count": 0.6,
        "one_hop_skill_count": 0.2,
        "required_match_weight": 0.8,
        "preferred_match_weight": 0.45,
        "mentioned_match_weight": 0.2,
        "occupation_match": 0.5,
        "best_path_score": 0.3,
        "mean_path_score": 0.2,
    }
    score = pd.Series(0.0, index=frame.index)
    for column in columns:
        values = frame[column].astype(float)
        scale = values.std(ddof=0)
        normalized = (values - values.mean()) / scale if scale > 0 else values
        score += normalized * weights[column]
    return score.replace([math.inf, -math.inf], 0.0)
