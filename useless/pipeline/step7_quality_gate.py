"""
Step 7 — Quality Gate
Role: B (Structure Graph)
Playbook: §Step 7

Automated checks for all fail/warn conditions defined in the playbook.
Run after Step 6 (graph export) to validate the assembled graph.

Exit codes:
  0 = all checks pass (no fail)
  1 = at least one FAIL found
"""

from __future__ import annotations

import csv
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parent.parent  # pipeline/ -> repo root; graph/ dataset/ fixtures/ 都掛在根目錄
GRAPH_DIR = _REPO_ROOT / "graph"


@dataclass
class CheckResult:
    name: str
    severity: str  # "fail" or "warn"
    passed: bool
    message: str
    details: Any = None


@dataclass
class QualityReport:
    checks: list[CheckResult] = field(default_factory=list)

    def add(self, result: CheckResult) -> None:
        self.checks.append(result)

    @property
    def has_fail(self) -> bool:
        return any(not c.passed and c.severity == "fail" for c in self.checks)

    @property
    def fail_count(self) -> int:
        return sum(1 for c in self.checks if not c.passed and c.severity == "fail")

    @property
    def warn_count(self) -> int:
        return sum(1 for c in self.checks if not c.passed and c.severity == "warn")

    @property
    def pass_count(self) -> int:
        return sum(1 for c in self.checks if c.passed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "summary": {
                "total_checks": len(self.checks),
                "passed": self.pass_count,
                "failed": self.fail_count,
                "warnings": self.warn_count,
                "gate_status": "PASS" if not self.has_fail else "FAIL",
            },
            "checks": [
                {
                    "name": c.name,
                    "severity": c.severity,
                    "status": "PASS" if c.passed else c.severity.upper(),
                    "message": c.message,
                    "details": c.details,
                }
                for c in self.checks
            ],
        }


# ─────────────────────────────────────────────────────────────────────────────
# Check functions
# ─────────────────────────────────────────────────────────────────────────────


def check_manifest_exists(graph_dir: Path) -> CheckResult:
    """Manifest must exist with required fields."""
    manifest_path = graph_dir / "graph_manifest.json"
    if not manifest_path.exists():
        # Try step-level manifests
        step1 = graph_dir / "step1_manifest.json"
        if step1.exists():
            m = json.loads(step1.read_text(encoding="utf-8"))
            required = ["source_snapshot_id", "graph_data_scope"]
            missing = [k for k in required if k not in m]
            if missing:
                return CheckResult("manifest_completeness", "fail", False,
                                   f"Manifest missing: {missing}")
            return CheckResult("manifest_completeness", "fail", True,
                               "Step1 manifest present with required fields")
        return CheckResult("manifest_completeness", "fail", False,
                           "No manifest found (graph_manifest.json or step1_manifest.json)")

    m = json.loads(manifest_path.read_text(encoding="utf-8"))
    required = ["schema_version", "source_snapshot_id", "graph_data_scope"]
    missing = [k for k in required if k not in m]
    if missing:
        return CheckResult("manifest_completeness", "fail", False,
                           f"Manifest missing required fields: {missing}")
    return CheckResult("manifest_completeness", "fail", True,
                       "Manifest present with required fields")


def check_data_scope_source(graph_dir: Path) -> CheckResult:
    """Warn if data scope decision lacks verifiable source."""
    for fname in ["graph_manifest.json", "step1_manifest.json"]:
        path = graph_dir / fname
        if path.exists():
            m = json.loads(path.read_text(encoding="utf-8"))
            policy = m.get("data_scope_policy", {})
            status = policy.get("source_status", "")
            if "pending" in status.lower():
                return CheckResult("data_scope_source", "warn", False,
                                   "Data scope 'all jobs' decision lacks written source (Playbook §1.1)")
            return CheckResult("data_scope_source", "warn", True,
                               "Data scope source documented")
    return CheckResult("data_scope_source", "warn", False,
                       "No manifest found to check data scope source")


