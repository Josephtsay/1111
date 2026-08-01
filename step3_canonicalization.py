"""
Step 3 — 技能正規化（Canonicalization）
Role: A (Content Graph) — alias / registry 內容；合併管線可協作
Playbook: docs/SKILL_GRAPH_PLAYBOOK.md §Step 3 + §3.4–3.5

建議流程（MVP deterministic；LLM verifier 留待 Gate 1）：
  raw_mention / canonical_candidate
    → NFKC / casefold / 空白正規化
    → 以 canonical_candidate 為主候選（不得用壞掉的 evidence 另開 ID）
    → alias 字典 exact match
    → protected-pair gate（禁止誤併）
    → evidence grounding（phrase：evidence 必須能對上 canonical）
    → accepted / quarantined / rejected

策略：precision-first（寧可漏併，不要誤併）
  - Registry seed 只來自 structured（2A）且通過品質檢查的 candidate
  - Phrase（2B）不得自行新增 registry entry；只能對上已 seed / alias
  - 新增 registry entry 須走審核升版流程（此腳本僅允許 structured seed）

輸入：
  - graph/extractions_structured.jsonl (Step 2A)
  - graph/extractions_phrase.jsonl (Step 2B)

輸出（A → B 最終介面）：
  - graph/extractions.jsonl
  - graph/skill_dictionary.csv
  - graph/credential_dictionary.csv
  - graph/alias_dictionary.csv
  - graph/canonicalization_audit.csv
  - graph/canonicalization_manifest.json
"""

from __future__ import annotations

import csv
import json
import re
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

GRAPH_DIR = Path(__file__).parent / "graph"
EXTRACTIONS_STRUCTURED = GRAPH_DIR / "extractions_structured.jsonl"
EXTRACTIONS_PHRASE = GRAPH_DIR / "extractions_phrase.jsonl"

OUTPUT_EXTRACTIONS = GRAPH_DIR / "extractions.jsonl"
OUTPUT_SKILL_DICT = GRAPH_DIR / "skill_dictionary.csv"
OUTPUT_CREDENTIAL_DICT = GRAPH_DIR / "credential_dictionary.csv"
OUTPUT_ALIAS_DICT = GRAPH_DIR / "alias_dictionary.csv"
OUTPUT_AUDIT = GRAPH_DIR / "canonicalization_audit.csv"
OUTPUT_MANIFEST = GRAPH_DIR / "canonicalization_manifest.json"

DICTIONARY_VERSION = "v0.1"
ALIAS_SEED_CSV = Path(__file__).parent / "fixtures" / "skill_alias_seed_v0.1.csv"

# 允許的極短 registry key（語意上成立的單字／符號技能）
SHORT_KEY_ALLOWLIST = frozenset({
    "c", "c#", "c++", "r", "go", "r語", "ai", "ui", "ux", "qa", "it",
    "pc", "os", "pr", "ae", "ps", "id", "xd", "bi", "ml", "dl", "vr", "ar",
    "nx", "hp", "正航",  # structured high-freq short brands / ERP
})

_CJK_RE = re.compile(r"^[\u4e00-\u9fff]{2}$")

# Leading "." defaults to rejected (offset/parse garbage). These are the
# known-legitimate terms where a leading "." is semantically required:
# Playbook explicitly requires preserving ".NET" as meaningful punctuation.
DOT_PREFIX_ALLOWLIST = frozenset({".net", ".net_core"})

# Protected pairs — 不可合併（Playbook §Step 3）
# 注意：Node / Node.js、K8s / Kubernetes 是縮寫 alias，不是 protected pair
PROTECTED_GROUPS: list[frozenset[str]] = [
    frozenset({"java", "javascript"}),
    frozenset({"c", "c++", "c#"}),
    frozenset({"react", "react_native", "react-native"}),
    frozenset({"sql", "mysql"}),
    frozenset({"aws", "azure"}),
    frozenset({"tensorflow", "pytorch"}),
    frozenset({"node.js", "javascript"}),
]

