from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
import yaml


class DataValidationError(ValueError):
    """Raised when configured input data does not satisfy the declared schema."""


class TestLeakageError(DataValidationError):
    """Raised whenever test data can influence a train-only artifact."""


REQUIRED_MAPPING_KEYS = {
    "jobs": {
        "job_id",
        "title",
        "requirements",
        "description",
        "posted_at",
        "location_code",
        "occupation_code",
        "split",
    },
    "queries": {
        "query_id",
        "query_text",
        "query_time",
        "session_id",
        "location_code",
        "occupation_code",
        "exposed_jobs",
        "exposed_ranks",
    },
    "labels": {"query_id", "job_id", "relevance", "original_rank"},
}


def load_yaml(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise DataValidationError(f"YAML root must be a mapping: {path}")
    return data


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_json_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ColumnMapping:
    values: dict[str, dict[str, str]]

    @classmethod
    def from_yaml(cls, path: str | Path) -> "ColumnMapping":
        data = load_yaml(path)
        for section, required in REQUIRED_MAPPING_KEYS.items():
            actual = data.get(section)
            if not isinstance(actual, dict):
                raise DataValidationError(f"Missing mapping section: {section}")
            missing_keys = required - set(actual)
            if missing_keys:
                raise DataValidationError(
                    f"Mapping section {section} is missing keys: {sorted(missing_keys)}"
                )
            empty = sorted(key for key in required if not str(actual.get(key) or "").strip())
            if empty:
                raise DataValidationError(
                    f"Mapping section {section} has empty values: {empty}; "
                    "fill exact CSV headers instead of relying on guesses"
                )
        return cls(values=data)

    def section(self, name: str) -> dict[str, str]:
        return self.values[name]


def _assert_columns(frame: pd.DataFrame, mapping: dict[str, str], table: str) -> None:
    missing = sorted(set(mapping.values()) - set(frame.columns))
    if missing:
        raise DataValidationError(f"{table} CSV is missing configured columns: {missing}")


def _parse_time_column(
    frame: pd.DataFrame, column: str, table: str, allow_empty: bool = False
) -> pd.Series:
    parsed = pd.to_datetime(
        frame[column], errors="coerce", utc=True, format="mixed"
    )
    invalid = parsed.isna() & frame[column].notna() & frame[column].astype(str).str.strip().ne("")
    if invalid.any() or (not allow_empty and parsed.isna().any()):
        examples = frame.loc[invalid | parsed.isna(), column].head(5).tolist()
        raise DataValidationError(f"{table}.{column} has unparseable timestamps: {examples}")
    return parsed


def validate_dataframes(
    jobs: pd.DataFrame,
    queries: pd.DataFrame,
    labels: pd.DataFrame,
    mapping: ColumnMapping,
    *,
    train_cutoff: str | pd.Timestamp | None = None,
) -> dict[str, Any]:
    jm, qm, lm = (mapping.section(name) for name in ("jobs", "queries", "labels"))
    _assert_columns(jobs, jm, "jobs")
    _assert_columns(queries, qm, "queries")
    _assert_columns(labels, lm, "labels")

    job_ids = jobs[jm["job_id"]].astype(str)
    if job_ids.duplicated().any():
        duplicates = job_ids[job_ids.duplicated(keep=False)].head(10).tolist()
        raise DataValidationError(f"jobs job_id must be unique; duplicates: {duplicates}")

    job_times = _parse_time_column(jobs, jm["posted_at"], "jobs")
    query_times = _parse_time_column(queries, qm["query_time"], "queries")
    relevance = pd.to_numeric(labels[lm["relevance"]], errors="coerce")
    invalid_relevance = ~relevance.isin([0, 1, 2])
    if invalid_relevance.any():
        values = labels.loc[invalid_relevance, lm["relevance"]].head(10).tolist()
        raise DataValidationError(f"labels relevance must contain only 0, 1, 2; got {values}")

    splits = jobs[jm["split"]].astype(str).str.lower().str.strip()
    allowed_splits = {"train", "validation", "test"}
    invalid_splits = sorted(set(splits) - allowed_splits)
    if invalid_splits:
        raise DataValidationError(f"Unknown jobs split values: {invalid_splits}")

    split_ranges: dict[str, dict[str, str | None]] = {}
    for split in ("train", "validation", "test"):
        values = job_times[splits.eq(split)]
        split_ranges[split] = {
            "min": values.min().isoformat() if not values.empty else None,
            "max": values.max().isoformat() if not values.empty else None,
        }

    nonempty_ranges = [
        (split, job_times[splits.eq(split)].min(), job_times[splits.eq(split)].max())
        for split in ("train", "validation", "test")
        if splits.eq(split).any()
    ]
    for (_, _, previous_max), (current, current_min, _) in zip(
        nonempty_ranges, nonempty_ranges[1:]
    ):
        if previous_max >= current_min:
            raise DataValidationError(
                f"Temporal split order overlaps before {current}: "
                f"previous max {previous_max}, current min {current_min}"
            )

    if train_cutoff is not None:
        cutoff = pd.Timestamp(train_cutoff)
        cutoff = cutoff.tz_localize("UTC") if cutoff.tzinfo is None else cutoff.tz_convert("UTC")
        train_mask = splits.eq("train")
        if (job_times[train_mask] > cutoff).any():
            raise TestLeakageError("Train graph input contains JD later than train cutoff")
        if (splits.ne("train")).any() and cutoff >= job_times[splits.ne("train")].min():
            raise TestLeakageError("Train cutoff must be earlier than validation/test JD timestamps")

    label_queries = set(labels[lm["query_id"]].astype(str))
    known_queries = set(queries[qm["query_id"]].astype(str))
    unknown_query_ids = sorted(label_queries - known_queries)
    if unknown_query_ids:
        raise DataValidationError(f"labels reference unknown query_id: {unknown_query_ids[:10]}")

    return {
        "jobs": len(jobs),
        "queries": len(queries),
        "labels": len(labels),
        "split_ranges": split_ranges,
        "query_time_min": query_times.min().isoformat(),
        "query_time_max": query_times.max().isoformat(),
        "status": "valid",
    }


def assert_train_only_jobs(
    jobs: pd.DataFrame, mapping: ColumnMapping, *, artifact_name: str = "graph"
) -> None:
    jm = mapping.section("jobs")
    _assert_columns(jobs, jm, "jobs")
    splits = jobs[jm["split"]].astype(str).str.lower().str.strip()
    leaked = jobs.loc[~splits.eq("train"), jm["job_id"]].astype(str).tolist()
    if leaked:
        raise TestLeakageError(
            f"{artifact_name} accepts train JD only; found {len(leaked)} non-train jobs: "
            f"{leaked[:10]}"
        )


def load_and_validate_csvs(
    jobs_path: str | Path,
    queries_path: str | Path,
    labels_path: str | Path,
    mapping_path: str | Path,
    *,
    train_cutoff: str | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, ColumnMapping, dict[str, Any]]:
    mapping = ColumnMapping.from_yaml(mapping_path)
    jobs = pd.read_csv(jobs_path)
    queries = pd.read_csv(queries_path)
    labels = pd.read_csv(labels_path)
    report = validate_dataframes(
        jobs, queries, labels, mapping, train_cutoff=train_cutoff
    )
    return jobs, queries, labels, mapping, report