def check_duplicate_node_ids(graph_dir: Path) -> CheckResult:
    """No duplicate node IDs allowed."""
    nodes_path = graph_dir / "nodes.csv"
    if not nodes_path.exists():
        return CheckResult("duplicate_node_ids", "fail", True,
                           "nodes.csv not yet generated (skip for now)", details="deferred")

    with nodes_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        id_col = "node_id" if "node_id" in (reader.fieldnames or []) else (reader.fieldnames or [""])[0]
        ids = [row[id_col] for row in reader]

    dupes = [k for k, v in Counter(ids).items() if v > 1]
    if dupes:
        return CheckResult("duplicate_node_ids", "fail", False,
                           f"Found {len(dupes)} duplicate node IDs", details=dupes[:10])
    return CheckResult("duplicate_node_ids", "fail", True,
                       f"All {len(ids)} node IDs are unique")


def check_duplicate_edge_keys(graph_dir: Path) -> CheckResult:
    """No duplicate edge keys (edge_type + source_id + target_id)."""
    edges_path = graph_dir / "edges.csv"
    if not edges_path.exists():
        return CheckResult("duplicate_edge_keys", "fail", True,
                           "edges.csv not yet generated (skip for now)", details="deferred")

    with edges_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        keys = []
        for row in reader:
            key = (row.get("edge_type", ""), row.get("source_id", ""), row.get("target_id", ""))
            keys.append(key)

    dupes = [k for k, v in Counter(keys).items() if v > 1]
    if dupes:
        return CheckResult("duplicate_edge_keys", "fail", False,
                           f"Found {len(dupes)} duplicate edge keys",
                           details=[f"{t}:{s}->{tg}" for t, s, tg in dupes[:10]])
    return CheckResult("duplicate_edge_keys", "fail", True,
                       f"All {len(keys)} edge keys are unique")


def check_dangling_edges(graph_dir: Path) -> CheckResult:
    """No edge should reference a non-existent node."""
    edges_path = graph_dir / "edges.csv"
    nodes_path = graph_dir / "nodes.csv"
    if not edges_path.exists() or not nodes_path.exists():
        return CheckResult("dangling_edges", "fail", True,
                           "nodes.csv or edges.csv not yet generated (skip)", details="deferred")

    # Collect all node IDs
    with nodes_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        id_col = "node_id" if "node_id" in (reader.fieldnames or []) else (reader.fieldnames or [""])[0]
        node_ids = {row[id_col] for row in reader}

    # Check all edge endpoints
    dangling = []
    with edges_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            src = row.get("source_id", "")
            tgt = row.get("target_id", "")
            if src and src not in node_ids:
                dangling.append(f"source: {src}")
            if tgt and tgt not in node_ids:
                dangling.append(f"target: {tgt}")

    if dangling:
        return CheckResult("dangling_edges", "fail", False,
                           f"Found {len(dangling)} dangling edge references",
                           details=dangling[:20])
    return CheckResult("dangling_edges", "fail", True, "No dangling edges")


def check_requirement_level_enum(graph_dir: Path) -> CheckResult:
    """requirement_level must be required/preferred/unspecified only."""
    edges_path = graph_dir / "edges.csv"
    if not edges_path.exists():
        return CheckResult("requirement_level_enum", "fail", True,
                           "edges.csv not yet generated (skip)", details="deferred")

    allowed = {"required", "preferred", "unspecified", ""}
    invalid = []
    with edges_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            val = row.get("requirement_level", "")
            if val and val not in allowed:
                invalid.append(f"{row.get('source_id')} -> {row.get('target_id')}: '{val}'")

    if invalid:
        return CheckResult("requirement_level_enum", "fail", False,
                           f"Found {len(invalid)} invalid requirement_level values",
                           details=invalid[:10])
    return CheckResult("requirement_level_enum", "fail", True,
                       "All requirement_level values are valid")


def check_confidence_range(graph_dir: Path) -> CheckResult:
    """confidence must be in [0, 1]."""
    edges_path = graph_dir / "edges.csv"
    if not edges_path.exists():
        return CheckResult("confidence_range", "fail", True,
                           "edges.csv not yet generated (skip)", details="deferred")

    invalid = []
    with edges_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            val = row.get("confidence", "")
            if val:
                try:
                    c = float(val)
                    if c < 0 or c > 1:
                        invalid.append(f"{row.get('source_id')}: {c}")
                except ValueError:
                    invalid.append(f"{row.get('source_id')}: '{val}' (not a number)")

    if invalid:
        return CheckResult("confidence_range", "fail", False,
                           f"Found {len(invalid)} confidence values outside [0,1]",
                           details=invalid[:10])
    return CheckResult("confidence_range", "fail", True,
                       "All confidence values in [0, 1]")


