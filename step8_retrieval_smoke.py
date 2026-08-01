"""
Step 8 — Retrieval Smoke Test
Role: B (Structure Graph) + A (共同)
Playbook: §Step 8

Proves the graph is usable: from a query, traverse the graph to find jobs,
and produce an explainable traversal trace.

Smoke queries (Playbook recommended):
- node.js
- 後端工程師
- React 前端
- 護理師
- 會計 (non-IT)
- reactjs (alias)
- k8s (abbreviation)
- Python 資料分析 (multi-skill)
"""

from __future__ import annotations

import csv
import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

GRAPH_DIR = Path(__file__).parent / "graph"


# ─────────────────────────────────────────────────────────────────────────────
# Graph loader (lazy, column-indexed for fast lookup)
# ─────────────────────────────────────────────────────────────────────────────


class GraphIndex:
    """In-memory inverted index for graph traversal. Only loads edges needed."""

    def __init__(self, graph_dir: Path = GRAPH_DIR):
        self.graph_dir = graph_dir
        # skill_id → list of (job_id, requirement_level, confidence)
        self.skill_to_jobs: dict[str, list[tuple[str, str, float]]] = defaultdict(list)
        # credential_id → list of job_id
        self.credential_to_jobs: dict[str, list[str]] = defaultdict(list)
        # occ_code → list of job_id
        self.occ_to_jobs: dict[str, list[str]] = defaultdict(list)
        # skill_id → list of (other_skill, npmi)
        self.co_occurs: dict[str, list[tuple[str, float]]] = defaultdict(list)
        # occ_code → list of (skill_id, rate)
        self.core_skills: dict[str, list[tuple[str, float]]] = defaultdict(list)
        # child_occ → parent_occ
        self.occ_parent: dict[str, str] = {}
        # Job titles for display
        self.job_titles: dict[str, str] = {}
        # Alias dictionaries
        self.skill_alias: dict[str, str] = {}  # normalized → canonical skill_id
        self.occ_alias: dict[str, str] = {}    # normalized → occ:code

    def load(self) -> None:
        print("  Loading graph index...")
        t0 = time.time()
        self._load_edges()
        self._load_aliases()
        self._load_job_titles()
        print(f"  Loaded in {time.time()-t0:.1f}s")
        print(f"    Skills with HAS_SKILL: {len(self.skill_to_jobs):,}")
        print(f"    Occupations with jobs: {len(self.occ_to_jobs):,}")
        print(f"    CO_OCCURS pairs: {sum(len(v) for v in self.co_occurs.values()):,}")
        print(f"    Skill aliases: {len(self.skill_alias):,}")
        print(f"    Occupation aliases: {len(self.occ_alias):,}")

    def _load_edges(self) -> None:
        edges_path = self.graph_dir / "edges.csv"
        with edges_path.open("r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                etype = row["edge_type"]
                src = row["source_id"]
                tgt = row["target_id"]

                if etype == "HAS_SKILL":
                    conf = float(row.get("confidence") or 0)
                    req = row.get("requirement_level", "unspecified")
                    self.skill_to_jobs[tgt].append((src, req, conf))
                elif etype == "REQUIRES_CREDENTIAL":
                    self.credential_to_jobs[tgt].append(src)
                elif etype == "IN_OCCUPATION":
                    occ = tgt.replace("occ:", "")
                    self.occ_to_jobs[occ].append(src)
                elif etype == "SUBCATEGORY_OF":
                    child = src.replace("occ:", "")
                    parent = tgt.replace("occ:", "")
                    self.occ_parent[child] = parent
                elif etype == "CO_OCCURS_WITH":
                    npmi = float(row.get("npmi") or 0)
                    self.co_occurs[src].append((tgt, npmi))
                    self.co_occurs[tgt].append((src, npmi))
                elif etype == "CORE_SKILL":
                    rate = float(row.get("rate") or 0)
                    occ = src.replace("occ:", "")
                    self.core_skills[occ].append((tgt, rate))

    def _load_aliases(self) -> None:
        # Skill aliases (from Step 3)
        skill_alias_path = self.graph_dir / "alias_dictionary.csv"
        if skill_alias_path.exists():
            with skill_alias_path.open("r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if row.get("ambiguity_status") == "unique":
                        self.skill_alias[row["alias_key"]] = row["canonical_id"]

        # Occupation aliases — unique go to occ_alias; ambiguous go to occ_alias_ambiguous
        self.occ_alias_ambiguous: dict[str, list[str]] = defaultdict(list)
        occ_alias_path = self.graph_dir / "alias_dictionary_occupation.csv"
        if occ_alias_path.exists():
            with occ_alias_path.open("r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    key = row["alias_key"]
                    canonical = row["canonical_id"]
                    if row.get("ambiguity_status") == "unique":
                        self.occ_alias[key] = canonical
                    else:
                        self.occ_alias_ambiguous[key].append(canonical)

    def _load_job_titles(self) -> None:
        """Load all job titles for display."""
        import duckdb
        con = duckdb.connect(":memory:")
        try:
            rows = con.execute(f"""
                SELECT job_id, title
                FROM read_parquet('{(self.graph_dir / "train_jobs.parquet").resolve().as_posix()}')
            """).fetchall()
            for job_id, title in rows:
                self.job_titles[f"job:{job_id}"] = title or ""
        finally:
            con.close()


# ─────────────────────────────────────────────────────────────────────────────
# Query resolver
# ─────────────────────────────────────────────────────────────────────────────

import unicodedata
import re


def normalize_query_token(token: str) -> str:
    """Same normalization as alias_key: NFKC + casefold + whitespace compress."""
    text = unicodedata.normalize("NFKC", token)
    text = text.casefold()
    text = re.sub(r"\s+", " ", text).strip()
    return text


@dataclass
class QueryResolution:
    raw_query: str
    resolved_skills: list[str] = field(default_factory=list)
    resolved_occupations: list[str] = field(default_factory=list)
    unresolved_terms: list[str] = field(default_factory=list)
    resolution_log: list[str] = field(default_factory=list)


def resolve_query(query: str, index: GraphIndex) -> QueryResolution:
    """
    Resolve a query string into skill/occupation anchors.
    Strategy: try full query as skill/occ alias first, then split tokens.
    """
    result = QueryResolution(raw_query=query)
    normalized = normalize_query_token(query)

    # 1. Try full query as skill
    skill_id = _resolve_skill(normalized, index)
    if skill_id:
        result.resolved_skills.append(skill_id)
        result.resolution_log.append(f"full_query → {skill_id}")
        return result

    # 2. Try full query as occupation
    occ_id = _resolve_occupation(normalized, index)
    if occ_id:
        result.resolved_occupations.append(occ_id)
        result.resolution_log.append(f"full_query → {occ_id}")
        return result

    # 3. Split into tokens and resolve each
    tokens = query.strip().split()
    for token in tokens:
        norm_tok = normalize_query_token(token)
        skill = _resolve_skill(norm_tok, index)
        if skill:
            if skill not in result.resolved_skills:
                result.resolved_skills.append(skill)
                result.resolution_log.append(f"token '{token}' → {skill}")
            continue
        occ = _resolve_occupation(norm_tok, index)
        if occ:
            if occ not in result.resolved_occupations:
                result.resolved_occupations.append(occ)
                result.resolution_log.append(f"token '{token}' → {occ}")
            continue
        result.unresolved_terms.append(token)
        result.resolution_log.append(f"token '{token}' → unresolved")

    return result


def _resolve_skill(normalized: str, index: GraphIndex) -> str | None:
    """Try to resolve a normalized token to a skill_id."""
    # Direct registry hit
    candidate = f"skill:{normalized.replace(' ', '_')}"
    if candidate in index.skill_to_jobs:
        return candidate
    # Alias lookup
    if normalized in index.skill_alias:
        canonical = index.skill_alias[normalized]
        if canonical in index.skill_to_jobs:
            return canonical
    # Try with dots preserved (node.js)
    candidate_dot = f"skill:{normalized}"
    if candidate_dot in index.skill_to_jobs:
        return candidate_dot
    return None


def _resolve_occupation(normalized: str, index: GraphIndex) -> str | None:
    """Try to resolve a normalized token to an occupation code."""
    # Unique alias
    if normalized in index.occ_alias:
        canonical = index.occ_alias[normalized]
        occ_code = canonical.replace("occ:", "")
        if occ_code in index.occ_to_jobs or occ_code in index.core_skills:
            return canonical
    # Ambiguous alias: resolve to parent (middle) that covers all candidates
    # This is the conservative strategy per Playbook: don't silently pick one
    if normalized in index.occ_alias_ambiguous:
        candidates = index.occ_alias_ambiguous[normalized]
        # Find common parent: all candidates share at least a middle-level parent
        codes = [c.replace("occ:", "") for c in candidates]
        # Try middle parent (first 4 digits + "00")
        parents = set(c[:4] + "00" for c in codes)
        if len(parents) == 1:
            parent_code = parents.pop()
            if parent_code in index.occ_to_jobs or parent_code in index.core_skills:
                return f"occ:{parent_code}"
        # Try major parent (first 2 digits + "0000")
        major_parents = set(c[:2] + "0000" for c in codes)
        if len(major_parents) == 1:
            major_code = major_parents.pop()
            if major_code in index.occ_to_jobs or major_code in index.core_skills:
                return f"occ:{major_code}"
        # Fallback: use the candidate with most jobs
        best = None
        best_count = 0
        for c in candidates:
            code = c.replace("occ:", "")
            count = len(index.occ_to_jobs.get(code, []))
            if count > best_count:
                best = c
                best_count = count
        if best:
            return best

    # Prefix fallback: find alias keys that start with the query token
    # (e.g. "會計" matches "會計人員", "會計師" etc.)
    if len(normalized) >= 2:
        prefix_matches: list[str] = []
        for alias_key in index.occ_alias:
            if alias_key.startswith(normalized):
                prefix_matches.append(index.occ_alias[alias_key])
        for alias_key in index.occ_alias_ambiguous:
            if alias_key.startswith(normalized):
                prefix_matches.extend(index.occ_alias_ambiguous[alias_key])
        if prefix_matches:
            codes = list(set(c.replace("occ:", "") for c in prefix_matches))
            # Try common middle parent
            parents = set(c[:4] + "00" for c in codes)
            if len(parents) == 1:
                parent_code = parents.pop()
                if parent_code in index.occ_to_jobs or parent_code in index.core_skills:
                    return f"occ:{parent_code}"
            # Multiple parents: pick the middle parent with most jobs
            parent_job_counts = {}
            for c in codes:
                p = c[:4] + "00"
                parent_job_counts[p] = parent_job_counts.get(p, 0) + len(index.occ_to_jobs.get(c, []))
            if parent_job_counts:
                best_parent = max(parent_job_counts, key=parent_job_counts.get)
                if best_parent in index.occ_to_jobs or best_parent in index.core_skills:
                    return f"occ:{best_parent}"

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Traversal + ranking
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class TraversalResult:
    query: str
    resolution: QueryResolution
    exact_hits: int = 0
    expanded_hits: int = 0
    occupation_hits: int = 0
    top_jobs: list[dict[str, Any]] = field(default_factory=list)
    trace_lines: list[str] = field(default_factory=list)
    latency_ms: float = 0.0


def _get_descendants(occ_code: str, index: GraphIndex) -> list[str]:
    """Get all descendant occupation codes via reverse parent lookup."""
    # Build child → parent is already in index.occ_parent
    # We need parent → children (reverse)
    children_map: dict[str, list[str]] = defaultdict(list)
    for child, parent in index.occ_parent.items():
        children_map[parent].append(child)

    descendants = []
    stack = [occ_code]
    while stack:
        current = stack.pop()
        for child in children_map.get(current, []):
            descendants.append(child)
            stack.append(child)
    return descendants


def traverse_and_rank(
    query: str,
    index: GraphIndex,
    *,
    top_k: int = 5,
    expand_top_n: int = 5,
    expand_min_npmi: float = 0.2,
) -> TraversalResult:
    """
    Execute graph traversal for a query. Strategy:
    - 0-hop: exact skill/occ → jobs
    - 1-hop: top CO_OCCURS_WITH by NPMI → expanded jobs (lower weight)
    - Occupation: IN_OCCUPATION → jobs + CORE_SKILL for scoring
    """
    t0 = time.time()
    resolution = resolve_query(query, index)
    result = TraversalResult(query=query, resolution=resolution)

    # Score accumulator: job_id → score
    job_scores: dict[str, float] = defaultdict(float)
    job_paths: dict[str, list[str]] = defaultdict(list)

    # 0-hop: exact skill matches
    for skill_id in resolution.resolved_skills:
        jobs = index.skill_to_jobs.get(skill_id, [])
        result.exact_hits += len(jobs)
        result.trace_lines.append(
            f"→ {skill_id} <-[HAS_SKILL]- {len(jobs):,} jobs (0-hop exact)"
        )
        for job_id, req, conf in jobs:
            weight = 1.0 if req == "required" else (0.8 if req == "preferred" else 0.6)
            job_scores[job_id] += weight
            job_paths[job_id].append(f"exact:{skill_id}")

    # 1-hop: expand via CO_OCCURS_WITH (only if exact hits are sparse)
    if resolution.resolved_skills and result.exact_hits < 100:
        for skill_id in resolution.resolved_skills:
            co = index.co_occurs.get(skill_id, [])
            top_co = sorted(co, key=lambda x: -x[1])[:expand_top_n]
            for related_skill, npmi in top_co:
                if npmi < expand_min_npmi:
                    continue
                expanded_jobs = index.skill_to_jobs.get(related_skill, [])
                result.expanded_hits += len(expanded_jobs)
                result.trace_lines.append(
                    f"→ {skill_id} -[CO_OCCURS {npmi:.3f}]-> {related_skill} "
                    f"<-[HAS_SKILL]- {len(expanded_jobs):,} jobs (1-hop)"
                )
                for job_id, req, conf in expanded_jobs:
                    job_scores[job_id] += 0.3 * npmi
                    job_paths[job_id].append(f"expand:{related_skill}(npmi={npmi:.2f})")

    # Occupation path (with hierarchy descendant expansion)
    for occ_id in resolution.resolved_occupations:
        occ_code = occ_id.replace("occ:", "")

        # Collect direct jobs + all descendant occupation jobs
        all_occ_codes = [occ_code]
        # Find descendants via occ_parent (reverse lookup)
        descendants = _get_descendants(occ_code, index)
        all_occ_codes.extend(descendants)

        jobs = []
        for code in all_occ_codes:
            jobs.extend(index.occ_to_jobs.get(code, []))
        # Deduplicate
        jobs = list(set(jobs))

        result.occupation_hits += len(jobs)
        if descendants:
            result.trace_lines.append(
                f"→ {occ_id} + {len(descendants)} descendants <-[IN_OCCUPATION]- {len(jobs):,} jobs"
            )
        else:
            result.trace_lines.append(
                f"→ {occ_id} <-[IN_OCCUPATION]- {len(jobs):,} jobs"
            )
        for job_id in jobs:
            job_scores[job_id] += 0.5
            job_paths[job_id].append(f"occupation:{occ_id}")

        # Boost with CORE_SKILL
        core = index.core_skills.get(occ_code, [])
        if core:
            top_core = sorted(core, key=lambda x: -x[1])[:5]
            core_names = [f"{s}({r:.2f})" for s, r in top_core]
            result.trace_lines.append(
                f"→ {occ_id} -[CORE_SKILL]-> top: {', '.join(core_names)}"
            )

    # Rank and get top-K
    ranked = sorted(job_scores.items(), key=lambda x: -x[1])[:top_k]
    for job_id, score in ranked:
        title = index.job_titles.get(job_id, "")
        result.top_jobs.append({
            "job_id": job_id,
            "score": round(score, 3),
            "title": title[:60],
            "paths": job_paths[job_id][:3],
        })

    result.latency_ms = (time.time() - t0) * 1000
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

SMOKE_QUERIES = [
    "node.js",
    "後端工程師",
    "React 前端",
    "護理師",
    "會計",
    "reactjs",
    "k8s",
    "Python 資料分析",
]


def main() -> None:
    print("=" * 60)
    print("Step 8 — Retrieval Smoke Test")
    print("=" * 60)

    index = GraphIndex()
    index.load()

    results = []
    print("\n" + "─" * 60)

    for query in SMOKE_QUERIES:
        result = traverse_and_rank(query, index)
        results.append(result)

        print(f"\n  Query: \"{query}\"")
        print(f"  Resolution: {result.resolution.resolution_log}")
        print(f"  Hits: exact={result.exact_hits:,}, expanded={result.expanded_hits:,}, "
              f"occupation={result.occupation_hits:,}")
        print(f"  Latency: {result.latency_ms:.1f}ms")
        print(f"  Trace:")
        for line in result.trace_lines[:5]:
            print(f"    {line}")
        print(f"  Top {len(result.top_jobs)} jobs:")
        for job in result.top_jobs:
            print(f"    {job['job_id']} (score={job['score']}) \"{job['title']}\"")
            print(f"      paths: {job['paths']}")

    # Export trace report
    report = {
        "step": "step8_retrieval_smoke",
        "schema_version": "v0.1",
        "feature_flags": {
            "use_graph": True,
            "use_llm_extraction": False,
            "use_llm_relations": False,
            "use_adaptive_traversal": False,
        },
        "queries": [
            {
                "query": r.query,
                "resolution": {
                    "skills": r.resolution.resolved_skills,
                    "occupations": r.resolution.resolved_occupations,
                    "unresolved": r.resolution.unresolved_terms,
                    "log": r.resolution.resolution_log,
                },
                "hits": {
                    "exact": r.exact_hits,
                    "expanded": r.expanded_hits,
                    "occupation": r.occupation_hits,
                },
                "trace": r.trace_lines,
                "top_jobs": r.top_jobs,
                "latency_ms": round(r.latency_ms, 1),
            }
            for r in results
        ],
    }

    report_path = GRAPH_DIR / "retrieval_smoke_report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    resolved = sum(1 for r in results if r.exact_hits > 0 or r.occupation_hits > 0)
    print(f"  Queries with hits: {resolved}/{len(results)}")
    print(f"  Average latency: {sum(r.latency_ms for r in results)/len(results):.1f}ms")
    failed = [r.query for r in results if r.exact_hits == 0 and r.occupation_hits == 0]
    if failed:
        print(f"  ⚠ No hits: {failed}")
    else:
        print("  ✓ All queries returned results")
    print(f"\n  Report: {report_path}")
    print("\n✓ Step 8 complete.")


if __name__ == "__main__":
    main()
