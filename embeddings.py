from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

import numpy as np
import pandas as pd

from .schema import stable_json_hash


class EmbeddingProvider(Protocol):
    model_id: str
    dimension: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


@dataclass
class DeterministicEmbeddingProvider:
    """Offline embedding substitute used by tests and the synthetic demo."""

    dimension: int = 256
    model_id: str = "deterministic-sha256-v1"

    def _one(self, text: str) -> list[float]:
        vector = np.zeros(self.dimension, dtype=np.float64)
        normalized = " ".join(text.casefold().split())
        tokens = normalized.split() or [normalized]
        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            for index, value in enumerate(digest):
                position = (index * 31 + value) % self.dimension
                vector[position] += 1.0 if value % 2 else -1.0
        norm = np.linalg.norm(vector)
        if norm:
            vector /= norm
        return vector.astype(float).tolist()

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._one(text) for text in texts]


@dataclass
class BedrockTitanEmbeddingProvider:
    """Paid provider. Construction is safe; invocation is explicitly gated."""

    model_id: str = "amazon.titan-embed-text-v2:0"
    dimension: int = 256
    region_name: str = "us-east-1"
    allow_paid_api: bool = False

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not self.allow_paid_api:
            raise RuntimeError(
                "Bedrock embedding call is disabled. Use the deterministic provider "
                "or explicitly set allow_paid_api=True in your own environment."
            )
        import boto3  # Delayed: importing the package never creates an AWS client.

        client = boto3.client("bedrock-runtime", region_name=self.region_name)
        vectors: list[list[float]] = []
        for text in texts:
            response = client.invoke_model(
                modelId=self.model_id,
                contentType="application/json",
                accept="application/json",
                body=json.dumps(
                    {
                        "inputText": text,
                        "dimensions": self.dimension,
                        "normalize": True,
                    }
                ),
            )
            payload = json.loads(response["body"].read())
            vectors.append([float(value) for value in payload["embedding"]])
        return vectors


def build_skill_embedding_text(row: pd.Series | dict[str, object]) -> str:
    values = dict(row)
    return " | ".join(
        part
        for part in (
            str(values.get("canonical_name", "") or ""),
            str(values.get("skill_type", "") or ""),
            str(values.get("description", "") or ""),
        )
        if part
    )


def build_occupation_embedding_text(row: pd.Series | dict[str, object]) -> str:
    values = dict(row)
    return " | ".join(
        part
        for part in (
            str(values.get("name", "") or ""),
            str(values.get("occupation_code", "") or ""),
            str(values.get("description", "") or ""),
        )
        if part
    )


def embed_graph_nodes(
    nodes: pd.DataFrame, provider: EmbeddingProvider
) -> tuple[pd.DataFrame, dict[str, object]]:
    required = {"node_id", "label"}
    missing = required - set(nodes.columns)
    if missing:
        raise ValueError(f"nodes missing columns: {sorted(missing)}")
    selected = nodes[nodes["label"].isin(["Skill", "Occupation"])].copy()
    texts: list[str] = []
    for _, row in selected.iterrows():
        if row["label"] == "Skill":
            texts.append(build_skill_embedding_text(row))
        else:
            texts.append(build_occupation_embedding_text(row))
    vectors = provider.embed(texts)
    if len(vectors) != len(selected):
        raise ValueError("Embedding provider returned the wrong number of vectors")
    if any(len(vector) != provider.dimension for vector in vectors):
        raise ValueError("Embedding provider returned an unexpected dimension")
    selected["embedding_text"] = texts
    selected["embedding"] = vectors
    selected["embedding_model"] = provider.model_id
    selected["embedding_dimension"] = provider.dimension
    manifest = {
        "model_id": provider.model_id,
        "dimension": provider.dimension,
        "row_count": len(selected),
        "node_ids_hash": stable_json_hash(selected["node_id"].astype(str).tolist()),
        "contains_test_jd": False,
    }
    return selected[
        [
            "node_id",
            "label",
            "embedding_text",
            "embedding",
            "embedding_model",
            "embedding_dimension",
        ]
    ], manifest


def save_embeddings(
    embeddings: pd.DataFrame,
    manifest: dict[str, object],
    output_dir: str | Path,
) -> tuple[Path, Path]:
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    parquet_path = target / "embeddings.parquet"
    manifest_path = target / "embedding_manifest.json"
    embeddings.to_parquet(parquet_path, index=False)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return parquet_path, manifest_path


def build_neptune_import_plan(
    *,
    node_csv: str,
    edge_csv: str,
    s3_uri_prefix: str,
    neptune_endpoint: str,
    iam_role_arn: str,
    region: str,
) -> dict[str, object]:
    """Return an auditable plan; this function performs no I/O and no AWS calls."""

    return {
        "dry_run": True,
        "uploads": [
            {"local": node_csv, "remote": f"{s3_uri_prefix.rstrip('/')}/nodes.csv"},
            {"local": edge_csv, "remote": f"{s3_uri_prefix.rstrip('/')}/edges.csv"},
        ],
        "neptune_loader": {
            "source": s3_uri_prefix.rstrip("/"),
            "format": "csv",
            "iamRoleArn": iam_role_arn,
            "region": region,
            "failOnError": "TRUE",
            "parallelism": "MEDIUM",
            "endpoint": neptune_endpoint,
        },
        "aws_actions_executed": [],
    }


def execute_neptune_import(
    plan: dict[str, object],
    *,
    dry_run: bool = True,
    allow_aws_write: bool | None = None,
) -> dict[str, object]:
    """Execute only after two explicit gates; workshop/demo code always uses dry-run."""

    if dry_run:
        return plan
    allowed = (
        os.environ.get("ALLOW_AWS_WRITE") == "1"
        if allow_aws_write is None
        else allow_aws_write
    )
    if not allowed:
        raise RuntimeError(
            "AWS write blocked. Set dry_run=False and ALLOW_AWS_WRITE=1 explicitly."
        )
    raise NotImplementedError(
        "Production upload is intentionally not implemented in this workshop. "
        "Review the dry-run plan and add organization-approved AWS controls first."
    )