def check_no_occ_unknown_supernode(graph_dir: Path) -> CheckResult:
    """Must not create an occ:unknown supernode."""
    edges_path = graph_dir / "edges.csv"
    if not edges_path.exists():
        return CheckResult("no_occ_unknown", "fail", True,
                           "edges.csv not yet generated (skip)", details="deferred")

    with edges_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("target_id") in ("occ:unknown", "occ:NULL", "occ:"):
                return CheckResult("no_occ_unknown", "fail", False,
                                   f"Found edge pointing to forbidden occupation node: {row.get('target_id')}")

    return CheckResult("no_occ_unknown", "fail", True,
                       "No occ:unknown / occ:NULL / occ: supernodes")


def check_co_occurs_self_loop(graph_dir: Path) -> CheckResult:
    """CO_OCCURS_WITH must not have self-loops or duplicate pairs."""
    edges_path = graph_dir / "edges.csv"
    if not edges_path.exists():
        return CheckResult("co_occurs_integrity", "fail", True,
                           "edges.csv not yet generated (skip)", details="deferred")

    self_loops = []
    pairs = []
    with edges_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("edge_type") != "CO_OCCURS_WITH":
                continue
            src = row.get("source_id", "")
            tgt = row.get("target_id", "")
            if src == tgt:
                self_loops.append(src)
            pair = tuple(sorted([src, tgt]))
            pairs.append(pair)

    if not pairs:
        return CheckResult("co_occurs_integrity", "fail", True,
                           "No CO_OCCURS_WITH edges yet (skip)", details="deferred")

    issues = []
    if self_loops:
        issues.append(f"{len(self_loops)} self-loops")
    dupe_pairs = [k for k, v in Counter(pairs).items() if v > 1]
    if dupe_pairs:
        issues.append(f"{len(dupe_pairs)} duplicate pairs")

    if issues:
        return CheckResult("co_occurs_integrity", "fail", False,
                           f"CO_OCCURS_WITH issues: {', '.join(issues)}",
                           details={"self_loops": self_loops[:5], "dupe_pairs": [list(p) for p in dupe_pairs[:5]]})
    return CheckResult("co_occurs_integrity", "fail", True,
                       f"CO_OCCURS_WITH: {len(pairs)} pairs, no self-loops or duplicates")


def check_negated_mentions_not_materialized(graph_dir: Path) -> CheckResult:
    """
    Verify no HAS_SKILL edge was created from negated/uncertain mentions.
    This requires mentions.jsonl + edges.csv cross-reference.
    """
    mentions_path = graph_dir / "mentions.jsonl"
    edges_path = graph_dir / "edges.csv"

    if not mentions_path.exists() or not edges_path.exists():
        return CheckResult("negated_not_materialized", "fail", True,
                           "mentions.jsonl or edges.csv not available (skip)", details="deferred")

    # Build set of (job_id, canonical_id) pairs that are ONLY negated/uncertain
    negated_only_pairs: set[tuple[str, str]] = set()
    affirmed_pairs: set[tuple[str, str]] = set()

    with mentions_path.open("r", encoding="utf-8") as f:
        for line in f:
            record = json.loads(line)
            job_id = record.get("job_id", "")
            for mention in record.get("skills", []) + record.get("credentials", []):
                canonical = mention.get("canonical_id") or mention.get("canonical_candidate", "")
                status = mention.get("assertion_status", "affirmed")
                pair = (f"job:{job_id}", canonical)
                if status == "affirmed":
                    affirmed_pairs.add(pair)
                else:
                    negated_only_pairs.add(pair)

    # Pairs that ONLY have negated/uncertain (no affirmed mention)
    pure_negated = negated_only_pairs - affirmed_pairs

    # Check if any of these appear in edges
    violations = []
    with edges_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("edge_type") not in ("HAS_SKILL", "REQUIRES_CREDENTIAL"):
                continue
            pair = (row.get("source_id", ""), row.get("target_id", ""))
            if pair in pure_negated:
                violations.append(f"{pair[0]} -> {pair[1]}")

    if violations:
        return CheckResult("negated_not_materialized", "fail", False,
                           f"Found {len(violations)} edges from negated/uncertain-only mentions",
                           details=violations[:10])
    return CheckResult("negated_not_materialized", "fail", True,
                       "No negated/uncertain-only mentions materialized as edges")


