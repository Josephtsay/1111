from __future__ import annotations

import itertools
import math
from collections import Counter, defaultdict
from typing import Iterable

import pandas as pd

from .schema import TestLeakageError


def _require_train(rows: pd.DataFrame, split_column: str) -> pd.DataFrame:
    splits = rows[split_column].astype(str).str.lower().str.strip()
    if (~splits.eq("train")).any():
        leaked = rows.loc[~splits.eq("train")].head(5).to_dict("records")
        raise TestLeakageError(f"Train-only statistic received non-train rows: {leaked}")
    return rows


def compute_cooccurrence(
    job_skills: pd.DataFrame,
    *,
    job_id_column: str = "job_id",
    skill_column: str = "skill",
    split_column: str = "data_split",
    min_cooccurrence: int = 5,
    min_npmi: float = 0.1,
    train_start: str | None = None,
    train_end: str | None = None,
) -> pd.DataFrame:
    rows = _require_train(job_skills, split_column)
    per_job = {
        str(job_id): sorted(set(group[skill_column].dropna().astype(str)))
        for job_id, group in rows.groupby(job_id_column, sort=True)
    }
    total_jobs = len(per_job)
    skill_freq: Counter[str] = Counter()
    pair_freq: Counter[tuple[str, str]] = Counter()
    for skills in per_job.values():
        skill_freq.update(skills)
        pair_freq.update(itertools.combinations(skills, 2))

    records = []
    for (left, right), count in sorted(pair_freq.items()):
        if count < min_cooccurrence or total_jobs == 0:
            continue
        p_ab = count / total_jobs
        p_a = skill_freq[left] / total_jobs
        p_b = skill_freq[right] / total_jobs
        pmi = math.log(p_ab / (p_a * p_b)) if p_a and p_b and p_ab else 0.0
        npmi = pmi / (-math.log(p_ab)) if 0 < p_ab < 1 else 0.0
        if npmi < min_npmi:
            continue
        records.append(
            {
                "skill_a": left,
                "skill_b": right,
                "count": count,
                "support": p_ab,
                "pmi": pmi,
                "npmi": npmi,
                "p_b_given_a": count / skill_freq[left],
                "p_a_given_b": count / skill_freq[right],
                "train_start": train_start,
                "train_end": train_end,
            }
        )
    return pd.DataFrame.from_records(
        records,
        columns=[
            "skill_a",
            "skill_b",
            "count",
            "support",
            "pmi",
            "npmi",
            "p_b_given_a",
            "p_a_given_b",
            "train_start",
            "train_end",
        ],
    )


def compute_core_skills(
    job_skills: pd.DataFrame,
    *,
    job_id_column: str = "job_id",
    occupation_column: str = "occupation_code",
    skill_column: str = "skill",
    requirement_column: str = "requirement",
    split_column: str = "data_split",
) -> pd.DataFrame:
    rows = _require_train(job_skills, split_column)
    job_occupations = (
        rows[[job_id_column, occupation_column]]
        .drop_duplicates()
        .groupby(occupation_column)[job_id_column]
        .nunique()
    )
    records = []
    for (occupation, skill), group in rows.groupby(
        [occupation_column, skill_column], sort=True
    ):
        denominator = int(job_occupations.loc[occupation])
        by_requirement = {
            requirement: group.loc[
                group[requirement_column].astype(str).eq(requirement), job_id_column
            ].nunique()
            / denominator
            for requirement in ("required", "preferred", "mentioned")
        }
        records.append(
            {
                "occupation_code": str(occupation),
                "skill": str(skill),
                "core_skill_weight": group[job_id_column].nunique() / denominator,
                "required_rate": by_requirement["required"],
                "preferred_rate": by_requirement["preferred"],
                "mentioned_rate": by_requirement["mentioned"],
                "job_count": denominator,
            }
        )
    return pd.DataFrame.from_records(records)

