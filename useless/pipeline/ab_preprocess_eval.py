"""
A/B full-retrieval eval on a fixed 2000-query test sample.

A = legacy resolve (whitespace split only, no reverse-suffix / job-suffix peel)
B = current preprocess + resolve
Both share the same BM25 index and query_id ORDER BY sample.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path

import sys

import duckdb
import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parent.parent  # pipeline/ -> repo root
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import step8_retrieval_smoke as s8  # noqa: E402  (pipeline/ sibling)
from job_skill_graph.metrics import evaluate_rankings  # noqa: E402

# 腳本住 pipeline/，產物仍寫回 graph_track_a_compare/（已進 git 的結果目錄）
OUT = _REPO_ROOT / "graph_track_a_compare"
TOP_K = 50


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


def legacy_resolve(query: str, index: s8.GraphIndex) -> s8.QueryResolution:
    """Pre-preprocess resolver: whitespace tokens + old occupation path only."""
    result = s8.QueryResolution(raw_query=query)
    normalized = s8.normalize_query_token(query)
    if not normalized:
        return result

    skill_id = s8._resolve_skill(normalized, index)
    if skill_id:
        result.resolved_skills.append(skill_id)
        return result
    # Occupation without reverse-suffix: temporarily empty ending matches
    # by using exact/prefix/ambiguous only via a shim.
    occ_id = _legacy_resolve_occupation(normalized, index)
    if occ_id:
        result.resolved_occupations.append(occ_id)
        return result

    pending = []
    for token in query.strip().split():
        norm_tok = s8.normalize_query_token(token)
        skill = s8._resolve_skill(norm_tok, index)
        if skill:
            if skill not in result.resolved_skills:
                result.resolved_skills.append(skill)
            continue
        occ = _legacy_resolve_occupation(norm_tok, index)
        if occ:
            if occ not in result.resolved_occupations:
                result.resolved_occupations.append(occ)
            continue
        pending.append(norm_tok)

    for token in pending:
        skill = s8._resolve_skill(token, index, allow_substring=True)
        if skill:
            if skill not in result.resolved_skills:
                result.resolved_skills.append(skill)
            continue
        occ = _legacy_resolve_occupation(token, index, allow_substring=True)
        if occ:
            if occ not in result.resolved_occupations:
                result.resolved_occupations.append(occ)
            continue
        result.unresolved_terms.append(token)

    if not result.resolved_skills and not result.resolved_occupations:
        skill_id = s8._resolve_skill(normalized, index, allow_substring=True)
        if skill_id:
            result.resolved_skills.append(skill_id)
            return result
        occ_id = _legacy_resolve_occupation(
            normalized, index, allow_substring=True
        )
        if occ_id:
            result.resolved_occupations.append(occ_id)
    return result


def _legacy_resolve_occupation(
    normalized: str, index: s8.GraphIndex, *, allow_substring: bool = False
) -> str | None:
    if normalized in index.occ_alias:
        canonical = index.occ_alias[normalized]
        code = canonical.replace("occ:", "")
        if code in index.occ_to_jobs or code in index.core_skills:
            return canonical
    if normalized in index.occ_alias_ambiguous:
        picked = s8._pick_occupation_from_candidates(
            index.occ_alias_ambiguous[normalized], index
        )
        if picked:
            return picked
    if len(normalized) >= 2:
        prefix_matches = []
        for alias_key in index.occ_alias:
            if alias_key.startswith(normalized):
                prefix_matches.append(index.occ_alias[alias_key])
        for alias_key in index.occ_alias_ambiguous:
            if alias_key.startswith(normalized):
                prefix_matches.extend(index.occ_alias_ambiguous[alias_key])
        if prefix_matches:
            picked = s8._pick_occupation_from_candidates(prefix_matches, index)
            if picked:
                return picked
    if not allow_substring:
        return None
    for term, occ_id in getattr(index, "occ_substrings", ()):
        if len(term) > len(normalized):
            continue
        if s8._substring_hit(term, normalized):
            return occ_id
    return None


def run_arm(name: str, resolve_fn, index, queries, relevance_lookup, bm25_retrieve):
    ranking_rows = []
    incremental_rows = []
    resolve_stats = {"skill": 0, "occ": 0, "both": 0, "none": 0}
    t0 = time.time()

    for _, row in queries.iterrows():
        qid = row["query_id"]
        qtext = row["query"]
        rel_map = relevance_lookup.get(qid, {})
        if not rel_map:
            continue

        # Patch resolve for this arm
        original = s8.resolve_query
        s8.resolve_query = resolve_fn
        try:
            bm25_ranked = [jid for jid, _ in bm25_retrieve(qtext, top_k=TOP_K)]
            result = s8.traverse_and_rank(qtext, index, top_k=TOP_K)
            graph_ranked = [
                j["job_id"].removeprefix("job:") for j in result.top_jobs
            ]
            retrieved = s8._rrf_fuse([bm25_ranked, graph_ranked])
            res = result.resolution
        finally:
            s8.resolve_query = original

        hs, ho = bool(res.resolved_skills), bool(res.resolved_occupations)
        if hs and ho:
            resolve_stats["both"] += 1
        elif hs:
            resolve_stats["skill"] += 1
        elif ho:
            resolve_stats["occ"] += 1
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
    report = {
        "arm": name,
        "metrics": {k: float(v) for k, v in metrics.items()},
        "resolve": {
            **resolve_stats,
            "any_anchor_rate": (n_q - resolve_stats["none"]) / n_q if n_q else 0,
        },
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
    return report


def main() -> None:
    print("Loading fixed query sample + graph + BM25...")
    queries, labels = load_fixed_queries(2000)
    relevance_lookup: dict[str, dict[str, int]] = {}
    for _, row in labels.iterrows():
        relevance_lookup.setdefault(row["query_id"], {})[str(row["job_id"])] = int(
            row["relevance"]
        )

    index = s8.GraphIndex()
    index.load()

    # Build BM25 once (copy of step8 harness)
    import re
    import unicodedata

    def tokenize(text: str) -> set[str]:
        text = unicodedata.normalize("NFKC", text).casefold()
        return set(re.findall(r"[a-z0-9\u4e00-\u9fff\u3400-\u4dbf]+", text))

    train_jobs_path = s8.GRAPH_DIR / "train_jobs.parquet"
    inverted: dict[str, set[str]] = {}
    con = duckdb.connect()
    rows = con.execute(
        f"""
        SELECT job_id, title,
               concat_ws(' ', computer_skills, work_skills,
                         certifications, additional_requirements) AS requirements
        FROM read_parquet('{train_jobs_path.resolve().as_posix()}')
        """
    ).fetchall()
    for job_id, title, requirements in rows:
        jid = str(job_id)
        for tok in tokenize((title or "") + " " + (requirements or "")):
            inverted.setdefault(tok, set()).add(jid)

    def bm25_retrieve(query_text: str, top_k: int = TOP_K):
        q_tokens = tokenize(query_text)
        scores: dict[str, float] = defaultdict(float)
        for tok in q_tokens:
            for jid in inverted.get(tok, ()):
                scores[jid] += 1.0
        return sorted(scores.items(), key=lambda x: -x[1])[:top_k]

    print(f"Queries: {len(queries):,}  Labels rows: {len(labels):,}")
    print("\n── Arm A: legacy resolve ──")
    a = run_arm(
        "legacy", legacy_resolve, index, queries, relevance_lookup, bm25_retrieve
    )
    print(json.dumps(a, ensure_ascii=False, indent=2))

    print("\n── Arm B: preprocess resolve ──")
    b = run_arm(
        "preprocess", s8.resolve_query, index, queries, relevance_lookup, bm25_retrieve
    )
    print(json.dumps(b, ensure_ascii=False, indent=2))

    delta = {
        k: b["metrics"].get(k, 0) - a["metrics"].get(k, 0)
        for k in ("ndcg@10", "mrr", "hit@10", "hit@1", "recall@50")
    }
    summary = {
        "sample": "test ORDER BY query_id LIMIT 2000",
        "legacy": a,
        "preprocess": b,
        "delta_preprocess_minus_legacy": delta,
        "resolve_any_anchor": {
            "legacy": a["resolve"]["any_anchor_rate"],
            "preprocess": b["resolve"]["any_anchor_rate"],
        },
    }
    out = OUT / "ab_preprocess_eval.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote {out}")
    print("\nDelta (preprocess - legacy):")
    for k, v in delta.items():
        print(f"  {k}: {v:+.4f}")
    print(
        f"  any_anchor: {a['resolve']['any_anchor_rate']:.1%} → "
        f"{b['resolve']['any_anchor_rate']:.1%}"
    )
    print(
        f"  graph-only positives: "
        f"{a['graph_incremental']['positives_found_only_by_graph']} → "
        f"{b['graph_incremental']['positives_found_only_by_graph']}"
    )


if __name__ == "__main__":
    main()
