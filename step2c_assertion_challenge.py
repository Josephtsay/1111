"""
Step 2C — Assertion / extraction challenge set
Role: A (Content Graph)
Playbook: docs/SKILL_GRAPH_PLAYBOOK.md §2C

交付：
  1. 版本化 challenge set（fixtures/assertion_challenge_set_v0.1.jsonl）
  2. 規則 assertion / requirement detector（assertion_detection.py）
  3. 評估報告 graph/assertion_challenge_report.json
     - assertion_accuracy（依 category 分開）
     - requirement_accuracy
     - protected_pair_error
     - abbreviation_resolution
  4. 可選：把 detector 套用到 phrase extractions / 最終 extractions.jsonl

政策（playbook）：
  若否定／不確定偵測未達門檻，非結構化 mention 不得預設全部 affirmed 入圖。
  本腳本會在 report 寫入 gate 建議（pass / quarantine_phrase）。
"""

from __future__ import annotations

import csv
import json
import shutil
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from assertion_detection import detect_assertion_and_requirement
from step3_canonicalization import (
    CanonicalRegistry,
    normalize_key,
    parse_canonical_candidate,
)

# ─────────────────────────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────────────────────────

ROOT = Path(__file__).parent
FIXTURE_SET = ROOT / "fixtures" / "assertion_challenge_set_v0.1.jsonl"
GRAPH_DIR = ROOT / "graph"
GRAPH_SET = GRAPH_DIR / "assertion_challenge_set_v0.1.jsonl"
REPORT_PATH = GRAPH_DIR / "assertion_challenge_report.json"
EXTRACTIONS_JSONL = GRAPH_DIR / "extractions.jsonl"
EXTRACTIONS_PHRASE = GRAPH_DIR / "extractions_phrase.jsonl"
SKILL_DICT = GRAPH_DIR / "skill_dictionary.csv"
ALIAS_DICT = GRAPH_DIR / "alias_dictionary.csv"
EXTRACTION_CONFIG = GRAPH_DIR / "extraction_config.yaml"

CHALLENGE_VERSION = "v0.1"
# Gate 1 建議門檻（可於 extraction_config 覆寫）
DEFAULT_MIN_ASSERTION_ACCURACY = 0.85
DEFAULT_MIN_PROTECTED_PAIR_ACCURACY = 1.0  # protected-pair error 必須為 0


# ─────────────────────────────────────────────────────────────────────────────
# IO
# ─────────────────────────────────────────────────────────────────────────────

def load_challenge_set(path: Path = FIXTURE_SET) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def sync_fixture_to_graph() -> None:
    GRAPH_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(FIXTURE_SET, GRAPH_SET)


