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
  2. Optionally binds fixtures/skill_alias_seed_v0.2.csv (manual seed)
  3. Derives alias candidates via step3_canonicalization.derive_paren_aliases
  4. Two-pass collision detection: an alias pointing at >1 skill is written as
     ambiguous (Step 8 loads only ambiguity_status == "unique")
  5. Seed / manual rows win over derived_paren on the same alias_key
  6. Backs up the previous file before writing

Usage:
  python step3b_derive_aliases.py --dry-run
  python step3b_derive_aliases.py
  python step3b_derive_aliases.py --seed ../fixtures/skill_alias_seed_v0.2.csv
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
    load_alias_seed_specs,
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
DEFAULT_SEED = ROOT / "fixtures" / "skill_alias_seed_v0.2.csv"

DERIVED_SOURCE = "derived_paren"
MANUAL_SOURCE = "manual"
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


def bind_seed(
    registry: dict[str, str],
    version: str,
    seed_path: Path,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Bind seed CSV → unique/ambiguous manual alias rows (sanitize targets)."""
    rows: list[dict[str, Any]] = []
    stats: Counter[str] = Counter()
    if not seed_path.exists():
        stats["seed_missing"] = 1
        return rows, dict(stats)

    # First pass: alias_key → set of resolved canonical keys
    buckets: dict[str, set[str]] = {}
    meta: dict[str, tuple[str, str]] = {}  # alias_key → (raw_alias, language)
    for raw_alias, language, targets in load_alias_seed_specs(seed_path):
        alias_key = normalize_key(raw_alias)
        if not alias_key or not is_meaningful_alias_key(alias_key):
            stats["rejected_alias_key"] += 1
            continue
        sanitized = [sanitize_registry_key(t) for t in targets]
        hit = next((t for t in sanitized if t in registry), None)
        if hit is None:
            stats["unbound"] += 1
            continue
        if alias_key == hit:
            stats["skipped_identity"] += 1
            continue
        if alias_key in registry and alias_key != hit:
            stats["skipped_is_registry_entry"] += 1
            continue
        buckets.setdefault(alias_key, set()).add(hit)
        meta.setdefault(alias_key, (raw_alias, language))

    for alias_key, targets in sorted(buckets.items()):
        status = "unique" if len(targets) == 1 else "ambiguous"
        raw_alias, language = meta[alias_key]
        for canonical_key in sorted(targets):
            rows.append({
                "alias_key": alias_key,
                "raw_alias": raw_alias,
                "entity_type": "skill",
                "canonical_id": f"skill:{canonical_key}",
                "source": MANUAL_SOURCE,
                "language": language or _guess_language(alias_key),
                "ambiguity_status": status,
                "dictionary_version": version,
            })
        stats["unique" if status == "unique" else "ambiguous"] += 1
    return rows, dict(stats)


def main() -> int:
    parser = argparse.ArgumentParser(description="Derive skill aliases from parentheticals")
    parser.add_argument("--dry-run", action="store_true", help="Report only, do not write")
    parser.add_argument(
        "--seed",
        type=Path,
        default=DEFAULT_SEED,
        help=f"Skill alias seed CSV (default: {DEFAULT_SEED.name})",
    )
    parser.add_argument(
        "--no-seed",
        action="store_true",
        help="Skip seed merge; only regenerate derived_paren rows",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("Step 3B — Derive skill aliases from parentheticals")
    print("=" * 60)

    registry, version = load_registry()
    print(f"  Registry skills: {len(registry):,}  (dictionary_version={version})")

    seed_rows: list[dict[str, Any]] = []
    seed_stats: dict[str, int] = {}
    if not args.no_seed:
        seed_rows, seed_stats = bind_seed(registry, version, args.seed)
        print(f"  Seed file: {args.seed}")
        print(f"  Seed bindings: unique={seed_stats.get('unique', 0)}, "
              f"ambiguous={seed_stats.get('ambiguous', 0)}, "
              f"unbound={seed_stats.get('unbound', 0)}")
    else:
        print("  Seed merge: skipped (--no-seed)")

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
    print(f"  Usable (unique) derived rows: {len(unique_rows):,}")
    if ambiguous_keys:
        print(f"  Ambiguous derived (left unbound): {ambiguous_keys}")

    # Seed / remaining manual rows win over derived on the same alias_key.
    # Drop prior manual/derived from disk when regenerating from seed+derive,
    # but keep any non-manual / non-derived rows (future sources).
    existing = load_existing_aliases()
    other_preserved = [
        r for r in existing
        if r.get("source") not in {DERIVED_SOURCE, MANUAL_SOURCE}
    ]
    print(f"  Existing alias rows: {len(existing):,} "
          f"(preserving {len(other_preserved):,} other-source)")

    seed_keys = {r.get("alias_key") for r in seed_rows}
    derived_final = [r for r in derived_rows if r["alias_key"] not in seed_keys]
    shadowed = len(derived_rows) - len(derived_final)
    if shadowed:
        print(f"  Derived rows deferring to seed bindings: {shadowed}")

    merged = seed_rows + other_preserved + derived_final
    seen: set[tuple[str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for row in merged:
        key = (row.get("alias_key", ""), row.get("canonical_id", ""))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)

    final_unique = sum(1 for r in deduped if r.get("ambiguity_status") == "unique")
    prev_unique = sum(1 for r in existing if r.get("ambiguity_status") == "unique")
    print(f"\n  Final alias_dictionary: {len(deduped):,} rows, {final_unique:,} unique")
    print(f"  Step 8 usable aliases: {prev_unique:,} → {final_unique:,}")

    if args.dry_run:
        print("\n  (dry-run — nothing written)")
        sample = [r for r in seed_rows if r["ambiguity_status"] == "unique"][:10]
        print("  Sample seed bindings:")
        for r in sample:
            print(f"    {r['alias_key']!r:<28} → {r['canonical_id']}")
        sample_d = [r for r in derived_final if r["ambiguity_status"] == "unique"][:10]
        print("  Sample derived bindings:")
        for r in sample_d:
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
        "seed_path": str(args.seed) if not args.no_seed else None,
        "seed": seed_stats,
        "derived": stats,
        "ambiguous_alias_keys": ambiguous_keys,
        "rows_preserved_other_source": len(other_preserved),
        "rows_deferred_to_seed": shadowed,
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