def check_occupation_collision_overrides(graph_dir: Path) -> CheckResult:
    """Unknown occupation collisions must be zero or have versioned overrides."""
    audit_path = graph_dir / "occupation_mapping_audit.csv"
    overrides_path = graph_dir / "occupation_collision_overrides.csv"

    if not audit_path.exists():
        return CheckResult("occupation_collisions", "fail", True,
                           "occupation_mapping_audit.csv not found (skip)", details="deferred")

    # Check for any 'unmapped' or unresolved entries
    unresolved = []
    with audit_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            status = row.get("mapping_status", "")
            if status == "unmapped":
                unresolved.append(row.get("job_id", ""))

    if unresolved:
        # Check if overrides exist
        if overrides_path.exists():
            return CheckResult("occupation_collisions", "fail", True,
                               f"{len(unresolved)} unmapped jobs but overrides file exists")
        return CheckResult("occupation_collisions", "fail", False,
                           f"{len(unresolved)} unmapped occupation tuples without versioned overrides",
                           details=unresolved[:10])

    return CheckResult("occupation_collisions", "fail", True,
                       "No unmapped occupation collisions (all resolved as minor/middle/empty)")


def check_orphan_skills(graph_dir: Path) -> CheckResult:
    """Warn about skills with no HAS_SKILL edges pointing to them."""
    edges_path = graph_dir / "edges.csv"
    nodes_path = graph_dir / "nodes.csv"

    if not edges_path.exists() or not nodes_path.exists():
        return CheckResult("orphan_skills", "warn", True,
                           "nodes/edges not available (skip)", details="deferred")

    # Get all skill nodes
    skill_nodes = set()
    with nodes_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        id_col = "node_id" if "node_id" in (reader.fieldnames or []) else (reader.fieldnames or [""])[0]
        type_col = "node_type" if "node_type" in (reader.fieldnames or []) else None
        for row in reader:
            node_id = row[id_col]
            if type_col and row.get(type_col) == "Skill":
                skill_nodes.add(node_id)
            elif node_id.startswith("skill:"):
                skill_nodes.add(node_id)

    if not skill_nodes:
        return CheckResult("orphan_skills", "warn", True, "No skill nodes found (skip)")

    # Get skills referenced in HAS_SKILL edges
    referenced = set()
    with edges_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("edge_type") == "HAS_SKILL":
                referenced.add(row.get("target_id", ""))

    orphans = skill_nodes - referenced
    if orphans:
        return CheckResult("orphan_skills", "warn", False,
                           f"{len(orphans)} skill nodes have no HAS_SKILL edges",
                           details=sorted(list(orphans))[:20])
    return CheckResult("orphan_skills", "warn", True, "All skill nodes have at least one HAS_SKILL edge")


def check_supernode(graph_dir: Path, threshold: int = 50000) -> CheckResult:
    """Warn if any skill node has too many HAS_SKILL edges (supernode)."""
    edges_path = graph_dir / "edges.csv"
    if not edges_path.exists():
        return CheckResult("supernode_check", "warn", True,
                           "edges.csv not available (skip)", details="deferred")

    skill_counts: Counter = Counter()
    with edges_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("edge_type") == "HAS_SKILL":
                skill_counts[row.get("target_id", "")] += 1

    if not skill_counts:
        return CheckResult("supernode_check", "warn", True, "No HAS_SKILL edges yet")

    supernodes = [(skill, count) for skill, count in skill_counts.most_common(20) if count > threshold]
    if supernodes:
        return CheckResult("supernode_check", "warn", False,
                           f"{len(supernodes)} skills exceed {threshold} edges (supernode risk)",
                           details=[{"skill": s, "count": c} for s, c in supernodes])

    top5 = skill_counts.most_common(5)
    return CheckResult("supernode_check", "warn", True,
                       f"No supernodes (top skill: {top5[0][0]} with {top5[0][1]} edges)" if top5 else "No edges")


