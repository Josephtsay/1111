"""
Step 2A — 結構化技能抽取
Role: A (Content Graph)
Playbook: docs/SKILL_GRAPH_PLAYBOOK.md §2A + §3.4

從 dataset/職缺.csv 的三個結構化能力欄位抽取 skill / credential mentions，
輸出符合 playbook §3.4 介面契約的 JSONL (A → B 唯一介面格式)。

來源欄位與 parser 策略：
- 電腦技能資料: ASCII comma delimiter (高可靠)
- 工作技能: ASCII comma delimiter (高可靠)
- 專業證照: ASCII comma delimiter → 路由到 credentials[] (高可靠)

結構化欄位規則：
- confidence = 1.0 (結構化精確命中)
- assertion_status = "affirmed" (結構化列出即為肯定)
- requirement_level = "unspecified" (結構化欄位無法判定必要程度)
- method = "structured"
- 同一 (job, skill/credential) 多筆 mention 不刪除，保留全部
- evidence = raw_mention (結構化欄位值就是 evidence)
- start_offset / end_offset = null (結構化欄位無法精確定位)
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parent.parent  # pipeline/ -> repo root; graph/ dataset/ fixtures/ 都掛在根目錄
DATASET_DIR = _REPO_ROOT / "dataset"
JOBS_CSV = DATASET_DIR / "職缺.csv"
OUTPUT_DIR = _REPO_ROOT / "graph"

EXTRACTOR_VERSION = "v0.1"
BATCH_SIZE = 10_000

# 結構化欄位 → source_field 名稱映射
SOURCE_FIELDS = {
    "電腦技能資料": "電腦技能資料",
    "工作技能": "工作技能",
    "專業證照": "專業證照",
}

# 不應視為技能的值（清洗用）
IGNORED_VALUES = frozenset({
    "", "null", "none", "無", "不拘", "其他", "略", "不限", "n/a", "na",
})


# ─────────────────────────────────────────────────────────────────────────────
# Normalization (shared with canonicalization, keep lightweight here)
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_for_comparison(text: str) -> str:
    """NFKC + casefold + 空白壓縮，保留語意標點 (C++, C#, .NET, Node.js)"""
    value = unicodedata.normalize("NFKC", text)
    value = re.sub(r"\s+", " ", value.strip())
    return value.casefold()


# ─────────────────────────────────────────────────────────────────────────────
# Source-specific parsers
# ─────────────────────────────────────────────────────────────────────────────

def _split_computer_skills(raw: str) -> list[str]:
    """
    電腦技能資料: 以 ASCII comma 為主要 delimiter。
    不把中文逗號/頓號/斜線無條件切開（playbook §2A: 依來源欄位使用不同 parser）。
    """
    if not raw or raw.strip().upper() in ("NULL", "NONE", ""):
        return []
    items = []
    for part in raw.split(","):
        cleaned = part.strip()
        if _normalize_for_comparison(cleaned) not in IGNORED_VALUES and 1 <= len(cleaned) <= 120:
            items.append(cleaned)
    return items


def _split_work_skills(raw: str) -> list[str]:
    """
    工作技能: 以 ASCII comma 為主要 delimiter。
    """
    if not raw or raw.strip().upper() in ("NULL", "NONE", ""):
        return []
    items = []
    for part in raw.split(","):
        cleaned = part.strip()
        if _normalize_for_comparison(cleaned) not in IGNORED_VALUES and 1 <= len(cleaned) <= 120:
            items.append(cleaned)
    return items


def _split_certifications(raw: str) -> list[str]:
    """
    專業證照: 以 ASCII comma 為主要 delimiter。
    """
    if not raw or raw.strip().upper() in ("NULL", "NONE", ""):
        return []
    items = []
    for part in raw.split(","):
        cleaned = part.strip()
        if _normalize_for_comparison(cleaned) not in IGNORED_VALUES and 1 <= len(cleaned) <= 120:
            items.append(cleaned)
    return items


# ─────────────────────────────────────────────────────────────────────────────
# Mention ID generation (deterministic per playbook §3.4)
# ─────────────────────────────────────────────────────────────────────────────

def _mention_id(job_id: str, source_field: str, ordinal: int, version: str) -> str:
    """
    Deterministic mention_id.
    結構化欄位沒有 char offset，改用 ordinal (在該欄位中的第幾個值，0-based)。
    格式: mention:<job_id>:<source_field>:<ordinal>:<version>
    """
    return f"mention:{job_id}:{source_field}:{ordinal}:{version}"


# ─────────────────────────────────────────────────────────────────────────────
# Canonical candidate generation
# ─────────────────────────────────────────────────────────────────────────────

def _canonical_candidate_skill(raw_mention: str) -> str:
    """
    初步 canonical_candidate (抽取端建議；最終以版本化 registry 為準)。
    格式: skill:<registry_key>
    registry_key = NFKC + casefold + 空白壓縮 + 移除首尾空白
    保留語意標點 (c++, c#, .net, node.js)
    """
    key = _normalize_for_comparison(raw_mention)
    # 移除非語意的外層括號/引號
    key = key.strip("\"'「」『』【】《》()")
    key = re.sub(r"\s+", "_", key.strip())
    if not key:
        key = "unknown"
    return f"skill:{key}"


def _canonical_candidate_credential(raw_mention: str) -> str:
    """
    格式: credential:<registry_key>
    """
    key = _normalize_for_comparison(raw_mention)
    key = key.strip("\"'「」『』【】《》()")
    key = re.sub(r"\s+", "_", key.strip())
    if not key:
        key = "unknown"
    return f"credential:{key}"


# ─────────────────────────────────────────────────────────────────────────────
# Core extraction logic
# ─────────────────────────────────────────────────────────────────────────────

def extract_structured_for_job(
    job_id: str,
    computer_skills: str | None,
    work_skills: str | None,
    certifications: str | None,
) -> dict[str, Any]:
    """
    對單一 Job 的三個結構化欄位做抽取，回傳符合 §3.4 契約的 dict。
    """
    skills: list[dict[str, Any]] = []
    credentials: list[dict[str, Any]] = []

    # 1. 電腦技能資料 → skills[]
    for ordinal, value in enumerate(_split_computer_skills(computer_skills or "")):
        skills.append({
            "mention_id": _mention_id(job_id, "電腦技能資料", ordinal, EXTRACTOR_VERSION),
            "raw_mention": value,
            "canonical_candidate": _canonical_candidate_skill(value),
            "source_field": "電腦技能資料",
            "start_offset": None,
            "end_offset": None,
            "requirement_level": "unspecified",
            "assertion_status": "affirmed",
            "confidence": 1.0,
            "evidence": value,
            "method": "structured",
            "extractor_version": EXTRACTOR_VERSION,
        })

    # 2. 工作技能 → skills[]
    for ordinal, value in enumerate(_split_work_skills(work_skills or "")):
        skills.append({
            "mention_id": _mention_id(job_id, "工作技能", ordinal, EXTRACTOR_VERSION),
            "raw_mention": value,
            "canonical_candidate": _canonical_candidate_skill(value),
            "source_field": "工作技能",
            "start_offset": None,
            "end_offset": None,
            "requirement_level": "unspecified",
            "assertion_status": "affirmed",
            "confidence": 1.0,
            "evidence": value,
            "method": "structured",
            "extractor_version": EXTRACTOR_VERSION,
        })

    # 3. 專業證照 → credentials[]
    for ordinal, value in enumerate(_split_certifications(certifications or "")):
        credentials.append({
            "mention_id": _mention_id(job_id, "專業證照", ordinal, EXTRACTOR_VERSION),
            "raw_mention": value,
            "canonical_candidate": _canonical_candidate_credential(value),
            "source_field": "專業證照",
            "start_offset": None,
            "end_offset": None,
            "requirement_level": "unspecified",
            "assertion_status": "affirmed",
            "confidence": 1.0,
            "evidence": value,
            "method": "structured",
            "extractor_version": EXTRACTOR_VERSION,
        })

    return {
        "job_id": job_id,
        "skills": skills,
        "credentials": credentials,
        "extraction_version": EXTRACTOR_VERSION,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main pipeline
# ─────────────────────────────────────────────────────────────────────────────

def _sql_path(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "''")


def run_extraction(
    jobs_csv: Path = JOBS_CSV,
    output_dir: Path = OUTPUT_DIR,
    *,
    limit: int | None = None,
    batch_size: int = BATCH_SIZE,
) -> dict[str, Any]:
    """
    全量結構化抽取：DuckDB 讀取 CSV，逐批產出 JSONL。
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    output_jsonl = output_dir / "extractions_structured.jsonl"
    manifest_path = output_dir / "extraction_structured_manifest.json"

    jobs_path = _sql_path(jobs_csv)
    limit_sql = f"LIMIT {int(limit)}" if limit is not None else ""

    con = duckdb.connect(":memory:")
    con.execute("SET threads=4")
    con.execute("SET memory_limit='4GB'")

    # 計數器
    total_jobs = 0
    total_skills = 0
    total_credentials = 0
    jobs_with_skills = 0
    jobs_with_credentials = 0

    try:
        cursor = con.execute(f"""
            SELECT
                CAST("職缺編號" AS VARCHAR) AS job_id,
                COALESCE(NULLIF(TRIM("電腦技能資料"), ''), NULL) AS computer_skills,
                COALESCE(NULLIF(TRIM("工作技能"), ''), NULL) AS work_skills,
                COALESCE(NULLIF(TRIM("專業證照"), ''), NULL) AS certifications
            FROM read_csv(
                '{jobs_path}',
                header=true,
                all_varchar=true,
                strict_mode=false
            )
            WHERE CAST("職缺編號" AS VARCHAR) IS NOT NULL
              AND TRIM(CAST("職缺編號" AS VARCHAR)) != ''
            ORDER BY CAST("職缺編號" AS VARCHAR)
            {limit_sql}
        """)

        with output_jsonl.open("w", encoding="utf-8") as f:
            while True:
                rows = cursor.fetchmany(batch_size)
                if not rows:
                    break
                for job_id, computer_skills, work_skills, certifications in rows:
                    record = extract_structured_for_job(
                        job_id=job_id.strip(),
                        computer_skills=computer_skills,
                        work_skills=work_skills,
                        certifications=certifications,
                    )
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")

                    total_jobs += 1
                    n_skills = len(record["skills"])
                    n_creds = len(record["credentials"])
                    total_skills += n_skills
                    total_credentials += n_creds
                    if n_skills > 0:
                        jobs_with_skills += 1
                    if n_creds > 0:
                        jobs_with_credentials += 1

                    if total_jobs % 100_000 == 0:
                        print(f"  Processed {total_jobs:,} jobs...")

    finally:
        con.close()

    # 產出 manifest
    manifest = {
        "step": "step2a_structured_extraction",
        "schema_version": "v0.1",
        "extractor_version": EXTRACTOR_VERSION,
        "method": "structured",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_file": str(jobs_csv.resolve()),
        "output_file": str(output_jsonl.resolve()),
        "source_fields": ["電腦技能資料", "工作技能", "專業證照"],
        "delimiter_policy": {
            "電腦技能資料": "ASCII comma",
            "工作技能": "ASCII comma",
            "專業證照": "ASCII comma",
        },
        "extraction_rules": {
            "confidence": 1.0,
            "assertion_status": "affirmed",
            "requirement_level": "unspecified",
            "evidence_rule": "raw_mention == evidence (structured field value)",
        },
        "statistics": {
            "total_jobs_processed": total_jobs,
            "jobs_with_skills": jobs_with_skills,
            "jobs_with_credentials": jobs_with_credentials,
            "total_skill_mentions": total_skills,
            "total_credential_mentions": total_credentials,
            "skill_coverage_rate": (
                jobs_with_skills / total_jobs if total_jobs > 0 else 0.0
            ),
            "credential_coverage_rate": (
                jobs_with_credentials / total_jobs if total_jobs > 0 else 0.0
            ),
        },
        "contract_compliance": {
            "mention_id_deterministic": True,
            "evidence_required": True,
            "same_job_skill_duplicates_preserved": True,
            "credentials_separated_from_skills": True,
        },
        "known_limitations": [
            "結構化欄位無法判定 requirement_level，一律標記 unspecified",
            "start_offset/end_offset 為 null（結構化欄位非自由文字）",
            "assertion_status 全部為 affirmed（結構化列出即為肯定；否定/不確定偵測留待 Step 2B）",
            "canonical_candidate 僅為初步建議，最終以 Step 3 版本化 registry 為準",
        ],
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return manifest


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Step 2A: 結構化技能抽取 (電腦技能資料 / 工作技能 / 專業證照)"
    )
    parser.add_argument(
        "--jobs-csv",
        type=Path,
        default=JOBS_CSV,
        help="職缺.csv 路徑",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=OUTPUT_DIR,
        help="輸出目錄 (預設: graph/)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="限制處理筆數 (smoke test 用)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=BATCH_SIZE,
        help="DuckDB fetchmany batch size",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("Step 2A — 結構化技能抽取")
    print("=" * 60)
    print(f"  來源: {args.jobs_csv}")
    print(f"  輸出: {args.output_dir}")
    if args.limit:
        print(f"  限制: {args.limit} 筆")
    print()

    manifest = run_extraction(
        jobs_csv=args.jobs_csv,
        output_dir=args.output_dir,
        limit=args.limit,
        batch_size=args.batch_size,
    )

    stats = manifest["statistics"]
    print("\n" + "=" * 60)
    print("STEP 2A SUMMARY")
    print("=" * 60)
    print(f"  Total jobs processed:     {stats['total_jobs_processed']:,}")
    print(f"  Jobs with skills:         {stats['jobs_with_skills']:,} ({stats['skill_coverage_rate']:.1%})")
    print(f"  Jobs with credentials:    {stats['jobs_with_credentials']:,} ({stats['credential_coverage_rate']:.1%})")
    print(f"  Total skill mentions:     {stats['total_skill_mentions']:,}")
    print(f"  Total credential mentions:{stats['total_credential_mentions']:,}")
    print(f"\n  Output: {manifest['output_file']}")
    print(f"  Manifest: {args.output_dir / 'extraction_structured_manifest.json'}")
    print("\n✓ Step 2A complete.")


if __name__ == "__main__":
    main()
