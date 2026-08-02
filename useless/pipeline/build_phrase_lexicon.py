"""
Build phrase lexicon from Step 2A structured extraction results.
Role: A (Content Graph)

Reads graph/extractions_structured.jsonl, counts skill mentions by
canonical_candidate (job-level dedup), and outputs a versioned phrase
lexicon CSV for use in Step 2B phrase matching.

Output: graph/phrase_lexicon_v0.1.csv
Columns: canonical_candidate, display_name, source_field, job_frequency, total_mentions
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parent.parent  # pipeline/ -> repo root; graph/ dataset/ fixtures/ 都掛在根目錄
GRAPH_DIR = _REPO_ROOT / "graph"
EXTRACTIONS_JSONL = GRAPH_DIR / "extractions_structured.jsonl"
OUTPUT_LEXICON = GRAPH_DIR / "phrase_lexicon_v0.1.csv"
OUTPUT_MANIFEST = GRAPH_DIR / "phrase_lexicon_manifest.json"

# Minimum job frequency to include in lexicon
# (playbook §4.4: 舊 structured_extraction.py 用 min_frequency=2)
MIN_JOB_FREQUENCY = 2


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def build_lexicon(
    extractions_path: Path = EXTRACTIONS_JSONL,
    output_path: Path = OUTPUT_LEXICON,
    min_freq: int = MIN_JOB_FREQUENCY,
) -> dict[str, Any]:
    """
    Scan all extractions, count job-level frequency per canonical_candidate.
    Job-level dedup: same skill in one job counts as 1 job occurrence.
    """

    # canonical_candidate -> set of job_ids (for job frequency)
    skill_jobs: dict[str, set[str]] = defaultdict(set)
    # canonical_candidate -> total mention count (not deduped)
    skill_mentions: Counter[str] = Counter()
    # canonical_candidate -> first seen display name (raw_mention)
    display_names: dict[str, str] = {}
    # canonical_candidate -> source_fields seen
    source_fields: dict[str, set[str]] = defaultdict(set)

    total_lines = 0
    with extractions_path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            total_lines += 1
            record = json.loads(line)
            job_id = record["job_id"]

            # Process skills
            for mention in record.get("skills", []):
                canonical = mention["canonical_candidate"]
                skill_jobs[canonical].add(job_id)
                skill_mentions[canonical] += 1
                if canonical not in display_names:
                    display_names[canonical] = mention["raw_mention"]
                source_fields[canonical].add(mention["source_field"])

            # Process credentials (separate track, but include in lexicon
            # for phrase matching coverage — they'll still be routed to
            # credentials[] in the final extraction)
            for mention in record.get("credentials", []):
                canonical = mention["canonical_candidate"]
                skill_jobs[canonical].add(job_id)
                skill_mentions[canonical] += 1
                if canonical not in display_names:
                    display_names[canonical] = mention["raw_mention"]
                source_fields[canonical].add(mention["source_field"])

    # Filter by minimum job frequency
    qualified = {
        canonical: len(jobs)
        for canonical, jobs in skill_jobs.items()
        if len(jobs) >= min_freq
    }

    # Sort by job frequency descending
    sorted_entries = sorted(qualified.items(), key=lambda x: (-x[1], x[0]))

    # Write CSV
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        f.write("canonical_candidate,display_name,source_fields,job_frequency,total_mentions\n")
        for canonical, job_freq in sorted_entries:
            display = display_names[canonical].replace('"', '""')
            fields = "|".join(sorted(source_fields[canonical]))
            total = skill_mentions[canonical]
            f.write(f'"{canonical}","{display}","{fields}",{job_freq},{total}\n')

    # Manifest
    manifest = {
        "step": "build_phrase_lexicon",
        "version": "v0.1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": str(extractions_path.resolve()),
        "output": str(output_path.resolve()),
        "min_job_frequency": min_freq,
        "statistics": {
            "total_jobs_scanned": total_lines,
            "unique_canonical_candidates": len(skill_jobs),
            "qualified_entries": len(qualified),
            "filtered_out": len(skill_jobs) - len(qualified),
            "top_20": [
                {"canonical": c, "job_freq": f, "display": display_names[c]}
                for c, f in sorted_entries[:20]
            ],
        },
    }
    OUTPUT_MANIFEST.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return manifest


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Build phrase lexicon from Step 2A structured extractions"
    )
    parser.add_argument("--min-freq", type=int, default=MIN_JOB_FREQUENCY)
    args = parser.parse_args()

    print("=" * 60)
    print("Building phrase lexicon from structured extractions")
    print("=" * 60)

    manifest = build_lexicon(min_freq=args.min_freq)
    stats = manifest["statistics"]

    print(f"\n  Jobs scanned:        {stats['total_jobs_scanned']:,}")
    print(f"  Unique candidates:   {stats['unique_canonical_candidates']:,}")
    print(f"  Qualified (freq>={manifest['min_job_frequency']}): {stats['qualified_entries']:,}")
    print(f"  Filtered out:        {stats['filtered_out']:,}")
    print(f"\n  Top 20 by job frequency:")
    for i, entry in enumerate(stats["top_20"], 1):
        print(f"    {i:2}. {entry['display']:<30} freq={entry['job_freq']:,}")
    print(f"\n  Output: {manifest['output']}")
    print("\n✓ Phrase lexicon built.")


if __name__ == "__main__":
    main()