def load_registry() -> CanonicalRegistry:
    """Rebuild a lightweight registry view from exported dictionaries."""
    reg = CanonicalRegistry()
    if SKILL_DICT.exists():
        with SKILL_DICT.open("r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                reg.skills[row["registry_key"]] = {
                    "registry_key": row["registry_key"],
                    "canonical_name": row["canonical_name"],
                    "skill_kind": row.get("skill_kind", "technical"),
                    "dictionary_version": row.get("dictionary_version", "v0.1"),
                    "source": row.get("source", "dictionary"),
                }
    if ALIAS_DICT.exists():
        with ALIAS_DICT.open("r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("entity_type") != "skill":
                    continue
                if row.get("ambiguity_status") == "ambiguous":
                    continue
                parsed = parse_canonical_candidate(row["canonical_id"])
                if not parsed:
                    continue
                _, key = parsed
                reg.skill_aliases[row["alias_key"]] = key
                reg.alias_meta.append(row)
    return reg


def resolve_canonical(reg: CanonicalRegistry, mention: str) -> str:
    key = normalize_key(mention)
    if key in reg.skill_aliases:
        key = reg.skill_aliases[key]
    if key in reg.skills:
        return f"skill:{key}"
    # prefix match for structured keys like react(reactjs
    for existing in reg.skills:
        if existing == key or existing.startswith(key + "(") or key.startswith(existing + "("):
            return f"skill:{existing}"
    return f"skill:{key}"


# ─────────────────────────────────────────────────────────────────────────────
# Evaluation
# ─────────────────────────────────────────────────────────────────────────────

def evaluate_challenge_set(
    challenges: list[dict[str, Any]],
    *,
    min_assertion_accuracy: float = DEFAULT_MIN_ASSERTION_ACCURACY,
    min_protected_pair_accuracy: float = DEFAULT_MIN_PROTECTED_PAIR_ACCURACY,
) -> dict[str, Any]:
    reg = load_registry()

    assertion_cases = 0
    assertion_correct = 0
    requirement_cases = 0
    requirement_correct = 0
    protected_cases = 0
    protected_correct = 0
    abbr_cases = 0
    abbr_correct = 0

    by_category: dict[str, Counter[str]] = defaultdict(Counter)
    failures: list[dict[str, Any]] = []

    for ch in challenges:
        cat = ch["category"]
        mention = ch["skill_mention"]
        context = ch.get("context", "")

        # --- assertion / requirement via detector ---
        if "expected_assertion_status" in ch or "expected_requirement_level" in ch:
            det = detect_assertion_and_requirement(
                context,
                mention,
                source_field=ch.get("source_field", ""),
            )
            if "expected_assertion_status" in ch:
                assertion_cases += 1
                ok = det["assertion_status"] == ch["expected_assertion_status"]
                if ok:
                    assertion_correct += 1
                    by_category[cat]["assertion_pass"] += 1
                else:
                    by_category[cat]["assertion_fail"] += 1
                    failures.append({
                        "challenge_id": ch["challenge_id"],
                        "category": cat,
                        "metric": "assertion_status",
                        "expected": ch["expected_assertion_status"],
                        "predicted": det["assertion_status"],
                        "cues": det.get("cues_fired", []),
                        "notes": ch.get("notes", ""),
                    })
            if "expected_requirement_level" in ch:
                requirement_cases += 1
                ok = det["requirement_level"] == ch["expected_requirement_level"]
                if ok:
                    requirement_correct += 1
                    by_category[cat]["requirement_pass"] += 1
                else:
                    by_category[cat]["requirement_fail"] += 1
                    failures.append({
                        "challenge_id": ch["challenge_id"],
                        "category": cat,
                        "metric": "requirement_level",
                        "expected": ch["expected_requirement_level"],
                        "predicted": det["requirement_level"],
                        "cues": det.get("cues_fired", []),
                        "notes": ch.get("notes", ""),
                    })

        # --- protected pairs ---
        if cat == "protected_pairs":
            protected_cases += 1
            got = resolve_canonical(reg, mention)
            expected = ch.get("expected_canonical")
            other = ch.get("must_not_merge_with")
            ok = True
            reasons: list[str] = []
            if expected and got != expected:
                # allow alias-equivalent if both resolve to same final id
                if resolve_canonical(reg, expected.replace("skill:", "")) != got:
                    ok = False
                    reasons.append(f"canonical {got} != {expected}")
            if other:
                other_resolved = other
                # if other is a skill: id, compare directly
                if got == other_resolved:
                    ok = False
                    reasons.append(f"merged with {other}")
                # also ensure alias map doesn't point mention → other key
                key = normalize_key(mention)
                alias_target = reg.skill_aliases.get(key)
                other_key = other.replace("skill:", "")
                if alias_target and alias_target == other_key:
                    ok = False
                    reasons.append(f"alias maps to {other}")
            if ok:
                protected_correct += 1
                by_category[cat]["protected_pass"] += 1
            else:
                by_category[cat]["protected_fail"] += 1
                failures.append({
                    "challenge_id": ch["challenge_id"],
                    "category": cat,
                    "metric": "protected_pair",
                    "expected": expected,
                    "predicted": got,
                    "must_not_merge_with": other,
                    "reasons": reasons,
                    "notes": ch.get("notes", ""),
                })

        # --- abbreviation resolution ---
        if cat == "abbreviation" and ch.get("expected_canonical"):
            abbr_cases += 1
            got = resolve_canonical(reg, mention)
            expected = ch["expected_canonical"]
            ok = got == expected
            if not ok:
                # accept if alias resolves expected key equivalently
                ok = resolve_canonical(reg, expected.replace("skill:", "")) == got and got.startswith("skill:")
                if expected.replace("skill:", "") in reg.skills or got == expected:
                    ok = got == expected
            if ok:
                abbr_correct += 1
                by_category[cat]["abbr_pass"] += 1
            else:
                by_category[cat]["abbr_fail"] += 1
                failures.append({
                    "challenge_id": ch["challenge_id"],
                    "category": cat,
                    "metric": "abbreviation",
                    "expected": expected,
                    "predicted": got,
                    "notes": ch.get("notes", ""),
                })

    def _rate(num: int, den: int) -> float | None:
        return (num / den) if den else None

    assertion_acc = _rate(assertion_correct, assertion_cases)
    requirement_acc = _rate(requirement_correct, requirement_cases)
    protected_acc = _rate(protected_correct, protected_cases)
    abbr_acc = _rate(abbr_correct, abbr_cases)
    protected_pair_error = (
        (protected_cases - protected_correct) / protected_cases if protected_cases else 0.0
    )

    assertion_pass = (
        assertion_acc is not None and assertion_acc >= min_assertion_accuracy
    )
    protected_pass = (
        protected_acc is not None and protected_acc >= min_protected_pair_accuracy
    )

    # confusion 類別失敗不單獨擋 gate，但要揭露
    confusion_fail = by_category.get("confusion", Counter()).get("assertion_fail", 0)

    if assertion_pass and protected_pass:
        gate = "pass"
        policy = (
            "assertion detector meets minimum; phrase mentions may keep "
            "detector-assigned assertion_status (negated/uncertain not materialized by B)"
        )
    else:
        gate = "quarantine_phrase_recommended"
        policy = (
            "playbook §2C: negation/uncertain detection below threshold — "
            "unstructured phrase mentions should be quarantined (or detector-applied) "
            "rather than default-all-affirmed into the graph"
        )

    report = {
        "step": "step2c_assertion_challenge",
        "challenge_set_version": CHALLENGE_VERSION,
        "challenge_set_path": str(FIXTURE_SET),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "detector_version": "rules_v0.1",
        "thresholds": {
            "minimum_assertion_accuracy": min_assertion_accuracy,
            "minimum_protected_pair_accuracy": min_protected_pair_accuracy,
        },
        "metrics": {
            "assertion_accuracy": assertion_acc,
            "assertion_correct": assertion_correct,
            "assertion_total": assertion_cases,
            "requirement_accuracy": requirement_acc,
            "requirement_correct": requirement_correct,
            "requirement_total": requirement_cases,
            "protected_pair_accuracy": protected_acc,
            "protected_pair_error": protected_pair_error,
            "protected_correct": protected_correct,
            "protected_total": protected_cases,
            "abbreviation_accuracy": abbr_acc,
            "abbreviation_correct": abbr_correct,
            "abbreviation_total": abbr_cases,
        },
        "by_category": {k: dict(v) for k, v in sorted(by_category.items())},
        "gate": gate,
        "policy_recommendation": policy,
        "known_limitations": [
            "規則偵測器無法可靠處理語意混淆（寵物 Python、旅遊 Java 島）",
            "requirement cue「佳」可能在少數無相關上下文誤觸發",
            "abbreviation 解析依賴 Step 3 alias/registry；缺 seed 時會 fail",
            "結構化欄位仍預設 affirmed/unspecified（不跑否定窗口）",
        ],
        "confusion_assertion_failures": confusion_fail,
        "failure_count": len(failures),
        "failures": failures[:80],
        "challenge_count": len(challenges),
        "category_counts": dict(Counter(c["category"] for c in challenges)),
    }
    return report


# ─────────────────────────────────────────────────────────────────────────────
# Apply detector to extractions
# ─────────────────────────────────────────────────────────────────────────────

def apply_detector_to_jsonl(
    input_path: Path,
    output_path: Path | None = None,
    *,
    methods: set[str] | None = None,
) -> dict[str, int]:
    """
    Re-annotate unstructured mentions using evidence as context window.
    For stronger context, prefer integrating detector in Step 2B (has full field text).
    """
    methods = methods or {"phrase"}
    output_path = output_path or input_path
    stats = Counter()

    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    with input_path.open("r", encoding="utf-8") as src, tmp_path.open(
        "w", encoding="utf-8"
    ) as dst:
        for line in src:
            if not line.strip():
                continue
            record = json.loads(line)
            for bucket in ("skills", "credentials"):
                for mention in record.get(bucket, []):
                    if mention.get("method") not in methods:
                        continue
                    stats["considered"] += 1
                    before_a = mention.get("assertion_status")
                    before_r = mention.get("requirement_level")
                    context = mention.get("evidence") or mention.get("raw_mention") or ""
                    det = detect_assertion_and_requirement(
                        context,
                        mention.get("raw_mention", ""),
                        source_field=mention.get("source_field", ""),
                    )
                    # evidence-only window 太短時，否定 cue 常不在 evidence 內。
                    # 若 evidence 本身就是 mention，保持原值，避免假陰性大面積改寫。
                    if context.casefold() == (mention.get("raw_mention") or "").casefold():
                        stats["skipped_evidence_equals_mention"] += 1
                        continue
                    mention["assertion_status"] = det["assertion_status"]
                    mention["requirement_level"] = det["requirement_level"]
                    if before_a != mention["assertion_status"]:
                        stats[f"assertion:{before_a}->{mention['assertion_status']}"] += 1
                    if before_r != mention["requirement_level"]:
                        stats[f"requirement:{before_r}->{mention['requirement_level']}"] += 1
                    stats["updated"] += 1
            dst.write(json.dumps(record, ensure_ascii=False) + "\n")
    tmp_path.replace(output_path)
    return dict(stats)


def update_extraction_config(report: dict[str, Any]) -> None:
    """Patch graph/extraction_config.yaml assertion_challenge fields if file exists."""
    if not EXTRACTION_CONFIG.exists():
        return
    import re

    text = EXTRACTION_CONFIG.read_text(encoding="utf-8")
    text = re.sub(
        r'(assertion_challenge:\n(?:[^\n]*\n)*?  version:\s*).*',
        rf'\1"{CHALLENGE_VERSION}"',
        text,
        count=1,
    )
    text = re.sub(
        r"(minimum_pass_rate:\s*).*",
        rf"\g<1>{DEFAULT_MIN_ASSERTION_ACCURACY}",
        text,
        count=1,
    )
    acc = report["metrics"]["assertion_accuracy"]
    ppe = report["metrics"]["protected_pair_error"]
    gate = report["gate"]
    if re.search(r"last_report_gate:", text):
        text = re.sub(
            r"(last_report_gate:\s*).*",
            rf'\1"{gate}"  # step2c',
            text,
            count=1,
        )
    else:
        text = text.replace(
            f'version: "{CHALLENGE_VERSION}"',
            f'version: "{CHALLENGE_VERSION}"\n'
            f'  last_report_gate: "{gate}"\n'
            f"  last_assertion_accuracy: {acc}\n"
            f"  last_protected_pair_error: {ppe}",
            1,
        )
    EXTRACTION_CONFIG.write_text(text, encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Step 2C: Assertion challenge set")
    parser.add_argument(
        "--challenge-set",
        type=Path,
        default=FIXTURE_SET,
        help="Path to challenge set JSONL",
    )
    parser.add_argument(
        "--min-assertion-accuracy",
        type=float,
        default=DEFAULT_MIN_ASSERTION_ACCURACY,
    )
    parser.add_argument(
        "--apply-phrase",
        action="store_true",
        help="Apply detector to graph/extractions_phrase.jsonl (evidence-window; prefer 2B integration)",
    )
    parser.add_argument(
        "--apply-extractions",
        action="store_true",
        help="Apply detector to graph/extractions.jsonl phrase mentions",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("Step 2C — Assertion / extraction challenge set")
    print("=" * 60)

    challenges = load_challenge_set(args.challenge_set)
    sync_fixture_to_graph()
    print(f"  Challenge set: {args.challenge_set} ({len(challenges)} cases)")

    report = evaluate_challenge_set(
        challenges,
        min_assertion_accuracy=args.min_assertion_accuracy,
    )
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    update_extraction_config(report)

    metrics = report["metrics"]
    print("\n  METRICS")
    print(
        f"    assertion_accuracy:     {metrics['assertion_accuracy']:.3f} "
        f"({metrics['assertion_correct']}/{metrics['assertion_total']})"
        if metrics["assertion_accuracy"] is not None
        else "    assertion_accuracy:     n/a"
    )
    print(
        f"    requirement_accuracy:   {metrics['requirement_accuracy']:.3f} "
        f"({metrics['requirement_correct']}/{metrics['requirement_total']})"
        if metrics["requirement_accuracy"] is not None
        else "    requirement_accuracy:   n/a"
    )
    print(
        f"    protected_pair_error:   {metrics['protected_pair_error']:.3f} "
        f"(acc={metrics['protected_pair_accuracy']:.3f})"
        if metrics["protected_pair_accuracy"] is not None
        else "    protected_pair_error:   n/a"
    )
    print(
        f"    abbreviation_accuracy:  {metrics['abbreviation_accuracy']:.3f} "
        f"({metrics['abbreviation_correct']}/{metrics['abbreviation_total']})"
        if metrics["abbreviation_accuracy"] is not None
        else "    abbreviation_accuracy:  n/a"
    )
    print(f"\n  GATE: {report['gate']}")
    print(f"  Policy: {report['policy_recommendation']}")
    print(f"  Failures listed: {min(len(report['failures']), 80)} (of {report['failure_count']})")
    print(f"  Report: {REPORT_PATH}")

    if args.apply_phrase and EXTRACTIONS_PHRASE.exists():
        print("\n  Applying detector to extractions_phrase.jsonl ...")
        stats = apply_detector_to_jsonl(EXTRACTIONS_PHRASE)
        print(f"    {stats}")
    if args.apply_extractions and EXTRACTIONS_JSONL.exists():
        print("\n  Applying detector to extractions.jsonl ...")
        stats = apply_detector_to_jsonl(EXTRACTIONS_JSONL)
        print(f"    {stats}")

    print("\n✓ Step 2C evaluate complete.")


if __name__ == "__main__":
    main()
