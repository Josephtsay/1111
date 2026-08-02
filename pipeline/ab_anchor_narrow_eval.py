"""
Fixed-sample A/B for anchor-narrowing stages.

Usage:
  python pipeline/ab_anchor_narrow_eval.py --stage 1
  python pipeline/ab_anchor_narrow_eval.py --stage 2
  python pipeline/ab_anchor_narrow_eval.py --stage 3
  python pipeline/ab_anchor_narrow_eval.py --stage 4

Stage N compares:
  baseline = behaviour with stage-N change disabled (monkeypatch)
  treatment = current module code (all stages implemented so far)
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Callable

import duckdb
import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parent.parent  # pipeline/ -> repo root
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import step8_retrieval_smoke as s8  # noqa: E402  (pipeline/ sibling)
from job_skill_graph.metrics import evaluate_rankings  # noqa: E402

# 腳本住 pipeline/，產物仍寫回 graph_track_a_compare/
OUT = _REPO_ROOT / "graph_track_a_compare"
TOP_K = 50
HUGE_POOL = 20_000


def load_fixed_queries(limit: int = 2000):
    path = s8.GRAPH_DIR / "eval_dataset" / "labeled_queries.parquet"
    labels_path = s8.GRAPH_DIR / "eval_dataset" / "labels.parquet"
    con = duckdb.connect()
    queries = con.execute(
        f"""
        SELECT query_id, query
        FROM read_parquet('{path.resolve().as_posix()}')
        WHERE data_split = 'test'
        ORDER BY query_id
        LIMIT {limit}
        """
    ).fetchdf()
    ids = ", ".join(f"'{q}'" for q in queries["query_id"].tolist())
    labels = con.execute(
        f"""
        SELECT query_id, job_id, relevance
        FROM read_parquet('{labels_path.resolve().as_posix()}')
        WHERE query_id IN ({ids})
        """
    ).fetchdf()
    return queries, labels


def occ_pool_size(
    occ_ids: list[str],
    index: s8.GraphIndex,
    *,
    narrow_large_occ: bool = True,
) -> int:
    """Effective job pool matching traverse_and_rank occupation policy."""
    jobs: set[str] = set()
    for oid in occ_ids:
        code = oid.replace("occ:", "")
        descendants = s8._get_descendants(code, index)
        expanded: set[str] = set(index.occ_to_jobs.get(code, []))
        for c in descendants:
            expanded.update(index.occ_to_jobs.get(c, []))
        level = s8._occ_level(code)
        use_narrow = narrow_large_occ and (
            level in ("middle", "major") or len(expanded) > s8.OCC_POOL_CAP
        )
        if use_narrow:
            selected = set(index.occ_to_jobs.get(code, []))
            core = index.core_skills.get(code, [])
            if not core and descendants:
                merged: dict[str, float] = {}
                for child in descendants[:40]:
                    for sid, rate in index.core_skills.get(child, []):
                        merged[sid] = max(merged.get(sid, 0.0), rate)
                core = list(merged.items())
            top_core = sorted(core, key=lambda x: -x[1])[: s8.OCC_CORE_SKILL_TOP_N]
            for skill_id, _rate in top_core:
                per_skill = 0
                for job_id, _req, _conf in index.skill_to_jobs.get(skill_id, []):
                    if job_id not in expanded:
                        continue
                    selected.add(job_id)
                    per_skill += 1
                    if per_skill >= 500 or len(selected) >= s8.OCC_POOL_CAP:
                        break
                if len(selected) >= s8.OCC_POOL_CAP:
                    break
            jobs |= selected
        else:
            jobs |= expanded
    return len(jobs)


# ── baseline shims (pre-stage behaviour) ─────────────────────────────────────


def _pick_occupation_parent_collapse(
    candidates: list[str], index: s8.GraphIndex
) -> str | None:
    """Pre-stage-1 picker: always prefer middle/major collapse."""
    if not candidates:
        return None
    uniq = list(dict.fromkeys(candidates))
    if len(uniq) == 1:
        only = uniq[0]
        code = only.replace("occ:", "")
        if code in index.occ_to_jobs or code in index.core_skills:
            return only if only.startswith("occ:") else f"occ:{only}"
        return None
    codes = [c.replace("occ:", "") for c in uniq]
    parents = {c[:4] + "00" for c in codes if len(c) >= 4}
    if len(parents) == 1:
        parent_code = parents.pop()
        if parent_code in index.occ_to_jobs or parent_code in index.core_skills:
            return f"occ:{parent_code}"
    major_parents = {c[:2] + "0000" for c in codes if len(c) >= 2}
    if len(major_parents) == 1:
        major_code = major_parents.pop()
        if major_code in index.occ_to_jobs or major_code in index.core_skills:
            return f"occ:{major_code}"
    parent_job_counts: dict[str, int] = {}
    for c in codes:
        if len(c) < 4:
            continue
        p = c[:4] + "00"
        parent_job_counts[p] = parent_job_counts.get(p, 0) + len(
            index.occ_to_jobs.get(c, [])
        )
    if parent_job_counts:
        best_parent = max(parent_job_counts, key=parent_job_counts.get)
        if best_parent in index.occ_to_jobs or best_parent in index.core_skills:
            return f"occ:{best_parent}"
    best, best_count = None, -1
    for c in uniq:
        code = c.replace("occ:", "")
        count = len(index.occ_to_jobs.get(code, []))
        if count > best_count:
            best, best_count = c, count
    if best is None:
        return None
    return best if best.startswith("occ:") else f"occ:{best}"


def _traverse_always_expand_descendants(
    query: str,
    index: s8.GraphIndex,
    *,
    top_k: int = 5,
    expand_top_n: int = 5,
    expand_min_npmi: float = 0.2,
    use_skill_kind_weights: bool = False,
) -> s8.TraversalResult:
    """Stage-2 baseline: force full descendant expansion via flag."""
    return s8.traverse_and_rank(
        query,
        index,
        top_k=top_k,
        expand_top_n=expand_top_n,
        expand_min_npmi=expand_min_npmi,
        use_skill_kind_weights=use_skill_kind_weights,
        narrow_large_occ=False,
    )


def build_bm25(index_jobs_path: Path):
    import re
    import unicodedata

    def tokenize(text: str) -> set[str]:
        text = unicodedata.normalize("NFKC", text).casefold()
        return set(re.findall(r"[a-z0-9\u4e00-\u9fff\u3400-\u4dbf]+", text))

    inverted: dict[str, set[str]] = {}
    con = duckdb.connect()
    rows = con.execute(
        f"""
        SELECT job_id, title,
               concat_ws(' ', computer_skills, work_skills,
                         certifications, additional_requirements) AS requirements
        FROM read_parquet('{index_jobs_path.resolve().as_posix()}')
        """
    ).fetchall()
    for job_id, title, requirements in rows:
        jid = str(job_id)
        for tok in tokenize((title or "") + " " + (requirements or "")):
            inverted.setdefault(tok, set()).add(jid)

    def bm25_retrieve(query_text: str, top_k: int = TOP_K):
        scores: dict[str, float] = defaultdict(float)
        for tok in tokenize(query_text):
            for jid in inverted.get(tok, ()):
                scores[jid] += 1.0
        return sorted(scores.items(), key=lambda x: -x[1])[:top_k]

    return bm25_retrieve


def run_arm(
    name: str,
    index: s8.GraphIndex,
    queries,
    relevance_lookup: dict,
    bm25_retrieve: Callable,
    *,
    traverse_fn: Callable | None = None,
    narrow_large_occ: bool = True,
) -> dict:
    traverse = traverse_fn or s8.traverse_and_rank
    ranking_rows: list[dict] = []
    incremental_rows: list[dict] = []
    resolve_stats = {"skill": 0, "occ": 0, "both": 0, "none": 0}
    pool_sizes: list[int] = []
    t0 = time.time()

    for _, row in queries.iterrows():
        qid = row["query_id"]
        qtext = row["query"]
        rel_map = relevance_lookup.get(qid, {})
        if not rel_map:
            continue

        bm25_ranked = [jid for jid, _ in bm25_retrieve(qtext, top_k=TOP_K)]
        result = traverse(qtext, index, top_k=TOP_K)
        graph_ranked = [
            j["job_id"].removeprefix("job:") for j in result.top_jobs
        ]
        retrieved = s8._rrf_fuse([bm25_ranked, graph_ranked])
        res = result.resolution

        hs, ho = bool(res.resolved_skills), bool(res.resolved_occupations)
        if hs and ho:
            resolve_stats["both"] += 1
        elif hs:
            resolve_stats["skill"] += 1
        elif ho:
            resolve_stats["occ"] += 1
            pool_sizes.append(
                occ_pool_size(
                    res.resolved_occupations,
                    index,
                    narrow_large_occ=narrow_large_occ,
                )
            )
        else:
            resolve_stats["none"] += 1

        ranked = sorted(retrieved.items(), key=lambda x: (-x[1], x[0]))[:TOP_K]
        for jid, score in ranked:
            ranking_rows.append(
                {
                    "query_id": qid,
                    "job_id": jid,
                    "score": score,
                    "relevance": rel_map.get(jid, 0),
                }
            )

        positives = {jid for jid, rel in rel_map.items() if rel > 0}
        if positives:
            bm25_set = set(bm25_ranked)
            graph_only = (set(graph_ranked) - bm25_set) & positives
            incremental_rows.append(
                {
                    "positives": len(positives),
                    "bm25_found": len(bm25_set & positives),
                    "graph_only_found": len(graph_only),
                }
            )

    elapsed = time.time() - t0
    rankings_df = pd.DataFrame(ranking_rows)
    metrics, per_query_df = evaluate_rankings(rankings_df, k=10)
    hit1_rows = []
    for _, group in rankings_df.groupby("query_id"):
        ordered = group.sort_values("score", ascending=False)
        rels = ordered["relevance"].astype(float).tolist()
        hit1_rows.append(float(bool(rels) and rels[0] > 0))
    metrics["hit@1"] = sum(hit1_rows) / len(hit1_rows) if hit1_rows else 0.0

    retrieved_by_q: dict[str, set[str]] = defaultdict(set)
    for r in ranking_rows:
        retrieved_by_q[r["query_id"]].add(r["job_id"])
    recalls = []
    for qid, retrieved in retrieved_by_q.items():
        pos = {j for j, rel in relevance_lookup.get(qid, {}).items() if rel > 0}
        if pos:
            recalls.append(len(pos & retrieved) / len(pos))
    metrics[f"recall@{TOP_K}"] = sum(recalls) / len(recalls) if recalls else 0.0

    total_pos = sum(r["positives"] for r in incremental_rows)
    graph_only = sum(r["graph_only_found"] for r in incremental_rows)
    n_q = sum(resolve_stats.values())
    pool_sizes_sorted = sorted(pool_sizes)
    pool_report = {
        "occ_queries": len(pool_sizes),
        "pool_p50": (
            statistics.median(pool_sizes_sorted) if pool_sizes_sorted else 0
        ),
        "pool_p90": (
            pool_sizes_sorted[int(0.9 * (len(pool_sizes_sorted) - 1))]
            if pool_sizes_sorted
            else 0
        ),
        "pool_mean": (
            sum(pool_sizes) / len(pool_sizes) if pool_sizes else 0
        ),
        "huge_pool_share": (
            sum(1 for p in pool_sizes if p >= HUGE_POOL) / len(pool_sizes)
            if pool_sizes
            else 0
        ),
    }
    return {
        "arm": name,
        "metrics": {k: float(v) for k, v in metrics.items()},
        "resolve": {
            **resolve_stats,
            "any_anchor_rate": (n_q - resolve_stats["none"]) / n_q if n_q else 0,
        },
        "occ_pool": pool_report,
        "graph_incremental": {
            "positives_found_only_by_graph": graph_only,
            "positives_total": total_pos,
            "queries_helped": sum(
                1 for r in incremental_rows if r["graph_only_found"] > 0
            ),
            "share": graph_only / total_pos if total_pos else 0,
        },
        "eval_time_seconds": round(elapsed, 1),
        "queries_scored": len(retrieved_by_q),
        "with_hits": int((per_query_df.get("hit@10", pd.Series([0])) > 0).sum())
        if not per_query_df.empty
        else 0,
    }


def apply_baseline_patches(stage: int):
    """Monkeypatch module to disable *only this stage* for the baseline arm.

    Prior stages stay enabled so each A/B isolates the newest change.
    """
    patches = {}
    if stage == 1:
        patches["_pick_occupation_from_candidates"] = (
            s8._pick_occupation_from_candidates,
            _pick_occupation_parent_collapse,
        )
    # stage 2 baseline is handled via traverse_fn / narrow_large_occ=False
    if stage == 3:
        patches["_INTENT_DENYLIST"] = (
            getattr(s8, "_INTENT_DENYLIST", frozenset()),
            frozenset(),
        )
    if stage == 4:
        patches["_SHORT_OCC_ALIAS"] = (
            getattr(s8, "_SHORT_OCC_ALIAS", {}),
            {},
        )
    applied = {}
    for name, (original, baseline) in patches.items():
        applied[name] = original
        setattr(s8, name, baseline)
    return applied


def restore_patches(applied: dict):
    for name, original in applied.items():
        setattr(s8, name, original)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", type=int, required=True, choices=[1, 2, 3, 4])
    parser.add_argument("--limit", type=int, default=2000)
    args = parser.parse_args()

    print(f"Loading fixed sample + graph (stage {args.stage})...")
    queries, labels = load_fixed_queries(args.limit)
    relevance_lookup: dict[str, dict[str, int]] = {}
    for _, row in labels.iterrows():
        relevance_lookup.setdefault(row["query_id"], {})[str(row["job_id"])] = int(
            row["relevance"]
        )

    index = s8.GraphIndex()
    index.load()
    bm25_retrieve = build_bm25(s8.GRAPH_DIR / "train_jobs.parquet")

    print(f"\n── Baseline (stage {args.stage} off) ──")
    applied = apply_baseline_patches(args.stage)
    try:
        if args.stage == 2:
            baseline = run_arm(
                "baseline",
                index,
                queries,
                relevance_lookup,
                bm25_retrieve,
                traverse_fn=_traverse_always_expand_descendants,
                narrow_large_occ=False,
            )
        else:
            baseline = run_arm(
                "baseline",
                index,
                queries,
                relevance_lookup,
                bm25_retrieve,
                narrow_large_occ=(args.stage != 2),
            )
    finally:
        restore_patches(applied)
    print(json.dumps(baseline, ensure_ascii=False, indent=2))

    print(f"\n── Treatment (stage {args.stage} on) ──")
    treatment = run_arm(
        "treatment",
        index,
        queries,
        relevance_lookup,
        bm25_retrieve,
        narrow_large_occ=True,
    )
    print(json.dumps(treatment, ensure_ascii=False, indent=2))

    delta_metrics = {
        k: treatment["metrics"].get(k, 0) - baseline["metrics"].get(k, 0)
        for k in ("ndcg@10", "mrr", "hit@10", "hit@1", "recall@50")
    }
    summary = {
        "stage": args.stage,
        "sample": f"test ORDER BY query_id LIMIT {args.limit}",
        "baseline": baseline,
        "treatment": treatment,
        "delta_treatment_minus_baseline": delta_metrics,
        "delta_pool_p50": (
            treatment["occ_pool"]["pool_p50"] - baseline["occ_pool"]["pool_p50"]
        ),
        "delta_huge_pool_share": (
            treatment["occ_pool"]["huge_pool_share"]
            - baseline["occ_pool"]["huge_pool_share"]
        ),
        "delta_any_anchor": (
            treatment["resolve"]["any_anchor_rate"]
            - baseline["resolve"]["any_anchor_rate"]
        ),
    }
    out = OUT / f"ab_anchor_stage{args.stage}.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote {out}")
    print("Delta (treatment - baseline):")
    for k, v in delta_metrics.items():
        print(f"  {k}: {v:+.4f}")
    print(f"  any_anchor: {baseline['resolve']['any_anchor_rate']:.1%} → "
          f"{treatment['resolve']['any_anchor_rate']:.1%}")
    print(f"  occ pool p50: {baseline['occ_pool']['pool_p50']:.0f} → "
          f"{treatment['occ_pool']['pool_p50']:.0f}")
    print(f"  huge_pool_share: {baseline['occ_pool']['huge_pool_share']:.1%} → "
          f"{treatment['occ_pool']['huge_pool_share']:.1%}")
    print(
        f"  graph-only positives: "
        f"{baseline['graph_incremental']['positives_found_only_by_graph']} → "
        f"{treatment['graph_incremental']['positives_found_only_by_graph']}"
    )


if __name__ == "__main__":
    main()
