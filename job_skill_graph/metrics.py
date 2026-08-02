from __future__ import annotations

from typing import Callable

import numpy as np
import pandas as pd


def dcg(relevances: list[float], k: int = 10) -> float:
    values = np.asarray(relevances[:k], dtype=float)
    if not len(values):
        return 0.0
    discounts = np.log2(np.arange(2, len(values) + 2))
    return float(np.sum((np.power(2.0, values) - 1.0) / discounts))


def ndcg_at_k(relevances: list[float], k: int = 10) -> float:
    actual = dcg(relevances, k)
    ideal = dcg(sorted(relevances, reverse=True), k)
    return actual / ideal if ideal else 0.0


def reciprocal_rank(relevances: list[float]) -> float:
    for rank, relevance in enumerate(relevances, start=1):
        if relevance > 0:
            return 1.0 / rank
    return 0.0


def hit_at_k(relevances: list[float], k: int = 10) -> float:
    return float(any(value > 0 for value in relevances[:k]))


def precision_at_k(relevances: list[float], k: int = 10) -> float:
    """Official workshop definition: denominator stays k for short result lists."""

    if k <= 0:
        raise ValueError("k must be positive")
    return sum(value > 0 for value in relevances[:k]) / k


def top1_relevance(relevances: list[float]) -> float:
    return float(bool(relevances) and relevances[0] > 0)


def evaluate_rankings(
    rankings: pd.DataFrame,
    *,
    query_col: str = "query_id",
    relevance_col: str = "relevance",
    score_col: str = "score",
    k: int = 10,
) -> tuple[dict[str, float], pd.DataFrame]:
    required = {query_col, relevance_col, score_col}
    if missing := required - set(rankings.columns):
        raise ValueError(f"rankings missing columns: {sorted(missing)}")
    rows: list[dict[str, float | str]] = []
    for query_id, group in rankings.groupby(query_col, sort=True):
        ordered = group.sort_values(
            [score_col, "job_id"], ascending=[False, True]
        )
        relevance = ordered[relevance_col].astype(float).tolist()
        rows.append(
            {
                "query_id": str(query_id),
                f"ndcg@{k}": ndcg_at_k(relevance, k),
                f"precision@{k}": precision_at_k(relevance, k),
                "top1": top1_relevance(relevance),
                "mrr": reciprocal_rank(relevance),
                f"hit@{k}": hit_at_k(relevance, k),
            }
        )
    per_query = pd.DataFrame(rows)
    metrics = {
        column: float(per_query[column].mean()) if len(per_query) else 0.0
        for column in (
            f"ndcg@{k}",
            f"precision@{k}",
            "top1",
            "mrr",
            f"hit@{k}",
        )
    }
    metrics["query_count"] = float(len(per_query))
    return metrics, per_query


def paired_bootstrap_difference(
    baseline: pd.DataFrame,
    treatment: pd.DataFrame,
    *,
    metric: str = "ndcg@10",
    iterations: int = 2000,
    seed: int = 42,
) -> dict[str, float]:
    joined = baseline[["query_id", metric]].merge(
        treatment[["query_id", metric]],
        on="query_id",
        suffixes=("_baseline", "_treatment"),
        validate="one_to_one",
    )
    if joined.empty:
        raise ValueError("No common queries for paired bootstrap")
    differences = (
        joined[f"{metric}_treatment"] - joined[f"{metric}_baseline"]
    ).to_numpy(dtype=float)
    rng = np.random.default_rng(seed)
    boot = np.empty(iterations, dtype=float)
    for index in range(iterations):
        sample = rng.choice(differences, size=len(differences), replace=True)
        boot[index] = sample.mean()
    return {
        "mean_difference": float(differences.mean()),
        "ci_low": float(np.quantile(boot, 0.025)),
        "ci_high": float(np.quantile(boot, 0.975)),
        "probability_positive": float(np.mean(boot > 0)),
        "iterations": float(iterations),
        "seed": float(seed),
    }