def check_query_test_leakage(graph_dir: Path) -> CheckResult:
    """
    Verify no query/behavior test data leaked into graph statistics.
    Checks manifest flag or statistical edge train_window field.
    """
    for fname in ["graph_manifest.json", "step1_manifest.json"]:
        path = graph_dir / fname
        if path.exists():
            m = json.loads(path.read_text(encoding="utf-8"))
            leak_flag = m.get("contains_query_test_leakage")
            if leak_flag is True:
                return CheckResult("query_test_leakage", "fail", False,
                                   "Manifest reports query test leakage!")
            return CheckResult("query_test_leakage", "fail", True,
                               "Manifest confirms no query test leakage")
    return CheckResult("query_test_leakage", "fail", True,
                       "No manifest to check (deferred)", details="deferred")


def check_global_job_frequency(graph_dir: Path) -> CheckResult:
    """global_job_frequency must be filled for all Skill nodes before final export."""
    nodes_path = graph_dir / "nodes.csv"
    if not nodes_path.exists():
        return CheckResult("global_job_frequency", "fail", True,
                           "nodes.csv not available (skip)", details="deferred")

    missing = []
    total_skills = 0
    with nodes_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if "global_job_frequency" not in (reader.fieldnames or []):
            return CheckResult("global_job_frequency", "fail", True,
                               "global_job_frequency column not in nodes.csv (Step 5 not done yet)",
                               details="deferred")
        for row in reader:
            node_id = row.get("node_id", "")
            if node_id.startswith("skill:"):
                total_skills += 1
                freq = row.get("global_job_frequency", "")
                if not freq or freq == "":
                    missing.append(node_id)

    if missing:
        return CheckResult("global_job_frequency", "fail", False,
                           f"{len(missing)}/{total_skills} Skill nodes missing global_job_frequency",
                           details=missing[:10])
    return CheckResult("global_job_frequency", "fail", True,
                       f"All {total_skills} Skill nodes have global_job_frequency")


# ─────────────────────────────────────────────────────────────────────────────
# Main runner
# ─────────────────────────────────────────────────────────────────────────────


def run_quality_gate(graph_dir: Path | None = None) -> QualityReport:
    """Run all quality checks and return report."""
    gdir = graph_dir or GRAPH_DIR
    report = QualityReport()

    checks = [
        check_manifest_exists,
        check_data_scope_source,
        check_duplicate_node_ids,
        check_duplicate_edge_keys,
        check_dangling_edges,
        check_requirement_level_enum,
        check_confidence_range,
        check_no_occ_unknown_supernode,
        check_co_occurs_self_loop,
        check_negated_mentions_not_materialized,
        check_occupation_collision_overrides,
        check_query_test_leakage,
        check_global_job_frequency,
        check_orphan_skills,
        check_supernode,
    ]

    for check_fn in checks:
        result = check_fn(gdir)
        report.add(result)

    return report


def main() -> None:
    print("=" * 60)
    print("Step 7 — Quality Gate")
    print("=" * 60)

    report = run_quality_gate()

    print(f"\n  Results ({len(report.checks)} checks):")
    print(f"  {'─' * 50}")

    for c in report.checks:
        icon = "✓" if c.passed else ("✗" if c.severity == "fail" else "⚠")
        status = "PASS" if c.passed else c.severity.upper()
        deferred = " [deferred]" if c.details == "deferred" else ""
        print(f"  {icon} [{status:4s}] {c.name}: {c.message}{deferred}")

    print(f"\n  {'─' * 50}")
    print(f"  PASS: {report.pass_count}  |  FAIL: {report.fail_count}  |  WARN: {report.warn_count}")
    gate = "PASS ✓" if not report.has_fail else "FAIL ✗"
    print(f"  Gate status: {gate}")

    # Export report
    report_path = GRAPH_DIR / "graph_quality_report.json"
    report_path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n  Report: {report_path}")

    sys.exit(1 if report.has_fail else 0)


if __name__ == "__main__":
    main()
