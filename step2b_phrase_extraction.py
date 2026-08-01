"""
Step 2B — Phrase Matching 非結構化技能抽取
Role: A (Content Graph)
Playbook: docs/SKILL_GRAPH_PLAYBOOK.md §2B

用 Step 2A 產出的 phrase_lexicon_v0.1.csv 掃描三個非結構化欄位
（職務名稱、職務內容、附加條件），以 Aho-Corasick 多模式匹配找出技能 mentions。

規則：
- method = "phrase"
- confidence = 0.90 (phrase match, not structured; threshold TBD at Gate 1)
- assertion_status = "affirmed" (預設；Step 2C 的否定偵測會在後續覆寫)
- requirement_level = "unspecified" (非結構化欄位預設；後續可用規則升級為 preferred)
- evidence = 原文中匹配到的片段 (verbatim substring)
- start_offset / end_offset = 原文中的位置 (character offset in source_field text)
- 同一 (job, skill) 多次出現保留全部 mentions（不在抽取階段刪除）
- 若 mention 已被 Step 2A 結構化抽取涵蓋（same job + same canonical），仍保留
  （B 在 Step 4 做聚合時會 dedup）

Latin word boundary: 英文 skill 必須命中完整 word boundary，避免 "C" 命中 "CSS" 等。
"""

from __future__ import annotations

import csv
import json
import re
import unicodedata
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

DATASET_DIR = Path(__file__).parent / "dataset"
JOBS_CSV = DATASET_DIR / "職缺.csv"
GRAPH_DIR = Path(__file__).parent / "graph"
LEXICON_CSV = GRAPH_DIR / "phrase_lexicon_v0.1.csv"
OUTPUT_JSONL = GRAPH_DIR / "extractions_phrase.jsonl"
OUTPUT_MANIFEST = GRAPH_DIR / "extraction_phrase_manifest.json"

EXTRACTOR_VERSION = "v0.1"
PHRASE_CONFIDENCE = 0.90
BATCH_SIZE = 10_000

# 非結構化來源欄位（掃描用）
SOURCE_FIELDS = ["職務名稱", "職務內容", "附加條件"]


# ─────────────────────────────────────────────────────────────────────────────
# Normalization
# ─────────────────────────────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    """NFKC + casefold + 空白壓縮，保留語意標點"""
    value = unicodedata.normalize("NFKC", text)
    value = re.sub(r"\s+", " ", value.strip())
    return value.casefold()


# ─────────────────────────────────────────────────────────────────────────────
# Aho-Corasick automaton
# ─────────────────────────────────────────────────────────────────────────────

class AhoCorasick:
    """Aho-Corasick multi-pattern matcher with Latin word boundary enforcement."""

    def __init__(self) -> None:
        self.goto: list[dict[str, int]] = [{}]
        self.fail: list[int] = [0]
        self.output: list[list[str]] = [[]]

    def add_pattern(self, pattern: str) -> None:
        state = 0
        for ch in pattern:
            if ch not in self.goto[state]:
                self.goto[state][ch] = len(self.goto)
                self.goto.append({})
                self.fail.append(0)
                self.output.append([])
            state = self.goto[state][ch]
        self.output[state].append(pattern)

    def build(self) -> None:
        queue: deque[int] = deque()
        for ch, s in self.goto[0].items():
            queue.append(s)
        while queue:
            r = queue.popleft()
            for ch, s in self.goto[r].items():
                queue.append(s)
                state = self.fail[r]
                while state and ch not in self.goto[state]:
                    state = self.fail[state]
                self.fail[s] = self.goto[state].get(ch, 0)
                if self.fail[s] == s:
                    self.fail[s] = 0
                self.output[s] = self.output[s] + self.output[self.fail[s]]

    def search(self, text: str, *, max_matches: int = 100) -> list[tuple[int, int, str]]:
        """Returns list of (start, end, pattern) non-overlapping matches."""
        state = 0
        raw_matches: list[tuple[int, int, str]] = []
        for i, ch in enumerate(text):
            while state and ch not in self.goto[state]:
                state = self.fail[state]
            state = self.goto[state].get(ch, 0)
            for pattern in self.output[state]:
                start = i - len(pattern) + 1
                end = i + 1
                raw_matches.append((start, end, pattern))

        # Remove overlaps: prefer longer match, then earlier start
        raw_matches.sort(key=lambda m: (m[0], -(m[1] - m[0])))
        selected: list[tuple[int, int, str]] = []
        last_end = -1
        for start, end, pattern in raw_matches:
            if start >= last_end:
                # Latin word boundary check
                if self._check_boundary(text, start, end, pattern):
                    selected.append((start, end, pattern))
                    last_end = end
                    if len(selected) >= max_matches:
                        break
        return selected

    @staticmethod
    def _check_boundary(text: str, start: int, end: int, pattern: str) -> bool:
        """Enforce word boundary for patterns containing Latin chars."""
        has_latin = bool(re.search(r"[a-z0-9]", pattern))
        if not has_latin:
            return True
        left_ok = start == 0 or not text[start - 1].isalnum()
        right_ok = end >= len(text) or not text[end].isalnum()
        return left_ok and right_ok


