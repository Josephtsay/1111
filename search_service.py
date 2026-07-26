from __future__ import annotations

import os
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Protocol, Sequence

import duckdb
import pandas as pd


@dataclass(frozen=True)
class ScoredJob:
    job_id: str
    score: float
    lexical_score: float
    location_match: float
    duty_match: float


class SearchBackend(Protocol):
    def search(
        self,
        query: str,
        *,
        location_codes: Sequence[str] = (),
        duty_codes: Sequence[str] = (),
        limit: int = 50,
    ) -> list[ScoredJob]: ...


def search_tokens(text: str, *, max_characters: int = 2500) -> list[str]:
    """Deterministic Latin words plus Han unigrams/bigrams/trigrams."""

    normalized = unicodedata.normalize("NFKC", str(text)).casefold()[
        :max_characters
    ]
    tokens: list[str] = re.findall(r"[a-z0-9+#.]{2,}", normalized)
    for segment in re.findall(r"[\u3400-\u9fff]+", normalized):
        tokens.extend(segment)
        tokens.extend(segment[index : index + 2] for index in range(len(segment) - 1))
        tokens.extend(segment[index : index + 3] for index in range(len(segment) - 2))
    return list(dict.fromkeys(token for token in tokens if token.strip()))


def _query_terms(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", str(text)).casefold()[:500]
    terms = re.findall(r"[a-z0-9+#.]{2,}", normalized)
    for segment in re.findall(r"[\u3400-\u9fff]+", normalized):
        if len(segment) <= 2:
            terms.append(segment)
        else:
            terms.extend(
                segment[index : index + 3]
                for index in range(len(segment) - 2)
            )
    return list(dict.fromkeys(term for term in terms if term))


def _fts_query(text: str, *, operator: str = "AND") -> str:
    tokens = _query_terms(text)
    return f" {operator} ".join(
        f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens
    )


def _read_jobs_query(path: Path, limit: int | None) -> str:
    escaped = path.resolve().as_posix().replace("'", "''")
    suffix = f" LIMIT {int(limit)}" if limit is not None else ""
    return f"""
        SELECT
            cast(job_id AS VARCHAR) AS job_id,
            coalesce(cast(title AS VARCHAR), '') AS title,
            coalesce(cast(description AS VARCHAR), '') AS description,
            coalesce(cast(requirements AS VARCHAR), '') AS requirements,
            coalesce(cast(location_code AS VARCHAR), '') AS location_code,
            coalesce(cast(occupation_code AS VARCHAR), '') AS occupation_code,
            coalesce(cast(posted_at AS VARCHAR), '') AS posted_at,
            coalesce(try_cast(salary_min AS DOUBLE), 0.0) AS salary_min,
            coalesce(try_cast(salary_max AS DOUBLE), 0.0) AS salary_max
        FROM read_parquet('{escaped}')
        ORDER BY job_id
        {suffix}
    """


def build_sqlite_search_index(
    jobs_parquet: str | Path,
    index_path: str | Path,
    *,
    overwrite: bool = False,
    limit: int | None = None,
    batch_size: int = 2000,
) -> dict[str, object]:
    """Build a portable offline FTS index; production can replace it with OpenSearch."""

    source = Path(jobs_parquet)
    target = Path(index_path)
    if not source.is_file():
        raise FileNotFoundError(source)
    if target.exists():
        if not overwrite:
            raise FileExistsError(target)
        target.unlink()
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(target)
    duck = duckdb.connect()
    inserted = 0
    try:
        connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=NORMAL;
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                requirements TEXT NOT NULL,
                location_code TEXT NOT NULL,
                occupation_code TEXT NOT NULL,
                posted_at TEXT NOT NULL,
                salary_min REAL NOT NULL,
                salary_max REAL NOT NULL
            );
            CREATE VIRTUAL TABLE jobs_fts USING fts5(
                job_id UNINDEXED,
                search_text,
                tokenize='unicode61 remove_diacritics 2'
            );
            CREATE INDEX jobs_location_idx ON jobs(location_code);
            CREATE INDEX jobs_occupation_idx ON jobs(occupation_code);
            """
        )
        cursor = duck.execute(_read_jobs_query(source, limit))
        while rows := cursor.fetchmany(batch_size):
            job_rows = []
            fts_rows = []
            for (
                job_id,
                title,
                description,
                requirements,
                location_code,
                occupation_code,
                posted_at,
                salary_min,
                salary_max,
            ) in rows:
                job_id = str(job_id)
                search_text = " ".join(
                    search_tokens(
                        " ".join(
                            (
                                str(title),
                                str(occupation_code),
                                str(location_code),
                                str(requirements),
                                str(description),
                            )
                        )
                    )
                )
                job_rows.append(
                    (
                        job_id,
                        title,
                        description,
                        requirements,
                        location_code,
                        occupation_code,
                        posted_at,
                        salary_min,
                        salary_max,
                    )
                )
                fts_rows.append((job_id, search_text))
            connection.executemany(
                "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", job_rows
            )
            connection.executemany(
                "INSERT INTO jobs_fts(job_id, search_text) VALUES (?, ?)", fts_rows
            )
            inserted += len(job_rows)
            connection.commit()
        connection.execute("INSERT INTO jobs_fts(jobs_fts) VALUES ('optimize')")
        connection.commit()
    finally:
        duck.close()
        connection.close()
    return {
        "index_path": str(target.resolve()),
        "job_count": inserted,
        "backend": "sqlite-fts5",
        "production_replacement": "Amazon OpenSearch",
    }


class SQLiteFTSSearchBackend:
    def __init__(
        self,
        index_path: str | Path,
        *,
        location_boost: float = 0.35,
        duty_boost: float = 0.5,
    ) -> None:
        self.index_path = Path(index_path)
        if not self.index_path.is_file():
            raise FileNotFoundError(self.index_path)
        self.location_boost = location_boost
        self.duty_boost = duty_boost

    def _connect(self) -> sqlite3.Connection:
        uri = f"{self.index_path.resolve().as_uri()}?mode=ro"
        return sqlite3.connect(uri, uri=True)

    def search(
        self,
        query: str,
        *,
        location_codes: Sequence[str] = (),
        duty_codes: Sequence[str] = (),
        limit: int = 50,
    ) -> list[ScoredJob]:
        strict_query = _fts_query(query, operator="AND")
        if not strict_query:
            return []
        location_values = {str(value).strip() for value in location_codes if str(value).strip()}
        duty_values = {str(value).strip() for value in duty_codes if str(value).strip()}
        candidate_limit = max(limit * 5, 100)
        statement = """
                SELECT
                    j.job_id,
                    -bm25(jobs_fts, 0.0, 1.0) AS lexical_score,
                    j.location_code,
                    j.occupation_code
                FROM jobs_fts
                JOIN jobs j ON j.job_id = jobs_fts.job_id
                WHERE jobs_fts MATCH ?
                ORDER BY bm25(jobs_fts, 0.0, 1.0), j.job_id
                LIMIT ?
                """
        with self._connect() as connection:
            rows = connection.execute(
                statement, (strict_query, candidate_limit)
            ).fetchall()
            if len(rows) < limit:
                broad_query = _fts_query(query, operator="OR")
                broad = connection.execute(
                    statement, (broad_query, candidate_limit)
                ).fetchall()
                seen = {str(row[0]) for row in rows}
                rows.extend(row for row in broad if str(row[0]) not in seen)
        scored = []
        for job_id, lexical_score, location_code, occupation_code in rows:
            location_match = float(
                bool(location_values) and str(location_code) in location_values
            )
            duty_match = float(
                bool(duty_values) and str(occupation_code) in duty_values
            )
            score = (
                float(lexical_score)
                + location_match * self.location_boost
                + duty_match * self.duty_boost
            )
            scored.append(
                ScoredJob(
                    job_id=str(job_id),
                    score=score,
                    lexical_score=float(lexical_score),
                    location_match=location_match,
                    duty_match=duty_match,
                )
            )
        return sorted(scored, key=lambda item: (-item.score, item.job_id))[:limit]

    def fetch_jobs(self, job_ids: Sequence[str]) -> dict[str, dict[str, object]]:
        unique = list(dict.fromkeys(str(job_id) for job_id in job_ids))
        if not unique:
            return {}
        placeholders = ",".join("?" for _ in unique)
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                f"SELECT * FROM jobs WHERE job_id IN ({placeholders})", unique
            ).fetchall()
        return {str(row["job_id"]): dict(row) for row in rows}


class EmptySearchBackend:
    def search(
        self,
        query: str,
        *,
        location_codes: Sequence[str] = (),
        duty_codes: Sequence[str] = (),
        limit: int = 50,
    ) -> list[ScoredJob]:
        return []


class HybridRerankBackend:
    """Union lexical and graph candidates, then apply persisted LambdaMART."""

    def __init__(
        self,
        lexical_backend: SQLiteFTSSearchBackend,
        graph: object,
        *,
        ranker: object | None = None,
        candidate_pool: int = 200,
    ) -> None:
        self.lexical_backend = lexical_backend
        self.graph = graph
        self.ranker = ranker
        self.candidate_pool = candidate_pool

    def search(
        self,
        query: str,
        *,
        location_codes: Sequence[str] = (),
        duty_codes: Sequence[str] = (),
        limit: int = 50,
    ) -> list[ScoredJob]:
        from .graph_features import (
            ALL_FEATURE_NAMES,
            add_structured_pair_features,
            compute_pair_features,
            weighted_baseline_score,
        )

        lexical = self.lexical_backend.search(
            query,
            location_codes=location_codes,
            duty_codes=duty_codes,
            limit=self.candidate_pool,
        )
        parsed = self.graph.parse_query(query)
        if duty_codes:
            parsed.occupation_candidate = str(duty_codes[0])
        graph_candidates = self.graph.retrieve(
            parsed, top_k=self.candidate_pool
        )
        lexical_by_id = {item.job_id: item for item in lexical}
        graph_by_id = {item.job_id: item for item in graph_candidates}
        candidate_ids = list(
            dict.fromkeys([item.job_id for item in lexical] + list(graph_by_id))
        )
        jobs = self.lexical_backend.fetch_jobs(candidate_ids)
        query_row = pd.Series(
            {
                "query": query,
                "query_time": datetime.now(timezone.utc).isoformat(),
                "location_code": ",".join(location_codes),
                "occupation_code": ",".join(duty_codes),
            }
        )
        rows = []
        row_ids = []
        for job_id in candidate_ids:
            job = jobs.get(job_id)
            if not job:
                continue
            lexical_candidate = lexical_by_id.get(job_id)
            pair = compute_pair_features(
                query_id="online",
                query=query,
                job_id=job_id,
                title=str(job["title"]),
                description=str(job["description"]),
                requirements=str(job["requirements"]),
                graph=self.graph,
                parsed_query=parsed,
                retrieved_candidates=graph_by_id,
                bm25_score=(
                    lexical_candidate.lexical_score
                    if lexical_candidate is not None
                    else 0.0
                ),
            )
            add_structured_pair_features(
                pair, query_row, pd.Series(job), None
            )
            rows.append(pair)
            row_ids.append(job_id)
        if not rows:
            return []
        frame = pd.DataFrame(rows)
        for column in ALL_FEATURE_NAMES:
            if column not in frame:
                frame[column] = 0.0
        scores = (
            self.ranker.predict(frame)
            if self.ranker is not None
            else weighted_baseline_score(frame, "D").to_numpy()
        )
        output = []
        for job_id, score in zip(row_ids, scores):
            lexical_candidate = lexical_by_id.get(job_id)
            output.append(
                ScoredJob(
                    job_id=job_id,
                    score=float(score),
                    lexical_score=(
                        lexical_candidate.lexical_score
                        if lexical_candidate is not None
                        else 0.0
                    ),
                    location_match=float(
                        frame.loc[
                            frame["job_id"].astype(str).eq(job_id),
                            "location_match",
                        ].iloc[0]
                    ),
                    duty_match=float(
                        frame.loc[
                            frame["job_id"].astype(str).eq(job_id),
                            "duty_code_match",
                        ].iloc[0]
                    ),
                )
            )
        return sorted(output, key=lambda item: (-item.score, item.job_id))[:limit]
