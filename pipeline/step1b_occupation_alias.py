"""
Step 1B — Occupation Alias Dictionary
Role: B (Structure Graph)
Playbook: §3.5 Alias 字典契約

Builds a versioned occupation alias dictionary from 職務對照表.csv:
- CodeAlike: split by <br> variants (not by / or ／)
- CodeNameA: the leaf/minor name → maps to its own CodeNo
- CodeNameEN: English name → maps to its own CodeNo
- CodeNameB/C: maps to the PARENT middle/major CodeNo, NOT to descendant leaves

Output: graph/alias_dictionary_occupation.csv (§3.5 schema)

Rules (from Playbook §3.5):
1. <br>, <br/>, <br /> and newlines are alias delimiters; NOT /, ／
2. CodeAlike/CodeNameA/CodeNameEN → maps to that row's CodeNo
3. CodeNameB/C → maps to resolved parent CodeNo (middle/major)
4. Same alias_key → multiple canonical_id = mark as 'ambiguous'
5. Write input hash, version, ambiguity count to manifest
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parent.parent  # pipeline/ -> repo root; graph/ dataset/ fixtures/ 都掛在根目錄
RAW_DIR = _REPO_ROOT / "data" / "raw"
OUTPUT_DIR = _REPO_ROOT / "graph"
DUTIES_CSV = RAW_DIR / "職務對照表.csv"

DICTIONARY_VERSION = "v0.1"
# Matches <br>, <br/>, <br />, and malformed <br without closing > (typos in source data)
# e.g. "<brAnimation" or "<br儀電" — the source intends <br> but forgot >
BR_PATTERN = re.compile(r"<br\s*/?\s*>|<br(?=[^\s>/<])(?!>)")
# Pattern for cross-reference entries: "140402_MIS工程師"
CROSS_REF_PATTERN = re.compile(r"^(\d{6})_(.+)$")
# Pattern to find all cross-refs in a space-separated string: "140703_xxx 140702_yyy"
CROSS_REF_SPLIT_PATTERN = re.compile(r"(\d{6})_([^\s]+(?:[／/][^\s]+)*)")


# ─────────────────────────────────────────────────────────────────────────────
# Normalization (shared with skill alias — NFKC + casefold + whitespace compress)
# ─────────────────────────────────────────────────────────────────────────────


def normalize_alias_key(raw: str) -> str:
    """
    Normalize an alias to its canonical key form.
    - NFKC normalization
    - casefold (Unicode-aware lowercase)
    - Compress whitespace to single space, strip
    - Preserve semantic punctuation (C++, C#, .NET, Node.js, ／ in names)
    """
    text = unicodedata.normalize("NFKC", raw)
    text = text.casefold()
    text = re.sub(r"\s+", " ", text).strip()
    return text


def detect_language(text: str) -> str:
    """Detect language: zh, en, mixed, unknown."""
    has_cjk = bool(re.search(r"[\u4e00-\u9fff\u3400-\u4dbf]", text))
    has_latin = bool(re.search(r"[a-zA-Z]", text))
    if has_cjk and has_latin:
        return "mixed"
    elif has_cjk:
        return "zh"
    elif has_latin:
        return "en"
    else:
        return "unknown"


# ─────────────────────────────────────────────────────────────────────────────
# Alias entry
# ─────────────────────────────────────────────────────────────────────────────


def _make_entry(
    raw_alias: str,
    canonical_id: str,
    source: str,
) -> dict[str, str] | None:
    """Create a single alias entry, or None if the raw alias is empty/invalid."""
    raw = raw_alias.strip()
    if not raw or raw.upper() == "NULL":
        return None
    alias_key = normalize_alias_key(raw)
    if not alias_key:
        return None
    return {
        "alias_key": alias_key,
        "raw_alias": raw,
        "entity_type": "occupation",
        "canonical_id": canonical_id,
        "source": source,
        "language": detect_language(raw),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main builder
# ─────────────────────────────────────────────────────────────────────────────


def load_duties() -> list[dict[str, str]]:
    with DUTIES_CSV.open("r", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def classify_level(code: str) -> str:
    if code[-4:] == "0000":
        return "major"
    elif code[-2:] == "00":
        return "middle"
    else:
        return "minor"


def resolve_parent_code(code: str, level: str) -> str | None:
    """Given a CodeNo, resolve its parent CodeNo."""
    if level == "minor":
        return code[:4] + "00"  # middle parent
    elif level == "middle":
        return code[:2] + "0000"  # major parent
    return None


def build_occupation_aliases(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """
    Build alias entries from 職務對照表.

    Sources:
    - CodeNameA → own CodeNo (source=CodeName)
    - CodeNameEN → own CodeNo (source=CodeNameEN)
    - CodeAlike (split by <br>) → own CodeNo (source=CodeAlike)
    - CodeNameB → middle-level parent CodeNo (source=CodeName)
    - CodeNameC → major-level parent CodeNo (source=CodeName)

    For CodeNameB/C, we map to the PARENT code, not to every descendant.
    """
    entries: list[dict[str, str]] = []

    # Build code lookup for cross-references
    code_set = {r["CodeNo"].strip() for r in rows}

    # Build parent code lookup
    # middle code for each row: first 4 digits + "00"
    # major code for each row: first 2 digits + "0000"

    for row in rows:
        code = row["CodeNo"].strip()
        level = classify_level(code)
        canonical_id = f"occ:{code}"

        # 1. CodeNameA → own CodeNo
        name_a = row["CodeNameA"].strip()
        entry = _make_entry(name_a, canonical_id, "CodeName")
        if entry:
            entries.append(entry)

        # 2. CodeNameEN → own CodeNo
        name_en = row["CodeNameEN"].strip()
        entry = _make_entry(name_en, canonical_id, "CodeNameEN")
        if entry:
            entries.append(entry)

        # 3. CodeAlike → own CodeNo (split by <br>)
        code_alike = row["CodeAlike"].strip()
        if code_alike:
            # Split by <br> variants
            parts = BR_PATTERN.split(code_alike)
            for part in parts:
                part = part.strip()
                if not part:
                    continue
                # Check if this part contains space-separated cross-references
                # Pattern: "140703_資訊威脅分析師 140702_資訊安全架構師 ..."
                cross_refs = CROSS_REF_SPLIT_PATTERN.findall(part)
                if cross_refs:
                    # This entire part is cross-references
                    for ref_code, ref_name in cross_refs:
                        if ref_code in code_set:
                            entry = _make_entry(ref_name, f"occ:{ref_code}", "CodeAlike")
                            if entry:
                                entries.append(entry)
                else:
                    # Check single cross-reference
                    cross_match = CROSS_REF_PATTERN.match(part)
                    if cross_match:
                        ref_code = cross_match.group(1)
                        ref_name = cross_match.group(2)
                        if ref_code in code_set:
                            entry = _make_entry(ref_name, f"occ:{ref_code}", "CodeAlike")
                            if entry:
                                entries.append(entry)
                    else:
                        entry = _make_entry(part, canonical_id, "CodeAlike")
                        if entry:
                            entries.append(entry)

        # 4. CodeNameB → middle parent CodeNo (not to this leaf!)
        # Only emit if this row is a minor-level leaf
        name_b = row["CodeNameB"].strip()
        if level == "minor" and name_b and name_b != name_a:
            middle_code = code[:4] + "00"
            if middle_code in code_set:
                entry = _make_entry(name_b, f"occ:{middle_code}", "CodeName")
                if entry:
                    entries.append(entry)

        # 5. CodeNameC → major parent CodeNo (not to this leaf!)
        # Only emit if this row is a minor or middle level
        name_c = row["CodeNameC"].strip()
        if level in ("minor", "middle") and name_c and name_c != name_a and name_c != name_b:
            major_code = code[:2] + "0000"
            if major_code in code_set:
                entry = _make_entry(name_c, f"occ:{major_code}", "CodeName")
                if entry:
                    entries.append(entry)

    return entries


def deduplicate_and_mark_ambiguity(
    entries: list[dict[str, str]],
) -> list[dict[str, str]]:
    """
    Deduplicate: same (alias_key, canonical_id, source) → keep one.
    Mark ambiguity: same alias_key → multiple canonical_ids → 'ambiguous'.
    """
    # First deduplicate exact triples
    seen: set[tuple[str, str, str]] = set()
    unique_entries: list[dict[str, str]] = []
    for e in entries:
        key = (e["alias_key"], e["canonical_id"], e["source"])
        if key not in seen:
            seen.add(key)
            unique_entries.append(e)

    # Now check ambiguity: same alias_key → different canonical_ids
    alias_to_ids: dict[str, set[str]] = defaultdict(set)
    for e in unique_entries:
        alias_to_ids[e["alias_key"]].add(e["canonical_id"])

    ambiguous_keys = {k for k, v in alias_to_ids.items() if len(v) > 1}

    for e in unique_entries:
        if e["alias_key"] in ambiguous_keys:
            e["ambiguity_status"] = "ambiguous"
        else:
            e["ambiguity_status"] = "unique"
        e["dictionary_version"] = DICTIONARY_VERSION

    return unique_entries


def export_dictionary(entries: list[dict[str, str]]) -> Path:
    """Write alias_dictionary_occupation.csv."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = OUTPUT_DIR / "alias_dictionary_occupation.csv"

    fieldnames = [
        "alias_key",
        "raw_alias",
        "entity_type",
        "canonical_id",
        "source",
        "language",
        "ambiguity_status",
        "dictionary_version",
    ]

    # Sort for determinism
    entries.sort(key=lambda e: (e["alias_key"], e["canonical_id"], e["source"]))

    with output_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(entries)

    return output_path


def build_manifest(
    entries: list[dict[str, str]], source_hash: str
) -> dict[str, Any]:
    """Build manifest for the occupation alias dictionary."""
    # Stats
    total = len(entries)
    unique_aliases = len({e["alias_key"] for e in entries})
    unique_canonical = len({e["canonical_id"] for e in entries})
    ambiguous_count = len({e["alias_key"] for e in entries if e["ambiguity_status"] == "ambiguous"})

    source_dist = defaultdict(int)
    lang_dist = defaultdict(int)
    for e in entries:
        source_dist[e["source"]] += 1
        lang_dist[e["language"]] += 1

    return {
        "step": "step1b_occupation_alias",
        "dictionary_version": DICTIONARY_VERSION,
        "entity_type": "occupation",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_file": {
            "path": str(DUTIES_CSV.resolve()),
            "sha256": source_hash,
        },
        "statistics": {
            "total_entries": total,
            "unique_alias_keys": unique_aliases,
            "unique_canonical_ids": unique_canonical,
            "ambiguous_alias_keys": ambiguous_count,
            "by_source": dict(source_dist),
            "by_language": dict(lang_dist),
        },
        "normalization_rules": {
            "method": "NFKC + casefold + whitespace_compress",
            "preserves_semantic_punctuation": True,
            "examples": ["C++", "C#", ".NET", "Node.js", "／ in names"],
        },
        "delimiter_rules": {
            "CodeAlike": "<br>, <br/>, <br /> treated as delimiter; / and ／ NOT split",
            "cross_references": "Pattern NNNNNN_Name maps alias to referenced CodeNo",
        },
        "ambiguity_policy": (
            "Same alias_key mapping to multiple canonical_ids → all kept, marked 'ambiguous'. "
            "Query resolver can disambiguate using duty_code or other context."
        ),
    }