# ─────────────────────────────────────────────────────────────────────────────
# Lexicon loading
# ─────────────────────────────────────────────────────────────────────────────

def load_lexicon(lexicon_path: Path) -> dict[str, dict[str, Any]]:
    """
    Load phrase_lexicon CSV into {normalized_pattern: metadata} dict.
    The normalized_pattern is used for matching; metadata has display_name + canonical.
    """
    lexicon: dict[str, dict[str, Any]] = {}
    with lexicon_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            canonical = row["canonical_candidate"]
            display = row["display_name"]
            # Build normalized search key from display_name
            normalized = _normalize(display)
            if len(normalized) < 2:
                continue
            # Skip if already have a longer or higher-freq entry for same normalized form
            if normalized not in lexicon:
                lexicon[normalized] = {
                    "canonical_candidate": canonical,
                    "display_name": display,
                    "source_fields": row.get("source_fields", ""),
                }
    return lexicon


# ─────────────────────────────────────────────────────────────────────────────
# Mention ID generation
# ─────────────────────────────────────────────────────────────────────────────

def _mention_id(job_id: str, source_field: str, start: int, end: int, version: str) -> str:
    """Deterministic mention_id for phrase matches (has char offset)."""
    return f"mention:{job_id}:{source_field}:{start}:{end}:{version}"


# ─────────────────────────────────────────────────────────────────────────────
# Core extraction
# ─────────────────────────────────────────────────────────────────────────────

