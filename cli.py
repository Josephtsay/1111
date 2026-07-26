from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

import pandas as pd

from .canonicalization import CanonicalizationPipeline, normalize_skill_text
from .dataset_1111 import Dataset1111Adapter, Dataset1111Config
from .embeddings import (
    DeterministicEmbeddingProvider,
    build_neptune_import_plan,
    embed_graph_nodes,
    save_embeddings,
)
from .extraction import (
    build_bedrock_request,
    build_extraction_prompt,
    parse_extraction_response,
)
from .graph_builder import GraphBuilder, save_graph_artifacts
from .graph_features import compute_feature_frame, compute_labeled_feature_frame
from .graph_validator import GraphValidator, save_validation_result
from .ltr import (
    LambdaMARTRanker,
    add_bm25_scores,
    evaluate_ablation,
    save_ablation_outputs,
)
from .graph_features import select_feature_set
from .models import JobSkillExtraction
from .retrieval import InMemorySkillGraph, traversal_trace_frame
from .search_service import (
    HybridRerankBackend,
    SQLiteFTSSearchBackend,
    build_sqlite_search_index,
)
from .schema import (
    ColumnMapping,
    assert_train_only_jobs,
    load_and_validate_csvs,
    load_yaml,
    sha256_file,
    stable_json_hash,
    validate_dataframes,
)
from .structured_extraction import extract_structured_skills_from_parquet


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SCHEMA = PROJECT_ROOT / "config" / "graph_schema.yaml"
DEFAULT_MAPPING = PROJECT_ROOT / "config" / "column_mapping.synthetic.yaml"
FIXTURES = PROJECT_ROOT / "tests" / "fixtures"


def _read_table(path: str | Path) -> pd.DataFrame:
    source = Path(path)
    if source.suffix.casefold() in {".parquet", ".pq"}:
        return pd.read_parquet(source)
    return pd.read_csv(source)


def _canonical_jobs(frame: pd.DataFrame, mapping: ColumnMapping) -> pd.DataFrame:
    rename = {
        source: ("data_split" if key == "split" else key)
        for key, source in mapping.section("jobs").items()
    }
    return frame.rename(columns=rename)[
        [
            "job_id",
            "title",
            "requirements",
            "description",
            "posted_at",
            "location_code",
            "occupation_code",
            "data_split",
        ]
    ].copy()


def _canonical_queries(frame: pd.DataFrame, mapping: ColumnMapping) -> pd.DataFrame:
    rename = {
        source: ("query" if key == "query_text" else key)
        for key, source in mapping.section("queries").items()
    }
    return frame.rename(columns=rename).copy()


def _canonical_labels(frame: pd.DataFrame, mapping: ColumnMapping) -> pd.DataFrame:
    return frame.rename(
        columns={source: key for key, source in mapping.section("labels").items()}
    ).copy()


def _load_extractions(
    path: str | Path, jobs: pd.DataFrame | None = None
) -> list[JobSkillExtraction]:
    source_lookup: dict[str, str] = {}
    if jobs is not None:
        source_lookup = {
            str(row["job_id"]): "\n".join(
                str(row[column])
                for column in ("title", "requirements", "description")
            )
            for _, row in jobs.iterrows()
        }
    parsed: list[JobSkillExtraction] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            payload = json.loads(line)
            job_id = str(payload.get("job_id", ""))
            try:
                parsed.append(
                    parse_extraction_response(
                        json.dumps(payload, ensure_ascii=False),
                        source_text=source_lookup.get(job_id),
                        expected_job_id=job_id,
                    )
                )
            except Exception as error:
                raise ValueError(
                    f"Invalid extraction at {path}:{line_number}: {error}"
                ) from error
    return parsed


def _canonicalizer(config: dict[str, Any]) -> CanonicalizationPipeline:
    section = config["canonicalization"]
    return CanonicalizationPipeline(
        aliases=dict(section["high_precision_aliases"]),
        verifier_threshold=float(section["verifier_threshold"]),
        top_k=int(section["embedding_top_k"]),
    )


