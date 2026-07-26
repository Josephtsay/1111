from __future__ import annotations

import csv
import json
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import duckdb

from .schema import stable_json_hash


class Dataset1111Error(ValueError):
    """Raised when the six-file 1111 dataset cannot be prepared safely."""


EXPECTED_SIGNATURES = {
    "search": {"talentNo", "ks", "c0", "d0", "search_time", "empStr"},
    "browse": {"organNo", "employeeNo", "dateIn", "talentNo"},
    "apply": {"LogTitle", "empNo", "empName", "talentNo", "datein"},
    "cities": {
        "CodeNo",
        "CodeNameA",
        "CodeNameB",
        "CodeNameC",
        "CodeType",
        "CodeArea",
        "CodeZip",
    },
    "duties": {
        "CodeNo",
        "CodeNameA",
        "CodeNameB",
        "CodeNameC",
        "CodeDescript",
        "CodeAlike",
        "CodeDefinition",
        "CodeNameEN",
    },
}

JOB_COLUMNS = {
    "job_id": "職缺編號",
    "title": "職務名稱",
    "description": "職務內容",
    "salary_text": "薪資",
    "salary_min": "薪資下限",
    "salary_max": "薪資上限",
    "duty_major": "職務大類",
    "duty_middle": "職務中類",
    "duty_minor": "職務小類",
    "job_type": "職缺屬性",
    "work_hours": "工時",
    "work_hours_description": "工時說明",
    "location_code": "工作城市",
    "education": "學歷需求",
    "experience": "工作經驗需求",
    "computer_skills": "電腦技能資料",
    "certifications": "專業證照",
    "work_skills": "工作技能",
    "additional_requirements": "附加條件",
    "company_id": "廠商編號",
    "industry_major": "產業大類",
    "industry_middle": "產業中類",
    "industry_minor": "產業小類",
    "last_modified_at": "職缺最後修改時間",
}