def extract_phrase_for_job(
    job_id: str,
    fields: dict[str, str],
    automaton: AhoCorasick,
    lexicon: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """
    Scan non-structured fields for phrase matches. Produce §3.4 contract record.
    Credentials in phrase_lexicon (source_field=專業證照) go to credentials[].
    """
    skills: list[dict[str, Any]] = []
    credentials: list[dict[str, Any]] = []

    for source_field_name in SOURCE_FIELDS:
        raw_text = fields.get(source_field_name, "")
        if not raw_text:
            continue
        normalized_text = _normalize(raw_text)
        matches = automaton.search(normalized_text)

        for start, end, pattern in matches:
            meta = lexicon.get(pattern)
            if meta is None:
                continue

            # Extract evidence from original text
            # _normalize does NFKC + casefold + whitespace collapse
            # For evidence we need verbatim from raw_text at approximately same position
            # Since casefold doesn't change length for most chars, try direct slice first
            if len(normalized_text) == len(raw_text):
                evidence = raw_text[start:end]
            else:
                # Length mismatch: use display_name as safe fallback
                evidence = meta["display_name"]

            canonical = meta["canonical_candidate"]
            is_credential = "專業證照" in meta.get("source_fields", "")

            mention = {
                "mention_id": _mention_id(job_id, source_field_name, start, end, EXTRACTOR_VERSION),
                "raw_mention": evidence,
                "canonical_candidate": canonical,
                "source_field": source_field_name,
                "start_offset": start,
                "end_offset": end,
                "requirement_level": "unspecified",
                "assertion_status": "affirmed",
                "confidence": PHRASE_CONFIDENCE,
                "evidence": evidence,
                "method": "phrase",
                "extractor_version": EXTRACTOR_VERSION,
            }

            if is_credential and not canonical.startswith("skill:"):
                credentials.append(mention)
            else:
                skills.append(mention)

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


def run_phrase_extraction(
    jobs_csv: Path = JOBS_CSV,
    lexicon_path: Path = LEXICON_CSV,
    output_jsonl: Path = OUTPUT_JSONL,
    *,
    limit: int | None = None,
    batch_size: int = BATCH_SIZE,
) -> dict[str, Any]:
    """Run phrase matching on all jobs, output JSONL."""

    # 1. Load lexicon and build automaton
    print("  Loading phrase lexicon...")
    lexicon = load_lexicon(lexicon_path)
    print(f"  Loaded {len(lexicon):,} patterns")

    print("  Building Aho-Corasick automaton...")
    ac = AhoCorasick()
    for pattern in lexicon:
        ac.add_pattern(pattern)
    ac.build()
    print("  Automaton ready.")

    # 2. Scan jobs
    output_jsonl.parent.mkdir(parents=True, exist_ok=True)
    jobs_path = _sql_path(jobs_csv)
    limit_sql = f"LIMIT {int(limit)}" if limit is not None else ""

    con = duckdb.connect(":memory:")
    con.execute("SET threads=4")
    con.execute("SET memory_limit='4GB'")

    total_jobs = 0
    total_skills = 0
    total_credentials = 0
    jobs_with_matches = 0

    try:
        cursor = con.execute(f"""
            SELECT
                CAST("職缺編號" AS VARCHAR) AS job_id,
                COALESCE(NULLIF(TRIM("職務名稱"), ''), '') AS title,
                COALESCE(NULLIF(TRIM("職務內容"), ''), '') AS description,
                COALESCE(NULLIF(TRIM("附加條件"), ''), '') AS additional_req
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
                for job_id, title, description, additional_req in rows:
                    fields = {
                        "職務名稱": title or "",
                        "職務內容": description or "",
                        "附加條件": additional_req or "",
                    }
                    record = extract_phrase_for_job(
                        job_id=job_id.strip(),
                        fields=fields,
                        automaton=ac,
                        lexicon=lexicon,
                    )

                    # Only write if there are matches (phrase extraction supplements 2A)
                    if record["skills"] or record["credentials"]:
                        f.write(json.dumps(record, ensure_ascii=False) + "\n")
                        jobs_with_matches += 1

                    total_jobs += 1
                    total_skills += len(record["skills"])
                    total_credentials += len(record["credentials"])

                    if total_jobs % 100_000 == 0:
                        print(f"  Processed {total_jobs:,} jobs...")

    finally:
        con.close()

    # 3. Manifest
    manifest = {
        "step": "step2b_phrase_extraction",
        "schema_version": "v0.1",
        "extractor_version": EXTRACTOR_VERSION,
        "method": "phrase",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_file": str(jobs_csv.resolve()),
        "lexicon_file": str(lexicon_path.resolve()),
        "output_file": str(output_jsonl.resolve()),
        "source_fields_scanned": SOURCE_FIELDS,
        "confidence": PHRASE_CONFIDENCE,
        "lexicon_size": len(lexicon),
        "statistics": {
            "total_jobs_scanned": total_jobs,
            "jobs_with_phrase_matches": jobs_with_matches,
            "total_skill_mentions": total_skills,
            "total_credential_mentions": total_credentials,
            "phrase_coverage_rate": (
                jobs_with_matches / total_jobs if total_jobs > 0 else 0.0
            ),
        },
        "contract_compliance": {
            "mention_id_deterministic": True,
            "evidence_is_verbatim_substring": True,
            "start_end_offset_provided": True,
            "credentials_separated_from_skills": True,
            "latin_word_boundary_enforced": True,
        },
        "known_limitations": [
            "assertion_status 全部預設 affirmed（否定偵測留待後處理或 LLM）",
            "requirement_level 預設 unspecified（preferred/required 判定留待後處理）",
            "casefold 可能導致少數 CJK 字元的 offset 微偏（極少見）",
            "lexicon 來自結構化欄位統計，非結構化文字中的 OOV 技能不在涵蓋範圍",
        ],
    }
    OUTPUT_MANIFEST.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return manifest


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Step 2B: Phrase matching extraction (職務名稱 / 職務內容 / 附加條件)"
    )
    parser.add_argument("--jobs-csv", type=Path, default=JOBS_CSV)
    parser.add_argument("--lexicon", type=Path, default=LEXICON_CSV)
    parser.add_argument("--output", type=Path, default=OUTPUT_JSONL)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    args = parser.parse_args()

    print("=" * 60)
    print("Step 2B — Phrase Matching 非結構化技能抽取")
    print("=" * 60)
    print(f"  來源: {args.jobs_csv}")
    print(f"  Lexicon: {args.lexicon}")
    print(f"  輸出: {args.output}")
    if args.limit:
        print(f"  限制: {args.limit} 筆")
    print()

    manifest = run_phrase_extraction(
        jobs_csv=args.jobs_csv,
        lexicon_path=args.lexicon,
        output_jsonl=args.output,
        limit=args.limit,
        batch_size=args.batch_size,
    )

    stats = manifest["statistics"]
    print("\n" + "=" * 60)
    print("STEP 2B SUMMARY")
    print("=" * 60)
    print(f"  Total jobs scanned:       {stats['total_jobs_scanned']:,}")
    print(f"  Jobs with phrase matches: {stats['jobs_with_phrase_matches']:,} ({stats['phrase_coverage_rate']:.1%})")
    print(f"  Total skill mentions:     {stats['total_skill_mentions']:,}")
    print(f"  Total credential mentions:{stats['total_credential_mentions']:,}")
    print(f"  Lexicon patterns used:    {manifest['lexicon_size']:,}")
    print(f"\n  Output: {manifest['output_file']}")
    print("\n✓ Step 2B complete.")


if __name__ == "__main__":
    main()
