from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .graph_features import select_feature_set, tokenize, weighted_baseline_score
from .metrics import evaluate_rankings, paired_bootstrap_difference


class BM25:
    def __init__(self, documents: dict[str, str], *, k1: float = 1.5, b: float = 0.75):
        self.documents = {key: tokenize(value) for key, value in documents.items()}
        self.k1 = k1
        self.b = b
        self.average_length = (
            np.mean([len(tokens) for tokens in self.documents.values()])
            if self.documents
            else 0.0
        )
        self.document_frequency: Counter[str] = Counter()
        for tokens in self.documents.values():
            self.document_frequency.update(set(tokens))

    def score(self, query: str, document_id: str) -> float:
        tokens = self.documents[document_id]
        counts = Counter(tokens)
        score = 0.0
        total = len(self.documents)
        for term in tokenize(query):
            frequency = counts[term]
            if not frequency:
                continue
            document_frequency = self.document_frequency[term]
            inverse = np.log(1 + (total - document_frequency + 0.5) / (document_frequency + 0.5))
            denominator = frequency + self.k1 * (
                1 - self.b + self.b * len(tokens) / max(self.average_length, 1.0)
            )
            score += inverse * frequency * (self.k1 + 1) / denominator
        return float(score)


def add_bm25_scores(
    feature_frame: pd.DataFrame,
    queries: pd.DataFrame,
    jobs: pd.DataFrame,
) -> pd.DataFrame:
    output = feature_frame.copy()
    corpus = {
        str(row["job_id"]): " ".join(
            str(row[column]) for column in ("title", "requirements", "description")
        )
        for _, row in jobs.iterrows()
    }
    bm25 = BM25(corpus)
    query_lookup = {
        str(row["query_id"]): str(row["query"]) for _, row in queries.iterrows()
    }
    output["bm25_score"] = [
        bm25.score(query_lookup[str(row["query_id"])], str(row["job_id"]))
        for _, row in output.iterrows()
    ]
    return output


