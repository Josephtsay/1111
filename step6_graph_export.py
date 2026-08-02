"""
Step 6 — Graph Assembly Export
Role: B (Structure Graph)
Playbook: §Step 6

Assembles all artifacts into final graph package:
- nodes.csv: Job, Skill, Occupation, Credential nodes
- edges.csv: merged from Step 4 (HAS_SKILL, REQUIRES_CREDENTIAL, IN_OCCUPATION, SUBCATEGORY_OF)
             + Step 5 (CO_OCCURS_WITH, CORE_SKILL)
- graph_manifest.json: complete provenance

Input:
- graph/train_jobs.parquet (Step 1)
- graph/edges.csv (Step 4)
- graph/co_occurs_edges.csv (Step 5)
- graph/core_skill_edges.csv (Step 5)
- graph/global_job_frequency.json (Step 5)
- graph/skill_dictionary.csv (Step 3)
- graph/credential_dictionary.csv (Step 3)
- graph/occupation_hierarchy.csv (Step 1)

CLI:
  python step6_graph_export.py [--use-llm-classification] [--blacklist PATH]
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb

from step3_canonicalization import sanitize_registry_key

GRAPH_DIR = Path(__file__).parent / "graph"


@dataclass(frozen=True)
class Step6Config:
    """Feature flags and paths configurable via CLI."""

    use_graph: bool = True
    use_llm_extraction: bool = False
    use_llm_classification: bool = False
    use_llm_relations: bool = False
    use_adaptive_traversal: bool = False
    blacklist_path: Path | None = None

    def feature_flags_dict(self) -> dict[str, bool]:
        return {
            "use_graph": self.use_graph,
            "use_llm_extraction": self.use_llm_extraction,
            "use_llm_classification": self.use_llm_classification,
            "use_llm_relations": self.use_llm_relations,
            "use_adaptive_traversal": self.use_adaptive_traversal,
        }


def parse_step6_args(argv: list[str] | None = None) -> Step6Config:
    parser = argparse.ArgumentParser(description="Step 6 — Graph Assembly Export")
    parser.add_argument(
        "--use-llm-classification",
        action="store_true",
        default=False,
        help="Enable LLM skill classification (uses skill_kind from dictionary + blacklist v0.3)",
    )
    parser.add_argument(
        "--no-graph",
        action="store_true",
        default=False,
        help="Disable graph (for B0 baseline comparison)",
    )
    parser.add_argument(
        "--blacklist",
        type=Path,
        default=None,
        help="Path to soft_skill_blacklist CSV (overrides auto-detection)",
    )
    args = parser.parse_args(argv)
    return Step6Config(
        use_graph=not args.no_graph,
        use_llm_classification=args.use_llm_classification,
        blacklist_path=args.blacklist,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()[:16]


def build_nodes(graph_dir: Path) -> Path:
    """
    Build nodes.csv with all 4 node types:
    - Job: from train_jobs.parquet
    - Skill: from skill_dictionary.csv + global_job_frequency.json
    - Occupation: from occupation_hierarchy.csv + train_jobs.parquet
    - Credential: from credential_dictionary.csv
    """
    nodes_path = graph_dir / "nodes.csv"
    train_jobs = graph_dir / "train_jobs.parquet"
    skill_dict = graph_dir / "skill_dictionary.csv"
    cred_dict = graph_dir / "credential_dictionary.csv"
    hierarchy = graph_dir / "occupation_hierarchy.csv"
    freq_json = graph_dir / "global_job_frequency.json"

    # Load global_job_frequency
    freq = {}
    if freq_json.exists():
        freq = json.loads(freq_json.read_text(encoding="utf-8"))

    fieldnames = [
        "node_id", "node_type", "name", "level", "parent_code",
        "skill_kind", "credential_type", "dictionary_version",
        "global_job_frequency", "content_hash", "last_modified_at",
        "source_snapshot_id",
    ]

    rows_written = 0
    type_counts: Counter = Counter()

    with nodes_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()

        # 1. Job nodes
        con = duckdb.connect(":memory:")
        try:
            jobs = con.execute(f"""
                SELECT job_id, title, content_hash, last_modified_at
                FROM read_parquet('{train_jobs.resolve().as_posix()}')
                ORDER BY job_id
            """).fetchall()
            for job_id, title, content_hash, last_modified_at in jobs:
                writer.writerow({
                    "node_id": f"job:{job_id}",
                    "node_type": "Job",
                    "name": title or "",
                    "content_hash": content_hash or "",
                    "last_modified_at": str(last_modified_at) if last_modified_at else "",
                })
                rows_written += 1
                type_counts["Job"] += 1
        finally:
            con.close()

        # 2. Skill nodes
        if skill_dict.exists():
            with skill_dict.open("r", encoding="utf-8") as sf:
                reader = csv.DictReader(sf)
                for row in reader:
                    # Normalize on read: Step 3's registry_key formerly leaked
                    # parenthetical aliases ('kubernetes(k8s'), and a dictionary
                    # produced before that fix will not match the edge endpoints
                    # emitted by Step 4 ('kubernetes_k8s') — that mismatch shows
                    # up as thousands of dangling edges in Step 7. Applying the
                    # same sanitizer here makes the export robust to either
                    # dictionary vintage instead of requiring a manual repair.
                    key = sanitize_registry_key(row.get("registry_key", ""))
                    node_id = f"skill:{key}"
                    gf = freq.get(node_id, 0)  # 0 for blacklisted skills without frequency
                    writer.writerow({
                        "node_id": node_id,
                        "node_type": "Skill",
                        "name": row.get("canonical_name", key),
                        "skill_kind": row.get("skill_kind", "technical"),
                        "dictionary_version": row.get("dictionary_version", ""),
                        "global_job_frequency": gf,
                    })
                    rows_written += 1
                    type_counts["Skill"] += 1

        # 3. Credential nodes
        if cred_dict.exists():
            with cred_dict.open("r", encoding="utf-8") as cf:
                reader = csv.DictReader(cf)
                for row in reader:
                    key = sanitize_registry_key(row.get("registry_key", ""))
                    writer.writerow({
                        "node_id": f"credential:{key}",
                        "node_type": "Credential",
                        "name": row.get("canonical_name", key),
                        "credential_type": row.get("credential_type", ""),
                        "dictionary_version": row.get("dictionary_version", ""),
                    })
                    rows_written += 1
                    type_counts["Credential"] += 1

        # 4. Occupation nodes
        if hierarchy.exists():
            # Collect all occupation codes from hierarchy + jobs
            occ_codes: dict[str, dict[str, str]] = {}
            con = duckdb.connect(":memory:")
            try:
                # From hierarchy
                hier_rows = con.execute(f"""
                    SELECT child_code, parent_code FROM read_csv(
                        '{hierarchy.resolve().as_posix()}', header=true
                    )
                """).fetchall()
                for child, parent in hier_rows:
                    child, parent = str(child), str(parent)
                    if child not in occ_codes:
                        occ_codes[child] = {"parent_code": parent}
                    if parent not in occ_codes:
                        occ_codes[parent] = {"parent_code": ""}

                # Also collect from train_jobs (some leaf codes only appear there)
                if train_jobs.exists():
                    job_occ_rows = con.execute(f"""
                        SELECT DISTINCT occupation_code
                        FROM read_parquet('{train_jobs.resolve().as_posix()}')
                        WHERE occupation_code IS NOT NULL AND occupation_code != ''
                    """).fetchall()
                    for (code,) in job_occ_rows:
                        code = str(code)
                        if code not in occ_codes:
                            # Derive parent from code pattern
                            if code[-2:] != "00":
                                parent = code[:4] + "00"
                            elif code[-4:] != "0000":
                                parent = code[:2] + "0000"
                            else:
                                parent = ""
                            occ_codes[code] = {"parent_code": parent}

                # Get names from duties table (via train_jobs or raw)
                duties_csv = Path(__file__).parent / "data" / "raw" / "職務對照表.csv"
                if duties_csv.exists():
                    duty_rows = con.execute(f"""
                        SELECT
                            TRIM("CodeNo") AS code,
                            TRIM("CodeNameA") AS name
                        FROM read_csv('{duties_csv.resolve().as_posix()}',
                                      header=true, all_varchar=true)
                    """).fetchall()
                    duty_names = {code: name for code, name in duty_rows}
                else:
                    duty_names = {}
            finally:
                con.close()

            for code, meta in sorted(occ_codes.items()):
                code_str = str(code)
                if code_str[-4:] == "0000":
                    level = "major"
                elif code_str[-2:] == "00":
                    level = "middle"
                else:
                    level = "minor"
                writer.writerow({
                    "node_id": f"occ:{code}",
                    "node_type": "Occupation",
                    "name": duty_names.get(code, ""),
                    "level": level,
                    "parent_code": meta.get("parent_code", ""),
                })
                rows_written += 1
                type_counts["Occupation"] += 1

    print(f"    Nodes written: {rows_written:,}")
    for ntype, count in sorted(type_counts.items()):
        print(f"      {ntype}: {count:,}")

    return nodes_path


def merge_edges(graph_dir: Path) -> Path:
    """
    Merge Step 4 edges + Step 5 statistical edges into final edges.csv.
    """
    step4_edges = graph_dir / "edges_core.csv"
    co_occurs = graph_dir / "co_occurs_edges.csv"
    core_skill = graph_dir / "core_skill_edges.csv"
    final_edges = graph_dir / "edges_final.csv"

    # All possible columns across edge types
    all_fieldnames = [
        "edge_type", "source_id", "target_id",
        # HAS_SKILL / REQUIRES_CREDENTIAL
        "requirement_level", "confidence", "evidence_count",
        "source_fields", "evidence_refs", "extractor_version",
        # IN_OCCUPATION
        "mapping_status",
        # CO_OCCURS_WITH
        "count", "support", "npmi", "p_b_given_a", "p_a_given_b",
        # CORE_SKILL
        "occupation_level", "aggregation_scope", "job_count",
        "skill_job_count", "rate", "required_rate",
        # Common
        "train_window",
    ]

    type_counts: Counter = Counter()
    total = 0

    with final_edges.open("w", encoding="utf-8", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=all_fieldnames, extrasaction="ignore")
        writer.writeheader()

        # Step 4 edges
        if step4_edges.exists():
            with step4_edges.open("r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    writer.writerow(row)
                    type_counts[row.get("edge_type", "")] += 1
                    total += 1

        # CO_OCCURS_WITH
        if co_occurs.exists():
            with co_occurs.open("r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    writer.writerow(row)
                    type_counts["CO_OCCURS_WITH"] += 1
                    total += 1

        # CORE_SKILL
        if core_skill.exists():
            with core_skill.open("r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    writer.writerow(row)
                    type_counts["CORE_SKILL"] += 1
                    total += 1

    print(f"    Edges written: {total:,}")
    for etype, count in sorted(type_counts.items()):
        print(f"      {etype}: {count:,}")

    # Replace edges.csv with merged version
    target = graph_dir / "edges.csv"
    if target.exists():
        target.unlink()
    final_edges.rename(target)
    return target


def build_manifest(graph_dir: Path, node_counts: dict, edge_counts: dict, config: Step6Config) -> Path:
    """Build final graph_manifest.json."""

    # Load step manifests for provenance
    step_manifests = {}
    for name in ["step1_manifest", "step5_manifest", "canonicalization_manifest"]:
        path = graph_dir / f"{name}.json"
        if path.exists():
            step_manifests[name] = json.loads(path.read_text(encoding="utf-8"))

    # Source file hashes
    source_hashes = {}
    for name, path in [
        ("train_jobs", graph_dir / "train_jobs.parquet"),
        ("edges_step4", graph_dir / "edges.csv"),
        ("skill_dictionary", graph_dir / "skill_dictionary.csv"),
        ("credential_dictionary", graph_dir / "credential_dictionary.csv"),
        ("occupation_hierarchy", graph_dir / "occupation_hierarchy.csv"),
    ]:
        if path.exists():
            source_hashes[name] = _sha256_file(path)

    step1 = step_manifests.get("step1_manifest", {})

    manifest = {
        "schema_version": "v0.1",
        "graph_data_scope": "all_jobs_no_split",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_snapshot_id": step1.get("source_snapshot_id", ""),
        "data_scope_policy": step1.get("data_scope_policy", {}),
        "node_counts": node_counts,
        "edge_counts": edge_counts,
        "total_nodes": sum(node_counts.values()),
        "total_edges": sum(edge_counts.values()),
        "feature_flags": config.feature_flags_dict(),
        "dictionary_versions": {
            "skill": "v0.1",
            "credential": "v0.1",
            "occupation_alias": "v0.1",
        },
        "statistical_config": step_manifests.get("step5_manifest", {}).get("config", {}),
        "artifact_hashes": source_hashes,
        "contains_query_test_leakage": False,
        "known_limitations": [
            "assertion_status 否定偵測為 rules_v0.1（非 LLM）；已知限制見 Step 2C challenge report",
            "LLM verifier / embedding candidate generation 尚未接入",
            "泛用軟技能黑名單尚未套用",
            "SEMANTICALLY_RELATED 預設關閉（use_llm_relations=false）",
            "全量建圖決策的書面來源待補（見 Playbook §1.1）",
        ],
    }

    manifest_path = graph_dir / "graph_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest_path


def main(argv: list[str] | None = None) -> None:
    config = parse_step6_args(argv)

    print("=" * 60)
    print("Step 6 — Graph Assembly Export")
    print("=" * 60)
    print(f"  Feature flags: {config.feature_flags_dict()}")
    if config.blacklist_path:
        print(f"  Blacklist override: {config.blacklist_path}")

    graph_dir = GRAPH_DIR
    graph_dir.mkdir(parents=True, exist_ok=True)

    # 1. Build nodes.csv
    print("\n  [1/3] Building nodes.csv...")
    build_nodes(graph_dir)

    # Count nodes by type
    node_counts: Counter = Counter()
    with (graph_dir / "nodes.csv").open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            node_counts[row.get("node_type", "")] += 1

    # 2. Merge edges
    print("\n  [2/3] Merging edges (Step 4 + Step 5)...")
    merge_edges(graph_dir)

    # Count edges by type
    edge_counts: Counter = Counter()
    with (graph_dir / "edges.csv").open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            edge_counts[row.get("edge_type", "")] += 1

    # 3. Build manifest
    print("\n  [3/3] Building graph_manifest.json...")
    manifest_path = build_manifest(graph_dir, dict(node_counts), dict(edge_counts), config)
    print(f"    Manifest: {manifest_path}")

    # Summary
    print("\n" + "=" * 60)
    print("STEP 6 SUMMARY")
    print("=" * 60)
    print(f"  Total nodes: {sum(node_counts.values()):,}")
    for ntype, count in sorted(node_counts.items()):
        print(f"    {ntype}: {count:,}")
    print(f"  Total edges: {sum(edge_counts.values()):,}")
    for etype, count in sorted(edge_counts.items()):
        print(f"    {etype}: {count:,}")
    print(f"\n  Outputs:")
    print(f"    graph/nodes.csv")
    print(f"    graph/edges.csv")
    print(f"    graph/graph_manifest.json")
    print("\n✓ Step 6 complete.")


if __name__ == "__main__":
    main()