def _offline_extraction_manifest(
    extractions: list[JobSkillExtraction], source_path: str | Path
) -> list[dict[str, Any]]:
    input_hash = sha256_file(source_path)
    return [
        {
            "job_id": extraction.job_id,
            "provider": "offline-import",
            "model_id": "offline-import",
            "prompt_version": extraction.extraction_prompt_version,
            "input_file_hash": input_hash,
            "status": "validated",
            "paid_api_call": False,
        }
        for extraction in extractions
    ]


def command_validate_data(args: argparse.Namespace) -> dict[str, Any]:
    _, _, _, _, report = load_and_validate_csvs(
        args.jobs,
        args.queries,
        args.labels,
        args.columns,
        train_cutoff=args.train_cutoff,
    )
    report["input_hashes"] = {
        "jobs": sha256_file(args.jobs),
        "queries": sha256_file(args.queries),
        "labels": sha256_file(args.labels),
        "columns": sha256_file(args.columns),
    }
    if args.out:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    return report


def command_profile_1111_data(args: argparse.Namespace) -> dict[str, Any]:
    config = Dataset1111Config(
        train_end=args.train_end,
        validation_end=args.validation_end,
        test_end=args.test_end,
        threads=args.threads,
        memory_limit=args.memory_limit,
    )
    with Dataset1111Adapter(args.dataset, config=config) as adapter:
        report = adapter.profile_raw()
    if args.out:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return report


def command_prepare_1111_data(args: argparse.Namespace) -> dict[str, Any]:
    config = Dataset1111Config(
        train_end=args.train_end,
        validation_end=args.validation_end,
        test_end=args.test_end,
        browse_window_minutes=args.browse_window_minutes,
        apply_window_hours=args.apply_window_hours,
        negative_cap_per_query=args.negative_cap,
        only_queries_with_positive=not args.include_zero_positive_queries,
        strict_temporal_content=not args.allow_future_content,
        sample_queries=args.sample_queries,
        jobs_scope=args.jobs_scope,
        threads=args.threads,
        memory_limit=args.memory_limit,
    )
    with Dataset1111Adapter(args.dataset, config=config) as adapter:
        artifacts = adapter.prepare(args.out, overwrite=args.overwrite)
    manifest = json.loads(artifacts.manifest.read_text(encoding="utf-8"))
    return {
        "status": "prepared",
        "output_dir": str(artifacts.output_dir),
        "outputs": {
            "jobs": str(artifacts.jobs),
            "train_jobs": str(artifacts.train_jobs),
            "queries": str(artifacts.queries),
            "labeled_queries": str(artifacts.labeled_queries),
            "labels": str(artifacts.labels),
        },
        "profile": manifest["profile"],
        "privacy": manifest["privacy"],
        "labels": manifest["labels"],
    }


def command_build_search_index(args: argparse.Namespace) -> dict[str, Any]:
    return build_sqlite_search_index(
        args.jobs,
        args.out,
        overwrite=args.overwrite,
        limit=args.limit,
        batch_size=args.batch_size,
    )


def command_extract_structured_skills(args: argparse.Namespace) -> dict[str, Any]:
    return extract_structured_skills_from_parquet(
        args.jobs,
        args.out,
        split=args.split,
        limit=args.limit,
        batch_size=args.batch_size,
    )


def command_serve_api(args: argparse.Namespace) -> dict[str, Any]:
    import uvicorn

    from .api import create_app

    backend: object = SQLiteFTSSearchBackend(args.index)
    if args.graph:
        graph_dir = Path(args.graph)
        graph = InMemorySkillGraph(
            pd.read_csv(
                graph_dir / "nodes_plain.csv",
                dtype={"node_id": "string", "job_id": "string"},
            ),
            pd.read_csv(
                graph_dir / "edges_plain.csv",
                dtype={
                    "edge_id": "string",
                    "from_id": "string",
                    "to_id": "string",
                    "source_job_id": "string",
                },
            ),
        )
        ranker = LambdaMARTRanker.load(args.model) if args.model else None
        backend = HybridRerankBackend(backend, graph, ranker=ranker)
    elif args.model:
        raise ValueError("--model requires --graph for online feature generation")
    application = create_app(backend, result_limit=args.result_limit)
    uvicorn.run(application, host=args.host, port=args.port)
    return {"status": "stopped"}