@dataclass
class LambdaMARTRanker:
    random_seed: int = 42
    n_estimators: int = 80
    learning_rate: float = 0.05
    num_leaves: int = 15
    model: object | None = None
    feature_names: list[str] | None = None

    @staticmethod
    def _sort_and_groups(frame: pd.DataFrame) -> tuple[pd.DataFrame, list[int]]:
        ordered = frame.sort_values(["query_id", "job_id"]).reset_index(drop=True)
        groups = ordered.groupby("query_id", sort=False).size().astype(int).tolist()
        return ordered, groups

    def fit(self, frame: pd.DataFrame, feature_names: list[str]) -> "LambdaMARTRanker":
        try:
            from lightgbm import LGBMRanker
        except ImportError as error:
            raise RuntimeError(
                "LightGBM is required for LambdaMART. Install the 'ltr' extra."
            ) from error
        required = {"query_id", "job_id", "relevance", *feature_names}
        if missing := required - set(frame.columns):
            raise ValueError(f"Training frame missing columns: {sorted(missing)}")
        ordered, groups = self._sort_and_groups(frame)
        self.feature_names = list(feature_names)
        self.model = LGBMRanker(
            objective="lambdarank",
            metric="ndcg",
            label_gain=[0, 1, 3],
            random_state=self.random_seed,
            n_estimators=self.n_estimators,
            learning_rate=self.learning_rate,
            num_leaves=self.num_leaves,
            n_jobs=1,
            verbosity=-1,
        )
        self.model.fit(
            ordered[self.feature_names],
            ordered["relevance"].astype(int),
            group=groups,
        )
        return self

    def predict(self, frame: pd.DataFrame) -> np.ndarray:
        if self.model is None or self.feature_names is None:
            raise RuntimeError("Ranker has not been fitted")
        return np.asarray(self.model.predict(frame[self.feature_names]), dtype=float)

    def feature_importance(self) -> pd.DataFrame:
        if self.model is None or self.feature_names is None:
            raise RuntimeError("Ranker has not been fitted")
        values = self.model.booster_.feature_importance(importance_type="gain")
        return pd.DataFrame(
            {"feature": self.feature_names, "gain": values.astype(float)}
        ).sort_values(["gain", "feature"], ascending=[False, True])

    def save(self, path: str | Path) -> tuple[Path, Path]:
        if self.model is None or self.feature_names is None:
            raise RuntimeError("Ranker has not been fitted")
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self.model.booster_.save_model(str(target))
        metadata = target.with_suffix(target.suffix + ".json")
        metadata.write_text(
            json.dumps(
                {
                    "model_type": "LightGBM LambdaMART",
                    "feature_names": self.feature_names,
                    "random_seed": self.random_seed,
                    "n_estimators": self.n_estimators,
                    "learning_rate": self.learning_rate,
                    "num_leaves": self.num_leaves,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return target, metadata

    @classmethod
    def load(cls, path: str | Path) -> "LambdaMARTRanker":
        try:
            import lightgbm as lgb
        except ImportError as error:
            raise RuntimeError("LightGBM is required to load the ranker") from error
        target = Path(path)
        metadata_path = target.with_suffix(target.suffix + ".json")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        instance = cls(
            random_seed=int(metadata["random_seed"]),
            n_estimators=int(metadata["n_estimators"]),
            learning_rate=float(metadata["learning_rate"]),
            num_leaves=int(metadata["num_leaves"]),
        )
        instance.feature_names = list(metadata["feature_names"])
        instance.model = lgb.Booster(model_file=str(target))
        return instance


def evaluate_ablation(
    feature_frame: pd.DataFrame,
    *,
    seed: int = 42,
    k: int = 10,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, object]]:
    required = {"query_id", "job_id", "relevance", "data_split"}
    if missing := required - set(feature_frame.columns):
        raise ValueError(f"Feature frame missing columns: {sorted(missing)}")
    train = feature_frame[feature_frame["data_split"].eq("train")].copy()
    test = feature_frame[feature_frame["data_split"].eq("test")].copy()
    if train.empty or test.empty:
        raise ValueError("Ablation requires both train and test query-label pairs")
    all_results: list[dict[str, object]] = []
    all_per_query: list[pd.DataFrame] = []
    scored_sets: dict[str, pd.DataFrame] = {}
    importances: dict[str, list[dict[str, object]]] = {}

    for name in ("A", "B", "C", "D"):
        features = select_feature_set(name)
        scored = test.copy()
        if name in {"A", "B"}:
            scored["score"] = weighted_baseline_score(scored, name)
            model_name = "deterministic_weighted_baseline"
        else:
            ranker = LambdaMARTRanker(random_seed=seed).fit(train, features)
            scored["score"] = ranker.predict(scored)
            model_name = "LightGBM LambdaMART"
            importances[name] = ranker.feature_importance().to_dict("records")
        metrics, per_query = evaluate_rankings(scored, k=k)
        all_results.append({"ablation": name, "model": model_name, **metrics})
        per_query["ablation"] = name
        all_per_query.append(per_query)
        scored_sets[name] = scored

    per_query_frame = pd.concat(all_per_query, ignore_index=True)
    baseline = per_query_frame[per_query_frame["ablation"].eq("C")]
    treatment = per_query_frame[per_query_frame["ablation"].eq("D")]
    bootstrap = paired_bootstrap_difference(
        baseline, treatment, metric=f"ndcg@{k}", seed=seed
    )
    comparison = baseline[["query_id", f"ndcg@{k}"]].merge(
        treatment[["query_id", f"ndcg@{k}"]],
        on="query_id",
        suffixes=("_C", "_D"),
        validate="one_to_one",
    )
    differences = comparison[f"ndcg@{k}_D"] - comparison[f"ndcg@{k}_C"]
    tolerance = 1e-12
    details: dict[str, object] = {
        "seed": seed,
        "feature_importance": importances,
        "paired_bootstrap_C_to_D": bootstrap,
        "graph_win_loss_tie": {
            "win": int((differences > tolerance).sum()),
            "loss": int((differences < -tolerance).sum()),
            "tie": int((differences.abs() <= tolerance).sum()),
        },
        "models": {
            "A": "BM25",
            "B": "BM25 + deterministic dense",
            "C": "LambdaMART without graph features",
            "D": "LambdaMART with graph features",
        },
    }
    return pd.DataFrame(all_results), per_query_frame, details


def save_ablation_outputs(
    results: pd.DataFrame,
    per_query: pd.DataFrame,
    details: dict[str, object],
    output_dir: str | Path,
) -> list[Path]:
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    paths = [
        target / "ablation_results.csv",
        target / "ablation_results.json",
        target / "ablation_report.md",
        target / "per_query_metrics.csv",
        target / "ablation_details.json",
    ]
    results.to_csv(paths[0], index=False)
    paths[1].write_text(
        json.dumps(results.to_dict("records"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    bootstrap = details["paired_bootstrap_C_to_D"]
    win_loss_tie = details["graph_win_loss_tie"]
    lines = [
        "# Learning-to-Rank Ablation Report",
        "",
        "| Ablation | Model | NDCG@10 | Precision@10 | Top-1 | MRR |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for row in results.to_dict("records"):
        lines.append(
            "| {ablation} | {model} | {ndcg:.6f} | {precision:.6f} | "
            "{top1:.6f} | {mrr:.6f} |".format(
                ablation=row["ablation"],
                model=row["model"],
                ndcg=float(row.get("ndcg@10", 0.0)),
                precision=float(row.get("precision@10", 0.0)),
                top1=float(row.get("top1", 0.0)),
                mrr=float(row.get("mrr", 0.0)),
            )
        )
    lines.extend(
        [
            "",
            "## Graph C → D comparison",
            "",
            f"- Mean NDCG@10 difference: {bootstrap['mean_difference']:.6f}",
            f"- 95% CI: [{bootstrap['ci_low']:.6f}, {bootstrap['ci_high']:.6f}]",
            f"- Win/loss/tie queries: {win_loss_tie['win']} / "
            f"{win_loss_tie['loss']} / {win_loss_tie['tie']}",
            "",
            "Behavior-derived relevance is weak supervision, not the official hidden "
            "evaluation ground truth.",
        ]
    )
    paths[2].write_text("\n".join(lines) + "\n", encoding="utf-8")
    per_query.to_csv(paths[3], index=False)
    paths[4].write_text(
        json.dumps(details, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return paths
