"""
Step 5 — Statistical Edges (CO_OCCURS_WITH / CORE_SKILL / global_job_frequency)
Role: B (Structure Graph)
Playbook: §Step 5

Input: HAS_SKILL edges (from Step 4) + IN_OCCUPATION + SUBCATEGORY_OF
Output: CO_OCCURS_WITH edges, CORE_SKILL edges, global_job_frequency per Skill

Key rules:
- Uses shared `statistical_eligible` policy (not all HAS_SKILL)
- CO_OCCURS and CORE_SKILL must use the SAME eligibility filter
- global_job_frequency uses the same denominator/numerator policy
- Credentials excluded from statistical edges
- Soft skill blacklist excluded (provided by A)
- No query/behavior data allowed
"""

from __future__ import annotations

import csv as csv_mod
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
from typing import Any

import duckdb


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

GRAPH_DIR = Path(__file__).parent / "graph"

# Default thresholds (Playbook §Step 5: "count>=5, NPMI>=0.1, top-N TBD")
DEFAULT_CO_OCCURS_MIN_COUNT = 5
DEFAULT_CO_OCCURS_MIN_NPMI = 0.1
DEFAULT_CO_OCCURS_TOP_N = 50  # per skill, controls supernode

# Statistical eligibility: Playbook §Step 5
# Only these source_fields qualify for "unspecified" requirement_level
STRUCTURED_FIELDS_ALLOWING_UNSPECIFIED = {"電腦技能資料", "工作技能"}


