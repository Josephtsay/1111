from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from .canonicalization import CanonicalizationPipeline, normalize_skill_text
from .cooccurrence import compute_cooccurrence, compute_core_skills
from .models import (
    CanonicalizationAudit,
    GraphEdge,
    GraphNode,
    JobSkillExtraction,
    Requirement,
)
from .schema import TestLeakageError, stable_json_hash


DETERMINISTIC_CREATED_AT = "1970-01-01T00:00:00Z"


def skill_id(canonical_name: str, skill_type: str) -> str:
    key = f"{normalize_skill_text(canonical_name)}|{skill_type}"
    return f"skill:{hashlib.sha256(key.encode('utf-8')).hexdigest()[:16]}"


def alias_id(alias: str) -> str:
    normalized = normalize_skill_text(alias)
    return f"alias:{hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:16]}"


def job_node_id(job_id: str) -> str:
    return f"job:{job_id}"


def occupation_id(code: str) -> str:
    return f"occupation:{code}"


def category_id(name: str) -> str:
    return f"category:{normalize_skill_text(name)}"


def deterministic_edge_id(
    from_id: str, edge_type: str, to_id: str, source_identifier: str
) -> str:
    key = f"{from_id}|{edge_type}|{to_id}|{source_identifier}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]


def source_hash(job: dict[str, Any]) -> str:
    return stable_json_hash(
        {
            "title": str(job.get("title", "") or ""),
            "requirements": str(job.get("requirements", "") or ""),
            "description": str(job.get("description", "") or ""),
        }
    )


@dataclass
class GraphArtifacts:
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    skill_dictionary: pd.DataFrame
    alias_dictionary: pd.DataFrame
    canonicalization_audit: pd.DataFrame
    job_skills: pd.DataFrame
    extraction_manifest: list[dict[str, Any]] = field(default_factory=list)
    graph_manifest: dict[str, Any] = field(default_factory=dict)

    def node_frame(self) -> pd.DataFrame:
        rows = [
            {"node_id": n.node_id, "label": n.label, **n.properties}
            for n in self.nodes
        ]
        return pd.DataFrame(rows)

    def edge_frame(self) -> pd.DataFrame:
        rows = [
            {
                "edge_id": e.edge_id,
                "from_id": e.from_id,
                "to_id": e.to_id,
                "label": e.label,
                **e.properties,
            }
            for e in self.edges
        ]
        return pd.DataFrame(rows)