# Fallback alias specs if fixtures/skill_alias_seed_v0.1.csv missing
SEED_ALIAS_SPECS_FALLBACK: list[tuple[str, str, list[str]]] = [
    ("react", "en", ["react(reactjs", "react"]),
    ("k8s", "en", ["kubernetes(k8s", "kubernetes"]),
    ("node", "en", ["node.js(node/nodejs", "node.js"]),
    ("js", "en", ["javascript"]),
]


# ─────────────────────────────────────────────────────────────────────────────
# Normalization
# ─────────────────────────────────────────────────────────────────────────────

def normalize_text(text: str) -> str:
    """NFKC + casefold + 空白壓縮，保留語意標點。"""
    value = unicodedata.normalize("NFKC", text)
    value = value.replace("，", ",").replace("＃", "#")
    value = re.sub(r"\s+", " ", value.strip())
    return value.casefold()


def sanitize_registry_key(key: str) -> str:
    """
    Repair keys where a parenthetical alias/abbreviation survives
    boundary-only stripping.

    `str.strip("()")` only removes characters sitting at the two string
    *ends*. A raw value like 'Kubernetes(K8S)' loses its trailing ')' but
    keeps the interior '(' → 'kubernetes(k8s'. That garbage key was being
    seeded straight into skill_dictionary.csv (e.g. skill:kubernetes(k8s,
    skill:amazon_web_services_(aws, skill:docker(docker_compose).

    Join with '_' instead of truncating at '(': truncation would silently
    collide distinct skills that share a prefix before the paren — e.g.
    'EDA(Exploratory Data Analysis)' and 'EDA(Electronic Design
    Automation)' must stay distinct, not both collapse to 'eda'. Joining
    keeps every key deterministic and collision-safe; deliberate merges
    still have to go through the alias dictionary, per §Step 3's
    precision-first / two-person-review policy.
    """
    if "(" not in key and ")" not in key:
        return key
    value = key.replace("(", "_").replace(")", "")
    value = re.sub(r"_+", "_", value)
    return value.strip("_")


def normalize_key(text: str) -> str:
    """Produce registry_key：空白→底線，剝外層括號/引號。"""
    value = normalize_text(text)
    value = value.strip("\"'「」『』【】《》()")
    value = re.sub(r"\s+", "_", value.strip())
    return sanitize_registry_key(value) or ""


def parse_canonical_candidate(candidate: str) -> tuple[str, str] | None:
    """Parse 'skill:foo' / 'credential:bar' → (entity_type, registry_key)."""
    if not candidate:
        return None
    if candidate.startswith("skill:"):
        key = sanitize_registry_key(candidate[len("skill:"):])
        return ("skill", key) if key else None
    if candidate.startswith("credential:"):
        key = sanitize_registry_key(candidate[len("credential:"):])
        return ("credential", key) if key else None
    return None


def is_clean_registry_key(key: str) -> bool:
    """Reject obvious parse/offset garbage before seeding or accepting."""
    if not key or key == "unknown":
        return False
    # 2-char CJK (正航) and allowlisted short Latin/symbol skills are OK
    if len(key) <= 2:
        if key in SHORT_KEY_ALLOWLIST or _CJK_RE.fullmatch(key):
            pass
        else:
            return False
    if key[0] in ",#/:;.|\"'`、•-_+=":
        if not (key[0] == "." and key in DOT_PREFIX_ALLOWLIST):
            return False
    if key[-1] in ",、;:|\"'`":
        return False
    if "(" in key or ")" in key:
        return False
    # Only reject short Latin+顿号 fragments (e.g. "jav、"); keep real compounds
    # like "edm、banner設計與製作" (structured 工作技能).
    if len(key) <= 8 and (
        re.search(r"^,[a-z]", key) or re.search(r"^[a-z]{1,3}、", key)
    ):
        return False
    if key in {"#", "/", "ss", "gi", "jav", "wo", "n、", "t、", "u,"}:
        return False
    return True