def command_prepare_extraction(args: argparse.Namespace) -> dict[str, Any]:
    mapping = ColumnMapping.from_yaml(args.columns)
    raw_jobs = pd.read_csv(args.jobs)
    assert_train_only_jobs(raw_jobs, mapping, artifact_name="extraction")
    jobs = _canonical_jobs(raw_jobs, mapping)
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    with target.open("w", encoding="utf-8") as handle:
        for _, job in jobs.sort_values("job_id").iterrows():
            prompt = build_extraction_prompt(
                job_id=str(job["job_id"]),
                title=str(job["title"]),
                requirements=str(job["requirements"]),
                description=str(job["description"]),
            )
            request = build_bedrock_request(prompt)
            record = {
                "job_id": str(job["job_id"]),
                "request": request,
                "request_hash": stable_json_hash(request),
                "dry_run": True,
            }
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            records.append(record)
    return {
        "status": "prepared",
        "dry_run": True,
        "paid_api_calls": 0,
        "request_count": len(records),
        "output": str(target),
    }


def command_import_extraction(args: argparse.Namespace) -> dict[str, Any]:
    mapping = ColumnMapping.from_yaml(args.columns)
    raw_jobs = _read_table(args.jobs)
    canonical_columns = {
        "job_id",
        "title",
        "requirements",
        "description",
        "posted_at",
        "location_code",
        "occupation_code",
        "data_split",
    }
    jobs = (
        raw_jobs[list(canonical_columns)].copy()
        if canonical_columns <= set(raw_jobs.columns)
        else _canonical_jobs(raw_jobs, mapping)
    )
    parsed = _load_extractions(args.input, jobs)
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        for item in parsed:
            handle.write(item.model_dump_json() + "\n")
    return {"status": "valid", "record_count": len(parsed), "output": str(target)}


def command_canonicalize(args: argparse.Namespace) -> dict[str, Any]:
    config = load_yaml(args.config)
    pipeline = _canonicalizer(config)
    extractions = _load_extractions(args.input)
    canonical: list[str] = []
    canonical_index: dict[str, str] = {}
    audits: list[dict[str, Any]] = []
    for extraction in extractions:
        for mention in extraction.skills:
            selected, audit = pipeline.canonicalize(
                mention.raw_mention,
                canonical_skills=canonical,
                canonical_index=canonical_index,
                source_job_id=extraction.job_id,
            )
            selected = selected or mention.canonical_candidate
            if selected not in canonical:
                canonical.append(selected)
                canonical_index[normalize_skill_text(selected)] = selected
            audits.append(audit.model_dump(mode="json") | {"final_skill": selected})
    target = Path(args.out)
    target.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(audits).to_csv(target / "canonicalization_audit.csv", index=False)
    pd.DataFrame({"canonical_name": sorted(canonical)}).to_csv(
        target / "skill_dictionary.csv", index=False
    )
    return {"skills": len(canonical), "mentions": len(audits), "output": str(target)}


