"""Compare no-graph vs graph backend on 100 queries from userSearchLog."""

from __future__ import annotations

import csv
import json
import os
import sys
import time
from pathlib import Path

# Setup paths
_ROOT = Path(__file__).resolve().parent
_NO_GRAPH = _ROOT / "no-graph-search"
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_NO_GRAPH))
os.environ.setdefault("GRAPH_DIR", str(_ROOT / "csv"))

from unified_api.backend_no_graph import NoGraphBackend
from unified_api.backend_graph import GraphBackend

# --- Config ---
SEARCH_LOG = _ROOT / "csv" / "userSearchLog_20260601_20260607.csv"
N_QUERIES = 100
TOP_K = 10


def load_queries(path: Path, n: int) -> list[dict]:
    """Load first N queries from search log (skip empty ks)."""
    queries = []
    with open(path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ks = (row.get("ks") or "").strip()
            if not ks:
                continue
            c0 = [c.strip() for c in (row.get("c0") or "").split(",") if c.strip()]
            queries.append({"ks": ks, "c0": c0 or None})
            if len(queries) >= n:
                break
    return queries


def main():
    print(f"Loading {N_QUERIES} queries from search log...")
    queries = load_queries(SEARCH_LOG, N_QUERIES)
    print(f"Loaded {len(queries)} queries.\n")

    print("Initializing backends...")
    t0 = time.time()
    no_graph = NoGraphBackend()
    print(f"  no-graph ready ({time.time()-t0:.1f}s)")

    t0 = time.time()
    graph = GraphBackend()
    _ = graph.available  # trigger lazy load
    print(f"  graph ready ({time.time()-t0:.1f}s)\n")

    # Run comparison
    results = []
    print(f"{'#':<4} {'query':<20} {'no-graph ms':>11} {'graph ms':>9} {'overlap':>8} {'graph boost':>11}")
    print("-" * 75)

    for i, q in enumerate(queries):
        ks = q["ks"]
        c0 = q["c0"]

        r_ng = no_graph.search(ks, c0=c0, top_k=TOP_K)
        r_g = graph.search(ks, c0=c0, top_k=TOP_K)

        ng_ids = [h.job_id for h in r_ng.hits]
        g_ids = [h.job_id for h in r_g.hits]
        overlap = len(set(ng_ids) & set(g_ids))

        # How many graph-reranked results moved up vs no-graph order
        boosted = 0
        for rank, job_id in enumerate(g_ids):
            if job_id in ng_ids:
                ng_rank = ng_ids.index(job_id)
                if rank < ng_rank:
                    boosted += 1

        display_ks = ks[:18] if len(ks) <= 18 else ks[:16] + ".."
        print(
            f"{i+1:<4} {display_ks:<20} {r_ng.took_ms:>8}ms {r_g.took_ms:>7}ms "
            f"{overlap:>5}/{TOP_K}  {boosted:>5} jobs ↑"
        )

        results.append({
            "query": ks,
            "c0": c0,
            "no_graph_ms": r_ng.took_ms,
            "graph_ms": r_g.took_ms,
            "no_graph_top10": ng_ids,
            "graph_top10": g_ids,
            "overlap": overlap,
            "graph_boosted": boosted,
            "skills_parsed": r_g.filter_notes.get("skills_parsed", ""),
            "graph_candidates": r_g.filter_notes.get("graph_candidates", "0"),
        })

    # Summary
    print("\n" + "=" * 75)
    avg_ng_ms = sum(r["no_graph_ms"] for r in results) / len(results)
    avg_g_ms = sum(r["graph_ms"] for r in results) / len(results)
    avg_overlap = sum(r["overlap"] for r in results) / len(results)
    avg_boosted = sum(r["graph_boosted"] for r in results) / len(results)
    has_skills = sum(1 for r in results if r["skills_parsed"] and r["skills_parsed"] != "(none)")

    print(f"\nSummary ({len(results)} queries):")
    print(f"  Avg latency:   no-graph {avg_ng_ms:.0f}ms  |  graph {avg_g_ms:.0f}ms")
    print(f"  Avg overlap:   {avg_overlap:.1f}/{TOP_K} (same jobs in both top-10)")
    print(f"  Avg boosted:   {avg_boosted:.1f} jobs moved up by graph rerank")
    print(f"  Skills parsed: {has_skills}/{len(results)} queries had skill matches in graph")

    # Save detail
    out_path = _ROOT / "compare_results.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n  Detail saved to: {out_path}")


if __name__ == "__main__":
    main()