def _read_header(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        try:
            return next(csv.reader(handle))
        except StopIteration as error:
            raise Dataset1111Error(f"CSV is empty: {path}") from error


def discover_dataset_files(dataset_dir: str | Path) -> dict[str, Path]:
    """Identify all six files by their headers, not locale-dependent filenames."""

    root = Path(dataset_dir)
    if not root.is_dir():
        raise Dataset1111Error(f"Dataset directory does not exist: {root}")
    discovered: dict[str, Path] = {}
    for path in sorted(root.glob("*.csv")):
        header = _read_header(path)
        columns = set(header)
        matched = False
        for table, signature in EXPECTED_SIGNATURES.items():
            if signature <= columns:
                if table in discovered:
                    raise Dataset1111Error(
                        f"Multiple CSV files match table {table}: "
                        f"{discovered[table]} and {path}"
                    )
                discovered[table] = path
                matched = True
                break
        if not matched and set(JOB_COLUMNS.values()) <= columns:
            discovered["jobs"] = path
            matched = True
        if not matched and "職缺編號" in columns and "職務名稱" in columns:
            missing = sorted(set(JOB_COLUMNS.values()) - columns)
            raise Dataset1111Error(
                f"Job CSV is missing required columns: {missing}"
            )
    required = {"search", "browse", "apply", "cities", "duties", "jobs"}
    missing_files = required - set(discovered)
    if missing_files:
        raise Dataset1111Error(
            f"Could not identify all six CSV files; missing: {sorted(missing_files)}"
        )
    return discovered


def _sql_path(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "''")


def _sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _clean(column: str) -> str:
    quoted = _identifier(column)
    return f"NULLIF(NULLIF(trim({quoted}), ''), 'NULL')"


@dataclass(frozen=True)
class Dataset1111Config:
    train_end: str = "2026-06-05T00:00:00"
    validation_end: str = "2026-06-06T00:00:00"
    test_end: str = "2026-06-08T00:00:00"
    browse_window_minutes: int = 60
    apply_window_hours: int = 24
    negative_cap_per_query: int = 20
    only_queries_with_positive: bool = True
    strict_temporal_content: bool = True
    sample_queries: int | None = None
    jobs_scope: Literal["all", "exposed"] = "all"
    threads: int = 4
    memory_limit: str = "8GB"

    def __post_init__(self) -> None:
        train = datetime.fromisoformat(self.train_end)
        validation = datetime.fromisoformat(self.validation_end)
        test = datetime.fromisoformat(self.test_end)
        if not train < validation < test:
            raise Dataset1111Error(
                "Temporal cutoffs must satisfy train_end < validation_end < test_end"
            )
        if self.browse_window_minutes <= 0 or self.apply_window_hours <= 0:
            raise Dataset1111Error("Attribution windows must be positive")
        if self.negative_cap_per_query < 0:
            raise Dataset1111Error("negative_cap_per_query cannot be negative")
        if self.sample_queries is not None and self.sample_queries <= 0:
            raise Dataset1111Error("sample_queries must be positive")


@dataclass
class Dataset1111Artifacts:
    output_dir: Path
    jobs: Path
    train_jobs: Path
    queries: Path
    labeled_queries: Path
    labels: Path
    cities: Path
    duties: Path
    manifest: Path
    profile: Path


class Dataset1111Adapter:
    """Out-of-core six-CSV adapter using DuckDB.

    User identifiers are used only inside the temporary DuckDB connection for
    behavioral attribution. They are never exported to canonical artifacts.
    """

    def __init__(
        self,
        dataset_dir: str | Path,
        *,
        config: Dataset1111Config | None = None,
        database_path: str | Path | None = None,
    ) -> None:
        self.dataset_dir = Path(dataset_dir)
        self.files = discover_dataset_files(self.dataset_dir)
        self.config = config or Dataset1111Config()
        self.database_path = str(database_path or ":memory:")
        self.connection = duckdb.connect(self.database_path)
        self.connection.execute(f"SET threads={int(self.config.threads)}")
        self.connection.execute(
            f"SET memory_limit={_sql_string(self.config.memory_limit)}"
        )

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Dataset1111Adapter":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _raw_csv(self, table: str) -> str:
        path = _sql_path(self.files[table])
        extra = ", null_padding=true, parallel=false" if table == "search" else ""
        return (
            f"read_csv('{path}', header=true, all_varchar=true, "
            f"strict_mode=false{extra})"
        )

    def register_raw_views(self) -> None:
        for table in ("jobs", "search", "browse", "apply", "cities", "duties"):
            self.connection.execute(
                f"CREATE OR REPLACE VIEW raw_{table} AS "
                f"SELECT * FROM {self._raw_csv(table)}"
            )

    @property
    def _split_case_query(self) -> str:
        return (
            f"CASE WHEN query_time < TIMESTAMP {_sql_string(self.config.train_end)} "
            f"THEN 'train' "
            f"WHEN query_time < TIMESTAMP {_sql_string(self.config.validation_end)} "
            f"THEN 'validation' ELSE 'test' END"
        )

    @property
    def _split_case_job(self) -> str:
        return (
            f"CASE WHEN last_modified_at < TIMESTAMP "
            f"{_sql_string(self.config.train_end)} THEN 'train' "
            f"WHEN last_modified_at < TIMESTAMP "
            f"{_sql_string(self.config.validation_end)} "
            f"THEN 'validation' ELSE 'test' END"
        )

    def build_staging_tables(self) -> None:
        self.register_raw_views()
        columns = JOB_COLUMNS
        requirements_parts = [
            columns["computer_skills"],
            columns["certifications"],
            columns["work_skills"],
            columns["additional_requirements"],
            columns["education"],
            columns["experience"],
        ]
        requirement_sql = ", ".join(
            f"coalesce({_clean(column)}, '')" for column in requirements_parts
        )
        selected_job_ids = ""
        if self.config.jobs_scope == "exposed":
            selected_job_ids = """
                WHERE cast("職缺編號" AS VARCHAR) IN (
                    SELECT DISTINCT trim(job_id)
                    FROM queries_internal,
                    UNNEST(str_split(exposed_jobs, ',')) AS exposed(job_id)
                    WHERE trim(job_id) <> ''
                )
            """

        search_sample = (
            f"USING SAMPLE reservoir({int(self.config.sample_queries)} ROWS) "
            f"REPEATABLE (42)"
            if self.config.sample_queries is not None
            else ""
        )
        self.connection.execute(
            f"""
            CREATE OR REPLACE TEMP TABLE search_numbered AS
            SELECT row_number() OVER () AS source_row, *
            FROM raw_search {search_sample}
            """
        )
        self.connection.execute(
            f"""
            CREATE OR REPLACE TEMP TABLE queries_internal AS
            WITH typed AS (
                SELECT
                    source_row,
                    CASE
                        WHEN trim(coalesce(talentNo, '')) IN ('', '0')
                        THEN NULL
                        ELSE trim(talentNo)
                    END AS talent_key,
                    trim(coalesce(ks, '')) AS query,
                    trim(coalesce(c0, '')) AS location_code,
                    trim(coalesce(d0, '')) AS duty_code,
                    try_cast(search_time AS TIMESTAMP) AS query_time,
                    trim(coalesce(empStr, '')) AS exposed_jobs
                FROM search_numbered
            ),
            identified AS (
                SELECT
                    'q_' || substr(
                        md5(concat_ws(
                            '|',
                            cast(source_row AS VARCHAR),
                            coalesce(talent_key, 'anonymous'),
                            cast(query_time AS VARCHAR),
                            query,
                            location_code,
                            duty_code,
                            exposed_jobs
                        )),
                        1,
                        20
                    ) AS query_id,
                    *
                FROM typed
                WHERE query_time IS NOT NULL AND query <> ''
            )
            SELECT
                *,
                {self._split_case_query} AS data_split
            FROM identified
            """
        )
        self.connection.execute(
            f"""
            CREATE OR REPLACE TEMP TABLE jobs_all AS
            WITH city_name_map AS (
                SELECT
                    trim(CodeNameA) AS location_name,
                    trim(CodeNo) AS location_code
                FROM raw_cities
                WHERE trim(coalesce(CodeNameA, '')) <> ''
                QUALIFY row_number() OVER (
                    PARTITION BY trim(CodeNameA)
                    ORDER BY
                        CASE WHEN trim(CodeType) = '2' THEN 0 ELSE 1 END,
                        length(trim(CodeNo)),
                        trim(CodeNo)
                ) = 1
            ),
            duty_name_map AS (
                SELECT
                    trim(CodeNameA) AS duty_name,
                    trim(CodeNo) AS duty_code
                FROM raw_duties
                WHERE trim(coalesce(CodeNameA, '')) <> ''
                QUALIFY row_number() OVER (
                    PARTITION BY trim(CodeNameA)
                    ORDER BY length(trim(CodeNo)) DESC, trim(CodeNo)
                ) = 1
            ),
            source AS (
                SELECT
                    j.*,
                    c.location_code AS mapped_location_code,
                    d.duty_code AS mapped_duty_code
                FROM raw_jobs j
                LEFT JOIN city_name_map c
                  ON trim({_identifier(columns['location_code'])}) = c.location_name
                LEFT JOIN duty_name_map d
                  ON trim({_identifier(columns['duty_minor'])}) = d.duty_name
                {selected_job_ids}
            ),
            typed AS (
                SELECT
                    cast({_identifier(columns['job_id'])} AS VARCHAR) AS job_id,
                    coalesce({_clean(columns['title'])}, '') AS title,
                    coalesce({_clean(columns['description'])}, '') AS description,
                    trim(concat_ws(' | ', {requirement_sql})) AS requirements,
                    try_cast({_identifier(columns['last_modified_at'])} AS TIMESTAMP)
                        AS last_modified_at,
                    coalesce(
                        mapped_location_code,
                        {_clean(columns['location_code'])},
                        ''
                    ) AS location_code,
                    coalesce(
                        mapped_duty_code,
                        {_clean(columns['duty_minor'])},
                        ''
                    ) AS occupation_code,
                    coalesce({_clean(columns['location_code'])}, '')
                        AS location_name,
                    coalesce({_clean(columns['duty_major'])}, '') AS duty_major,
                    coalesce({_clean(columns['duty_middle'])}, '') AS duty_middle,
                    coalesce({_clean(columns['duty_minor'])}, '') AS duty_minor,
                    coalesce({_clean(columns['job_type'])}, '') AS job_type,
                    try_cast({_clean(columns['salary_min'])} AS DOUBLE) AS salary_min,
                    try_cast({_clean(columns['salary_max'])} AS DOUBLE) AS salary_max,
                    coalesce({_clean(columns['education'])}, '') AS education,
                    coalesce({_clean(columns['experience'])}, '') AS experience,
                    coalesce({_clean(columns['computer_skills'])}, '')
                        AS computer_skills,
                    coalesce({_clean(columns['certifications'])}, '')
                        AS certifications,
                    coalesce({_clean(columns['work_skills'])}, '')
                        AS work_skills,
                    coalesce({_clean(columns['additional_requirements'])}, '')
                        AS additional_requirements,
                    coalesce({_clean(columns['company_id'])}, '') AS company_id,
                    coalesce({_clean(columns['industry_major'])}, '') AS industry_major,
                    coalesce({_clean(columns['industry_middle'])}, '') AS industry_middle,
                    coalesce({_clean(columns['industry_minor'])}, '') AS industry_minor
                FROM source
            )
            SELECT
                job_id,
                title,
                requirements,
                description,
                cast(last_modified_at AS VARCHAR) AS posted_at,
                location_code,
                occupation_code,
                {self._split_case_job} AS data_split,
                last_modified_at,
                duty_major,
                duty_middle,
                duty_minor,
                location_name,
                job_type,
                salary_min,
                salary_max,
                education,
                experience,
                computer_skills,
                certifications,
                work_skills,
                additional_requirements,
                company_id,
                industry_major,
                industry_middle,
                industry_minor
            FROM typed
            WHERE job_id IS NOT NULL AND last_modified_at IS NOT NULL
            """
        )

    def build_attribution_tables(self) -> None:
        browse_window = int(self.config.browse_window_minutes)
        apply_window = int(self.config.apply_window_hours)
        self.connection.execute(
            """
            CREATE OR REPLACE TEMP TABLE browse_events AS
            SELECT
                row_number() OVER () AS event_id,
                trim(talentNo) AS talent_key,
                trim(employeeNo) AS job_id,
                try_cast(dateIn AS TIMESTAMP) AS event_time
            FROM raw_browse
            WHERE trim(coalesce(talentNo, '')) NOT IN ('', '0')
              AND trim(coalesce(employeeNo, '')) <> ''
              AND try_cast(dateIn AS TIMESTAMP) IS NOT NULL
            """
        )
        self.connection.execute(
            """
            CREATE OR REPLACE TEMP TABLE apply_events AS
            SELECT
                row_number() OVER () AS event_id,
                trim(talentNo) AS talent_key,
                trim(empNo) AS job_id,
                try_cast(datein AS TIMESTAMP) AS event_time
            FROM raw_apply
            WHERE trim(coalesce(talentNo, '')) NOT IN ('', '0')
              AND trim(coalesce(empNo, '')) <> ''
              AND try_cast(datein AS TIMESTAMP) IS NOT NULL
            """
        )
        self.connection.execute(
            f"""
            CREATE OR REPLACE TEMP TABLE attributed_browse AS
            SELECT
                q.query_id,
                b.job_id,
                b.event_id,
                b.event_time,
                q.query_time
            FROM browse_events b
            ASOF JOIN queries_internal q
              ON b.talent_key = q.talent_key
             AND b.event_time >= q.query_time
            WHERE b.event_time <= q.query_time + INTERVAL {browse_window} MINUTE
              AND list_contains(str_split(q.exposed_jobs, ','), b.job_id)
            """
        )
        self.connection.execute(
            f"""
            CREATE OR REPLACE TEMP TABLE attributed_apply AS
            SELECT
                q.query_id,
                a.job_id,
                a.event_id,
                a.event_time,
                q.query_time
            FROM apply_events a
            ASOF JOIN queries_internal q
              ON a.talent_key = q.talent_key
             AND a.event_time >= q.query_time
            WHERE a.event_time <= q.query_time + INTERVAL {apply_window} HOUR
              AND list_contains(str_split(q.exposed_jobs, ','), a.job_id)
            """
        )
        self.connection.execute(
            """
            CREATE OR REPLACE TEMP TABLE positive_labels AS
            SELECT
                query_id,
                job_id,
                max(relevance) AS relevance,
                max(viewed) AS viewed,
                max(applied) AS applied
            FROM (
                SELECT query_id, job_id, 1 AS relevance, 1 AS viewed, 0 AS applied
                FROM attributed_browse
                UNION ALL
                SELECT query_id, job_id, 2 AS relevance, 0 AS viewed, 1 AS applied
                FROM attributed_apply
            )
            GROUP BY query_id, job_id
            """
        )

    def build_label_tables(self) -> None:
        positive_filter = (
            "AND q.query_id IN (SELECT DISTINCT query_id FROM positive_labels)"
            if self.config.only_queries_with_positive
            else ""
        )
        temporal_filter = (
            "WHERE j.last_modified_at <= e.query_time"
            if self.config.strict_temporal_content
            else ""
        )
        self.connection.execute(
            f"""
            CREATE OR REPLACE TEMP TABLE exposures AS
            SELECT
                q.query_id,
                trim(exposed.job_id) AS job_id,
                cast(exposed.original_rank AS INTEGER) AS original_rank,
                q.query_time,
                q.data_split
            FROM queries_internal q,
            UNNEST(str_split(q.exposed_jobs, ','))
                WITH ORDINALITY AS exposed(job_id, original_rank)
            WHERE trim(exposed.job_id) <> ''
            {positive_filter}
            """
        )
        self.connection.execute(
            f"""
            CREATE OR REPLACE TEMP TABLE candidate_labels AS
            WITH joined AS (
                SELECT
                    e.query_id,
                    e.job_id,
                    coalesce(p.relevance, 0) AS relevance,
                    e.original_rank,
                    e.data_split,
                    coalesce(p.viewed, 0) AS viewed,
                    coalesce(p.applied, 0) AS applied,
                    e.query_time,
                    j.last_modified_at,
                    j.job_id IS NOT NULL AS known_job,
                    j.last_modified_at <= e.query_time AS content_available_at_query
                FROM exposures e
                LEFT JOIN positive_labels p
                  ON e.query_id = p.query_id AND e.job_id = p.job_id
                LEFT JOIN jobs_all j ON e.job_id = j.job_id
            ),
            eligible AS (
                SELECT *,
                    sum(CASE WHEN relevance = 0 THEN 1 ELSE 0 END)
                    OVER (
                        PARTITION BY query_id
                        ORDER BY original_rank
                        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                    ) AS negative_ordinal
                FROM joined e
                JOIN jobs_all j USING (job_id)
                {temporal_filter}
            )
            SELECT
                query_id,
                job_id,
                relevance,
                original_rank,
                data_split,
                viewed,
                applied,
                content_available_at_query
            FROM eligible
            WHERE relevance > 0
               OR negative_ordinal <= {int(self.config.negative_cap_per_query)}
            """
        )

    def profile_raw(self) -> dict[str, Any]:
        """Scan the six files without exploding search result lists."""

        self.register_raw_views()

        def one(sql: str) -> dict[str, Any]:
            cursor = self.connection.execute(sql)
            row = cursor.fetchone()
            return {
                item[0]: value
                for item, value in zip(cursor.description, row or ())
            }

        job_id = _identifier(JOB_COLUMNS["job_id"])
        job_title = _identifier(JOB_COLUMNS["title"])
        job_description = _identifier(JOB_COLUMNS["description"])
        modified = _identifier(JOB_COLUMNS["last_modified_at"])
        return json.loads(
            json.dumps(
                {
                    "files": {
                        name: {
                            "path": str(path.resolve()),
                            "size_bytes": path.stat().st_size,
                            "header": _read_header(path),
                        }
                        for name, path in sorted(self.files.items())
                    },
                    "jobs": one(
                        f"""
                        SELECT
                            count(*) AS row_count,
                            count(DISTINCT {job_id}) AS distinct_jobs,
                            count_if(trim(coalesce({job_title}, '')) = '')
                                AS empty_title,
                            count_if(trim(coalesce({job_description}, '')) = '')
                                AS empty_description,
                            min(try_cast({modified} AS TIMESTAMP)) AS min_modified,
                            max(try_cast({modified} AS TIMESTAMP)) AS max_modified,
                            count_if(
                                try_cast({modified} AS TIMESTAMP) >=
                                TIMESTAMP {_sql_string(self.config.test_end)}
                            ) AS modified_after_behavior_period
                        FROM raw_jobs
                        """
                    ),
                    "search": one(
                        """
                        WITH counts AS (
                            SELECT
                                CASE
                                    WHEN trim(coalesce(empStr, '')) = '' THEN 0
                                    ELSE list_count(str_split(empStr, ','))
                                END AS result_count,
                                *
                            FROM raw_search
                        )
                        SELECT
                            count(*) AS row_count,
                            count_if(trim(coalesce(ks, '')) = '') AS empty_query,
                            count_if(trim(coalesce(empStr, '')) = '')
                                AS empty_results,
                            count_if(trim(coalesce(talentNo, '')) IN ('', '0'))
                                AS anonymous_queries,
                            sum(result_count) AS exposure_count,
                            avg(result_count) AS average_results,
                            max(result_count) AS max_results,
                            min(try_cast(search_time AS TIMESTAMP)) AS min_time,
                            max(try_cast(search_time AS TIMESTAMP)) AS max_time
                        FROM counts
                        """
                    ),
                    "browse": one(
                        """
                        SELECT
                            count(*) AS row_count,
                            count_if(trim(coalesce(talentNo, '')) IN ('', '0'))
                                AS anonymous_events,
                            min(try_cast(dateIn AS TIMESTAMP)) AS min_time,
                            max(try_cast(dateIn AS TIMESTAMP)) AS max_time
                        FROM raw_browse
                        """
                    ),
                    "apply": one(
                        """
                        SELECT
                            count(*) AS row_count,
                            count_if(trim(coalesce(talentNo, '')) IN ('', '0'))
                                AS anonymous_events,
                            min(try_cast(datein AS TIMESTAMP)) AS min_time,
                            max(try_cast(datein AS TIMESTAMP)) AS max_time
                        FROM raw_apply
                        """
                    ),
                    "dimensions": {
                        "cities": one(
                            "SELECT count(*) AS row_count FROM raw_cities"
                        ),
                        "duties": one(
                            "SELECT count(*) AS row_count FROM raw_duties"
                        ),
                    },
                },
                default=str,
            )
        )

    def _profile(self) -> dict[str, Any]:
        def one(sql: str) -> dict[str, Any]:
            cursor = self.connection.execute(sql)
            row = cursor.fetchone()
            return {
                item[0]: value
                for item, value in zip(cursor.description, row or ())
            }

        profile = {
            "raw": {
                "jobs": one("SELECT count(*) AS row_count FROM raw_jobs"),
                "search": one(
                    """
                    SELECT
                        count(*) AS row_count,
                        count_if(trim(coalesce(empStr, '')) = '') AS empty_results,
                        count_if(trim(coalesce(talentNo, '')) IN ('', '0'))
                            AS anonymous_queries
                    FROM raw_search
                    """
                ),
                "browse": one("SELECT count(*) AS row_count FROM raw_browse"),
                "apply": one("SELECT count(*) AS row_count FROM raw_apply"),
            },
            "canonical": {
                "jobs": one(
                    """
                    SELECT
                        count(*) AS row_count,
                        count_if(data_split = 'train') AS train,
                        count_if(data_split = 'validation') AS validation,
                        count_if(data_split = 'test') AS test
                    FROM jobs_all
                    """
                ),
                "queries": one(
                    """
                    SELECT
                        count(*) AS row_count,
                        count_if(data_split = 'train') AS train,
                        count_if(data_split = 'validation') AS validation,
                        count_if(data_split = 'test') AS test
                    FROM queries_internal
                    """
                ),
                "attribution": one(
                    """
                    SELECT
                        (SELECT count(*) FROM browse_events) AS browse_events,
                        (SELECT count(*) FROM attributed_browse)
                            AS attributed_browse,
                        (SELECT count(*) FROM apply_events) AS apply_events,
                        (SELECT count(*) FROM attributed_apply)
                            AS attributed_apply,
                        (SELECT count(DISTINCT query_id) FROM positive_labels)
                            AS positive_queries
                    """
                ),
                "labels": one(
                    """
                    SELECT
                        count(*) AS row_count,
                        count_if(relevance = 0) AS relevance_0,
                        count_if(relevance = 1) AS relevance_1,
                        count_if(relevance = 2) AS relevance_2,
                        count(DISTINCT query_id) AS query_count
                    FROM candidate_labels
                    """
                ),
            },
        }
        return json.loads(json.dumps(profile, default=str))

    def prepare(
        self, output_dir: str | Path, *, overwrite: bool = False
    ) -> Dataset1111Artifacts:
        target = Path(output_dir)
        if target.exists() and any(target.iterdir()):
            if not overwrite:
                raise FileExistsError(
                    f"1111 adapter output exists: {target}; pass overwrite=True"
                )
            resolved = target.resolve()
            if resolved == Path(resolved.anchor) or self.dataset_dir.resolve() in resolved.parents:
                raise Dataset1111Error(f"Refusing unsafe output deletion: {resolved}")
            shutil.rmtree(resolved)
        target.mkdir(parents=True, exist_ok=True)
        self.build_staging_tables()
        self.build_attribution_tables()
        self.build_label_tables()

        paths = {
            "jobs": target / "jobs.parquet",
            "train_jobs": target / "train_jobs.parquet",
            "queries": target / "queries.parquet",
            "labeled_queries": target / "labeled_queries.parquet",
            "labels": target / "labels.parquet",
            "cities": target / "cities.parquet",
            "duties": target / "duties.parquet",
        }
        copy_statements = {
            "jobs": """
                SELECT * EXCLUDE(last_modified_at)
                FROM jobs_all ORDER BY job_id
            """,
            "train_jobs": """
                SELECT
                    job_id, title, requirements, description, posted_at,
                    location_code, occupation_code, data_split
                FROM jobs_all WHERE data_split = 'train' ORDER BY job_id
            """,
            "queries": """
                SELECT
                    query_id,
                    query,
                    cast(query_time AS VARCHAR) AS query_time,
                    query_id AS session_id,
                    location_code,
                    duty_code AS occupation_code,
                    replace(exposed_jobs, ',', '|') AS exposed_jobs,
                    array_to_string(
                        range(1, list_count(str_split(exposed_jobs, ',')) + 1),
                        '|'
                    ) AS exposed_ranks,
                    data_split
                FROM queries_internal ORDER BY query_time, query_id
            """,
            "labeled_queries": """
                SELECT DISTINCT
                    q.query_id,
                    q.query,
                    cast(q.query_time AS VARCHAR) AS query_time,
                    q.query_id AS session_id,
                    q.location_code,
                    q.duty_code AS occupation_code,
                    replace(q.exposed_jobs, ',', '|') AS exposed_jobs,
                    array_to_string(
                        range(1, list_count(str_split(q.exposed_jobs, ',')) + 1),
                        '|'
                    ) AS exposed_ranks,
                    q.data_split
                FROM queries_internal q
                JOIN candidate_labels l USING (query_id)
                ORDER BY query_time, query_id
            """,
            "labels": """
                SELECT * FROM candidate_labels
                ORDER BY query_id, original_rank
            """,
            "cities": "SELECT * FROM raw_cities ORDER BY CodeNo",
            "duties": "SELECT * FROM raw_duties ORDER BY CodeNo",
        }
        for name, query in copy_statements.items():
            output = _sql_path(paths[name])
            self.connection.execute(
                f"COPY ({query}) TO '{output}' "
                f"(FORMAT PARQUET, COMPRESSION ZSTD)"
            )

        profile = self._profile()
        profile_path = target / "dataset_profile.json"
        profile_path.write_text(
            json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        manifest = {
            "adapter_version": "1111-six-csv-v1",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "config": asdict(self.config),
            "config_hash": stable_json_hash(asdict(self.config)),
            "input_files": {
                name: {
                    "path": str(path.resolve()),
                    "size": path.stat().st_size,
                    "mtime_ns": path.stat().st_mtime_ns,
                }
                for name, path in sorted(self.files.items())
            },
            "outputs": {name: str(path.resolve()) for name, path in paths.items()},
            "privacy": {
                "talentNo_exported": False,
                "talentNo_usage": "temporary attribution only",
            },
            "labels": {
                "type": "weak_supervision",
                "browse_relevance": 1,
                "apply_relevance": 2,
                "exposure_without_attributed_behavior": 0,
                "official_ground_truth": False,
            },
            "temporal_content_guard": self.config.strict_temporal_content,
            "profile": profile,
        }
        manifest_path = target / "dataset_manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return Dataset1111Artifacts(
            output_dir=target,
            jobs=paths["jobs"],
            train_jobs=paths["train_jobs"],
            queries=paths["queries"],
            labeled_queries=paths["labeled_queries"],
            labels=paths["labels"],
            cities=paths["cities"],
            duties=paths["duties"],
            manifest=manifest_path,
            profile=profile_path,
        )


def profile_dataset(dataset_dir: str | Path) -> dict[str, Any]:
    """Fast schema/size/header profile without expanding the 88M exposures."""

    files = discover_dataset_files(dataset_dir)
    return {
        "files": {
            name: {
                "path": str(path.resolve()),
                "size_bytes": path.stat().st_size,
                "header": _read_header(path),
            }
            for name, path in sorted(files.items())
        }
    }
