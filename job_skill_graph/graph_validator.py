from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from .canonicalization import DEFAULT_PROTECTED_PAIRS, normalize_skill_text
from .models import ValidationFinding


class GraphValidationError(ValueError):
    pass


@dataclass
class GraphValidationResult:
    report: dict[str, Any]
    findings: list[ValidationFinding]
    invalid_nodes: pd.DataFrame
    invalid_edges: pd.DataFrame
    suspicious_aliases: pd.DataFrame
    unresolved_skills: pd.DataFrame


class GraphValidator:
    def __init__(self, schema_config: dict[str, Any]):
        self.config = schema_config
        self.valid_nodes = set(schema_config["node_types"])
        self.valid_edges = set(schema_config["edge_types"])
        self.policies = schema_config.get("quality", {}).get("policies", {})

    def policy(self, check: str, default: str = "warn") -> str:
        return str(self.policies.get(check, default))

    def validate(
        self,
        nodes: pd.DataFrame,
        edges: pd.DataFrame,
        *,
        source_jobs: pd.DataFrame | None = None,
        canonicalization_audit: pd.DataFrame | None = None,
    ) -> GraphValidationResult:
        findings: list[ValidationFinding] = []

        def add(
            check: str,
            message: str,
            *,
            record_id: str | None = None,
            details: dict[str, Any] | None = None,
            default: str = "warn",
        ) -> None:
            findings.append(
                ValidationFinding(
                    check=check,
                    severity=self.policy(check, default),  # type: ignore[arg-type]
                    message=message,
                    record_id=record_id,
                    details=details or {},
                )
            )

        node_ids = set(nodes["node_id"].astype(str))
        duplicate_nodes = nodes[nodes.duplicated("node_id", keep=False)]
        duplicate_edges = edges[edges.duplicated("edge_id", keep=False)]
        for node_id in sorted(set(duplicate_nodes.get("node_id", []))):
            add("duplicate_node_ids", "Duplicate node ID", record_id=str(node_id), default="fail")
        for edge_id in sorted(set(duplicate_edges.get("edge_id", []))):
            add("duplicate_edge_ids", "Duplicate edge ID", record_id=str(edge_id), default="fail")

        dangling = edges[
            ~edges["from_id"].astype(str).isin(node_ids)
            | ~edges["to_id"].astype(str).isin(node_ids)
        ]
        for row in dangling.to_dict("records"):
            add("dangling_edges", "Edge references a missing node", record_id=str(row["edge_id"]), default="quarantine")

        connected = set(edges["from_id"].astype(str)) | set(edges["to_id"].astype(str))
        orphan_skills = nodes[
            nodes["label"].eq("Skill") & ~nodes["node_id"].astype(str).isin(connected)
        ]
        for row in orphan_skills.to_dict("records"):
            add("orphan_skill_nodes", "Skill node has no edges", record_id=str(row["node_id"]))

        invalid_node_labels = nodes[~nodes["label"].isin(self.valid_nodes)]
        invalid_edge_labels = edges[~edges["label"].isin(self.valid_edges)]
        for row in invalid_node_labels.to_dict("records"):
            add("invalid_node_labels", f"Invalid node label {row['label']}", record_id=str(row["node_id"]), default="fail")
        for row in invalid_edge_labels.to_dict("records"):
            add("invalid_edge_labels", f"Invalid edge label {row['label']}", record_id=str(row["edge_id"]), default="fail")

        missing_node_property_rows: list[dict[str, Any]] = []
        for label, definition in self.config["node_types"].items():
            if not nodes["label"].eq(label).any():
                continue
            required_properties = set(definition.get("required_properties", []))
            missing_columns = required_properties - set(nodes.columns)
            for property_name in sorted(missing_columns):
                missing_node_property_rows.append(
                    {"label": label, "property": property_name}
                )
                add(
                    "missing_node_property",
                    f"{label} is missing required property column {property_name}",
                    record_id=label,
                    default="fail",
                )

        required_edge_properties = set(
            self.config.get("edge_required_properties", [])
        )
        missing_edge_properties = required_edge_properties - set(edges.columns)
        for property_name in sorted(missing_edge_properties):
            add(
                "missing_edge_property",
                f"All edge types require property column {property_name}",
                record_id=property_name,
                default="fail",
            )

        node_label_lookup = nodes.set_index("node_id")["label"].astype(str).to_dict()
        invalid_edge_endpoints: list[dict[str, Any]] = []
        for row in edges[edges["label"].isin(self.valid_edges)].to_dict("records"):
            definition = self.config["edge_types"][str(row["label"])]
            actual_from = node_label_lookup.get(str(row["from_id"]))
            actual_to = node_label_lookup.get(str(row["to_id"]))
            if actual_from is None or actual_to is None:
                continue
            if actual_from != definition["from"] or actual_to != definition["to"]:
                invalid_edge_endpoints.append(row)
                add(
                    "invalid_edge_endpoints",
                    (
                        f"{row['label']} expects {definition['from']} -> "
                        f"{definition['to']}, got {actual_from} -> {actual_to}"
                    ),
                    record_id=str(row["edge_id"]),
                    default="fail",
                )

        if "evidence" in edges:
            missing_evidence = edges[edges["evidence"].fillna("").astype(str).str.strip().eq("")]
            for row in missing_evidence.to_dict("records"):
                add("missing_evidence", "Edge evidence is empty", record_id=str(row["edge_id"]), default="quarantine")
        else:
            missing_evidence = edges.copy()

        invalid_confidence = edges[
            pd.to_numeric(edges.get("confidence"), errors="coerce").isna()
            | ~pd.to_numeric(edges.get("confidence"), errors="coerce").between(0, 1)
        ]
        for row in invalid_confidence.to_dict("records"):
            add("invalid_confidence", "Edge confidence is outside [0,1]", record_id=str(row["edge_id"]), default="fail")

        max_weight = float(self.config.get("quality", {}).get("max_reasonable_weight", 1.0))
        numeric_weight = pd.to_numeric(edges.get("weight"), errors="coerce")
        invalid_weight = edges[numeric_weight.isna() | ~numeric_weight.between(0, max_weight)]
        for row in invalid_weight.to_dict("records"):
            add("invalid_weight", f"Edge weight is outside [0,{max_weight}]", record_id=str(row["edge_id"]), default="fail")

        job_ids = (
            set(source_jobs["job_id"].astype(str))
            if source_jobs is not None and "job_id" in source_jobs
            else set(nodes.loc[nodes["label"].eq("Job"), "job_id"].astype(str))
        )
        with_source = edges[edges.get("source_job_id", "").fillna("").astype(str).str.strip().ne("")]
        unknown_source = with_source[~with_source["source_job_id"].astype(str).isin(job_ids)]
        for row in unknown_source.to_dict("records"):
            add("source_job_missing", "source_job_id does not exist", record_id=str(row["edge_id"]), default="fail")

        leaked_jobs = nodes[nodes["label"].eq("Job") & ~nodes["data_split"].astype(str).str.lower().eq("train")]
        leaked_edges = edges[edges.get("data_split", "").fillna("").astype(str).str.lower().eq("test")] if "data_split" in edges else edges.iloc[0:0]
        for row in leaked_jobs.to_dict("records"):
            add("test_leakage", "Non-train Job is present in train graph", record_id=str(row["node_id"]), default="fail")
        for row in leaked_edges.to_dict("records"):
            add("test_leakage", "Test provenance is present on edge", record_id=str(row["edge_id"]), default="fail")

        alias_edges = edges[edges["label"].eq("ALIAS_OF")]
        alias_targets = alias_edges.groupby("from_id")["to_id"].nunique()
        for alias, count in alias_targets[alias_targets > 1].items():
            add("alias_multiple_targets", f"Alias points to {count} skills", record_id=str(alias), default="fail")

        alias_graph = defaultdict(list)
        for row in alias_edges.to_dict("records"):
            alias_graph[str(row["from_id"])].append(str(row["to_id"]))
        for start in alias_graph:
            stack = [(start, {start})]
            while stack:
                current, visited = stack.pop()
                for nxt in alias_graph.get(current, []):
                    if nxt in visited:
                        add("alias_cycle", "Alias cycle detected", record_id=start, default="fail")
                        stack.clear()
                        break
                    stack.append((nxt, visited | {nxt}))

        node_name = {}
        for row in nodes.to_dict("records"):
            name = row.get("canonical_name") or row.get("alias") or row.get("name")
            if name:
                node_name[str(row["node_id"])] = str(name)
        suspicious_alias_rows = []
        for row in alias_edges.to_dict("records"):
            left = node_name.get(str(row["from_id"]), "")
            right = node_name.get(str(row["to_id"]), "")
            if frozenset((normalize_skill_text(left), normalize_skill_text(right))) in DEFAULT_PROTECTED_PAIRS:
                suspicious_alias_rows.append(row | {"alias_name": left, "skill_name": right})
                add("protected_distinction_violation", f"Protected distinction merged: {left} -> {right}", record_id=str(row["edge_id"]), default="fail")

        degree = Counter(edges["from_id"].astype(str))
        degree.update(edges["to_id"].astype(str))
        threshold = int(self.config.get("quality", {}).get("super_node_degree", 1000))
        for node_id, value in degree.items():
            if value > threshold:
                add("super_node", f"Node degree {value} exceeds {threshold}", record_id=node_id)

        co = edges[edges["label"].eq("CO_OCCURS_WITH")]
        min_count = int(self.config["cooccurrence"]["min_cooccurrence"])
        min_npmi = float(self.config["cooccurrence"]["min_npmi"])
        if not co.empty:
            invalid_co = co[
                (pd.to_numeric(co["count"], errors="coerce") < min_count)
                | (pd.to_numeric(co["npmi"], errors="coerce") < min_npmi)
            ]
            for row in invalid_co.to_dict("records"):
                add("low_support_cooccurrence", "CO_OCCURS_WITH is below configured threshold", record_id=str(row["edge_id"]), default="quarantine")
        else:
            invalid_co = co

        skill_nodes = nodes[nodes["label"].eq("Skill")]
        same_name_different_type = skill_nodes.groupby("normalized_name")["skill_type"].nunique()
        for name, count in same_name_different_type[same_name_different_type > 1].items():
            add("same_name_different_skill_type", f"{name} has {count} skill types")

        job_skill_edges = edges[edges["label"].isin(["REQUIRES", "PREFERS", "MENTIONS"])]
        conflicts = (
            job_skill_edges.groupby(["from_id", "to_id"])["label"]
            .agg(lambda values: sorted(set(values)))
        )
        for (job, skill), labels in conflicts.items():
            if "REQUIRES" in labels and "PREFERS" in labels:
                add("required_preferred_conflict", f"Both REQUIRES and PREFERS: {labels}", record_id=f"{job}->{skill}")

        evidence_mismatch = []
        if source_jobs is not None:
            source_by_id = {
                str(row["job_id"]): "\n".join(
                    str(row.get(key, "") or "") for key in ("title", "requirements", "description")
                )
                for row in source_jobs.to_dict("records")
            }
            for row in job_skill_edges.to_dict("records"):
                evidence = str(row.get("evidence", "") or "")
                source = source_by_id.get(str(row.get("source_job_id", "")), "")
                if evidence and evidence not in source:
                    evidence_mismatch.append(row)
                    add("evidence_not_in_jd", "Evidence is not a source JD substring", record_id=str(row["edge_id"]), default="fail")

        low_conf_threshold = float(self.config.get("quality", {}).get("low_confidence_threshold", 0.5))
        low_conf_count = int((pd.to_numeric(edges.get("confidence"), errors="coerce") < low_conf_threshold).sum())
        audit = canonicalization_audit if canonicalization_audit is not None else pd.DataFrame()
        unresolved = audit[audit.get("selected_skill", pd.Series(dtype=object)).isna()] if not audit.empty else audit
        canonicalized = 0 if audit.empty else int(audit["selected_skill"].notna().sum())
        occupation_nodes = int(nodes["label"].eq("Occupation").sum())
        jobs_with_occupation = (
            job_skill_edges.iloc[0:0]
            if "occupation_code" not in nodes
            else nodes[nodes["label"].eq("Job") & nodes["occupation_code"].fillna("").astype(str).str.strip().ne("")]
        )
        skill_mentions = len(job_skill_edges)
        extraction_coverage = (
            job_skill_edges["from_id"].nunique() / max(1, nodes["label"].eq("Job").sum())
        )

        check_counts = Counter(f.check for f in findings)
        severity_counts = Counter(f.severity for f in findings)
        report = {
            "status": (
                "failed"
                if severity_counts["fail"]
                else ("valid_with_findings" if findings else "valid")
            ),
            "node_count": len(nodes),
            "edge_count": len(edges),
            "checks": {
                "duplicate_node_ids": len(duplicate_nodes),
                "duplicate_edge_ids": len(duplicate_edges),
                "dangling_edges": len(dangling),
                "orphan_skill_nodes": len(orphan_skills),
                "invalid_node_labels": len(invalid_node_labels),
                "invalid_edge_labels": len(invalid_edge_labels),
                "missing_node_properties": len(missing_node_property_rows),
                "missing_edge_properties": len(missing_edge_properties),
                "invalid_edge_endpoints": len(invalid_edge_endpoints),
                "missing_evidence": len(missing_evidence),
                "invalid_confidence": len(invalid_confidence),
                "invalid_weight": len(invalid_weight),
                "source_job_missing": len(unknown_source),
                "test_leakage": len(leaked_jobs) + len(leaked_edges),
                "protected_distinction_violation": len(suspicious_alias_rows),
                "low_support_cooccurrence": len(invalid_co),
                "evidence_not_in_jd": len(evidence_mismatch),
            },
            "quality_metrics": {
                "extraction_coverage": extraction_coverage,
                "low_confidence_edge_rate": low_conf_count / max(1, len(edges)),
                "canonicalization_rate": canonicalized / max(1, len(audit)),
                "unresolved_skill_rate": len(unresolved) / max(1, len(audit)),
                "occupation_coverage": len(jobs_with_occupation) / max(1, nodes["label"].eq("Job").sum()),
                "skill_edge_count": skill_mentions,
            },
            "finding_counts": dict(check_counts),
            "severity_counts": dict(severity_counts),
            "auto_deleted_records": 0,
        }
        invalid_nodes = pd.concat(
            [duplicate_nodes, invalid_node_labels, leaked_jobs], ignore_index=True
        ).drop_duplicates() if any(map(len, [duplicate_nodes, invalid_node_labels, leaked_jobs])) else nodes.iloc[0:0]
        invalid_edges = pd.concat(
            [duplicate_edges, dangling, invalid_edge_labels, pd.DataFrame(invalid_edge_endpoints), invalid_confidence, invalid_weight, unknown_source, invalid_co, pd.DataFrame(evidence_mismatch)],
            ignore_index=True,
        ).drop_duplicates() if any(map(len, [duplicate_edges, dangling, invalid_edge_labels, invalid_edge_endpoints, invalid_confidence, invalid_weight, unknown_source, invalid_co, evidence_mismatch])) else edges.iloc[0:0]
        return GraphValidationResult(
            report=report,
            findings=findings,
            invalid_nodes=invalid_nodes,
            invalid_edges=invalid_edges,
            suspicious_aliases=pd.DataFrame(suspicious_alias_rows),
            unresolved_skills=unresolved,
        )