@dataclass
class StatisticalConfig:
    """Versioned configuration for statistical edge computation."""
    min_count: int = DEFAULT_CO_OCCURS_MIN_COUNT
    min_npmi: float = DEFAULT_CO_OCCURS_MIN_NPMI
    top_n_per_skill: int = DEFAULT_CO_OCCURS_TOP_N
    soft_skill_blacklist: set[str] = field(default_factory=set)
    config_version: str = "v0.1"

    # Method-specific confidence thresholds (from extraction_config.yaml)
    # These must match what A uses; placeholder until Gate 2 freeze
    threshold_structured: float = 0.8
    threshold_phrase: float = 0.7
    threshold_llm: float = 0.7

    def to_dict(self) -> dict[str, Any]:
        return {
            "config_version": self.config_version,
            "min_count": self.min_count,
            "min_npmi": self.min_npmi,
            "top_n_per_skill": self.top_n_per_skill,
            "soft_skill_blacklist_size": len(self.soft_skill_blacklist),
            "threshold_structured": self.threshold_structured,
            "threshold_phrase": self.threshold_phrase,
            "threshold_llm": self.threshold_llm,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Statistical eligibility filter
# ─────────────────────────────────────────────────────────────────────────────


def is_statistically_eligible(mention: dict, config: StatisticalConfig) -> bool:
    """
    Playbook §Step 5 statistical_eligible policy:
    
    - assertion_status == affirmed
    - canonicalization_status == accepted
    - confidence >= threshold_by_method[method]
    - requirement_level IN {required, preferred}
      OR (requirement_level == unspecified AND source_field IN structured fields)
    
    Excludes:
    - negated / uncertain / quarantined
    - Non-structured unspecified
    - Credentials (handled separately)
    - Soft skill blacklist
    """
    # Must be affirmed
    if mention.get("assertion_status") != "affirmed":
        return False

    # Must be accepted (default to accepted if not present yet)
    if mention.get("canonicalization_status", "accepted") != "accepted":
        return False

    # Confidence threshold by method
    method = mention.get("method", "phrase")
    confidence = mention.get("confidence", 0.0)
    if method == "structured":
        threshold = config.threshold_structured
    elif method == "llm":
        threshold = config.threshold_llm
    else:
        threshold = config.threshold_phrase

    if confidence < threshold:
        return False

    # Requirement level + source field filter
    req_level = mention.get("requirement_level", "unspecified")
    if req_level in ("required", "preferred"):
        pass  # eligible
    elif req_level == "unspecified":
        source_field = mention.get("source_field", "")
        if source_field not in STRUCTURED_FIELDS_ALLOWING_UNSPECIFIED:
            return False
    else:
        return False

    # Check canonical_id for credential exclusion
    canonical = mention.get("canonical_id") or mention.get("canonical_candidate", "")
    if canonical.startswith("credential:"):
        return False

    # Soft skill blacklist
    if canonical in config.soft_skill_blacklist:
        return False

    return True


# ─────────────────────────────────────────────────────────────────────────────
# Step 5A: Skill-Skill Co-occurrence
# ─────────────────────────────────────────────────────────────────────────────


def compute_co_occurrence(
    job_skill_sets: dict[str, set[str]],
    config: StatisticalConfig,
) -> list[dict[str, Any]]:
    """
    Compute CO_OCCURS_WITH edges from job→skill sets.

    For each job's eligible skill set, count all pairs.
    Then compute: count, support, PMI, NPMI, P(B|A), P(A|B).

    Returns edges passing min_count, min_npmi, and top_n thresholds.
    """
    total_jobs = len(job_skill_sets)
    if total_jobs == 0:
        return []

    # Skill document frequency: how many jobs have each skill
    skill_df: Counter = Counter()
    for skills in job_skill_sets.values():
        for skill in skills:
            skill_df[skill] += 1

    # Pair counts
    pair_count: Counter = Counter()
    for skills in job_skill_sets.values():
        sorted_skills = sorted(skills)
        for a, b in combinations(sorted_skills, 2):
            pair_count[(a, b)] += 1

    # Compute metrics and filter
    edges = []
    for (skill_a, skill_b), count in pair_count.items():
        if count < config.min_count:
            continue

        df_a = skill_df[skill_a]
        df_b = skill_df[skill_b]

        # Support: P(A and B)
        support = count / total_jobs

        # P(A), P(B)
        p_a = df_a / total_jobs
        p_b = df_b / total_jobs

        # PMI = log2(P(A,B) / (P(A) * P(B)))
        expected = p_a * p_b
        if expected == 0:
            continue
        pmi = math.log2(support / expected)

        # NPMI = PMI / (-log2(P(A,B)))
        if support <= 0:
            continue
        npmi = pmi / (-math.log2(support))

        if npmi < config.min_npmi:
            continue

        # Conditional probabilities
        p_b_given_a = count / df_a if df_a > 0 else 0.0
        p_a_given_b = count / df_b if df_b > 0 else 0.0

        edges.append({
            "edge_type": "CO_OCCURS_WITH",
            "source_id": skill_a,  # already canonical pair: min(a,b)
            "target_id": skill_b,
            "count": count,
            "support": round(support, 6),
            "npmi": round(npmi, 4),
            "p_b_given_a": round(p_b_given_a, 4),
            "p_a_given_b": round(p_a_given_b, 4),
            "train_window": "all_jobs",
        })

    # Apply top-N per skill (control supernodes)
    if config.top_n_per_skill:
        edges = _apply_top_n(edges, config.top_n_per_skill)

    return edges


def _apply_top_n(edges: list[dict], top_n: int) -> list[dict]:
    """Keep only top-N co-occurrences per skill (by NPMI), both directions."""
    # Build skill → edges mapping
    skill_edges: dict[str, list[dict]] = defaultdict(list)
    for e in edges:
        skill_edges[e["source_id"]].append(e)
        skill_edges[e["target_id"]].append(e)

    # For each skill, keep top-N by NPMI
    kept_pairs: set[tuple[str, str]] = set()
    for skill, skill_e in skill_edges.items():
        sorted_e = sorted(skill_e, key=lambda x: -x["npmi"])
        for e in sorted_e[:top_n]:
            pair = (e["source_id"], e["target_id"])
            kept_pairs.add(pair)

    return [e for e in edges if (e["source_id"], e["target_id"]) in kept_pairs]


# ─────────────────────────────────────────────────────────────────────────────
# Step 5B: Occupation Core Skill
# ─────────────────────────────────────────────────────────────────────────────


def compute_core_skills(
    job_skill_sets: dict[str, set[str]],
    job_occupation: dict[str, str],
    hierarchy: list[dict],
    config: StatisticalConfig,
    min_rate: float = 0.05,
) -> list[dict[str, Any]]:
    """
    Compute CORE_SKILL edges: Occupation → Skill.

    Aggregation:
    - minor (leaf): direct jobs only (aggregation_scope=direct)
    - middle/major: direct + all descendant jobs, deduplicated (aggregation_scope=descendants)

    Returns edges with rate, job_count, skill_job_count, required_rate.
    """
    # Build hierarchy tree: parent → children
    children_map: dict[str, list[str]] = defaultdict(list)
    for edge in hierarchy:
        children_map[edge["parent_code"]].append(edge["child_code"])

    # Classify occupation levels
    all_occ_codes = set()
    for edge in hierarchy:
        all_occ_codes.add(edge["child_code"])
        all_occ_codes.add(edge["parent_code"])
    # Add occupations from jobs that might not be in hierarchy
    all_occ_codes.update(job_occupation.values())

    def get_level(code: str) -> str:
        if code[-4:] == "0000":
            return "major"
        elif code[-2:] == "00":
            return "middle"
        return "minor"

    def get_all_descendants(code: str) -> set[str]:
        """Recursively get all descendant codes."""
        result = set()
        stack = [code]
        while stack:
            current = stack.pop()
            for child in children_map.get(current, []):
                result.add(child)
                stack.append(child)
        return result

    # Build occupation → set of job_ids
    occ_direct_jobs: dict[str, set[str]] = defaultdict(set)
    for job_id, occ_code in job_occupation.items():
        if job_id in job_skill_sets:  # only jobs with skills
            occ_direct_jobs[occ_code].add(job_id)

    # Compute CORE_SKILL for each occupation
    edges = []
    for occ_code in all_occ_codes:
        level = get_level(occ_code)

        if level == "minor":
            # Direct only
            job_ids = occ_direct_jobs.get(occ_code, set())
            scope = "direct"
        else:
            # Direct + all descendants, deduplicated
            descendants = get_all_descendants(occ_code)
            job_ids = set(occ_direct_jobs.get(occ_code, set()))
            for desc in descendants:
                job_ids.update(occ_direct_jobs.get(desc, set()))
            scope = "descendants"

        job_count = len(job_ids)
        if job_count == 0:
            continue

        # Count skills across these jobs
        skill_counts: Counter = Counter()
        for job_id in job_ids:
            for skill in job_skill_sets.get(job_id, set()):
                skill_counts[skill] += 1

        # Emit CORE_SKILL edges
        for skill_id, skill_job_count in skill_counts.items():
            rate = skill_job_count / job_count
            if rate < min_rate:
                continue

            edges.append({
                "edge_type": "CORE_SKILL",
                "source_id": f"occ:{occ_code}",
                "target_id": skill_id,
                "occupation_level": level,
                "aggregation_scope": scope,
                "job_count": job_count,
                "skill_job_count": skill_job_count,
                "rate": round(rate, 4),
                "required_rate": 0.0,  # TODO: compute from required-only mentions
                "train_window": "all_jobs",
            })

    return edges


# ─────────────────────────────────────────────────────────────────────────────
# Global job frequency (same policy as above)
# ─────────────────────────────────────────────────────────────────────────────


def compute_global_job_frequency(
    job_skill_sets: dict[str, set[str]],
    total_jobs_in_graph: int,
) -> dict[str, float]:
    """
    Compute global_job_frequency for each Skill node.
    
    = (# jobs with this skill in eligible set) / total_jobs_in_graph
    
    Uses the same statistical_eligible job-skill sets as CO_OCCURS and CORE_SKILL.
    """
    skill_df: Counter = Counter()
    for skills in job_skill_sets.values():
        for skill in skills:
            skill_df[skill] += 1

    return {
        skill: round(count / total_jobs_in_graph, 6)
        for skill, count in skill_df.items()
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main engine
# ─────────────────────────────────────────────────────────────────────────────


class StatisticalEdgeBuilder:
    """
    Builds Step 5 statistical edges from mentions.jsonl or extractions.jsonl.

    Usage:
        builder = StatisticalEdgeBuilder(config)
        builder.load_mentions(mentions_path)
        builder.load_occupation_data(train_jobs_path, hierarchy_path)
        builder.compute()
        builder.export(output_dir)
    """

    def __init__(self, config: StatisticalConfig | None = None):
        self.config = config or StatisticalConfig()

        # Job → set of eligible skill IDs
        self.job_skill_sets: dict[str, set[str]] = defaultdict(set)
        # Job → occupation code
        self.job_occupation: dict[str, str] = {}
        # Hierarchy edges
        self.hierarchy: list[dict] = []
        # Total jobs in graph (for global_job_frequency denominator)
        self.total_jobs_in_graph: int = 0

        # Results
        self.co_occurs_edges: list[dict] = []
        self.core_skill_edges: list[dict] = []
        self.global_freq: dict[str, float] = {}

    def load_mentions(self, path: Path) -> None:
        """
        Load extractions/mentions and apply statistical_eligible filter.
        Builds job_skill_sets with only eligible (job, skill) pairs.
        """
        if not path.exists():
            print(f"  [WARN] {path} not found. No mentions loaded.")
            return

        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                job_id = str(record.get("job_id", ""))
                if not job_id:
                    continue

                for mention in record.get("skills", []):
                    if is_statistically_eligible(mention, self.config):
                        canonical = mention.get("canonical_id") or mention.get("canonical_candidate", "")
                        if canonical and not canonical.startswith("credential:"):
                            self.job_skill_sets[job_id].add(canonical)

        print(f"    Jobs with eligible skills: {len(self.job_skill_sets)}")
        total_pairs = sum(len(s) for s in self.job_skill_sets.values())
        print(f"    Total eligible (job, skill) pairs: {total_pairs}")

    def load_occupation_data(
        self,
        train_jobs_path: Path | None = None,
        hierarchy_path: Path | None = None,
    ) -> None:
        """Load occupation mapping and hierarchy from Step 1 outputs."""
        tj_path = train_jobs_path or (GRAPH_DIR / "train_jobs.parquet")
        hier_path = hierarchy_path or (GRAPH_DIR / "occupation_hierarchy.csv")

        con = duckdb.connect(":memory:")
        try:
            if tj_path.exists():
                rows = con.execute(f"""
                    SELECT job_id, occupation_code
                    FROM read_parquet('{tj_path.resolve().as_posix()}')
                    WHERE occupation_code IS NOT NULL AND occupation_code != ''
                """).fetchall()
                for job_id, occ_code in rows:
                    self.job_occupation[job_id] = occ_code

                # Total jobs for global_job_frequency denominator
                row = con.execute(f"""
                    SELECT count(*) FROM read_parquet('{tj_path.resolve().as_posix()}')
                """).fetchone()
                self.total_jobs_in_graph = row[0] if row else 0

            if hier_path.exists():
                with hier_path.open("r", encoding="utf-8") as f:
                    reader = csv_mod.DictReader(f)
                    self.hierarchy = [
                        {"child_code": r["child_code"], "parent_code": r["parent_code"]}
                        for r in reader
                    ]
        finally:
            con.close()

        print(f"    Jobs with occupation: {len(self.job_occupation)}")
        print(f"    Hierarchy edges: {len(self.hierarchy)}")
        print(f"    Total jobs in graph: {self.total_jobs_in_graph}")

    def compute(self) -> None:
        """Run all statistical computations."""
        print("\n  [5A] Computing skill co-occurrence...")
        self.co_occurs_edges = compute_co_occurrence(
            self.job_skill_sets, self.config
        )
        print(f"    CO_OCCURS_WITH edges: {len(self.co_occurs_edges)}")

        print("\n  [5B] Computing occupation core skills...")
        self.core_skill_edges = compute_core_skills(
            self.job_skill_sets,
            self.job_occupation,
            self.hierarchy,
            self.config,
        )
        print(f"    CORE_SKILL edges: {len(self.core_skill_edges)}")

        print("\n  [5*] Computing global_job_frequency...")
        self.global_freq = compute_global_job_frequency(
            self.job_skill_sets,
            self.total_jobs_in_graph,
        )
        print(f"    Skills with frequency: {len(self.global_freq)}")

    def export(self, output_dir: Path | None = None) -> dict[str, Path]:
        """Export statistical edges and frequency data."""
        out = output_dir or GRAPH_DIR
        out.mkdir(parents=True, exist_ok=True)
        paths = {}

        # CO_OCCURS_WITH
        co_path = out / "co_occurs_edges.csv"
        with co_path.open("w", encoding="utf-8", newline="") as f:
            fieldnames = [
                "edge_type", "source_id", "target_id",
                "count", "support", "npmi",
                "p_b_given_a", "p_a_given_b", "train_window",
            ]
            writer = csv_mod.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for e in sorted(self.co_occurs_edges, key=lambda x: (-x["npmi"], x["source_id"])):
                writer.writerow(e)
        paths["co_occurs"] = co_path

        # CORE_SKILL
        core_path = out / "core_skill_edges.csv"
        with core_path.open("w", encoding="utf-8", newline="") as f:
            fieldnames = [
                "edge_type", "source_id", "target_id",
                "occupation_level", "aggregation_scope",
                "job_count", "skill_job_count", "rate", "required_rate",
                "train_window",
            ]
            writer = csv_mod.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for e in sorted(self.core_skill_edges, key=lambda x: (x["source_id"], -x["rate"])):
                writer.writerow(e)
        paths["core_skill"] = core_path

        # Global job frequency
        freq_path = out / "global_job_frequency.json"
        freq_path.write_text(
            json.dumps(self.global_freq, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        paths["global_freq"] = freq_path

        # Manifest
        manifest = {
            "step": "step5_statistical_edges",
            "schema_version": "v0.1",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "config": self.config.to_dict(),
            "statistics": {
                "jobs_with_eligible_skills": len(self.job_skill_sets),
                "total_jobs_in_graph": self.total_jobs_in_graph,
                "unique_eligible_skills": len(self.global_freq),
                "co_occurs_edges": len(self.co_occurs_edges),
                "core_skill_edges": len(self.core_skill_edges),
            },
            "policy_note": (
                "CO_OCCURS, CORE_SKILL, and global_job_frequency all use "
                "the same statistical_eligible filter (Playbook §Step 5). "
                "No query/behavior data was used."
            ),
        }
        manifest_path = out / "step5_manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        paths["manifest"] = manifest_path

        return paths


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def load_soft_skill_blacklist(
    path: Path | None = None,
) -> set[str]:
    """
    Load A's soft-skill blacklist (contract unchanged: canonical_id + status;
    only status == "rejected" is ignored).

    Default now resolves to the highest available version of
    fixtures/soft_skill_blacklist_v*.csv. Reason: the v0.1 file was a
    hand-written draft whose IDs (skill:溝通 ...) match 0 mentions in the full
    extractions — real canonical_ids come from 工作技能 / 電腦技能資料 field
    values (e.g. skill:具備溝通協調能力). v0.2+ is data-derived by
    step_a5_soft_skill_blacklist.py, so keeping v0.1 as the default would
    silently apply no blacklist at all.
    """
    if path is None:
        fixtures = Path(__file__).parent / "fixtures"
        versioned = sorted(fixtures.glob("soft_skill_blacklist_v*.csv"))
        if not versioned:
            return set()
        path = versioned[-1]
    if not path.exists():
        return set()
    ids: set[str] = set()
    with path.open("r", encoding="utf-8") as f:
        for row in csv_mod.DictReader(f):
            if row.get("status") == "rejected":
                continue
            cid = (row.get("canonical_id") or "").strip()
            if cid:
                ids.add(cid)
    return ids


def main(extractions_path: str | None = None) -> None:
    print("=" * 60)
    print("Step 5 — Statistical Edges")
    print("=" * 60)

    config = StatisticalConfig(soft_skill_blacklist=load_soft_skill_blacklist())
    builder = StatisticalEdgeBuilder(config)

    ext_path = Path(extractions_path) if extractions_path else (GRAPH_DIR / "extractions.jsonl")
    print(f"\n  Config: {config.to_dict()}")
    print(f"  Extractions: {ext_path}")

    print("\n  Loading mentions...")
    builder.load_mentions(ext_path)

    print("\n  Loading occupation data...")
    builder.load_occupation_data()

    builder.compute()

    print("\n  Exporting...")
    paths = builder.export()
    for name, path in paths.items():
        print(f"    {name}: {path}")

    print(f"\n✓ Step 5 complete.")


if __name__ == "__main__":
    import sys
    ext = sys.argv[1] if len(sys.argv) > 1 else None
    main(ext)
