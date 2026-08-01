"""
Step 9 — Ablation & Evaluation Harness
Role: B (Structure Graph) / 共同
Playbook: §Step 9

Runs the ablation matrix (B0/G1/G2) and produces a comparison report.
Also supports single-run evaluation for development iterations.

Ablation Matrix:
  B0: --no-graph                     (baseline, BM25-only)
  G1: (default)                      (graph, no LLM classification)
  G2: --use-llm-classification       (graph + LLM skill classification)

Usage:
  python step9_ablation.py --ablation              # run all 3 configs
  python step9_ablation.py --run B0 --eval         # single baseline run
  python step9_ablation.py --run G2 --eval         # single classification run
  python step9_ablation.py --compare               # compare existing reports
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

GRAPH_DIR = Path(__file__).parent / "graph"

# Ablation matrix definition
ABLATION_MATRIX = {
    "B0": {
        "description": "Baseline: no graph (BM25-only)",
        "argv": ["--no-graph", "--eval", "--run-id", "B0"],
    },
    "G1": {
        "description": "Graph ON, LLM classification OFF (blacklist v0.2 or empty)",
        "argv": ["--eval", "--run-id", "G1"],
    },
    "G2": {
        "description": "Graph ON, LLM classification ON (blacklist v0.3 + skill_kind)",
        "argv": ["--use-llm-classification", "--eval", "--run-id", "G2"],
    },
}


def run_single(run_id: str, extra_argv: list[str] | None = None) -> dict[str, Any]:
    """Run a single ablation configuration."""
    from step8_retrieval_smoke import main as step8_main, parse_step8_args

    matrix_entry = ABLATION_MATRIX.get(run_id)
    if matrix_entry:
        argv = matrix_entry["argv"]
    else:
        argv = ["--eval", "--run-id", run_id] + (extra_argv or [])

    print(f"\n{'═' * 60}")
    print(f"  ABLATION RUN: {run_id}")
    if matrix_entry:
        print(f"  {matrix_entry['description']}")
    print(f"{'═' * 60}")

    step8_main(argv)

    # Load the produced eval report
    eval_path = GRAPH_DIR / f"eval_report_{run_id}.json"
    if eval_path.exists():
        return json.loads(eval_path.read_text(encoding="utf-8"))
    return {"error": f"eval report not found: {eval_path}"}


def compare_reports() -> dict[str, Any]:
    """Load all eval_report_*.json and produce a comparison table."""
    reports: dict[str, dict] = {}
    for path in sorted(GRAPH_DIR.glob("eval_report_*.json")):
        run_id = path.stem.replace("eval_report_", "")
        reports[run_id] = json.loads(path.read_text(encoding="utf-8"))

    if not reports:
        print("  No eval reports found in graph/")
        return {"error": "no reports"}

    # Extract metrics for comparison
    comparison: dict[str, Any] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "runs": {},
    }

    metric_names = ["ndcg@10", "precision@10", "mrr", "hit@10", "hit@1", "top1", "query_count"]

    print(f"\n{'═' * 70}")
    print("  ABLATION COMPARISON")
    print(f"{'═' * 70}")

    # Header
    header = f"  {'Run':<8}" + "".join(f"{m:<14}" for m in metric_names)
    print(header)
    print("  " + "─" * (len(header) - 2))

    for run_id, report in sorted(reports.items()):
        metrics = report.get("metrics", {})
        flags = report.get("feature_flags", {})
        row = f"  {run_id:<8}"
        for m in metric_names:
            val = metrics.get(m, 0)
            if m == "query_count":
                row += f"{int(val):<14}"
            else:
                row += f"{val:<14.4f}"
        print(row)

        comparison["runs"][run_id] = {
            "feature_flags": flags,
            "metrics": {m: metrics.get(m, 0) for m in metric_names},
            "description": ABLATION_MATRIX.get(run_id, {}).get("description", ""),
        }

    # Compute deltas if B0 and G1/G2 exist
    if "B0" in reports and len(reports) > 1:
        b0_metrics = reports["B0"].get("metrics", {})
        print(f"\n  {'─' * 50}")
        print("  Deltas vs B0:")
        for run_id, report in sorted(reports.items()):
            if run_id == "B0":
                continue
            metrics = report.get("metrics", {})
            deltas = {}
            row = f"    {run_id} - B0:  "
            for m in ["ndcg@10", "mrr", "hit@10"]:
                delta = metrics.get(m, 0) - b0_metrics.get(m, 0)
                deltas[m] = delta
                sign = "+" if delta >= 0 else ""
                row += f"{m}={sign}{delta:.4f}  "
            print(row)
            comparison["runs"][run_id]["deltas_vs_B0"] = deltas

    print(f"\n{'═' * 70}")

    # Save comparison
    comparison_path = GRAPH_DIR / "ablation_comparison.json"
    comparison_path.write_text(
        json.dumps(comparison, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"  Comparison saved: {comparison_path}")

    return comparison


def main() -> None:
    parser = argparse.ArgumentParser(description="Step 9 — Ablation & Evaluation")
    parser.add_argument("--ablation", action="store_true", help="Run full ablation matrix (B0, G1, G2)")
    parser.add_argument("--run", type=str, default=None, help="Run a single config (B0, G1, G2, or custom)")
    parser.add_argument("--compare", action="store_true", help="Compare existing eval reports")
    parser.add_argument("--eval-split", type=str, default="test", help="Data split for evaluation")
    parser.add_argument("--eval-limit", type=int, default=None, help="Limit eval queries")
    args = parser.parse_args()

    if args.compare:
        compare_reports()
        return

    if args.ablation:
        print("=" * 60)
        print("Step 9 — Full Ablation Matrix")
        print("=" * 60)
        t0 = time.time()

        for run_id in ["B0", "G1", "G2"]:
            extra = []
            if args.eval_split != "test":
                extra += ["--eval-split", args.eval_split]
            if args.eval_limit:
                extra += ["--eval-limit", str(args.eval_limit)]

            matrix_argv = ABLATION_MATRIX[run_id]["argv"] + extra
            from step8_retrieval_smoke import main as step8_main
            step8_main(matrix_argv)

        total = time.time() - t0
        print(f"\n  All ablation runs done in {total:.1f}s")
        compare_reports()
        return

    if args.run:
        extra = []
        if args.eval_split != "test":
            extra += ["--eval-split", args.eval_split]
        if args.eval_limit:
            extra += ["--eval-limit", str(args.eval_limit)]

        run_id = args.run
        if run_id in ABLATION_MATRIX:
            matrix_argv = ABLATION_MATRIX[run_id]["argv"] + extra
            from step8_retrieval_smoke import main as step8_main
            step8_main(matrix_argv)
        else:
            argv = ["--eval", "--run-id", run_id] + extra
            from step8_retrieval_smoke import main as step8_main
            step8_main(argv)
        return

    # Default: show help
    parser.print_help()


if __name__ == "__main__":
    main()