def load_alias_seed_specs(
    path: Path = ALIAS_SEED_CSV,
) -> list[tuple[str, str, list[str]]]:
    """Load (raw_alias, language, preferred_targets[]) from versioned CSV."""
    if not path.exists():
        return list(SEED_ALIAS_SPECS_FALLBACK)
    specs: list[tuple[str, str, list[str]]] = []
    with path.open("r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            raw = (row.get("raw_alias") or row.get("alias_key") or "").strip()
            if not raw:
                continue
            language = (row.get("language") or "unknown").strip()
            targets = [
                t.strip()
                for t in (row.get("preferred_targets") or "").split("|")
                if t.strip()
            ]
            if targets:
                specs.append((raw, language, targets))
    return specs or list(SEED_ALIAS_SPECS_FALLBACK)


def _are_protected(key_a: str, key_b: str) -> bool:
    if key_a == key_b:
        return False
    for group in PROTECTED_GROUPS:
        if key_a in group and key_b in group:
            return True
    return False


def evidence_grounds_key(raw_mention: str, registry_key: str, display_name: str) -> bool:
    """
    Phrase evidence must exactly reconcile with the canonical target
    (after NFKC/casefold/空白正規化). Substring fuzzy match is too loose
    (e.g. 'gi' ⊂ 'git') and hides Step 2B offset bugs.
    """
    raw_n = normalize_text(raw_mention)
    raw_key = normalize_key(raw_mention)
    if not raw_n and not raw_key:
        return False

    display_n = normalize_text(display_name) if display_name else ""
    display_key = normalize_key(display_name) if display_name else ""
    key_as_text = registry_key.replace("_", " ")

    if display_n and raw_n == display_n:
        return True
    if display_key and raw_key == display_key:
        return True
    if raw_n == key_as_text or raw_key == registry_key:
        return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Registry
# ─────────────────────────────────────────────────────────────────────────────

class CanonicalRegistry:
    def __init__(self) -> None:
        self.skills: dict[str, dict[str, Any]] = {}
        self.credentials: dict[str, dict[str, Any]] = {}
        # alias_key → canonical registry_key
        self.skill_aliases: dict[str, str] = {}
        self.credential_aliases: dict[str, str] = {}
        self.alias_meta: list[dict[str, Any]] = []
        self.seed_rejected: Counter[str] = Counter()

    def register_skill(self, registry_key: str, display_name: str, source: str) -> bool:
        if not is_clean_registry_key(registry_key):
            self.seed_rejected["unclean_skill_key"] += 1
            return False
        if registry_key in self.skills:
            return True
        self.skills[registry_key] = {
            "registry_key": registry_key,
            "canonical_name": display_name,
            "skill_kind": "technical",
            "dictionary_version": DICTIONARY_VERSION,
            "source": source,
        }
        return True

    def register_credential(self, registry_key: str, display_name: str, source: str) -> bool:
        if not is_clean_registry_key(registry_key):
            self.seed_rejected["unclean_credential_key"] += 1
            return False
        if registry_key in self.credentials:
            return True
        self.credentials[registry_key] = {
            "registry_key": registry_key,
            "canonical_name": display_name,
            "credential_type": "professional",
            "dictionary_version": DICTIONARY_VERSION,
            "source": source,
        }
        return True

    def add_skill_alias(
        self,
        alias_key: str,
        raw_alias: str,
        canonical_key: str,
        *,
        language: str = "unknown",
        source: str = "manual",
    ) -> str | None:
        """Bind alias → canonical. Returns None if rejected (protected / missing / ambiguous)."""
        if not alias_key or not canonical_key:
            return None
        if canonical_key not in self.skills:
            return None
        if _are_protected(alias_key, canonical_key):
            return None
        if alias_key in self.skills and alias_key != canonical_key:
            # alias_key itself is already a distinct registry entry — don't collapse
            if _are_protected(alias_key, canonical_key):
                return None
        existing = self.skill_aliases.get(alias_key)
        if existing and existing != canonical_key:
            # ambiguous — do not silently overwrite
            self.alias_meta.append({
                "alias_key": alias_key,
                "raw_alias": raw_alias,
                "entity_type": "skill",
                "canonical_id": f"skill:{canonical_key}",
                "source": source,
                "language": language,
                "ambiguity_status": "ambiguous",
                "dictionary_version": DICTIONARY_VERSION,
            })
            return None
        self.skill_aliases[alias_key] = canonical_key
        self.alias_meta.append({
            "alias_key": alias_key,
            "raw_alias": raw_alias,
            "entity_type": "skill",
            "canonical_id": f"skill:{canonical_key}",
            "source": source,
            "language": language,
            "ambiguity_status": "unique",
            "dictionary_version": DICTIONARY_VERSION,
        })
        return canonical_key

    def apply_seed_aliases(self) -> int:
        bound = 0
        for raw_alias, language, targets in load_alias_seed_specs():
            alias_key = normalize_key(raw_alias)
            # fixtures/skill_alias_seed_v0.1.csv still lists raw, pre-fix
            # target forms (e.g. "kubernetes(k8s"); sanitize before matching
            # so they resolve against the now-clean registry keys.
            sanitized_targets = [sanitize_registry_key(t) for t in targets]
            target = next((t for t in sanitized_targets if t in self.skills), None)
            if target is None:
                continue
            if alias_key == target:
                continue
            if self.add_skill_alias(
                alias_key, raw_alias, target, language=language, source="manual"
            ):
                bound += 1
        return bound

    def resolve(
        self,
        *,
        entity_type: str,
        registry_key: str,
        raw_mention: str,
        display_hint: str,
        method: str,
        source_field: str,
    ) -> tuple[str, str, str]:
        """
        Returns (canonical_id, status, reason).
        status ∈ accepted / quarantined / rejected
        """
        if not registry_key or registry_key == "unknown" or not is_clean_registry_key(registry_key):
            return f"{entity_type}:unknown", "quarantined", "unclean_or_missing_key"

        # Alias exact match (after candidate key normalization)
        if entity_type == "skill" and registry_key in self.skill_aliases:
            registry_key = self.skill_aliases[registry_key]
        elif entity_type == "credential" and registry_key in self.credential_aliases:
            registry_key = self.credential_aliases[registry_key]

        # Phrase evidence must ground to canonical (catches 2B offset bugs)
        if method == "phrase":
            display = display_hint
            if entity_type == "skill" and registry_key in self.skills:
                display = self.skills[registry_key]["canonical_name"]
            elif entity_type == "credential" and registry_key in self.credentials:
                display = self.credentials[registry_key]["canonical_name"]
            if not evidence_grounds_key(raw_mention, registry_key, display):
                return (
                    f"{entity_type}:{registry_key}",
                    "quarantined",
                    "evidence_not_grounded",
                )

        table = self.skills if entity_type == "skill" else self.credentials
        if registry_key in table:
            return f"{entity_type}:{registry_key}", "accepted", "registry_hit"

        # Precision-first: only structured may seed new entries at runtime
        if method == "structured":
            ok = (
                self.register_skill(registry_key, raw_mention or display_hint, source_field)
                if entity_type == "skill"
                else self.register_credential(
                    registry_key, raw_mention or display_hint, source_field
                )
            )
            if ok:
                return f"{entity_type}:{registry_key}", "accepted", "structured_seed"
            return f"{entity_type}:{registry_key}", "quarantined", "structured_seed_rejected"

        return f"{entity_type}:{registry_key}", "quarantined", "oov_not_in_registry"

    def export_skill_dictionary(self, path: Path) -> None:
        with path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "registry_key", "canonical_name", "skill_kind",
                "dictionary_version", "source",
            ])
            writer.writeheader()
            for entry in sorted(self.skills.values(), key=lambda e: e["registry_key"]):
                writer.writerow(entry)

    def export_credential_dictionary(self, path: Path) -> None:
        with path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "registry_key", "canonical_name", "credential_type",
                "dictionary_version", "source",
            ])
            writer.writeheader()
            for entry in sorted(self.credentials.values(), key=lambda e: e["registry_key"]):
                writer.writerow(entry)

    def export_alias_dictionary(self, path: Path) -> None:
        # Dedup alias_meta by (alias_key, canonical_id), prefer unique over ambiguous
        seen: set[tuple[str, str]] = set()
        rows: list[dict[str, Any]] = []
        for row in self.alias_meta:
            key = (row["alias_key"], row["canonical_id"])
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
        with path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "alias_key", "raw_alias", "entity_type", "canonical_id",
                "source", "language", "ambiguity_status", "dictionary_version",
            ])
            writer.writeheader()
            for row in sorted(rows, key=lambda r: (r["alias_key"], r["canonical_id"])):
                writer.writerow(row)


