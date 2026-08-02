"""
Step 1 — 資料準備（train-only）
Role: B (Structure Graph)
Playbook: docs/SKILL_GRAPH_PLAYBOOK.md

This script:
1. Loads all 職缺.csv (all jobs are train-eligible per team decision)
2. Maps occupation (職務大類/中類/小類) to 職務對照表 CodeNo
3. Computes content_hash (SHA-256 of skill-graph-relevant fields) and source_snapshot_id
4. Selects a stratified golden slice of 500 fixed Job IDs
5. Outputs train_jobs (parquet + csv), golden_slice, occupation mapping audit, and manifest
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import random
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parent.parent  # pipeline/ -> repo root; graph/ dataset/ fixtures/ 都掛在根目錄
RAW_DIR = _REPO_ROOT / "data" / "raw"
OUTPUT_DIR = _REPO_ROOT / "graph"
JOBS_CSV = RAW_DIR / "職缺.csv"
DUTIES_CSV = RAW_DIR / "職務對照表.csv"

GOLDEN_SLICE_SIZE = 500
GOLDEN_SLICE_SEED = 2026  # deterministic

# Schema v0.1 frozen: timezone assumption
TIMEZONE_ASSUMPTION = "Asia/Taipei"

# All jobs are used for graph building — no train/test split for jobs
# (confirmed by organizer verbally on 2026-08-01; written source pending, see Playbook §1.1)
TRAIN_POLICY = "all_jobs_no_split"
TRAIN_POLICY_RATIONALE = (
    "主辦方口頭確認全部職缺皆可用於建圖，不需切分 train/test（來源待補，見 Playbook §1.1）。"
    "query/行為資料仍依 dataset_1111.py 切 train/validation/test。"
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _sha256_file(path: Path) -> str:
    """Compute SHA-256 of a file for source_snapshot_id."""
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _content_hash_sql() -> str:
    """
    Deterministic content hash for each job: SHA-256 of concatenation of
    skill-graph-relevant fields (title, description, structured skill fields,
    occupation classification). 
    
    Uses the same field ordering for reproducibility.
    """
    fields = [
        "job_id",
        "title",
        "description",
        "computer_skills",
        "certifications",
        "work_skills",
        "additional_requirements",
        "duty_major",
        "duty_middle",
        "duty_minor",
    ]
    concat_expr = "concat_ws('|', " + ", ".join(
        f"coalesce({f}, '')" for f in fields
    ) + ")"
    return f"sha256({concat_expr})"


def _sql_path(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "''")


# ─────────────────────────────────────────────────────────────────────────────
# Main pipeline
# ─────────────────────────────────────────────────────────────────────────────


def build_occupation_hierarchy(con: duckdb.DuckDBPyConnection) -> None:
    """
    Load 職務對照表 and build:
    - duties_raw: raw table
    - occupation_hierarchy: parent-child relationships
    - minor/middle/major lookup tables
    """
    duties_path = _sql_path(DUTIES_CSV)
    con.execute(f"""
        CREATE OR REPLACE TABLE duties_raw AS
        SELECT
            trim("CodeNo") AS code_no,
            trim(COALESCE(NULLIF(trim("CodeNameA"), ''), '')) AS code_name_a,
            trim(COALESCE(NULLIF(trim("CodeNameB"), ''), '')) AS code_name_b,
            trim(COALESCE(NULLIF(trim("CodeNameC"), ''), '')) AS code_name_c,
            COALESCE(NULLIF(trim("CodeDescript"), ''), '') AS code_descript,
            COALESCE(NULLIF(trim("CodeAlike"), ''), '') AS code_alike,
            COALESCE(NULLIF(trim("CodeDefinition"), ''), '') AS code_definition,
            COALESCE(NULLIF(trim("CodeNameEN"), ''), '') AS code_name_en,
            -- Determine hierarchy level
            CASE
                WHEN right(trim("CodeNo"), 4) = '0000' THEN 'major'
                WHEN right(trim("CodeNo"), 2) = '00' THEN 'middle'
                ELSE 'minor'
            END AS level
        FROM read_csv('{duties_path}', header=true, all_varchar=true)
    """)

    # Build hierarchy edges: minor -> middle -> major
    con.execute("""
        CREATE OR REPLACE TABLE occupation_hierarchy AS
        -- minor -> middle: first 4 digits + '00'
        SELECT
            code_no AS child_code,
            left(code_no, 4) || '00' AS parent_code,
            'minor_to_middle' AS relation
        FROM duties_raw
        WHERE level = 'minor'
          AND left(code_no, 4) || '00' IN (SELECT code_no FROM duties_raw WHERE level = 'middle')
        UNION ALL
        -- middle -> major: first 2 digits + '0000'
        SELECT
            code_no AS child_code,
            left(code_no, 2) || '0000' AS parent_code,
            'middle_to_major' AS relation
        FROM duties_raw
        WHERE level = 'middle'
          AND left(code_no, 2) || '0000' IN (SELECT code_no FROM duties_raw WHERE level = 'major')
    """)


def build_train_jobs(con: duckdb.DuckDBPyConnection) -> None:
    """
    Load all jobs, clean fields, map occupation CodeNo, compute content_hash.
    All jobs are train_eligible=true.
    """
    jobs_path = _sql_path(JOBS_CSV)
    content_hash = _content_hash_sql()

    con.execute(f"""
        CREATE OR REPLACE TABLE train_jobs AS
        WITH raw AS (
            SELECT
                trim("職缺編號") AS job_id,
                COALESCE(NULLIF(trim("職務名稱"), 'NULL'), '') AS title,
                COALESCE(NULLIF(trim("職務內容"), 'NULL'), '') AS description,
                COALESCE(NULLIF(trim("電腦技能資料"), 'NULL'), '') AS computer_skills,
                COALESCE(NULLIF(trim("專業證照"), 'NULL'), '') AS certifications,
                COALESCE(NULLIF(trim("工作技能"), 'NULL'), '') AS work_skills,
                COALESCE(NULLIF(trim("附加條件"), 'NULL'), '') AS additional_requirements,
                COALESCE(NULLIF(trim("職務大類"), 'NULL'), '') AS duty_major,
                COALESCE(NULLIF(trim("職務中類"), 'NULL'), '') AS duty_middle,
                COALESCE(NULLIF(trim("職務小類"), 'NULL'), '') AS duty_minor,
                try_cast("職缺最後修改時間" AS TIMESTAMP) AS last_modified_at,
                COALESCE(NULLIF(trim("薪資"), 'NULL'), '') AS salary_text,
                try_cast(NULLIF(trim("薪資下限"), 'NULL') AS DOUBLE) AS salary_min,
                try_cast(NULLIF(trim("薪資上限"), 'NULL') AS DOUBLE) AS salary_max,
                COALESCE(NULLIF(trim("職缺屬性"), 'NULL'), '') AS job_type,
                COALESCE(NULLIF(trim("工時"), 'NULL'), '') AS work_hours,
                COALESCE(NULLIF(trim("工作城市"), 'NULL'), '') AS location_name,
                COALESCE(NULLIF(trim("學歷需求"), 'NULL'), '') AS education,
                COALESCE(NULLIF(trim("工作經驗需求"), 'NULL'), '') AS experience,
                COALESCE(NULLIF(trim("廠商編號"), 'NULL'), '') AS company_id,
                COALESCE(NULLIF(trim("產業大類"), 'NULL'), '') AS industry_major,
                COALESCE(NULLIF(trim("產業中類"), 'NULL'), '') AS industry_middle,
                COALESCE(NULLIF(trim("產業小類"), 'NULL'), '') AS industry_minor
            FROM read_csv('{jobs_path}', header=true, all_varchar=true, strict_mode=false)
        ),
        -- Map occupation: try minor first (exact 3-level match), then middle
        mapped AS (
            SELECT
                r.*,
                -- Minor level mapping: (CodeNameA, CodeNameB, CodeNameC) = (小類, 中類, 大類)
                minor.code_no AS occ_minor_code,
                -- Middle level mapping: (CodeNameB, CodeNameC) = (中類, 大類)
                middle.code_no AS occ_middle_code,
                CASE
                    WHEN minor.code_no IS NOT NULL THEN minor.code_no
                    WHEN middle.code_no IS NOT NULL THEN middle.code_no
                    ELSE NULL
                END AS occupation_code,
                CASE
                    WHEN minor.code_no IS NOT NULL THEN 'minor'
                    WHEN middle.code_no IS NOT NULL THEN 'middle'
                    WHEN r.duty_major = '' AND r.duty_middle = '' AND r.duty_minor = '' THEN 'empty'
                    ELSE 'unmapped'
                END AS mapping_status,
                CASE
                    WHEN minor.code_no IS NOT NULL THEN 'minor'
                    WHEN middle.code_no IS NOT NULL THEN 'middle'
                    ELSE NULL
                END AS mapped_level
            FROM raw r
            LEFT JOIN duties_raw minor
                ON minor.level = 'minor'
                AND minor.code_name_a = r.duty_minor
                AND minor.code_name_b = r.duty_middle
                AND minor.code_name_c = r.duty_major
            LEFT JOIN duties_raw middle
                ON middle.level = 'middle'
                AND middle.code_name_b = r.duty_middle
                AND middle.code_name_c = r.duty_major
                AND r.duty_minor = r.duty_middle  -- only when minor name = middle name
        )
        SELECT
            job_id,
            title,
            description,
            computer_skills,
            certifications,
            work_skills,
            additional_requirements,
            duty_major,
            duty_middle,
            duty_minor,
            occupation_code,
            mapping_status,
            mapped_level,
            last_modified_at,
            salary_text,
            salary_min,
            salary_max,
            job_type,
            work_hours,
            location_name,
            education,
            experience,
            company_id,
            industry_major,
            industry_middle,
            industry_minor,
            {content_hash} AS content_hash,
            true AS train_eligible
        FROM mapped
        WHERE job_id IS NOT NULL AND job_id != ''
    """)


def select_golden_slice(con: duckdb.DuckDBPyConnection) -> list[str]:
    """
    Select 500 stratified golden slice job IDs. Strategy:
    
    - Ensure representation across:
      1. Structured field coverage (all 3 / some / none)
      2. Major occupation categories (all 20+ categories)
      3. Jobs with certifications (important for Credential node testing)
      4. Various mapping statuses
    
    Allocation:
      - 100 jobs with ALL 3 structured fields (computer_skills, certifications, work_skills)
      - 50 jobs with ONLY certifications (test Credential node separation)
      - 200 jobs with SOME structured fields (1-2 of the 3)
      - 100 jobs with NO structured fields (test non-structured extraction)
      - 50 jobs with edge cases (middle-only mapping, unmapped, empty occupation)
    
    Within each stratum, sample across occupation categories for diversity.
    """
    random.seed(GOLDEN_SLICE_SEED)

    # Stratum 1: All 3 structured fields (100)
    rows = con.execute("""
        SELECT job_id FROM train_jobs
        WHERE computer_skills != '' AND certifications != '' AND work_skills != ''
        AND mapping_status = 'minor'
        ORDER BY md5(job_id)
        LIMIT 100
    """).fetchall()
    stratum_all_three = [r[0] for r in rows]

    # Stratum 2: Has certifications but NOT all 3 (50)
    rows = con.execute("""
        SELECT job_id FROM train_jobs
        WHERE certifications != ''
        AND NOT (computer_skills != '' AND work_skills != '')
        AND mapping_status = 'minor'
        ORDER BY md5(job_id)
        LIMIT 50
    """).fetchall()
    stratum_certs = [r[0] for r in rows]

    # Stratum 3: Some structured fields, no certs (200)
    # Spread across occupation categories
    rows = con.execute("""
        SELECT job_id FROM train_jobs
        WHERE (computer_skills != '' OR work_skills != '')
        AND certifications = ''
        AND mapping_status = 'minor'
        ORDER BY md5(concat(duty_major, job_id))
        LIMIT 200
    """).fetchall()
    stratum_some = [r[0] for r in rows]

    # Stratum 4: No structured fields at all (100)
    rows = con.execute("""
        SELECT job_id FROM train_jobs
        WHERE computer_skills = '' AND certifications = '' AND work_skills = ''
        AND mapping_status = 'minor'
        ORDER BY md5(concat(duty_major, job_id))
        LIMIT 100
    """).fetchall()
    stratum_none = [r[0] for r in rows]

    # Stratum 5: Edge cases — middle-only, unmapped, empty occupation (50)
    rows = con.execute("""
        SELECT job_id FROM train_jobs
        WHERE mapping_status IN ('middle', 'unmapped', 'empty')
        ORDER BY md5(job_id)
        LIMIT 50
    """).fetchall()
    stratum_edge = [r[0] for r in rows]

    golden_ids = (
        stratum_all_three + stratum_certs + stratum_some +
        stratum_none + stratum_edge
    )

    # Verify uniqueness
    assert len(golden_ids) == len(set(golden_ids)), "Golden slice has duplicates!"
    assert len(golden_ids) == GOLDEN_SLICE_SIZE, (
        f"Expected {GOLDEN_SLICE_SIZE}, got {len(golden_ids)}"
    )

    return sorted(golden_ids)


def export_outputs(con: duckdb.DuckDBPyConnection, golden_ids: list[str]) -> dict[str, Any]:
    """Export train_jobs, golden_slice, occupation audit, and manifest."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Export full train_jobs as parquet
    train_jobs_parquet = OUTPUT_DIR / "train_jobs.parquet"
    con.execute(f"""
        COPY (
            SELECT * FROM train_jobs ORDER BY job_id
        ) TO '{_sql_path(train_jobs_parquet)}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)

    # 2. Export golden slice as CSV (for easy human inspection)
    golden_slice_csv = OUTPUT_DIR / "golden_slice.csv"
    ids_list = ", ".join(f"'{jid}'" for jid in golden_ids)
    con.execute(f"""
        COPY (
            SELECT * FROM train_jobs
            WHERE job_id IN ({ids_list})
            ORDER BY job_id
        ) TO '{_sql_path(golden_slice_csv)}' (FORMAT CSV, HEADER true)
    """)

    # 3. Golden slice IDs list (JSON)
    golden_ids_json = OUTPUT_DIR / "golden_slice_ids.json"
    golden_ids_json.write_text(
        json.dumps(golden_ids, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    # 4. Occupation mapping audit
    occ_audit_csv = OUTPUT_DIR / "occupation_mapping_audit.csv"
    con.execute(f"""
        COPY (
            SELECT
                job_id,
                duty_major,
                duty_middle,
                duty_minor,
                occupation_code,
                mapping_status,
                mapped_level
            FROM train_jobs
            WHERE mapping_status != 'minor'
            ORDER BY mapping_status, job_id
        ) TO '{_sql_path(occ_audit_csv)}' (FORMAT CSV, HEADER true)
    """)

    # 5. Occupation hierarchy export
    hierarchy_csv = OUTPUT_DIR / "occupation_hierarchy.csv"
    con.execute(f"""
        COPY (
            SELECT * FROM occupation_hierarchy ORDER BY child_code
        ) TO '{_sql_path(hierarchy_csv)}' (FORMAT CSV, HEADER true)
    """)

    # 6. Profiling stats
    stats = {}

    # Total counts
    row = con.execute("SELECT count(*) FROM train_jobs").fetchone()
    stats["total_jobs"] = row[0]

    # Time range
    row = con.execute("""
        SELECT
            min(last_modified_at) AS min_time,
            max(last_modified_at) AS max_time,
            count_if(last_modified_at IS NULL) AS null_time_count
        FROM train_jobs
    """).fetchone()
    stats["last_modified_at_min"] = str(row[0])
    stats["last_modified_at_max"] = str(row[1])
    stats["null_timestamp_count"] = row[2]

    # Mapping status distribution
    rows = con.execute("""
        SELECT mapping_status, count(*) AS cnt
        FROM train_jobs
        GROUP BY mapping_status
        ORDER BY cnt DESC
    """).fetchall()
    stats["occupation_mapping"] = {r[0]: r[1] for r in rows}

    # Structured field coverage
    row = con.execute("""
        SELECT
            count(*) AS total,
            count_if(computer_skills != '') AS has_computer_skills,
            count_if(certifications != '') AS has_certifications,
            count_if(work_skills != '') AS has_work_skills,
            count_if(computer_skills != '' OR certifications != '' OR work_skills != '') AS has_any_structured,
            count_if(computer_skills != '' AND certifications != '' AND work_skills != '') AS has_all_three
        FROM train_jobs
    """).fetchone()
    stats["structured_fields"] = {
        "total": row[0],
        "has_computer_skills": row[1],
        "has_certifications": row[2],
        "has_work_skills": row[3],
        "has_any_structured": row[4],
        "has_all_three": row[5],
    }

    # Golden slice distribution
    rows = con.execute(f"""
        SELECT mapping_status, count(*) AS cnt
        FROM train_jobs
        WHERE job_id IN ({ids_list})
        GROUP BY mapping_status
    """).fetchall()
    stats["golden_slice_mapping_distribution"] = {r[0]: r[1] for r in rows}

    row = con.execute(f"""
        SELECT
            count_if(computer_skills != '' AND certifications != '' AND work_skills != '') AS all_three,
            count_if(certifications != '' AND NOT (computer_skills != '' AND work_skills != '')) AS certs_partial,
            count_if((computer_skills != '' OR work_skills != '') AND certifications = '') AS some_no_certs,
            count_if(computer_skills = '' AND certifications = '' AND work_skills = '') AS no_structured
        FROM train_jobs
        WHERE job_id IN ({ids_list})
    """).fetchone()
    stats["golden_slice_structured_distribution"] = {
        "all_three_fields": row[0],
        "certs_partial": row[1],
        "some_no_certs": row[2],
        "no_structured": row[3],
    }

    # Major occupation categories in golden slice
    rows = con.execute(f"""
        SELECT duty_major, count(*) AS cnt
        FROM train_jobs
        WHERE job_id IN ({ids_list}) AND duty_major != ''
        GROUP BY duty_major
        ORDER BY cnt DESC
    """).fetchall()
    stats["golden_slice_occupation_categories"] = {r[0]: r[1] for r in rows}

    return stats


def build_manifest(
    stats: dict[str, Any],
    golden_ids: list[str],
    source_hashes: dict[str, str],
) -> dict[str, Any]:
    """Build graph_manifest.json for Step 1."""
    manifest = {
        "step": "step1_train_data_prep",
        "schema_version": "v0.1",
        "schema_status": "frozen",
        "gate0_status": "conditional_pass",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "graph_data_scope": "all_jobs_no_split",
        "data_scope_policy": {
            "policy": TRAIN_POLICY,
            "rationale": TRAIN_POLICY_RATIONALE,
            "source_status": "verbal_confirmation_pending_written_record",
            "source_note": (
                "主辦方 2026-08-01 口頭確認全量職缺可用於建圖；"
                "命題文件書面仍有 train-only 罰則條款。"
                "來源待補（工作坊 Q&A / email），見 Playbook §1.1"
            ),
            "timezone_assumption": TIMEZONE_ASSUMPTION,
            "timezone_impact": "None for job selection; only for manifest audit timestamps",
        },
        "full_graph_allowed": True,
        "full_graph_blocker": "llm_extraction_throughput",
        "source_files": {
            "jobs_csv": {
                "path": str(JOBS_CSV.resolve()),
                "sha256": source_hashes["jobs"],
            },
            "duties_csv": {
                "path": str(DUTIES_CSV.resolve()),
                "sha256": source_hashes["duties"],
            },
        },
        "source_snapshot_id": hashlib.sha256(
            (source_hashes["jobs"] + source_hashes["duties"]).encode()
        ).hexdigest()[:16],
        "outputs": {
            "train_jobs_parquet": str((OUTPUT_DIR / "train_jobs.parquet").resolve()),
            "golden_slice_csv": str((OUTPUT_DIR / "golden_slice.csv").resolve()),
            "golden_slice_ids_json": str((OUTPUT_DIR / "golden_slice_ids.json").resolve()),
            "occupation_mapping_audit": str((OUTPUT_DIR / "occupation_mapping_audit.csv").resolve()),
            "occupation_hierarchy": str((OUTPUT_DIR / "occupation_hierarchy.csv").resolve()),
        },
        "golden_slice": {
            "size": len(golden_ids),
            "seed": GOLDEN_SLICE_SEED,
            "strategy": (
                "Stratified by structured field coverage and occupation mapping status: "
                "100 all-three-structured, 50 certs-only, 200 some-structured, "
                "100 no-structured, 50 edge-cases (middle/unmapped/empty)"
            ),
            "ids_hash": hashlib.sha256(
                json.dumps(golden_ids).encode()
            ).hexdigest(),
        },
        "statistics": stats,
        "contains_post_cutoff_jd": "N/A — no train/test split for jobs",
        "full_graph_allowed": True,
        "full_graph_blocker": "llm_extraction_throughput",
        "content_hash_algorithm": "sha256(concat_ws('|', job_id, title, description, computer_skills, certifications, work_skills, additional_requirements, duty_major, duty_middle, duty_minor))",
        "id_rules": {
            "job": "job:<職缺編號>",
            "occupation": "occ:<CodeNo from 職務對照表>",
            "deterministic": True,
        },
        "known_limitations": [
            "Only 職缺最後修改時間 available, no posted_at/刊登時間",
            "No JD version history: content_hash reflects current snapshot only",
            "713 jobs have all-empty occupation fields (mapping_status=empty)",
            "470 jobs map to middle-level only (mapping_status=middle)",
        ],
    }
    return manifest


def main() -> None:
    print("=" * 60)
    print("Step 1 — 資料準備 (train-only)")
    print("=" * 60)

    # Compute source file hashes
    print("\n[1/6] Computing source file hashes...")
    source_hashes = {
        "jobs": _sha256_file(JOBS_CSV),
        "duties": _sha256_file(DUTIES_CSV),
    }
    print(f"  jobs.csv SHA-256: {source_hashes['jobs'][:16]}...")
    print(f"  duties.csv SHA-256: {source_hashes['duties'][:16]}...")

    # Connect to DuckDB (in-memory)
    con = duckdb.connect(":memory:")
    con.execute("SET threads=4")
    con.execute("SET memory_limit='4GB'")

    try:
        # Build occupation hierarchy
        print("\n[2/6] Building occupation hierarchy...")
        build_occupation_hierarchy(con)
        row = con.execute("SELECT count(*) FROM duties_raw").fetchone()
        print(f"  Loaded {row[0]} occupation codes")
        row = con.execute("SELECT count(*) FROM occupation_hierarchy").fetchone()
        print(f"  Built {row[0]} hierarchy edges")

        # Build train_jobs
        print("\n[3/6] Building train_jobs (all jobs)...")
        build_train_jobs(con)
        row = con.execute("SELECT count(*) FROM train_jobs").fetchone()
        print(f"  Loaded {row[0]} train jobs")

        # Validate: no duplicate job_ids
        row = con.execute("""
            SELECT count(*) - count(DISTINCT job_id) AS dup_count
            FROM train_jobs
        """).fetchone()
        assert row[0] == 0, f"Found {row[0]} duplicate job_ids!"
        print("  ✓ No duplicate job_ids")

        # Select golden slice
        print("\n[4/6] Selecting golden slice...")
        golden_ids = select_golden_slice(con)
        print(f"  Selected {len(golden_ids)} golden slice IDs")

        # Export outputs
        print("\n[5/6] Exporting outputs...")
        stats = export_outputs(con, golden_ids)
        print(f"  Exported to {OUTPUT_DIR}")

        # Build and save manifest
        print("\n[6/6] Writing manifest...")
        manifest = build_manifest(stats, golden_ids, source_hashes)
        manifest_path = OUTPUT_DIR / "step1_manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"  Manifest: {manifest_path}")

        # Print summary
        print("\n" + "=" * 60)
        print("STEP 1 SUMMARY")
        print("=" * 60)
        print(f"  Total train jobs: {stats['total_jobs']}")
        print(f"  Last modified range: {stats['last_modified_at_min']} ~ {stats['last_modified_at_max']}")
        print(f"  Null timestamps: {stats['null_timestamp_count']}")
        print(f"\n  Occupation mapping:")
        for status, count in stats["occupation_mapping"].items():
            print(f"    {status}: {count}")
        print(f"\n  Structured field coverage:")
        sf = stats["structured_fields"]
        print(f"    has_computer_skills: {sf['has_computer_skills']} ({sf['has_computer_skills']/sf['total']*100:.1f}%)")
        print(f"    has_certifications: {sf['has_certifications']} ({sf['has_certifications']/sf['total']*100:.1f}%)")
        print(f"    has_work_skills: {sf['has_work_skills']} ({sf['has_work_skills']/sf['total']*100:.1f}%)")
        print(f"    has_any: {sf['has_any_structured']} ({sf['has_any_structured']/sf['total']*100:.1f}%)")
        print(f"\n  Golden slice ({GOLDEN_SLICE_SIZE} jobs):")
        gs = stats["golden_slice_structured_distribution"]
        print(f"    all_three_fields: {gs['all_three_fields']}")
        print(f"    certs_partial: {gs['certs_partial']}")
        print(f"    some_no_certs: {gs['some_no_certs']}")
        print(f"    no_structured: {gs['no_structured']}")
        print(f"\n  Golden slice occupation categories: {len(stats['golden_slice_occupation_categories'])}")
        print("\n  Outputs:")
        for name, path in manifest["outputs"].items():
            print(f"    {name}: {path}")
        print(f"    manifest: {manifest_path.resolve()}")
        print("\n✓ Step 1 complete.")

    finally:
        con.close()


if __name__ == "__main__":
    main()
