"""
Step 4 — Edge Assembly
Role: B (Structure Graph)
Playbook: §Step 4

Reads A's extractions.jsonl (§3.4 contract) + train_jobs.parquet,
assembles edges:
- HAS_SKILL: Job → Skill (aggregated from affirmed+accepted mentions)
- REQUIRES_CREDENTIAL: Job → Credential
- IN_OCCUPATION: Job → Occupation (from Step 1 mapping)
- SUBCATEGORY_OF: Occupation hierarchy (from Step 1)

Rules (Playbook §Step 4):
1. Same (job, skill) multiple mentions → one HAS_SKILL edge
2. Only aggregate assertion_status==affirmed AND canonicalization_status==accepted
3. requirement_level aggregation: required > preferred > unspecified
4. Edge keeps evidence_refs, evidence_count, source_fields, max(confidence)
5. No ranking weights baked into edges
6. Occupation mapping from Step 1 (already in train_jobs.parquet)
"""

from __future__ import annotations

import hashlib
import json
import csv
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import duckdb


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

GRAPH_DIR = Path(__file__).parent / "graph"
TRAIN_JOBS_PARQUET = GRAPH_DIR / "train_jobs.parquet"
HIERARCHY_CSV = GRAPH_DIR / "occupation_hierarchy.csv"

# Default extraction input (A's deliverable)
EXTRACTIONS_JSONL = GRAPH_DIR / "extractions.jsonl"

# Requirement level priority (higher = stronger)
REQUIREMENT_PRIORITY = {"required": 3, "preferred": 2, "unspecified": 1}


# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class MentionAccumulator:
    """Accumulates mentions for a single (job_id, canonical_id) pair."""
    job_id: str
    canonical_id: str
    entity_type: str  # "skill" or "credential"
    mentions: list[dict] = field(default_factory=list)

    def add_mention(self, mention: dict) -> None:
        self.mentions.append(mention)

    @property
    def accepted_affirmed(self) -> list[dict]:
        """Only mentions that are affirmed AND accepted (or no canonicalization_status yet)."""
        result = []
        for m in self.mentions:
            if m.get("assertion_status") != "affirmed":
                continue
            canon_status = m.get("canonicalization_status", "accepted")
            if canon_status != "accepted":
                continue
            result.append(m)
        return result

    def to_edge(self) -> dict[str, Any] | None:
        """
        Materialize one edge from accepted+affirmed mentions.
        Returns None if no eligible mentions.
        """
        eligible = self.accepted_affirmed
        if not eligible:
            return None

        # Aggregate requirement_level: take highest priority
        req_levels = [m.get("requirement_level", "unspecified") for m in eligible]
        best_req = max(req_levels, key=lambda x: REQUIREMENT_PRIORITY.get(x, 0))

        # Confidence: max of eligible mentions
        confidences = [m.get("confidence", 0.0) for m in eligible if m.get("confidence") is not None]
        max_confidence = max(confidences) if confidences else 0.0

        # Evidence refs: collect all mention_ids
        evidence_refs = [m["mention_id"] for m in eligible if m.get("mention_id")]

        # Source fields: unique set
        source_fields = sorted(set(m.get("source_field", "") for m in eligible if m.get("source_field")))

        # Extractor versions
        versions = sorted(set(m.get("extractor_version", "") for m in eligible if m.get("extractor_version")))

        edge_type = "HAS_SKILL" if self.entity_type == "skill" else "REQUIRES_CREDENTIAL"

        return {
            "edge_type": edge_type,
            "source_id": f"job:{self.job_id}",
            "target_id": self.canonical_id,
            "requirement_level": best_req,
            "confidence": round(max_confidence, 4),
            "evidence_refs": evidence_refs,
            "evidence_count": len(evidence_refs),
            "source_fields": source_fields,
            "extractor_version": versions[0] if len(versions) == 1 else ",".join(versions),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Extraction reader
# ─────────────────────────────────────────────────────────────────────────────


def read_extractions(path: Path) -> Iterator[dict]:
    """Read extractions.jsonl, yield one dict per job."""
    with path.open("r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"Invalid JSON at line {line_num}: {e}") from e


def validate_extraction(record: dict) -> list[str]:
    """Validate a single extraction record against §3.4 contract. Returns list of errors."""
    errors = []
    if "job_id" not in record:
        errors.append("missing job_id")
        return errors

    for mention_type, key in [("skills", "skill"), ("credentials", "credential")]:
        for i, mention in enumerate(record.get(mention_type, [])):
            prefix = f"{mention_type}[{i}]"
            if "mention_id" not in mention:
                errors.append(f"{prefix}: missing mention_id")
            if "evidence" not in mention or not mention.get("evidence"):
                errors.append(f"{prefix}: missing evidence (required)")
            if mention.get("requirement_level") not in ("required", "preferred", "unspecified", None):
                errors.append(f"{prefix}: invalid requirement_level '{mention.get('requirement_level')}'")
            if mention.get("assertion_status") not in ("affirmed", "negated", "uncertain", None):
                errors.append(f"{prefix}: invalid assertion_status '{mention.get('assertion_status')}'")
            conf = mention.get("confidence")
            if conf is not None and (not isinstance(conf, (int, float)) or conf < 0 or conf > 1):
                errors.append(f"{prefix}: confidence not in [0,1]: {conf}")
            if "canonical_candidate" not in mention and "canonical_id" not in mention:
                errors.append(f"{prefix}: missing canonical_candidate or canonical_id")

    return errors


# ─────────────────────────────────────────────────────────────────────────────
# Edge assembly engine
# ─────────────────────────────────────────────────────────────────────────────


class EdgeAssembler:
    """
    Assembles graph edges from extractions.jsonl + train_jobs.parquet.

    Usage:
        assembler = EdgeAssembler(extractions_path, train_jobs_path)
        assembler.run()
        assembler.export(output_dir)
    """

    def __init__(
        self,
        extractions_path: Path = EXTRACTIONS_JSONL,
        train_jobs_path: Path = TRAIN_JOBS_PARQUET,
        hierarchy_path: Path = HIERARCHY_CSV,
    ):
        self.extractions_path = extractions_path
        self.train_jobs_path = train_jobs_path
        self.hierarchy_path = hierarchy_path

        # Edges
        self.has_skill_edges: list[dict] = []
        self.requires_credential_edges: list[dict] = []
        self.in_occupation_edges: list[dict] = []
        self.subcategory_of_edges: list[dict] = []

        # Nodes discovered
        self.skill_ids: set[str] = set()
        self.credential_ids: set[str] = set()
        self.job_ids: set[str] = set()
        self.occupation_codes: set[str] = set()

        # Audit
        self.validation_errors: list[dict] = []
        self.skipped_mentions: int = 0
        self.total_mentions: int = 0
        self.materialized_edges: int = 0

    def load_occupation_edges(self) -> None:
        """Load IN_OCCUPATION from train_jobs and SUBCATEGORY_OF from hierarchy."""
        con = duckdb.connect(":memory:")
        try:
            # IN_OCCUPATION edges
            rows = con.execute(f"""
                SELECT job_id, occupation_code, mapping_status
                FROM read_parquet('{self.train_jobs_path.resolve().as_posix()}')
                WHERE occupation_code IS NOT NULL AND occupation_code != ''
            """).fetchall()

            for job_id, occ_code, mapping_status in rows:
                self.in_occupation_edges.append({
                    "edge_type": "IN_OCCUPATION",
                    "source_id": f"job:{job_id}",
                    "target_id": f"occ:{occ_code}",
                    "mapping_status": mapping_status,
                })
                self.job_ids.add(job_id)
                self.occupation_codes.add(occ_code)

            # SUBCATEGORY_OF edges
            if self.hierarchy_path.exists():
                rows = con.execute(f"""
                    SELECT child_code, parent_code, relation
                    FROM read_csv('{self.hierarchy_path.resolve().as_posix()}', header=true)
                """).fetchall()

                for child, parent, relation in rows:
                    self.subcategory_of_edges.append({
                        "edge_type": "SUBCATEGORY_OF",
                        "source_id": f"occ:{child}",
                        "target_id": f"occ:{parent}",
                    })
                    self.occupation_codes.add(child)
                    self.occupation_codes.add(parent)
        finally:
            con.close()

    def assemble_extraction_edges(self) -> None:
        """Read extractions.jsonl and assemble HAS_SKILL + REQUIRES_CREDENTIAL edges."""
        if not self.extractions_path.exists():
            print(f"  [WARN] Extractions file not found: {self.extractions_path}")
            print(f"         Skipping HAS_SKILL / REQUIRES_CREDENTIAL assembly.")
            return

        # Accumulators: (job_id, canonical_id) → MentionAccumulator
        accumulators: dict[tuple[str, str], MentionAccumulator] = {}

        for record in read_extractions(self.extractions_path):
            # Validate
            errors = validate_extraction(record)
            if errors:
                self.validation_errors.append({
                    "job_id": record.get("job_id", "UNKNOWN"),
                    "errors": errors,
                })
                if "missing job_id" in errors:
                    continue

            job_id = str(record["job_id"])
            self.job_ids.add(job_id)

            # Process skills
            for mention in record.get("skills", []):
                self.total_mentions += 1
                canonical_id = mention.get("canonical_id") or mention.get("canonical_candidate")
                if not canonical_id:
                    self.skipped_mentions += 1
                    continue

                key = (job_id, canonical_id)
                if key not in accumulators:
                    accumulators[key] = MentionAccumulator(
                        job_id=job_id,
                        canonical_id=canonical_id,
                        entity_type="skill",
                    )
                accumulators[key].add_mention(mention)

            # Process credentials
            for mention in record.get("credentials", []):
                self.total_mentions += 1
                canonical_id = mention.get("canonical_id") or mention.get("canonical_candidate")
                if not canonical_id:
                    self.skipped_mentions += 1
                    continue

                key = (job_id, canonical_id)
                if key not in accumulators:
                    accumulators[key] = MentionAccumulator(
                        job_id=job_id,
                        canonical_id=canonical_id,
                        entity_type="credential",
                    )
                accumulators[key].add_mention(mention)

        # Materialize edges
        for acc in accumulators.values():
            edge = acc.to_edge()
            if edge is None:
                self.skipped_mentions += len(acc.mentions)
                continue

            self.materialized_edges += 1
            if edge["edge_type"] == "HAS_SKILL":
                self.has_skill_edges.append(edge)
                self.skill_ids.add(edge["target_id"])
            else:
                self.requires_credential_edges.append(edge)
                self.credential_ids.add(edge["target_id"])

    def run(self) -> None:
        """Run full assembly pipeline."""
        print("  Loading occupation edges...")
        self.load_occupation_edges()
        print(f"    IN_OCCUPATION: {len(self.in_occupation_edges)}")
        print(f"    SUBCATEGORY_OF: {len(self.subcategory_of_edges)}")

        print("  Assembling extraction edges...")
        self.assemble_extraction_edges()
        print(f"    HAS_SKILL: {len(self.has_skill_edges)}")
        print(f"    REQUIRES_CREDENTIAL: {len(self.requires_credential_edges)}")
        print(f"    Total mentions processed: {self.total_mentions}")
        print(f"    Skipped (no canonical / not affirmed+accepted): {self.skipped_mentions}")
        print(f"    Validation errors: {len(self.validation_errors)}")

    def export(self, output_dir: Path | None = None) -> dict[str, Path]:
        """Export assembled edges to CSV files."""
        out = output_dir or GRAPH_DIR
        out.mkdir(parents=True, exist_ok=True)
        paths = {}

        # edges_core.csv — Step 4 edges only (HAS_SKILL, REQUIRES_CREDENTIAL, IN_OCCUPATION, SUBCATEGORY_OF)
        edges_path = out / "edges_core.csv"
        all_edges = (
            self.has_skill_edges
            + self.requires_credential_edges
            + self.in_occupation_edges
            + self.subcategory_of_edges
        )

        fieldnames = [
            "edge_type", "source_id", "target_id",
            "requirement_level", "confidence", "evidence_count",
            "source_fields", "evidence_refs", "extractor_version",
            "mapping_status",
        ]

        with edges_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for edge in sorted(all_edges, key=lambda e: (e["edge_type"], e["source_id"], e["target_id"])):
                # Serialize lists as pipe-separated
                row = dict(edge)
                if isinstance(row.get("evidence_refs"), list):
                    row["evidence_refs"] = "|".join(row["evidence_refs"])
                if isinstance(row.get("source_fields"), list):
                    row["source_fields"] = "|".join(row["source_fields"])
                writer.writerow(row)

        paths["edges"] = edges_path

        # Validation errors audit
        if self.validation_errors:
            errors_path = out / "edge_assembly_errors.jsonl"
            with errors_path.open("w", encoding="utf-8") as f:
                for err in self.validation_errors:
                    f.write(json.dumps(err, ensure_ascii=False) + "\n")
            paths["errors"] = errors_path

        return paths

    def summary(self) -> dict[str, Any]:
        """Return assembly summary for manifest."""
        return {
            "step": "step4_edge_assembly",
            "schema_version": "v0.1",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "input_files": {
                "extractions": str(self.extractions_path),
                "train_jobs": str(self.train_jobs_path),
                "hierarchy": str(self.hierarchy_path),
            },
            "edge_counts": {
                "HAS_SKILL": len(self.has_skill_edges),
                "REQUIRES_CREDENTIAL": len(self.requires_credential_edges),
                "IN_OCCUPATION": len(self.in_occupation_edges),
                "SUBCATEGORY_OF": len(self.subcategory_of_edges),
                "total": (
                    len(self.has_skill_edges) + len(self.requires_credential_edges)
                    + len(self.in_occupation_edges) + len(self.subcategory_of_edges)
                ),
            },
            "node_counts": {
                "jobs_referenced": len(self.job_ids),
                "skills_discovered": len(self.skill_ids),
                "credentials_discovered": len(self.credential_ids),
                "occupations": len(self.occupation_codes),
            },
            "mention_stats": {
                "total_processed": self.total_mentions,
                "skipped": self.skipped_mentions,
                "materialized_edges": self.materialized_edges,
            },
            "validation_errors": len(self.validation_errors),
        }


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def main(extractions_path: Path | None = None) -> None:
    print("=" * 60)
    print("Step 4 — Edge Assembly")
    print("=" * 60)

    ext_path = extractions_path or EXTRACTIONS_JSONL
    assembler = EdgeAssembler(extractions_path=ext_path)

    print(f"\n  Extractions: {ext_path}")
    print(f"  Train jobs: {TRAIN_JOBS_PARQUET}")
    print(f"  Hierarchy: {HIERARCHY_CSV}")
    print()

    assembler.run()

    print("\n  Exporting edges...")
    paths = assembler.export()
    for name, path in paths.items():
        print(f"    {name}: {path}")

    # Write manifest
    manifest = assembler.summary()
    manifest_path = GRAPH_DIR / "step4_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"    manifest: {manifest_path}")

    print(f"\n✓ Step 4 complete. Total edges: {manifest['edge_counts']['total']}")


if __name__ == "__main__":
    import sys
    ext = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    main(ext)