# ─────────────────────────────────────────────────────────────────────────────
# IO helpers
# ─────────────────────────────────────────────────────────────────────────────

def _iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def seed_registry_from_structured(registry: CanonicalRegistry, path: Path) -> dict[str, int]:
    """First pass: build high-precision registry from 2A only."""
    skill_n = 0
    cred_n = 0
    skipped = 0
    for record in _iter_jsonl(path):
        for mention in record.get("skills", []):
            parsed = parse_canonical_candidate(mention.get("canonical_candidate", ""))
            if not parsed:
                skipped += 1
                continue
            _, key = parsed
            raw = mention.get("raw_mention", "") or key
            if registry.register_skill(key, raw, mention.get("source_field", "structured")):
                skill_n += 1
            else:
                skipped += 1
        for mention in record.get("credentials", []):
            parsed = parse_canonical_candidate(mention.get("canonical_candidate", ""))
            if not parsed:
                skipped += 1
                continue
            _, key = parsed
            raw = mention.get("raw_mention", "") or key
            if registry.register_credential(
                key, raw, mention.get("source_field", "structured")
            ):
                cred_n += 1
            else:
                skipped += 1
    return {
        "structured_skill_mentions_seeded": skill_n,
        "structured_credential_mentions_seeded": cred_n,
        "seed_skipped": skipped,
        "unique_skills": len(registry.skills),
        "unique_credentials": len(registry.credentials),
    }


