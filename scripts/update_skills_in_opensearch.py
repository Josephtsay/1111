"""Bulk update OpenSearch job index with graph-derived skills.

Reads HAS_SKILL edges from csv/edges.csv, aggregates skills per job,
and updates the `skills` field in jobs-search-dev-v2 index.

Also adds a new `graph_skills` keyword field for structured skill matching
(the existing `skills` text field is good for BM25 but not for exact matching).

Usage:
    cd /path/to/1111
    no-graph-search/.venv/bin/python scripts/update_skills_in_opensearch.py
"""

from __future__ import annotations

import csv
import sys
import time
from collections import defaultdict
from pathlib import Path

# Setup imports
_ROOT = Path(__file__).resolve().parent.parent
_NO_GRAPH = _ROOT / "no-graph-search"
sys.path.insert(0, str(_NO_GRAPH))

# Load .env from no-graph-search directory
import os
from dotenv import load_dotenv
load_dotenv(_NO_GRAPH / ".env")

from src.clients import opensearch_client  # noqa: E402

# --- Config ---
EDGES_CSV = _ROOT / "csv" / "edges.csv"
INDEX = "jobs-search-dev-v2"
BATCH_SIZE = 500


def load_skills_per_job(edges_path: Path) -> dict[str, list[str]]:
    """Read HAS_SKILL edges, return {job_id: [skill_names]}."""
    skills_by_job: dict[str, list[str]] = defaultdict(list)
    
    print(f"Reading {edges_path}...")
    t0 = time.time()
    
    with open(edges_path, "r", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader)
        # Columns: edge_type, source_id, target_id, requirement_level, ...
        
        for row in reader:
            if not row or row[0] != "HAS_SKILL":
                continue
            # source_id = "job:12345", target_id = "skill:excel"
            job_id = row[1].removeprefix("job:")
            skill_name = row[2].removeprefix("skill:")
            # Clean up skill name: replace underscores with spaces
            skill_clean = skill_name.replace("_", " ")
            skills_by_job[job_id].append(skill_clean)
    
    elapsed = time.time() - t0
    print(f"  Loaded {sum(len(v) for v in skills_by_job.values()):,} edges")
    print(f"  Covering {len(skills_by_job):,} unique jobs")
    print(f"  Time: {elapsed:.1f}s")
    return dict(skills_by_job)


def bulk_update_skills(
    client,
    skills_by_job: dict[str, list[str]],
    index: str,
    batch_size: int,
) -> dict[str, int]:
    """Bulk update jobs with graph_skills field."""
    
    stats = {"updated": 0, "errors": 0, "batches": 0}
    jobs = list(skills_by_job.items())
    total = len(jobs)
    
    print(f"\nBulk updating {total:,} jobs in batches of {batch_size}...")
    t0 = time.time()
    
    for i in range(0, total, batch_size):
        batch = jobs[i : i + batch_size]
        body = []
        
        for job_id, skills in batch:
            # Deduplicate and sort
            unique_skills = sorted(set(skills))
            # Update action
            body.append({"update": {"_index": index, "_id": job_id}})
            body.append({
                "doc": {
                    "graph_skills": unique_skills,
                    # Also update skills text field (merge with existing)
                    "skills": ", ".join(unique_skills),
                },
                "doc_as_upsert": False,  # Only update existing docs
            })
        
        try:
            resp = client.bulk(body=body, refresh=False)
            
            for item in resp.get("items", []):
                action = item.get("update", {})
                if action.get("status") in (200, 201):
                    stats["updated"] += 1
                elif action.get("status") == 404:
                    pass  # Job not in index, skip
                else:
                    stats["errors"] += 1
                    if stats["errors"] <= 5:
                        print(f"    Error: {action}")
        except Exception as e:
            stats["errors"] += len(batch)
            print(f"    Batch error: {e}")
        
        stats["batches"] += 1
        
        # Progress
        if stats["batches"] % 100 == 0:
            elapsed = time.time() - t0
            rate = stats["updated"] / elapsed if elapsed > 0 else 0
            eta = (total - i - batch_size) / rate if rate > 0 else 0
            print(
                f"  [{stats['batches']:>5}] {i + batch_size:>9,}/{total:,} "
                f"({(i + batch_size) / total * 100:.1f}%) "
                f"rate={rate:.0f} docs/s  ETA={eta:.0f}s"
            )
    
    elapsed = time.time() - t0
    print(f"\nDone in {elapsed:.1f}s")
    print(f"  Updated: {stats['updated']:,}")
    print(f"  Errors: {stats['errors']:,}")
    print(f"  Rate: {stats['updated'] / elapsed:.0f} docs/s")
    return stats


def ensure_mapping(client, index: str):
    """Add graph_skills keyword field if it doesn't exist."""
    mapping = client.indices.get_mapping(index=index)
    idx = list(mapping.keys())[0]
    props = mapping[idx]["mappings"]["properties"]
    
    if "graph_skills" not in props:
        print("Adding graph_skills field mapping...")
        client.indices.put_mapping(
            index=index,
            body={
                "properties": {
                    "graph_skills": {
                        "type": "keyword",
                    }
                }
            },
        )
        print("  Done.")
    else:
        print("graph_skills field already exists.")


def main():
    print("=" * 60)
    print("Update OpenSearch jobs with graph-derived skills")
    print("=" * 60)
    
    # Load edges
    skills_by_job = load_skills_per_job(EDGES_CSV)
    
    # Connect
    print("\nConnecting to OpenSearch...")
    client = opensearch_client()
    info = client.info()
    print(f"  Cluster: {info['cluster_name']}")
    
    # Ensure mapping
    ensure_mapping(client, INDEX)
    
    # Bulk update
    stats = bulk_update_skills(client, skills_by_job, INDEX, BATCH_SIZE)
    
    # Verify
    print("\nVerifying...")
    count = client.count(
        index=INDEX,
        body={"query": {"exists": {"field": "graph_skills"}}}
    )
    print(f"  Jobs with graph_skills: {count['count']:,}")
    
    # Sample
    sample = client.search(
        index=INDEX,
        body={
            "size": 3,
            "_source": ["job_id", "title", "skills", "graph_skills"],
            "query": {"exists": {"field": "graph_skills"}},
        },
    )
    print("\nSample results:")
    for hit in sample["hits"]["hits"]:
        s = hit["_source"]
        print(f"  {s['job_id']} | {s['title']}")
        print(f"    graph_skills: {s.get('graph_skills', [])[:8]}")
        print()


if __name__ == "__main__":
    main()