def command_build_graph(args: argparse.Namespace) -> dict[str, Any]:
    mapping = ColumnMapping.from_yaml(args.columns)
    raw_jobs = _read_table(args.jobs)
    canonical_columns = {
        "job_id",
        "title",
        "requirements",
        "description",
        "posted_at",
        "location_code",
        "occupation_code",
        "data_split",
    }
    if canonical_columns <= set(raw_jobs.columns):
        jobs = raw_jobs[list(canonical_columns)].copy()
        leaked = jobs[
            ~jobs["data_split"].astype(str).str.casefold().eq("train")
        ]
        if not leaked.empty:
            raise ValueError(
                "Canonical graph input contains non-train jobs: "
                f"{leaked['job_id'].astype(str).head(10).tolist()}"
            )
    else:
        assert_train_only_jobs(raw_jobs, mapping)
        jobs = _canonical_jobs(raw_jobs, mapping)
    extractions = _load_extractions(args.extractions, jobs)
    config = load_yaml(args.config)
    artifacts = GraphBuilder(
        config,
        _canonicalizer(config),
        source_file=str(args.jobs),
    ).build(
        jobs,
        extractions,
        extraction_manifest=_offline_extraction_manifest(
            extractions, args.extractions
        ),
        train_start=str(
            pd.to_datetime(jobs["posted_at"], utc=True, format="mixed").min()
        ),
        train_end=str(
            pd.to_datetime(jobs["posted_at"], utc=True, format="mixed").max()
        ),
    )
    files = save_graph_artifacts(artifacts, args.out, overwrite=args.overwrite)
    return {
        "status": "built",
        "nodes": len(artifacts.nodes),
        "edges": len(artifacts.edges),
        "files": [str(path) for path in files],
        "contains_test_jd": False,
    }


def command_validate_graph(args: argparse.Namespace) -> dict[str, Any]:
    graph_dir = Path(args.graph)
    nodes = pd.read_csv(
        graph_dir / "nodes_plain.csv",
        dtype={"node_id": "string", "job_id": "string"},
    )
    edges = pd.read_csv(
        graph_dir / "edges_plain.csv",
        dtype={
            "edge_id": "string",
            "from_id": "string",
            "to_id": "string",
            "source_job_id": "string",
        },
    )
    source_jobs = _read_table(args.source_jobs) if args.source_jobs else None
    audit_path = graph_dir / "canonicalization_audit.csv"
    audit = pd.read_csv(audit_path) if audit_path.exists() else None
    result = GraphValidator(load_yaml(args.config)).validate(
        nodes, edges, source_jobs=source_jobs, canonicalization_audit=audit
    )
    save_validation_result(result, args.out, overwrite=args.overwrite)
    if result.report["status"] == "failed":
        raise RuntimeError("Graph validation failed; inspect graph_quality_report.json")
    return result.report


def command_build_features(args: argparse.Namespace) -> dict[str, Any]:
    mapping = ColumnMapping.from_yaml(args.columns)
    raw_jobs = _read_table(args.jobs)
    jobs = (
        raw_jobs
        if {"job_id", "title", "requirements", "description"} <= set(raw_jobs)
        else _canonical_jobs(raw_jobs, mapping)
    )
    raw_queries = _read_table(args.queries)
    queries = (
        raw_queries.rename(columns={"query_text": "query"})
        if "query" not in raw_queries and "query_text" in raw_queries
        else raw_queries
    )
    raw_labels = _read_table(args.labels)
    labels = (
        raw_labels
        if {"query_id", "job_id", "relevance"} <= set(raw_labels)
        else _canonical_labels(raw_labels, mapping)
    )
    graph_dir = Path(args.graph)
    graph = InMemorySkillGraph(
        pd.read_csv(
            graph_dir / "nodes_plain.csv",
            dtype={"node_id": "string", "job_id": "string"},
        ),
        pd.read_csv(
            graph_dir / "edges_plain.csv",
            dtype={
                "edge_id": "string",
                "from_id": "string",
                "to_id": "string",
                "source_job_id": "string",
            },
        ),
    )
    frame = (
        compute_labeled_feature_frame(queries, jobs, labels, graph)
        if args.candidate_only
        else compute_feature_frame(queries, jobs, graph, labels=labels)
    )
    frame = add_bm25_scores(frame, queries, jobs)
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(target, index=False)
    return {"rows": len(frame), "columns": len(frame.columns), "output": str(target)}