def save_validation_result(
    result: GraphValidationResult, output_dir: str | Path, *, overwrite: bool = False
) -> list[Path]:
    target = Path(output_dir)
    if target.exists() and any(target.iterdir()) and not overwrite:
        raise FileExistsError(f"Validation output exists: {target}")
    target.mkdir(parents=True, exist_ok=True)
    json_path = target / "graph_quality_report.json"
    json_path.write_text(
        json.dumps(
            result.report
            | {"findings": [finding.model_dump(mode="json") for finding in result.findings]},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    md_path = target / "graph_quality_report.md"
    metrics = result.report["quality_metrics"]
    lines = [
        "# Graph Quality Report",
        "",
        f"- Status: `{result.report['status']}`",
        f"- Nodes: {result.report['node_count']}",
        f"- Edges: {result.report['edge_count']}",
        f"- Extraction coverage: {metrics['extraction_coverage']:.3f}",
        f"- Canonicalization rate: {metrics['canonicalization_rate']:.3f}",
        f"- Unresolved skill rate: {metrics['unresolved_skill_rate']:.3f}",
        "",
        "## Findings",
        "",
    ]
    lines.extend(
        f"- **{finding.severity}** `{finding.check}` {finding.record_id or ''}: {finding.message}"
        for finding in result.findings
    )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    frames = {
        "invalid_nodes.csv": result.invalid_nodes,
        "invalid_edges.csv": result.invalid_edges,
        "suspicious_aliases.csv": result.suspicious_aliases,
        "unresolved_skills.csv": result.unresolved_skills,
    }
    paths = [json_path, md_path]
    for name, frame in frames.items():
        path = target / name
        frame.to_csv(path, index=False)
        paths.append(path)
    return paths
