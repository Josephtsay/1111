"""
Build fixtures/skill_alias_seed_v0.2.csv — expanded skill query aliases.

Sources (offline, no test leakage):
  1. fixtures/skill_alias_seed_v0.1.csv (kept; targets re-sanitized)
  2. Curated high-value abbreviations that do not equal registry_key
  3. Safe surface forms from skill_dictionary canonical_name / registry_key
     (paren harvest + latin tool short names; collision → dropped)

Output schema matches v0.1:
  alias_key, raw_alias, language, preferred_targets, notes

Usage:
  python pipeline/build_skill_alias_seed_v0.2.py
  python pipeline/build_skill_alias_seed_v0.2.py --dry-run
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))

from step3_canonicalization import (  # noqa: E402
    _guess_language,
    derive_paren_aliases,
    is_meaningful_alias_key,
    normalize_key,
    sanitize_registry_key,
)

V01 = ROOT / "fixtures" / "skill_alias_seed_v0.1.csv"
OUT = ROOT / "fixtures" / "skill_alias_seed_v0.2.csv"
SKILL_DICT = ROOT / "graph" / "skill_dictionary.csv"

# Curated: query surface → preferred registry keys (first hit wins).
# Only list mappings where alias_key ≠ registry_key (otherwise direct hit works).
CURATED: list[tuple[str, str, str]] = [
    # Office / design shortcuts
    ("ppt", "powerpoint", "PowerPoint abbreviation"),
    ("pptx", "powerpoint", "PowerPoint file ext"),
    ("adobe_illustrator", "illustrator", ""),
    ("adobe_photoshop", "photoshop", ""),
    ("adobe_xd", "adobe_xd", ""),
    ("aftereffects", "after_effects", ""),
    ("after_effect", "after_effects", ""),
    ("3dsmax", "3ds_max", ""),
    ("3dmax", "3ds_max", ""),
    # Cloud / infra (compound registry keys)
    ("docker", "docker_docker_compose", "compose compound key"),
    ("docker_compose", "docker_docker_compose", ""),
    ("k8s", "kubernetes_k8s", ""),
    ("kubernetes", "kubernetes_k8s", ""),
    ("aws", "amazon_web_services_aws", ""),
    ("amazon_web_services", "amazon_web_services_aws", ""),
    ("azure", "azure_microsoft_azure", ""),
    ("microsoft_azure", "azure_microsoft_azure", ""),
    ("gcp", "gcp_google_cloud_platform", ""),
    ("google_cloud", "gcp_google_cloud_platform", ""),
    ("google_cloud_platform", "gcp_google_cloud_platform", ""),
    ("ecs", "amazon_ecs_ecs/amazon_elastic_container_service", ""),
    ("airflow", "apache_airflow_airflow", ""),
    ("apache_airflow", "apache_airflow_airflow", ""),
    # Frontend / backend compounds
    ("react", "react_reactjs", ""),
    ("reactjs", "react_reactjs", ""),
    ("react.js", "react_reactjs", ""),
    ("vue", "vue.js_vue/vuejs", ""),
    ("vuejs", "vue.js_vue/vuejs", ""),
    ("vue.js", "vue.js_vue/vuejs", ""),
    ("node", "node.js_node/nodejs", ""),
    ("nodejs", "node.js_node/nodejs", ""),
    ("node.js", "node.js_node/nodejs", ""),
    ("next", "next.js", ""),
    ("nextjs", "next.js", ""),
    ("nuxt", "nuxt.js", ""),
    ("nuxtjs", "nuxt.js", ""),
    ("golang", "golang_go", ""),
    ("go_lang", "golang_go", ""),
    # Data / ML
    ("mysql", "mysql/mariadb", ""),
    ("mariadb", "mysql/mariadb", ""),
    ("mssql", "ms_sql_mssql/sql_server", ""),
    ("sql_server", "ms_sql_mssql/sql_server", ""),
    ("ms_sql", "ms_sql_mssql/sql_server", ""),
    ("powerbi", "power_bi", ""),
    ("power_bi", "power_bi", "identity surface"),
    ("nlp", "nlp_natural_language_processing", ""),
    ("natural_language_processing", "nlp_natural_language_processing", ""),
    ("machine_learning", "機器學習_machine_learning模型開發與應用", ""),
    ("ml_models", "機器學習_machine_learning模型開發與應用", ""),
    ("seo", "seo_搜尋引擎優化", ""),
    ("搜尋引擎優化", "seo_搜尋引擎優化", ""),
    # ERP / Taiwan
    ("鼎新", "鼎新_erp", ""),
    ("鼎新erp", "鼎新_erp", ""),
    ("tiptop", "鼎新tiptop_erp", ""),
    ("sap", "sap_erp", "prefer SAP ERP over substring noise"),
    ("sap_erp", "sap_erp", ""),
    ("oracle_erp", "oracle_erp", ""),
    # C family
    ("c++", "c/c++", "do not merge to C alone"),
    ("cpp", "c/c++", ""),
    ("cplusplus", "c/c++", ""),
    ("c/c++", "c/c++", ""),
    ("csharp", "c#", ""),
    ("c_sharp", "c#", ""),
    ("dotnet", "asp.net", "no bare .NET node; map to ASP.NET"),
    ("aspnet", "asp.net", ""),
    ("vb.net", "vb.net_visual_basic_.net", ""),
    ("vbnet", "vb.net_visual_basic_.net", ""),
    # Misc high-value
    ("salesforce", "salesforce_crm", ""),
    ("meta_business_suite", "meta_business_suite_fb", ""),
    ("springboot", "spring_boot", ""),
    ("spring_boot", "spring_boot", ""),
    ("torch", "pytorch", ""),
    ("tf", "tensorflow", ""),
    ("js", "javascript", ""),
    ("ts", "typescript", ""),
    ("postgres", "postgresql", ""),
    ("psql", "postgresql", ""),
    ("cad", "autocad", "prefer AutoCAD over ArchiCAD for bare cad"),
    ("auto_cad", "autocad", ""),
]


def load_registry() -> dict[str, str]:
    registry: dict[str, str] = {}
    with SKILL_DICT.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            key = sanitize_registry_key((row.get("registry_key") or "").strip())
            if key:
                registry[key] = (row.get("canonical_name") or key).strip()
    return registry


def load_v01() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with V01.open("r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append({
                "alias_key": (row.get("alias_key") or "").strip(),
                "raw_alias": (row.get("raw_alias") or "").strip(),
                "language": (row.get("language") or "unknown").strip(),
                "preferred_targets": (row.get("preferred_targets") or "").strip(),
                "notes": (row.get("notes") or "").strip(),
            })
    return rows


def resolve_target(preferred: str, registry: dict[str, str]) -> str | None:
    """Pick first preferred target that exists in registry (sanitize each)."""
    for part in preferred.split("|"):
        t = sanitize_registry_key(part.strip())
        if t and t in registry:
            return t
    return None


def add_row(
    out: dict[str, dict[str, str]],
    *,
    raw_alias: str,
    preferred_targets: str,
    notes: str,
    language: str | None = None,
) -> None:
    alias_key = normalize_key(raw_alias)
    if not alias_key or not is_meaningful_alias_key(alias_key):
        return
    if alias_key in out:
        return  # first writer wins (v0.1 then curated then derived)
    out[alias_key] = {
        "alias_key": alias_key,
        "raw_alias": raw_alias,
        "language": language or _guess_language(raw_alias),
        "preferred_targets": preferred_targets,
        "notes": notes,
    }


def harvest_from_registry(registry: dict[str, str]) -> list[tuple[str, str, str]]:
    """(raw_alias, target_key, note) from paren + short latin surfaces."""
    found: list[tuple[str, str, str]] = []
    # alias_key → set of targets (detect collisions later)
    bucket: dict[str, set[str]] = {}
    raw_of: dict[str, str] = {}

    def propose(raw: str, target: str) -> None:
        ak = normalize_key(raw)
        if not ak or not is_meaningful_alias_key(ak):
            return
        if ak == target:
            return  # direct registry hit already works
        if ak in registry and ak != target:
            return  # would collapse a distinct skill
        bucket.setdefault(ak, set()).add(target)
        raw_of.setdefault(ak, raw)

    for key, name in registry.items():
        for raw in derive_paren_aliases(name):
            propose(raw, key)
        # Underscore ↔ space / dotted surfaces for short latin tool keys
        if re.fullmatch(r"[a-z0-9_.#+/\-]{2,32}", key):
            propose(key.replace("_", " "), key)
            propose(key.replace("_", "."), key)
            propose(key.replace("/", " "), key)
        # Chinese-only short display names
        if re.fullmatch(r"[\u4e00-\u9fff]{2,8}", name.strip()):
            propose(name.strip(), key)

    for ak, targets in bucket.items():
        if len(targets) != 1:
            continue
        target = next(iter(targets))
        found.append((raw_of[ak], target, "auto_from_registry"))
    return found


def build(dry_run: bool = False) -> int:
    if not SKILL_DICT.exists():
        raise SystemExit(f"missing {SKILL_DICT}")
    registry = load_registry()
    print(f"Registry: {len(registry):,} skills")

    out: dict[str, dict[str, str]] = {}
    stats = {"v01": 0, "v01_unbound": 0, "curated": 0, "curated_unbound": 0, "auto": 0}

    # 1) v0.1
    for row in load_v01():
        target = resolve_target(row["preferred_targets"], registry)
        if not target:
            stats["v01_unbound"] += 1
            # keep row anyway — step3b will skip unbound
            add_row(
                out,
                raw_alias=row["raw_alias"] or row["alias_key"],
                preferred_targets=row["preferred_targets"],
                notes=(row["notes"] + " | from_v0.1").strip(" |"),
                language=row["language"],
            )
            continue
        # Rewrite preferred_targets to sanitized primary for stability
        add_row(
            out,
            raw_alias=row["raw_alias"] or row["alias_key"],
            preferred_targets=target,
            notes=(row["notes"] + " | from_v0.1").strip(" |"),
            language=row["language"],
        )
        stats["v01"] += 1

    # 2) curated
    for raw, target_key, note in CURATED:
        target = sanitize_registry_key(target_key)
        if target not in registry:
            stats["curated_unbound"] += 1
            continue
        before = len(out)
        add_row(
            out,
            raw_alias=raw,
            preferred_targets=target,
            notes=(note + " | curated_v0.2").strip(" |"),
        )
        if len(out) > before:
            stats["curated"] += 1

    # 3) auto harvest
    for raw, target, note in harvest_from_registry(registry):
        before = len(out)
        add_row(
            out,
            raw_alias=raw,
            preferred_targets=target,
            notes=note,
        )
        if len(out) > before:
            stats["auto"] += 1

    rows = sorted(out.values(), key=lambda r: r["alias_key"])
    bound = 0
    for r in rows:
        if resolve_target(r["preferred_targets"], registry):
            bound += 1

    print(
        f"Seed rows: {len(rows):,}  (bound={bound}, "
        f"v01={stats['v01']}, curated_new={stats['curated']}, auto_new={stats['auto']})"
    )
    print(
        f"  unbound: v01={stats['v01_unbound']}, curated={stats['curated_unbound']}"
    )

    if dry_run:
        print("(dry-run — not written)")
        for r in rows[:20]:
            print(f"  {r['alias_key']!r:28} → {r['preferred_targets']}")
        return 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["alias_key", "raw_alias", "language", "preferred_targets", "notes"],
        )
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {OUT}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true")
    return build(dry_run=p.parse_args().dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
