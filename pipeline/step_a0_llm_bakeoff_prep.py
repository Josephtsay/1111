"""
A0 — LLM bake-off 準備（不呼叫任何模型 API）
Role: A (Content Graph)
Playbook: Model Decision Contract — Gate 1 用 golden slice 比較候選模型

前置：
  python step1_train_data_prep.py
  → graph/golden_slice_ids.json + graph/golden_slice.csv

本腳本：
  1. 讀取固定 golden slice job IDs（seed=2026）
  2. 用 prompts/llm_extraction_v0.1.txt 渲染每筆 prompt
  3. 輸出可重現的 bake-off 包（供各 candidate 離線／批次打 API）
  4. 附上 assertion challenge set 路徑與 model_registry 指引

輸出（gitignored graph/）：
  graph/llm_bakeoff_v0.1/
    prompts.jsonl          # 每行 {job_id, prompt, source_fields, prompt_version}
    job_ids.json
    manifest.json
"""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent  # pipeline/ -> repo root; graph/ dataset/ fixtures/ 都掛在根目錄
GRAPH_DIR = ROOT / "graph"
GOLDEN_IDS = GRAPH_DIR / "golden_slice_ids.json"
GOLDEN_CSV = GRAPH_DIR / "golden_slice.csv"
PROMPT_TEMPLATE = ROOT / "prompts" / "llm_extraction_v0.1.txt"
MODEL_REGISTRY = ROOT / "configs" / "model_registry.yaml"
CHALLENGE_SET = ROOT / "fixtures" / "assertion_challenge_set_v0.1.jsonl"
OUTPUT_DIR = GRAPH_DIR / "llm_bakeoff_v0.1"

PROMPT_VERSION = "llm_extraction_v0.1"
BAKEOFF_VERSION = "v0.1"