def main() -> None:
    print("=" * 60)
    print("Step 1B — Occupation Alias Dictionary")
    print("=" * 60)

    # Hash source
    print("\n[1/4] Hashing source file...")
    digest = hashlib.sha256()
    with DUTIES_CSV.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    source_hash = digest.hexdigest()
    print(f"  SHA-256: {source_hash[:16]}...")

    # Load and build
    print("\n[2/4] Building alias entries...")
    rows = load_duties()
    entries = build_occupation_aliases(rows)
    print(f"  Raw entries (before dedup): {len(entries)}")

    # Deduplicate and mark ambiguity
    print("\n[3/4] Deduplicating and marking ambiguity...")
    entries = deduplicate_and_mark_ambiguity(entries)
    print(f"  Final entries: {len(entries)}")

    unique_aliases = len({e["alias_key"] for e in entries})
    ambiguous = len({e["alias_key"] for e in entries if e["ambiguity_status"] == "ambiguous"})
    print(f"  Unique alias keys: {unique_aliases}")
    print(f"  Ambiguous keys: {ambiguous}")

    # Export
    print("\n[4/4] Exporting...")
    output_path = export_dictionary(entries)
    print(f"  Dictionary: {output_path}")

    manifest = build_manifest(entries, source_hash)
    manifest_path = OUTPUT_DIR / "step1b_alias_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"  Manifest: {manifest_path}")

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  Total entries: {len(entries)}")
    print(f"  Unique alias keys: {unique_aliases}")
    print(f"  Ambiguous: {ambiguous}")
    print(f"  By source:")
    for src, count in sorted(manifest["statistics"]["by_source"].items(), key=lambda x: -x[1]):
        print(f"    {src}: {count}")
    print(f"  By language:")
    for lang, count in sorted(manifest["statistics"]["by_language"].items(), key=lambda x: -x[1]):
        print(f"    {lang}: {count}")

    # Show some ambiguous examples
    if ambiguous > 0:
        print(f"\n  Sample ambiguous aliases:")
        amb_keys = sorted({e["alias_key"] for e in entries if e["ambiguity_status"] == "ambiguous"})
        for key in amb_keys[:5]:
            targets = [e["canonical_id"] for e in entries if e["alias_key"] == key]
            print(f"    '{key}' → {targets}")

    print("\n✓ Occupation alias dictionary complete.")


if __name__ == "__main__":
    main()