def command_evaluate_ablation(args: argparse.Namespace) -> dict[str, Any]:
    frame = pd.read_parquet(args.features)
    results, per_query, details = evaluate_ablation(frame, seed=args.seed)
    paths = save_ablation_outputs(results, per_query, details, args.out)
    train = frame[frame["data_split"].eq("train")].copy()
    for name in ("C", "D"):
        ranker = LambdaMARTRanker(random_seed=args.seed).fit(
            train, select_feature_set(name)
        )
        model_paths = ranker.save(Path(args.out) / f"lambdamart_{name}.txt")
        paths.extend(model_paths)
    return {"status": "complete", "files": [str(path) for path in paths]}


def command_synthetic_demo(args: argparse.Namespace) -> dict[str, Any]:
    out = Path(args.out)
    if out.exists():
        if not args.overwrite:
            raise FileExistsError(f"Synthetic demo output exists: {out}")
        shutil.rmtree(out)
    out.mkdir(parents=True)
    jobs_path = FIXTURES / "train_jobs.csv"
    queries_path = FIXTURES / "train_queries.csv"
    labels_path = FIXTURES / "train_labels.csv"
    extraction_path = FIXTURES / "sample_extractions.jsonl"
    mapping = ColumnMapping.from_yaml(DEFAULT_MAPPING)
    raw_jobs, raw_queries, raw_labels = (
        pd.read_csv(jobs_path),
        pd.read_csv(queries_path),
        pd.read_csv(labels_path),
    )
    validation = validate_dataframes(raw_jobs, raw_queries, raw_labels, mapping)
    jobs = _canonical_jobs(raw_jobs, mapping)
    queries = _canonical_queries(raw_queries, mapping)
    labels = _canonical_labels(raw_labels, mapping)
    config = load_yaml(DEFAULT_SCHEMA)
    extractions = _load_extractions(extraction_path, jobs)
    artifacts = GraphBuilder(
        config, _canonicalizer(config), source_file=str(jobs_path)
    ).build(
        jobs,
        extractions,
        extraction_manifest=_offline_extraction_manifest(
            extractions, extraction_path
        ),
        train_start=str(
            pd.to_datetime(jobs["posted_at"], utc=True, format="mixed").min()
        ),
        train_end=str(
            pd.to_datetime(jobs["posted_at"], utc=True, format="mixed").max()
        ),
    )
    graph_dir = out / "graph"
    save_graph_artifacts(artifacts, graph_dir)
    quality = GraphValidator(config).validate(
        artifacts.node_frame(),
        artifacts.edge_frame(),
        source_jobs=jobs,
        canonicalization_audit=artifacts.canonicalization_audit,
    )
    save_validation_result(quality, out / "quality")
    graph = InMemorySkillGraph(artifacts.node_frame(), artifacts.edge_frame())
    all_candidates = []
    all_traces = []
    for _, query_row in queries.sort_values("query_id").iterrows():
        candidates = graph.retrieve(str(query_row["query"]), top_k=len(jobs))
        for rank, candidate in enumerate(candidates, start=1):
            all_candidates.append(
                {
                    "query_id": str(query_row["query_id"]),
                    "job_id": candidate.job_id,
                    "rank": rank,
                    "graph_score": candidate.graph_score,
                    "matched_skills": "|".join(candidate.matched_skills),
                }
            )
        trace_frame = traversal_trace_frame(candidates)
        if not trace_frame.empty:
            trace_frame.insert(0, "query_id", str(query_row["query_id"]))
            all_traces.append(trace_frame)
    retrieval_dir = out / "retrieval"
    retrieval_dir.mkdir()
    pd.DataFrame(all_candidates).to_csv(
        retrieval_dir / "candidates.csv", index=False
    )
    (
        pd.concat(all_traces, ignore_index=True)
        if all_traces
        else pd.DataFrame()
    ).to_csv(retrieval_dir / "traversal_trace.csv", index=False)

    embedded, embedding_manifest = embed_graph_nodes(
        artifacts.node_frame(), DeterministicEmbeddingProvider(dimension=256)
    )
    save_embeddings(embedded, embedding_manifest, out / "embeddings")

    neptune_plan = build_neptune_import_plan(
        node_csv=str(graph_dir / "nodes.csv"),
        edge_csv=str(graph_dir / "edges.csv"),
        s3_uri_prefix="s3://REPLACE_ME/job-skill-graph/synthetic-demo",
        neptune_endpoint="REPLACE_ME.neptune.amazonaws.com",
        iam_role_arn="arn:aws:iam::ACCOUNT_ID:role/REPLACE_ME",
        region="ap-northeast-1",
    )
    (out / "neptune_import_dry_run.json").write_text(
        json.dumps(neptune_plan, ensure_ascii=False, indent=2), "utf-8"
    )

    features = add_bm25_scores(
        compute_feature_frame(queries, jobs, graph, labels=labels), queries, jobs
    )
    features.to_parquet(out / "features.parquet", index=False)
    results, per_query, details = evaluate_ablation(features, seed=42)
    save_ablation_outputs(results, per_query, details, out / "evaluation")
    report = {
        "status": "complete",
        "data_validation": validation,
        "graph_quality": quality.report,
        "ablation": results.to_dict("records"),
        "aws_actions_executed": [],
        "paid_api_calls": 0,
        "contains_test_jd_in_graph": False,
    }
    (out / "synthetic_demo_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), "utf-8"
    )
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="job-skill-graph",
        description="Train-only job Skill Graph and Learning-to-Rank workshop.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    profile_1111 = sub.add_parser("profile-1111-data")
    profile_1111.add_argument("--dataset", required=True)
    profile_1111.add_argument("--out")
    profile_1111.add_argument("--train-end", default="2026-06-05T00:00:00")
    profile_1111.add_argument(
        "--validation-end", default="2026-06-06T00:00:00"
    )
    profile_1111.add_argument("--test-end", default="2026-06-08T00:00:00")
    profile_1111.add_argument("--threads", type=int, default=4)
    profile_1111.add_argument("--memory-limit", default="8GB")
    profile_1111.set_defaults(func=command_profile_1111_data)

    prepare_1111 = sub.add_parser("prepare-1111-data")
    prepare_1111.add_argument("--dataset", required=True)
    prepare_1111.add_argument("--out", required=True)
    prepare_1111.add_argument("--train-end", default="2026-06-05T00:00:00")
    prepare_1111.add_argument(
        "--validation-end", default="2026-06-06T00:00:00"
    )
    prepare_1111.add_argument("--test-end", default="2026-06-08T00:00:00")
    prepare_1111.add_argument("--browse-window-minutes", type=int, default=60)
    prepare_1111.add_argument("--apply-window-hours", type=int, default=24)
    prepare_1111.add_argument("--negative-cap", type=int, default=20)
    prepare_1111.add_argument("--sample-queries", type=int)
    prepare_1111.add_argument(
        "--jobs-scope", choices=["all", "exposed"], default="all"
    )
    prepare_1111.add_argument(
        "--include-zero-positive-queries", action="store_true"
    )
    prepare_1111.add_argument("--allow-future-content", action="store_true")
    prepare_1111.add_argument("--threads", type=int, default=4)
    prepare_1111.add_argument("--memory-limit", default="8GB")
    prepare_1111.add_argument("--overwrite", action="store_true")
    prepare_1111.set_defaults(func=command_prepare_1111_data)

    search_index = sub.add_parser("build-search-index")
    search_index.add_argument("--jobs", required=True)
    search_index.add_argument("--out", required=True)
    search_index.add_argument("--limit", type=int)
    search_index.add_argument("--batch-size", type=int, default=2000)
    search_index.add_argument("--overwrite", action="store_true")
    search_index.set_defaults(func=command_build_search_index)

    structured = sub.add_parser("extract-structured-skills")
    structured.add_argument("--jobs", required=True)
    structured.add_argument("--out", required=True)
    structured.add_argument("--split", default="train")
    structured.add_argument("--limit", type=int)
    structured.add_argument("--batch-size", type=int, default=5000)
    structured.set_defaults(func=command_extract_structured_skills)

    serve = sub.add_parser("serve-api")
    serve.add_argument("--index", required=True)
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--result-limit", type=int, default=50)
    serve.add_argument("--graph")
    serve.add_argument("--model")
    serve.set_defaults(func=command_serve_api)

    validate = sub.add_parser("validate-data")
    validate.add_argument("--jobs", required=True)
    validate.add_argument("--queries", required=True)
    validate.add_argument("--labels", required=True)
    validate.add_argument("--columns", required=True)
    validate.add_argument("--train-cutoff")
    validate.add_argument("--out")
    validate.set_defaults(func=command_validate_data)

    prepare = sub.add_parser("prepare-extraction")
    prepare.add_argument("--jobs", required=True)
    prepare.add_argument("--columns", required=True)
    prepare.add_argument("--out", required=True)
    prepare.add_argument("--dry-run", action="store_true", default=True)
    prepare.set_defaults(func=command_prepare_extraction)

    imported = sub.add_parser("import-extraction")
    imported.add_argument("--input", required=True)
    imported.add_argument("--jobs", required=True)
    imported.add_argument("--columns", required=True)
    imported.add_argument("--out", required=True)
    imported.set_defaults(func=command_import_extraction)

    canonicalize = sub.add_parser("canonicalize")
    canonicalize.add_argument("--input", required=True)
    canonicalize.add_argument("--out", required=True)
    canonicalize.add_argument("--config", default=str(DEFAULT_SCHEMA))
    canonicalize.set_defaults(func=command_canonicalize)

    build_graph = sub.add_parser("build-graph")
    build_graph.add_argument("--jobs", required=True)
    build_graph.add_argument("--extractions", required=True)
    build_graph.add_argument("--out", required=True)
    build_graph.add_argument("--columns", default=str(DEFAULT_MAPPING))
    build_graph.add_argument("--config", default=str(DEFAULT_SCHEMA))
    build_graph.add_argument("--overwrite", action="store_true")
    build_graph.set_defaults(func=command_build_graph)

    validate_graph = sub.add_parser("validate-graph")
    validate_graph.add_argument("--graph", required=True)
    validate_graph.add_argument("--out", required=True)
    validate_graph.add_argument("--source-jobs")
    validate_graph.add_argument("--config", default=str(DEFAULT_SCHEMA))
    validate_graph.add_argument("--overwrite", action="store_true")
    validate_graph.set_defaults(func=command_validate_graph)

    features = sub.add_parser("build-features")
    features.add_argument("--graph", required=True)
    features.add_argument("--jobs", required=True)
    features.add_argument("--queries", required=True)
    features.add_argument("--labels", required=True)
    features.add_argument("--columns", default=str(DEFAULT_MAPPING))
    features.add_argument("--out", required=True)
    features.add_argument(
        "--candidate-only",
        action="store_true",
        help="Build only labeled/exposed query-job pairs; required for the real dataset.",
    )
    features.set_defaults(func=command_build_features)

    evaluate = sub.add_parser("evaluate-ablation")
    evaluate.add_argument("--features", required=True)
    evaluate.add_argument("--out", required=True)
    evaluate.add_argument("--seed", type=int, default=42)
    evaluate.set_defaults(func=command_evaluate_ablation)

    demo = sub.add_parser("synthetic-demo")
    demo.add_argument(
        "--out", default=str(PROJECT_ROOT / "outputs" / "synthetic_demo")
    )
    demo.add_argument("--overwrite", action="store_true")
    demo.set_defaults(func=command_synthetic_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.func(args)
    except Exception as error:
        parser.exit(2, f"error: {error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
