from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

import pandas as pd

from .canonicalization import normalize_skill_text
from .models import QueryParseResult, TraversalTrace


SKILL_TO_JOB_EDGE_WEIGHTS = {
    "REQUIRES": 1.0,
    "PREFERS": 0.72,
    "MENTIONS": 0.45,
}
EXPANSION_EDGE_WEIGHTS = {
    "ALIAS_OF": 1.0,
    "IS_A": 0.25,
    "CO_OCCURS_WITH": 0.65,
    "CORE_SKILL": 0.75,
}


@dataclass
class Candidate:
    job_id: str
    graph_score: float
    matched_skills: list[str]
    traces: list[TraversalTrace]


class InMemorySkillGraph:
    def __init__(
        self,
        nodes: pd.DataFrame,
        edges: pd.DataFrame,
        *,
        max_hops: int = 2,
        hop_decay: float = 0.7,
    ) -> None:
        required_nodes = {"node_id", "label"}
        required_edges = {"from_id", "to_id", "label"}
        if missing := required_nodes - set(nodes.columns):
            raise ValueError(f"nodes missing columns: {sorted(missing)}")
        if missing := required_edges - set(edges.columns):
            raise ValueError(f"edges missing columns: {sorted(missing)}")
        if max_hops > 2:
            raise ValueError("Retrieval is intentionally bounded to at most 2 hops")
        self.nodes = nodes.copy()
        self.edges = edges.copy()
        self.max_hops = max_hops
        self.hop_decay = hop_decay
        self.node_rows = {
            str(row["node_id"]): row.to_dict() for _, row in nodes.iterrows()
        }
        self.skill_name_to_id: dict[str, str] = {}
        self.alias_to_skill: dict[str, str] = {}
        self.occupation_to_id: dict[str, str] = {}
        for _, row in nodes.iterrows():
            node_id = str(row["node_id"])
            if row["label"] == "Skill":
                name = str(row.get("canonical_name", "") or "")
                self.skill_name_to_id[normalize_skill_text(name)] = node_id
            elif row["label"] == "Occupation":
                for value in (row.get("name"), row.get("occupation_code")):
                    if value and not pd.isna(value):
                        self.occupation_to_id[normalize_skill_text(str(value))] = node_id
        for _, row in edges[edges["label"].eq("ALIAS_OF")].iterrows():
            alias_node = self.node_rows.get(str(row["from_id"]), {})
            alias = str(alias_node.get("normalized_alias", "") or "")
            if alias:
                self.alias_to_skill[normalize_skill_text(alias)] = str(row["to_id"])

    def parse_query(self, query: str) -> QueryParseResult:
        normalized_query = normalize_skill_text(query)
        matches: list[tuple[int, str, str]] = []
        vocabulary = {
            **self.skill_name_to_id,
            **self.alias_to_skill,
        }
        for phrase, skill_id in vocabulary.items():
            if phrase and phrase in normalized_query:
                canonical = str(
                    self.node_rows.get(skill_id, {}).get("canonical_name", phrase)
                )
                matches.append((len(phrase), phrase, canonical))
        matches.sort(key=lambda item: (-item[0], item[1]))
        selected: list[str] = []
        mentions: list[str] = []
        occupied: list[tuple[int, int]] = []
        for _, phrase, canonical in matches:
            start = normalized_query.find(phrase)
            end = start + len(phrase)
            if start >= 0 and not any(start < right and end > left for left, right in occupied):
                occupied.append((start, end))
                mentions.append(phrase)
                if canonical not in selected:
                    selected.append(canonical)
        occupation = next(
            (
                key
                for key in sorted(self.occupation_to_id, key=lambda value: (-len(value), value))
                if key and key in normalized_query
            ),
            None,
        )
        return QueryParseResult(
            raw_query=query,
            skill_mentions=mentions,
            canonical_skills=selected,
            occupation_candidate=occupation,
            unresolved_terms=[] if selected or occupation else [query.strip()],
        )

    @staticmethod
    def _edge_weight(row: pd.Series, default: float) -> float:
        value = row.get("weight", default)
        if value is None or pd.isna(value):
            return default
        return max(0.0, float(value))

    def retrieve(
        self,
        query: str | QueryParseResult,
        *,
        top_k: int = 50,
    ) -> list[Candidate]:
        parsed = self.parse_query(query) if isinstance(query, str) else query
        anchors: list[tuple[str, str]] = []
        for skill in parsed.canonical_skills:
            skill_node = self.skill_name_to_id.get(normalize_skill_text(skill))
            if skill_node:
                anchors.append((skill, skill_node))

        occupation_node = None
        if parsed.occupation_candidate:
            occupation_node = self.occupation_to_id.get(
                normalize_skill_text(parsed.occupation_candidate)
            )

        scores: defaultdict[str, float] = defaultdict(float)
        matched: defaultdict[str, set[str]] = defaultdict(set)
        traces: defaultdict[str, list[TraversalTrace]] = defaultdict(list)
        job_edges = self.edges[self.edges["label"].isin(SKILL_TO_JOB_EDGE_WEIGHTS)]

        def add_jobs(
            query_term: str,
            anchor_id: str,
            anchor_name: str,
            prefix_nodes: list[str],
            prefix_edges: list[str],
            prefix_weights: list[float],
            prefix_score: float,
        ) -> None:
            incoming = job_edges[job_edges["to_id"].astype(str).eq(anchor_id)]
            for _, edge in incoming.iterrows():
                relation = str(edge["label"])
                relation_weight = self._edge_weight(
                    edge, SKILL_TO_JOB_EDGE_WEIGHTS[relation]
                )
                hop_count = len(prefix_edges) + 1
                path_score = (
                    prefix_score
                    * relation_weight
                    * (self.hop_decay ** max(0, hop_count - 1))
                )
                job_node = str(edge["from_id"])
                job_row = self.node_rows.get(job_node, {})
                job_id = str(job_row.get("job_id", job_node.removeprefix("job:")))
                scores[job_id] += path_score
                matched[job_id].add(anchor_name)
                traces[job_id].append(
                    TraversalTrace(
                        query_term=query_term,
                        canonical_anchor=anchor_name,
                        path=prefix_nodes + [anchor_id, job_node],
                        edge_types=prefix_edges + [relation],
                        individual_edge_weights=prefix_weights + [relation_weight],
                        final_path_score=path_score,
                        matched_job=job_id,
                        explanation=(
                            f"{query_term} matched {anchor_name}; "
                            f"path uses {' -> '.join(prefix_edges + [relation])}"
                        ),
                    )
                )

        for query_term, anchor_id in anchors:
            add_jobs(query_term, anchor_id, query_term, [], [], [], 1.0)
            if self.max_hops < 2:
                continue
            related = self.edges[
                self.edges["label"].isin(["CO_OCCURS_WITH", "CORE_SKILL"])
                & (
                    self.edges["from_id"].astype(str).eq(anchor_id)
                    | self.edges["to_id"].astype(str).eq(anchor_id)
                )
            ]
            for _, relation in related.iterrows():
                relation_type = str(relation["label"])
                other = (
                    str(relation["to_id"])
                    if str(relation["from_id"]) == anchor_id
                    else str(relation["from_id"])
                )
                if self.node_rows.get(other, {}).get("label") != "Skill":
                    continue
                relation_weight = self._edge_weight(
                    relation, EXPANSION_EDGE_WEIGHTS[relation_type]
                )
                add_jobs(
                    query_term,
                    other,
                    str(self.node_rows[other].get("canonical_name", other)),
                    [anchor_id],
                    [relation_type],
                    [relation_weight],
                    relation_weight,
                )

        if occupation_node:
            instance_edges = self.edges[
                self.edges["label"].eq("INSTANCE_OF")
                & self.edges["to_id"].astype(str).eq(occupation_node)
            ]
            for _, edge in instance_edges.iterrows():
                job_node = str(edge["from_id"])
                job_row = self.node_rows.get(job_node, {})
                job_id = str(job_row.get("job_id", job_node.removeprefix("job:")))
                score = self._edge_weight(edge, 1.0)
                scores[job_id] += score
                traces[job_id].append(
                    TraversalTrace(
                        query_term=parsed.occupation_candidate or "",
                        canonical_anchor=parsed.occupation_candidate or "",
                        path=[occupation_node, job_node],
                        edge_types=["INSTANCE_OF"],
                        individual_edge_weights=[score],
                        final_path_score=score,
                        matched_job=job_id,
                        explanation="Occupation constraint matched the job occupation.",
                    )
                )

        ranked = sorted(scores, key=lambda job_id: (-scores[job_id], job_id))[:top_k]
        return [
            Candidate(
                job_id=job_id,
                graph_score=float(scores[job_id]),
                matched_skills=sorted(matched[job_id]),
                traces=sorted(
                    traces[job_id],
                    key=lambda trace: (-trace.final_path_score, trace.explanation),
                ),
            )
            for job_id in ranked
        ]


def traversal_trace_frame(candidates: Iterable[Candidate]) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for candidate in candidates:
        for trace in candidate.traces:
            rows.append(trace.model_dump(mode="json"))
    return pd.DataFrame(rows)


PARAMETERIZED_OPENCYPHER_TEMPLATES = {
    "exact_skill_jobs": (
        "MATCH (s:Skill)<-[r:REQUIRES|PREFERS|MENTIONS]-(j:Job) "
        "WHERE s.normalized_name IN $skill_names "
        "RETURN j.job_id AS job_id, s.skill_id AS skill_id, type(r) AS relation, "
        "r.weight AS weight LIMIT $limit"
    ),
    "occupation_jobs": (
        "MATCH (j:Job)-[:INSTANCE_OF]->(o:Occupation) "
        "WHERE o.occupation_code = $occupation_code "
        "RETURN j.job_id AS job_id LIMIT $limit"
    ),
}