def _sha256_file(path: Path) -> str | None:
    if not path.exists():
        return None
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_golden_ids(path: Path = GOLDEN_IDS) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(
            f"Missing {path}. Run: python step1_train_data_prep.py"
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return [str(x) for x in data]
    if isinstance(data, dict):
        for key in ("job_ids", "golden_slice_ids", "ids"):
            if key in data:
                return [str(x) for x in data[key]]
    raise ValueError(f"Unrecognized golden slice JSON shape: {path}")


def load_golden_jobs(csv_path: Path, job_ids: list[str]) -> dict[str, dict[str, str]]:
    """Load source fields from golden_slice.csv (produced by step1)."""
    if not csv_path.exists():
        raise FileNotFoundError(
            f"Missing {csv_path}. Run: python step1_train_data_prep.py"
        )
    want = set(job_ids)
    found: dict[str, dict[str, str]] = {}

    # Column names may be English aliases from step1 or original Chinese
    field_aliases = {
        "job_id": ["job_id", "職缺編號"],
        "title": ["title", "職務名稱", "job_title"],
        "description": ["description", "職務內容", "job_description"],
        "additional": [
            "additional_requirements",
            "additional_req",
            "附加條件",
            "additional",
            "requirements",
        ],
    }

    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError("golden_slice.csv has no header")
        cols = {name: name for name in reader.fieldnames}

        def pick(row: dict[str, str], logical: str) -> str:
            for alias in field_aliases[logical]:
                if alias in row and row[alias] is not None:
                    return str(row[alias])
            return ""

        for row in reader:
            jid = pick(row, "job_id").strip()
            if jid in want:
                found[jid] = {
                    "title": pick(row, "title"),
                    "description": pick(row, "description"),
                    "additional": pick(row, "additional"),
                }

    missing = [j for j in job_ids if j not in found]
    if missing:
        print(f"  [WARN] {len(missing)} golden IDs not found in CSV (e.g. {missing[:3]})")
    return found


def render_prompt(template: str, job_id: str, fields: dict[str, str]) -> str:
    return (
        template.replace("{{job_id}}", job_id)
        .replace("{{title}}", fields.get("title") or "")
        .replace("{{description}}", fields.get("description") or "")
        .replace("{{additional}}", fields.get("additional") or "")
    )


def prepare_bakeoff(
    *,
    limit: int | None = None,
    output_dir: Path = OUTPUT_DIR,
) -> dict[str, Any]:
    job_ids = load_golden_ids()
    if limit is not None:
        job_ids = job_ids[:limit]

    template = PROMPT_TEMPLATE.read_text(encoding="utf-8")
    jobs = load_golden_jobs(GOLDEN_CSV, job_ids)

    output_dir.mkdir(parents=True, exist_ok=True)
    prompts_path = output_dir / "prompts.jsonl"
    ids_path = output_dir / "job_ids.json"

    n_written = 0
    total_chars = 0
    with prompts_path.open("w", encoding="utf-8") as out:
        for job_id in job_ids:
            fields = jobs.get(job_id)
            if not fields:
                continue
            prompt = render_prompt(template, job_id, fields)
            record = {
                "job_id": job_id,
                "prompt_version": PROMPT_VERSION,
                "prompt": prompt,
                "source_fields": {
                    "職務名稱": fields["title"],
                    "職務內容": fields["description"],
                    "附加條件": fields["additional"],
                },
                "char_count": len(prompt),
            }
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
            n_written += 1
            total_chars += len(prompt)

    ids_path.write_text(
        json.dumps(job_ids, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    manifest = {
        "step": "step_a0_llm_bakeoff_prep",
        "bakeoff_version": BAKEOFF_VERSION,
        "prompt_version": PROMPT_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "golden_slice_ids": str(GOLDEN_IDS.resolve()),
        "golden_slice_csv": str(GOLDEN_CSV.resolve()),
        "prompt_template": str(PROMPT_TEMPLATE.resolve()),
        "model_registry": str(MODEL_REGISTRY.resolve()),
        "challenge_set": str(CHALLENGE_SET.resolve()),
        "output_dir": str(output_dir.resolve()),
        "statistics": {
            "requested_jobs": len(job_ids),
            "prompts_written": n_written,
            "avg_prompt_chars": (total_chars / n_written) if n_written else 0,
            "total_prompt_chars": total_chars,
        },
        "hashes": {
            "golden_slice_ids_sha256": _sha256_file(GOLDEN_IDS),
            "prompt_template_sha256": _sha256_file(PROMPT_TEMPLATE),
            "challenge_set_sha256": _sha256_file(CHALLENGE_SET),
        },
        "next_steps": [
            "選擇 configs/model_registry.yaml 中 unstructured_extraction.candidates",
            "對 graph/llm_bakeoff_v0.1/prompts.jsonl 逐筆呼叫模型（temperature=0）",
            "輸出必須通過 §3.4 契約；可用 assertion challenge set 另測 assertion_accuracy",
            "填回 model_registry metrics → Gate 1 review；Gate 2 前勿 freeze",
            "feature_flags.use_llm_extraction 保持 false 直到 bake-off 通過",
        ],
        "known_limitations": [
            "本腳本不呼叫 API、不產生 extractions",
            "LLM 輸出需另寫 parser 對齊 mention_id / offsets（可用現有 extraction.py 的 limited_json_repair 思路）",
            "舊 models.JobSkillExtraction schema 與 playbook §3.4 不同；bake-off 以 §3.4 為準",
        ],
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="A0: Prepare LLM bake-off prompts from step1 golden slice"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only first N golden IDs (smoke). Default: all 500",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=OUTPUT_DIR,
    )
    args = parser.parse_args()

    print("=" * 60)
    print("A0 — LLM bake-off prep (no API calls)")
    print("=" * 60)
    print(f"  Golden IDs: {GOLDEN_IDS}")
    print(f"  Prompt:     {PROMPT_TEMPLATE}")
    print(f"  Registry:   {MODEL_REGISTRY}")
    if args.limit:
        print(f"  Limit:      {args.limit}")
    print()

    manifest = prepare_bakeoff(limit=args.limit, output_dir=args.output_dir)
    stats = manifest["statistics"]
    print(f"  Prompts written: {stats['prompts_written']:,} / {stats['requested_jobs']:,}")
    print(f"  Avg prompt chars: {stats['avg_prompt_chars']:.0f}")
    print(f"  Output: {manifest['output_dir']}")
    print("\n  Next:")
    for step in manifest["next_steps"]:
        print(f"    - {step}")
    print("\n✓ A0 bake-off package ready.")


if __name__ == "__main__":
    main()