@dataclass
class GraphBuilder:
    schema_config: dict[str, Any]
    canonicalizer: CanonicalizationPipeline
    extraction_model: str = "offline-mock-v1"
    source_file: str = "synthetic/train_jobs.csv"
    created_at: str = DETERMINISTIC_CREATED_AT

    @property
    def schema_version(self) -> str:
        return str(self.schema_config["schema_version"])

    def _common_edge_properties(
        self,
        *,
        weight: float,
        confidence: float,
        evidence: str,
        source_job_id: str,
        prompt_version: str,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        values = {
            "weight": float(weight),
            "confidence": float(confidence),
            "evidence": evidence,
            "source_job_id": source_job_id,
            "source_file": self.source_file,
            "extraction_model": self.extraction_model,
            "extraction_prompt_version": prompt_version,
            "schema_version": self.schema_version,
            "created_at": self.created_at,
        }
        values.update(extra or {})
        return values

    def build(
        self,
        jobs: pd.DataFrame,
        extractions: Iterable[JobSkillExtraction],
        *,
        extraction_manifest: list[dict[str, Any]] | None = None,
        train_start: str | None = None,
        train_end: str | None = None,
    ) -> GraphArtifacts:
        required_job_columns = {
            "job_id",
            "title",
            "requirements",
            "description",
            "posted_at",
            "location_code",
            "occupation_code",
            "data_split",
        }
        missing = required_job_columns - set(jobs.columns)
        if missing:
            raise ValueError(f"Canonical jobs frame is missing columns: {sorted(missing)}")
        splits = jobs["data_split"].astype(str).str.lower().str.strip()
        if (~splits.eq("train")).any():
            leaked = jobs.loc[~splits.eq("train"), "job_id"].astype(str).tolist()
            raise TestLeakageError(
                f"GraphBuilder accepts train JD only; leaked jobs: {leaked[:10]}"
            )
        if jobs["job_id"].astype(str).duplicated().any():
            raise ValueError("job_id must be unique before graph building")

        job_lookup = {
            str(row["job_id"]): row.to_dict() for _, row in jobs.iterrows()
        }
        extraction_list = list(extractions)
        unknown = sorted({e.job_id for e in extraction_list} - set(job_lookup))
        if unknown:
            raise TestLeakageError(
                f"Extractions reference jobs outside train input: {unknown[:10]}"
            )

        node_map: dict[str, GraphNode] = {}
        edge_map: dict[str, GraphEdge] = {}
        aliases: dict[tuple[str, str], dict[str, Any]] = {}
        skills: dict[tuple[str, str], dict[str, Any]] = {}
        audits: list[CanonicalizationAudit] = []
        job_skill_rows: list[dict[str, Any]] = []

        def add_node(node: GraphNode) -> None:
            existing = node_map.get(node.node_id)
            if existing and existing.model_dump() != node.model_dump():
                raise ValueError(f"Deterministic node ID collision: {node.node_id}")
            node_map[node.node_id] = node

        def add_edge(edge: GraphEdge) -> None:
            existing = edge_map.get(edge.edge_id)
            if existing and existing.model_dump() != edge.model_dump():
                raise ValueError(f"Deterministic edge ID collision: {edge.edge_id}")
            edge_map[edge.edge_id] = edge

        for job_id_value, job in sorted(job_lookup.items()):
            add_node(
                GraphNode(
                    node_id=job_node_id(job_id_value),
                    label="Job",
                    properties={
                        "job_id": job_id_value,
                        "title": str(job.get("title", "") or ""),
                        "description": str(job.get("description", "") or ""),
                        "requirements": str(job.get("requirements", "") or ""),
                        "location_code": str(job.get("location_code", "") or ""),
                        "occupation_code": str(job.get("occupation_code", "") or ""),
                        "posted_at": str(job.get("posted_at", "") or ""),
                        "data_split": "train",
                        "source_hash": source_hash(job),
                    },
                )
            )
            occupation_code = str(job.get("occupation_code", "") or "").strip()
            if occupation_code:
                occ_id = occupation_id(occupation_code)
                add_node(
                    GraphNode(
                        node_id=occ_id,
                        label="Occupation",
                        properties={
                            "occupation_id": occ_id,
                            "name": occupation_code,
                            "occupation_code": occupation_code,
                        },
                    )
                )
                prompt_version = "deterministic-structure-v1"
                props = self._common_edge_properties(
                    weight=1.0,
                    confidence=1.0,
                    evidence=occupation_code,
                    source_job_id=job_id_value,
                    prompt_version=prompt_version,
                )
                edge_id = deterministic_edge_id(
                    job_node_id(job_id_value),
                    "INSTANCE_OF",
                    occ_id,
                    job_id_value,
                )
                add_edge(
                    GraphEdge(
                        edge_id=edge_id,
                        from_id=job_node_id(job_id_value),
                        to_id=occ_id,
                        label="INSTANCE_OF",
                        properties=props,
                    )
                )

        canonical_names: list[str] = []
        canonical_index: dict[str, str] = {}
        for extraction in sorted(extraction_list, key=lambda item: item.job_id):
            job = job_lookup[extraction.job_id]
            source_text = "\n".join(
                str(job.get(key, "") or "")
                for key in ("title", "requirements", "description")
            )
            for mention in extraction.skills:
                selected, audit = self.canonicalizer.canonicalize(
                    mention.raw_mention,
                    canonical_skills=canonical_names,
                    canonical_index=canonical_index,
                    source_job_id=extraction.job_id,
                    context=source_text,
                )
                if selected is None:
                    selected = mention.canonical_candidate.strip()
                    audit.selected_skill = selected
                    audit.rule_used = "new_canonical_candidate"
                    if selected not in canonical_names:
                        canonical_names.append(selected)
                        canonical_index[normalize_skill_text(selected)] = selected
                elif selected not in canonical_names:
                    canonical_names.append(selected)
                    canonical_index[normalize_skill_text(selected)] = selected
                audits.append(audit)

                key = (normalize_skill_text(selected), mention.skill_type.value)
                skill = skills.setdefault(
                    key,
                    {
                        "skill_id": skill_id(selected, mention.skill_type.value),
                        "canonical_name": selected,
                        "normalized_name": normalize_skill_text(selected),
                        "skill_type": mention.skill_type.value,
                        "description": "",
                        "first_seen_at": str(job["posted_at"]),
                        "jobs": set(),
                    },
                )
                skill["jobs"].add(extraction.job_id)
                if str(job["posted_at"]) < skill["first_seen_at"]:
                    skill["first_seen_at"] = str(job["posted_at"])
                category = mention.skill_type.value
                cat_id = category_id(category)
                add_node(
                    GraphNode(
                        node_id=cat_id,
                        label="SkillCategory",
                        properties={"category_id": cat_id, "name": category},
                    )
                )

                requirement_weights = self.schema_config["requirement_weights"]
                edge_label = {
                    Requirement.REQUIRED: "REQUIRES",
                    Requirement.PREFERRED: "PREFERS",
                    Requirement.MENTIONED: "MENTIONS",
                }[mention.requirement]
                weight = (
                    float(requirement_weights[mention.requirement.value])
                    * mention.importance
                    * mention.confidence
                )
                skill_node = skill["skill_id"]
                props = self._common_edge_properties(
                    weight=weight,
                    confidence=mention.confidence,
                    evidence=mention.evidence,
                    source_job_id=extraction.job_id,
                    prompt_version=extraction.extraction_prompt_version,
                    extra={
                        "importance": mention.importance,
                        "requirement": mention.requirement.value,
                        "raw_mention": mention.raw_mention,
                    },
                )
                add_edge(
                    GraphEdge(
                        edge_id=deterministic_edge_id(
                            job_node_id(extraction.job_id),
                            edge_label,
                            skill_node,
                            f"{extraction.job_id}:{normalize_skill_text(mention.raw_mention)}",
                        ),
                        from_id=job_node_id(extraction.job_id),
                        to_id=skill_node,
                        label=edge_label,
                        properties=props,
                    )
                )
                job_skill_rows.append(
                    {
                        "job_id": extraction.job_id,
                        "occupation_code": str(job["occupation_code"]),
                        "skill": selected,
                        "skill_type": mention.skill_type.value,
                        "requirement": mention.requirement.value,
                        "weight": weight,
                        "confidence": mention.confidence,
                        "data_split": "train",
                    }
                )

                raw_normalized = normalize_skill_text(mention.raw_mention)
                if raw_normalized != normalize_skill_text(selected):
                    a_id = alias_id(mention.raw_mention)
                    alias_key = (raw_normalized, skill_node)
                    aliases[alias_key] = {
                        "alias_id": a_id,
                        "alias": mention.raw_mention,
                        "normalized_alias": raw_normalized,
                        "language": "und",
                        "first_seen_at": str(job["posted_at"]),
                        "skill_id": skill_node,
                    }
                    add_node(
                        GraphNode(
                            node_id=a_id,
                            label="SkillAlias",
                            properties={
                                "alias_id": a_id,
                                "alias": mention.raw_mention,
                                "normalized_alias": raw_normalized,
                                "language": "und",
                                "first_seen_at": str(job["posted_at"]),
                            },
                        )
                    )
                    alias_props = self._common_edge_properties(
                        weight=1.0,
                        confidence=mention.confidence,
                        evidence=mention.evidence,
                        source_job_id=extraction.job_id,
                        prompt_version=extraction.extraction_prompt_version,
                    )
                    add_edge(
                        GraphEdge(
                            edge_id=deterministic_edge_id(
                                a_id, "ALIAS_OF", skill_node, raw_normalized
                            ),
                            from_id=a_id,
                            to_id=skill_node,
                            label="ALIAS_OF",
                            properties=alias_props,
                        )
                    )

        for skill in sorted(skills.values(), key=lambda item: item["skill_id"]):
            add_node(
                GraphNode(
                    node_id=skill["skill_id"],
                    label="Skill",
                    properties={
                        "skill_id": skill["skill_id"],
                        "canonical_name": skill["canonical_name"],
                        "normalized_name": skill["normalized_name"],
                        "skill_type": skill["skill_type"],
                        "description": skill["description"],
                        "first_seen_at": skill["first_seen_at"],
                        "train_frequency": len(skill["jobs"]),
                        "schema_version": self.schema_version,
                    },
                )
            )
            cat_id = category_id(skill["skill_type"])
            props = self._common_edge_properties(
                weight=1.0,
                confidence=1.0,
                evidence=skill["skill_type"],
                source_job_id="",
                prompt_version="deterministic-category-v1",
            )
            add_edge(
                GraphEdge(
                    edge_id=deterministic_edge_id(
                        skill["skill_id"], "IS_A", cat_id, skill["skill_type"]
                    ),
                    from_id=skill["skill_id"],
                    to_id=cat_id,
                    label="IS_A",
                    properties=props,
                )
            )

        job_skills = pd.DataFrame(job_skill_rows)
        co_config = self.schema_config["cooccurrence"]
        if not job_skills.empty:
            co = compute_cooccurrence(
                job_skills,
                min_cooccurrence=int(co_config["min_cooccurrence"]),
                min_npmi=float(co_config["min_npmi"]),
                train_start=train_start,
                train_end=train_end,
            )
            skill_by_name: dict[str, str] = {}
            for item in skills.values():
                skill_by_name.setdefault(item["canonical_name"], item["skill_id"])
            for row in co.to_dict("records"):
                left, right = row["skill_a"], row["skill_b"]
                if left not in skill_by_name or right not in skill_by_name:
                    continue
                from_id, to_id = sorted(
                    (skill_by_name[left], skill_by_name[right])
                )
                props = self._common_edge_properties(
                    weight=float(max(0.0, row["npmi"])),
                    confidence=1.0,
                    evidence=f"train co-occurrence count={row['count']}",
                    source_job_id="",
                    prompt_version="deterministic-cooccurrence-v1",
                    extra={k: row[k] for k in row if k not in {"skill_a", "skill_b"}},
                )
                add_edge(
                    GraphEdge(
                        edge_id=deterministic_edge_id(
                            from_id,
                            "CO_OCCURS_WITH",
                            to_id,
                            f"{train_start}|{train_end}",
                        ),
                        from_id=from_id,
                        to_id=to_id,
                        label="CO_OCCURS_WITH",
                        properties=props,
                    )
                )

            core = compute_core_skills(job_skills)
            for row in core.to_dict("records"):
                target = skill_by_name.get(row["skill"])
                if not target:
                    continue
                from_id = occupation_id(row["occupation_code"])
                props = self._common_edge_properties(
                    weight=float(row["core_skill_weight"]),
                    confidence=1.0,
                    evidence=f"train occupation aggregation over {row['job_count']} jobs",
                    source_job_id="",
                    prompt_version="deterministic-core-skill-v1",
                    extra={
                        "required_rate": row["required_rate"],
                        "preferred_rate": row["preferred_rate"],
                        "mentioned_rate": row["mentioned_rate"],
                        "job_count": row["job_count"],
                    },
                )
                add_edge(
                    GraphEdge(
                        edge_id=deterministic_edge_id(
                            from_id, "CORE_SKILL", target, f"{train_start}|{train_end}"
                        ),
                        from_id=from_id,
                        to_id=target,
                        label="CORE_SKILL",
                        properties=props,
                    )
                )

        skill_dictionary = pd.DataFrame(
            [
                {
                    k: v
                    for k, v in skill.items()
                    if k not in {"jobs"}
                }
                | {"train_frequency": len(skill["jobs"])}
                for skill in sorted(skills.values(), key=lambda item: item["skill_id"])
            ]
        )
        alias_dictionary = pd.DataFrame(
            sorted(aliases.values(), key=lambda item: (item["normalized_alias"], item["skill_id"]))
        )
        audit_frame = pd.DataFrame([audit.model_dump(mode="json") for audit in audits])
        nodes = sorted(node_map.values(), key=lambda item: item.node_id)
        edges = sorted(edge_map.values(), key=lambda item: item.edge_id)
        manifest = {
            "schema_version": self.schema_version,
            "random_seed": int(self.schema_config.get("random_seed", 42)),
            "train_start": train_start,
            "train_end": train_end,
            "job_count": len(job_lookup),
            "node_count": len(nodes),
            "edge_count": len(edges),
            "node_labels": sorted({node.label for node in nodes}),
            "edge_labels": sorted({edge.label for edge in edges}),
            "contains_test_jd": False,
            "created_at": self.created_at,
            "config_hash": stable_json_hash(self.schema_config),
        }
        return GraphArtifacts(
            nodes=nodes,
            edges=edges,
            skill_dictionary=skill_dictionary,
            alias_dictionary=alias_dictionary,
            canonicalization_audit=audit_frame,
            job_skills=job_skills,
            extraction_manifest=extraction_manifest or [],
            graph_manifest=manifest,
        )


def _neptune_node_frame(nodes: list[GraphNode]) -> pd.DataFrame:
    property_names = sorted({key for node in nodes for key in node.properties})
    rows = []
    for node in nodes:
        row = {"~id": node.node_id, "~label": node.label}
        for name in property_names:
            value = node.properties.get(name, "")
            if isinstance(value, bool):
                column = f"{name}:Bool"
            elif isinstance(value, int):
                column = f"{name}:Long"
            elif isinstance(value, float):
                column = f"{name}:Double"
            else:
                column = f"{name}:String"
            row[column] = value
        rows.append(row)
    return pd.DataFrame(rows).fillna("")


def _neptune_edge_frame(edges: list[GraphEdge]) -> pd.DataFrame:
    rows = []
    for edge in edges:
        row = {
            "~id": edge.edge_id,
            "~from": edge.from_id,
            "~to": edge.to_id,
            "~label": edge.label,
        }
        for name, value in edge.properties.items():
            if isinstance(value, bool):
                column = f"{name}:Bool"
            elif isinstance(value, int):
                column = f"{name}:Long"
            elif isinstance(value, float):
                column = f"{name}:Double"
            else:
                column = f"{name}:String"
            row[column] = value
        rows.append(row)
    return pd.DataFrame(rows).fillna("")


def save_graph_artifacts(
    artifacts: GraphArtifacts, output_dir: str | Path, *, overwrite: bool = False
) -> list[Path]:
    target = Path(output_dir)
    if target.exists() and any(target.iterdir()) and not overwrite:
        raise FileExistsError(
            f"Output directory is not empty: {target}; pass overwrite=True explicitly"
        )
    target.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []

    outputs = {
        "nodes.csv": _neptune_node_frame(artifacts.nodes),
        "edges.csv": _neptune_edge_frame(artifacts.edges),
        "nodes_plain.csv": artifacts.node_frame(),
        "edges_plain.csv": artifacts.edge_frame(),
        "skill_dictionary.csv": artifacts.skill_dictionary,
        "alias_dictionary.csv": artifacts.alias_dictionary,
        "canonicalization_audit.csv": artifacts.canonicalization_audit,
    }
    for name, frame in outputs.items():
        path = target / name
        frame.to_csv(path, index=False)
        files.append(path)
    artifacts.node_frame().to_parquet(target / "nodes.parquet", index=False)
    artifacts.edge_frame().to_parquet(target / "edges.parquet", index=False)
    files.extend([target / "nodes.parquet", target / "edges.parquet"])

    extraction_path = target / "extraction_manifest.json"
    extraction_path.write_text(
        json.dumps(artifacts.extraction_manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    graph_path = target / "graph_manifest.json"
    graph_path.write_text(
        json.dumps(artifacts.graph_manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    files.extend([extraction_path, graph_path])
    return files
