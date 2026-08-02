"""
Step 3B — Derive skill aliases from parenthetical abbreviations
Role: A (alias content) / executed by B for retrieval work
Playbook: §Step 3 (alias dictionary)

Why this exists as a separate entry point:
  The alias derivation logic lives in step3_canonicalization (single source of
  truth). But a full Step 3 re-run reads graph/extractions.jsonl (1.1 GB) and
  changes registry_key output, which per the re-run decision table forces
  Step 4→8 to be rebuilt as well.

  Aliases are different: graph/alias_dictionary.csv is consumed *only* by the
  query resolver in Step 8. Step 4 (edge assembly), Step 5 (statistics) and
  Step 6 (node/edge export) never read it. So aliases can be regenerated from
  the already-exported skill_dictionary.csv without touching the graph at all.

  Verified before writing: build_nodes()/merge_edges() in step6_graph_export
  read train_jobs / skill_dictionary / credential_dictionary /
  occupation_hierarchy / global_job_frequency — no alias input.

What it does:
  1. Reads graph/skill_dictionary.csv as the registry
  2. Derives alias candidates via step3_canonicalization.derive_paren_aliases
  3. Two-pass collision detection: an alias pointing at >1 skill is written as
     ambiguous (Step 8 loads only ambiguity_status == "unique")
  4. Preserves existing non-derived rows (manual / seed) already in
     alias_dictionary.csv; only rows with source == "derived_paren" are replaced
  5. Backs up the previous file before writing

Usage:
  python step3b_derive_aliases.py --dry-run
  python step3b_derive_aliases.py
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from step3_canonicalization import (
    derive_paren_aliases,
    is_meaningful_alias_key,
    normalize_key,
    sanitize_registry_key,
    _guess_language,
)

ROOT = Path(__file__).resolve().parent.parent  # pipeline/ -> repo root; graph/ dataset/ fixtures/ 都掛在根目錄
GRAPH_DIR = ROOT / "graph"

SKILL_DICT = GRAPH_DIR / "skill_dictionary.csv"
ALIAS_DICT = GRAPH_DIR / "alias_dictionary.csv"
ALIAS_BACKUP = GRAPH_DIR / "alias_dictionary_pre_derived.csv"
MANIFEST_OUT = GRAPH_DIR / "alias_derivation_manifest.json"

DERIVED_SOURCE = "derived_paren"
FIELDNAMES = [
    "alias_key", "raw_alias", "entity_type", "canonical_id",
    "source", "language", "ambiguity_status", "dictionary_version",
]


def load_registry() -> tuple[dict[str, str], str]:
    """registry_key → canonical_name. Keys are sanitized to match graph IDs."""
    if not SKILL_DICT.exists():
        raise SystemExit(f"missing {SKILL_DICT}")
    registry: dict[str, str] = {}
    version = "v0.1"
    with SKILL_DICT.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            key = sanitize_registry_key((row.get("registry_key") or "").strip())
            if not key:
                continue
            registry[key] = (row.get("canonical_name") or key).strip()
            version = (row.get("dictionary_version") or version).strip() or version
    return registry, version


def load_existing_aliases() -> list[dict[str, str]]:
    if not ALIAS_DICT.exists():
        return []
    with ALIAS_DICT.open("r", encoding="utf-8-sig", newline="") as f:
        return [dict(r) for r in csv.DictReader(f)]


def derive(registry: dict[str, str]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    candidates: dict[str, set[str]] = {}
    raw_forms: dict[str, str] = {}
    for canonical_key, canonical_name in registry.items():
        for raw_alias in derive_paren_aliases(canonical_name):
            alias_key = normalize_key(raw_alias)
            if not alias_key or alias_key == canonical_key:
                continue
            if not is_meaningful_alias_key(alias_key):
                continue
            candidates.setdefault(alias_key, set()).add(canonical_key)
            raw_forms.setdefault(alias_key, raw_alias)

    rows: list[dict[str, Any]] = []
    stats = Counter()
    for alias_key, targets in sorted(candidates.items()):
        # An alias that is itself a distinct registry entry must not collapse
        # that entry into another skill.
        if alias_key in registry and alias_key not in targets:
            stats["skipped_is_registry_entry"] += 1
            continue
        status = "unique" if len(targets) == 1 else "ambiguous"
        for canonical_key in sorted(targets):
            rows.append({
                "alias_key": alias_key,
                "raw_alias": raw_forms[alias_key],
                "entity_type": "skill",
                "canonical_id": f"skill:{canonical_key}",
                "source": DERIVED_SOURCE,
                "language": _guess_language(alias_key),
                "ambiguity_status": status,
                "dictionary_version": "",
            })
        stats["unique" if status == "unique" else "ambiguous"] += 1
    return rows, dict(stats)


def main() -> int:
    parser = argparse.ArgumentParser(description="Derive skill aliases from parentheticals")
    parser.add_argument("--dry-run", action="store_true", help="Report only, do not write")
    args = parser.parse_args()

    print("=" * 60)
    print("Step 3B — Derive skill aliases from parentheticals")
    print("=" * 60)

    registry, version = load_registry()
    print(f"  Registry skills: {len(registry):,}  (dictionary_version={version})")

    derived_rows, stats = derive(registry)
    for row in derived_rows:
        row["dictionary_version"] = version

    unique_rows = [r for r in derived_rows if r["ambiguity_status"] == "unique"]
    ambiguous_keys = sorted({
        r["alias_key"] for r in derived_rows if r["ambiguity_status"] == "ambiguous"
    })

    print(f"  Derived alias keys : unique={stats.get('unique', 0)}, "
          f"ambiguous={stats.get('ambiguous', 0)}, "
          f"skipped_is_registry_entry={stats.get('skipped_is_registry_entry', 0)}")
    print(f"  Usable (unique) rows: {len(unique_rows):,}")
    if ambiguous_keys:
        print(f"  Ambiguous (left unbound): {ambiguous_keys}")

    existing = load_existing_aliases()
    preserved = [r for r in existing if r.get("source") != DERIVED_SOURCE]
    print(f"  Existing alias rows: {len(existing):,} "
          f"(preserving {len(preserved):,} non-derived)")

    # Do not let a derived row shadow a manually curated binding.
    manual_keys = {r.get("alias_key") for r in preserved}
    derived_final = [r for r in derived_rows if r["alias_key"] not in manual_keys]
    shadowed = len(derived_rows) - len(derived_final)
    if shadowed:
        print(f"  Derived rows deferring to manual bindings: {shadowed}")

    merged = preserved + derived_final
    seen: set[tuple[str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for row in merged:
        key = (row.get("alias_key", ""), row.get("canonical_id", ""))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)

    final_unique = sum(1 for r in deduped if r.get("ambiguity_status") == "unique")
    print(f"\n  Final alias_dictionary: {len(deduped):,} rows, {final_unique:,} unique")
    print(f"  Step 8 usable aliases: {len(existing and [r for r in existing if r.get('ambiguity_status') == 'unique']):,}"
          f" → {final_unique:,}")

    if args.dry_run:
        print("\n  (dry-run — nothing written)")
        sample = [r for r in derived_final if r["ambiguity_status"] == "unique"][:15]
        print("  Sample derived bindings:")
        for r in sample:
            print(f"    {r['alias_key']!r:<28} → {r['canonical_id']}")
        return 0

    if ALIAS_DICT.exists():
        shutil.copy2(ALIAS_DICT, ALIAS_BACKUP)
        print(f"  Backup: {ALIAS_BACKUP}")

    with ALIAS_DICT.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        for row in sorted(deduped, key=lambda r: (r.get("alias_key", ""), r.get("canonical_id", ""))):
            writer.writerow(row)
    print(f"  Wrote: {ALIAS_DICT}")

    MANIFEST_OUT.write_text(json.dumps({
        "step": "step3b_derive_aliases",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dictionary_version": version,
        "registry_skills": len(registry),
        "derived": stats,
        "ambiguous_alias_keys": ambiguous_keys,
        "rows_preserved_non_derived": len(preserved),
        "rows_deferred_to_manual": shadowed,
        "final_rows": len(deduped),
        "final_unique_rows": final_unique,
        "affects": [
            "graph/alias_dictionary.csv (consumed only by Step 8 query resolver)",
        ],
        "does_not_require_rerun": ["step4", "step5", "step6"],
        "rationale": (
            "alias_dictionary.csv is read only by the Step 8 resolver; "
            "Step 4/5/6 do not consume it, so the graph does not change."
        ),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  Manifest: {MANIFEST_OUT}")
    print("\n✓ Step 3B complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