def load_phrase_by_job(path: Path) -> dict[str, dict[str, Any]]:
    """Index phrase extractions by job_id (much smaller than structured)."""
    indexed: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return indexed
    for record in _iter_jsonl(path):
        indexed[record["job_id"]] = record
    return indexed


def _candidate_key_for_mention(mention: dict[str, Any], entity_type: str) -> str:
    """Prefer canonical_candidate; fall back to normalizing raw_mention."""
    parsed = parse_canonical_candidate(mention.get("canonical_candidate", ""))
    if parsed and parsed[0] == entity_type:
        return parsed[1]
    return normalize_key(mention.get("raw_mention", ""))


def canonicalize_mention(
    registry: CanonicalRegistry,
    mention: dict[str, Any],
    *,
    entity_type: str,
) -> tuple[str, str, str]:
    key = _candidate_key_for_mention(mention, entity_type)
    display_hint = mention.get("raw_mention", "") or key
    return registry.resolve(
        entity_type=entity_type,
        registry_key=key,
        raw_mention=mention.get("raw_mention", ""),
        display_hint=display_hint,
        method=mention.get("method", "unknown"),
        source_field=mention.get("source_field", ""),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Main pipeline
# ─────────────────────────────────────────────────────────────────────────────

def run_canonicalization(
    *,
    limit: int | None = None,
) -> dict[str, Any]:
    GRAPH_DIR.mkdir(parents=True, exist_ok=True)

    if not EXTRACTIONS_STRUCTURED.exists():
        raise FileNotFoundError(f"Missing structured extractions: {EXTRACTIONS_STRUCTURED}")

    registry = CanonicalRegistry()

    print("  Pass 1: seeding registry from structured (2A)...")
    seed_stats = seed_registry_from_structured(registry, EXTRACTIONS_STRUCTURED)
    print(
        f"  Seeded unique skills={seed_stats['unique_skills']:,}, "
        f"credentials={seed_stats['unique_credentials']:,} "
        f"(skipped={seed_stats['seed_skipped']:,})"
    )

    print("  Binding seed aliases...")
    alias_bound = registry.apply_seed_aliases()
    print(f"  Alias bindings applied: {alias_bound}")

    print("  Loading phrase extractions index (2B)...")
    phrase_by_job = load_phrase_by_job(EXTRACTIONS_PHRASE)
    print(f"  Phrase jobs indexed: {len(phrase_by_job):,}")

    stats = Counter()
    reason_counts: Counter[str] = Counter()

    print("  Pass 2: merge + canonicalize (stream write)...")
    processed = 0

    with OUTPUT_EXTRACTIONS.open("w", encoding="utf-8") as out_f, \
         OUTPUT_AUDIT.open("w", encoding="utf-8", newline="") as audit_f:
        audit_writer = csv.DictWriter(audit_f, fieldnames=[
            "job_id", "mention_id", "entity_type", "raw_mention",
            "canonical_candidate", "canonical_id", "canonicalization_status",
            "reason", "method", "source_field",
        ])
        audit_writer.writeheader()

        for record in _iter_jsonl(EXTRACTIONS_STRUCTURED):
            job_id = record["job_id"]
            phrase = phrase_by_job.get(job_id)

            skills = list(record.get("skills", []))
            credentials = list(record.get("credentials", []))
            if phrase:
                skills.extend(phrase.get("skills", []))
                credentials.extend(phrase.get("credentials", []))

            for mention in skills:
                stats["total_skill_mentions"] += 1
                canonical_id, status, reason = canonicalize_mention(
                    registry, mention, entity_type="skill"
                )
                mention["canonical_id"] = canonical_id
                mention["canonicalization_status"] = status
                mention["dictionary_version"] = DICTIONARY_VERSION
                stats[f"skill_{status}"] += 1
                reason_counts[f"skill:{reason}"] += 1
                audit_writer.writerow({
                    "job_id": job_id,
                    "mention_id": mention.get("mention_id", ""),
                    "entity_type": "skill",
                    "raw_mention": mention.get("raw_mention", ""),
                    "canonical_candidate": mention.get("canonical_candidate", ""),
                    "canonical_id": canonical_id,
                    "canonicalization_status": status,
                    "reason": reason,
                    "method": mention.get("method", ""),
                    "source_field": mention.get("source_field", ""),
                })

            for mention in credentials:
                stats["total_credential_mentions"] += 1
                canonical_id, status, reason = canonicalize_mention(
                    registry, mention, entity_type="credential"
                )
                mention["canonical_id"] = canonical_id
                mention["canonicalization_status"] = status
                mention["dictionary_version"] = DICTIONARY_VERSION
                stats[f"credential_{status}"] += 1
                reason_counts[f"credential:{reason}"] += 1
                audit_writer.writerow({
                    "job_id": job_id,
                    "mention_id": mention.get("mention_id", ""),
                    "entity_type": "credential",
                    "raw_mention": mention.get("raw_mention", ""),
                    "canonical_candidate": mention.get("canonical_candidate", ""),
                    "canonical_id": canonical_id,
                    "canonicalization_status": status,
                    "reason": reason,
                    "method": mention.get("method", ""),
                    "source_field": mention.get("source_field", ""),
                })

            out_record = {
                "job_id": job_id,
                "skills": skills,
                "credentials": credentials,
                "extraction_version": record.get("extraction_version", "v0.1"),
            }
            out_f.write(json.dumps(out_record, ensure_ascii=False) + "\n")

            processed += 1
            if processed % 200_000 == 0:
                print(f"  Processed {processed:,} jobs...")
            if limit is not None and processed >= limit:
                break

    print("  Exporting dictionaries...")
    registry.export_skill_dictionary(OUTPUT_SKILL_DICT)
    registry.export_credential_dictionary(OUTPUT_CREDENTIAL_DICT)
    registry.export_alias_dictionary(OUTPUT_ALIAS_DICT)

    manifest = {
        "step": "step3_canonicalization",
        "schema_version": "v0.1",
        "dictionary_version": DICTIONARY_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "strategy": (
            "precision-first: seed from structured; resolve via canonical_candidate; "
            "phrase cannot invent registry IDs; evidence grounding; "
            "LLM verifier pending Gate 1"
        ),
        "inputs": [
            str(EXTRACTIONS_STRUCTURED),
            str(EXTRACTIONS_PHRASE),
        ],
        "outputs": {
            "extractions": str(OUTPUT_EXTRACTIONS),
            "skill_dictionary": str(OUTPUT_SKILL_DICT),
            "credential_dictionary": str(OUTPUT_CREDENTIAL_DICT),
            "alias_dictionary": str(OUTPUT_ALIAS_DICT),
            "audit": str(OUTPUT_AUDIT),
        },
        "seed_statistics": seed_stats,
        "alias_bindings": alias_bound,
        "statistics": {
            "total_jobs": processed,
            "phrase_jobs_merged": len(phrase_by_job),
            "total_skill_mentions": int(stats["total_skill_mentions"]),
            "total_credential_mentions": int(stats["total_credential_mentions"]),
            "accepted_skills": int(stats["skill_accepted"]),
            "quarantined_skills": int(stats["skill_quarantined"]),
            "rejected_skills": int(stats["skill_rejected"]),
            "accepted_credentials": int(stats["credential_accepted"]),
            "quarantined_credentials": int(stats["credential_quarantined"]),
            "rejected_credentials": int(stats["credential_rejected"]),
            "unique_skills_in_registry": len(registry.skills),
            "unique_credentials_in_registry": len(registry.credentials),
            "skill_aliases": len(registry.skill_aliases),
            "credential_aliases": len(registry.credential_aliases),
            "reason_counts": dict(reason_counts.most_common()),
            "seed_rejected": dict(registry.seed_rejected),
        },
        "protected_pairs": [sorted(g) for g in PROTECTED_GROUPS],
        "normalization_rules": (
            "NFKC + casefold + whitespace collapse; semantic punctuation preserved; "
            "canonical_id resolved from canonical_candidate (not raw evidence)"
        ),
        "known_limitations": [
            "LLM verifier / embedding candidate generation 尚未接入（待 Gate 1）",
            "泛用軟技能黑名單尚未套用（待 Gate 2 與 B 共同決定）",
            "Step 2B 已用 alignment map 修正 offset；本步仍保留 evidence_not_grounded 作為安全網",
            "structured seed 仍含部分職掌長句（工作技能欄）；未自動當軟技能剔除",
            "assertion_status 否定偵測仍依賴上游；本步不改寫 assertion_status",
        ],
    }
    OUTPUT_MANIFEST.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Step 3: 技能正規化 (Canonicalization)")
    parser.add_argument("--limit", type=int, default=None, help="限制處理 job 數（smoke test）")
    args = parser.parse_args()

    print("=" * 60)
    print("Step 3 — 技能正規化 (Canonicalization)")
    print("=" * 60)
    if args.limit:
        print(f"  限制: {args.limit} jobs")
    print()

    manifest = run_canonicalization(limit=args.limit)
    stats = manifest["statistics"]

    print("\n" + "=" * 60)
    print("STEP 3 SUMMARY")
    print("=" * 60)
    print(f"  Total jobs:               {stats['total_jobs']:,}")
    print(
        f"  Skill mentions:           {stats['total_skill_mentions']:,} "
        f"(accepted: {stats['accepted_skills']:,}, "
        f"quarantined: {stats['quarantined_skills']:,})"
    )
    print(
        f"  Credential mentions:      {stats['total_credential_mentions']:,} "
        f"(accepted: {stats['accepted_credentials']:,}, "
        f"quarantined: {stats['quarantined_credentials']:,})"
    )
    print(f"  Unique skills in registry:{stats['unique_skills_in_registry']:,}")
    print(f"  Unique credentials:       {stats['unique_credentials_in_registry']:,}")
    print(f"  Skill aliases:            {stats['skill_aliases']:,}")
    print("  Reason breakdown (top):")
    for reason, count in list(stats["reason_counts"].items())[:10]:
        print(f"    {reason}: {count:,}")
    print(f"\n  Output: {manifest['outputs']['extractions']}")
    print("\n✓ Step 3 complete.")


if __name__ == "__main__":
    main()
