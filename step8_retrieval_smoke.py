"""
Step 8 — Retrieval Smoke Test
Role: B (Structure Graph) + A (共同)
Playbook: §Step 8

Proves the graph is usable: from a query, traverse the graph to find jobs,
and produce an explainable traversal trace.

Query preprocessing (train-searchlog driven) runs before resolution:
separator split, job-suffix peel, reverse-suffix occupation match.

Smoke queries (Playbook recommended):
- node.js
- 後端工程師
- React 前端
- 護理師
- 會計 (non-IT)
- reactjs (alias)
- k8s (abbreviation)
- Python 資料分析 (multi-skill)

CLI:
  python step8_retrieval_smoke.py [--use-llm-classification] [--no-graph] [--blacklist PATH]
                                  [--eval] [--eval-split test] [--eval-limit N]
                                  [--run-id RUN_ID]
"""

from __future__ import annotations

import argparse
import csv
import json
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

GRAPH_DIR = Path(__file__).parent / "graph"


# ─────────────────────────────────────────────────────────────────────────────
# Config (replaces hard-coded feature_flags)
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Step8Config:
    """Feature flags and evaluation settings configurable via CLI."""

    use_graph: bool = True
    use_llm_extraction: bool = False
    use_llm_classification: bool = False
    use_llm_relations: bool = False
    use_adaptive_traversal: bool = False
    blacklist_path: Path | None = None
    run_eval: bool = False
    eval_split: str = "test"
    eval_limit: int | None = None
    run_id: str = ""

    def feature_flags_dict(self) -> dict[str, bool]:
        return {
            "use_graph": self.use_graph,
            "use_llm_extraction": self.use_llm_extraction,
            "use_llm_classification": self.use_llm_classification,
            "use_llm_relations": self.use_llm_relations,
            "use_adaptive_traversal": self.use_adaptive_traversal,
        }


def parse_step8_args(argv: list[str] | None = None) -> Step8Config:
    parser = argparse.ArgumentParser(description="Step 8 — Retrieval Smoke Test + Evaluation")
    parser.add_argument(
        "--use-llm-classification",
        action="store_true",
        default=False,
        help="Enable LLM skill classification flag in manifest",
    )
    parser.add_argument(
        "--no-graph",
        action="store_true",
        default=False,
        help="Disable graph traversal (for B0 baseline: BM25-only)",
    )
    parser.add_argument(
        "--blacklist",
        type=Path,
        default=None,
        help="Path to soft_skill_blacklist CSV (overrides auto-detection)",
    )
    parser.add_argument(
        "--eval",
        action="store_true",
        default=False,
        help="Run full evaluation with relevance labels (NDCG/MRR/Hit metrics)",
    )
    parser.add_argument(
        "--eval-split",
        type=str,
        default="test",
        choices=["train", "validation", "test"],
        help="Which data split to evaluate on (default: test)",
    )
    parser.add_argument(
        "--eval-limit",
        type=int,
        default=None,
        help="Limit number of evaluation queries (for quick iterations)",
    )
    parser.add_argument(
        "--run-id",
        type=str,
        default="",
        help="Identifier for this ablation run (e.g. B0, G1, G2)",
    )
    args = parser.parse_args(argv)
    return Step8Config(
        use_graph=not args.no_graph,
        use_llm_classification=args.use_llm_classification,
        blacklist_path=args.blacklist,
        run_eval=args.eval,
        eval_split=args.eval_split,
        eval_limit=args.eval_limit,
        run_id=args.run_id,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Graph loader (lazy, column-indexed for fast lookup)
# ─────────────────────────────────────────────────────────────────────────────


class GraphIndex:
    """In-memory inverted index for graph traversal. Only loads edges needed."""

    def __init__(self, graph_dir: Path = GRAPH_DIR):
        self.graph_dir = graph_dir
        # skill_id → list of (job_id, requirement_level, confidence)
        self.skill_to_jobs: dict[str, list[tuple[str, str, float]]] = defaultdict(list)
        # credential_id → list of job_id
        self.credential_to_jobs: dict[str, list[str]] = defaultdict(list)
        # occ_code → list of job_id
        self.occ_to_jobs: dict[str, list[str]] = defaultdict(list)
        # skill_id → list of (other_skill, npmi)
        self.co_occurs: dict[str, list[tuple[str, float]]] = defaultdict(list)
        # occ_code → list of (skill_id, rate)
        self.core_skills: dict[str, list[tuple[str, float]]] = defaultdict(list)
        # child_occ → parent_occ
        self.occ_parent: dict[str, str] = {}
        # Job titles for display
        self.job_titles: dict[str, str] = {}
        # Alias dictionaries
        self.skill_alias: dict[str, str] = {}  # normalized → canonical skill_id
        self.occ_alias: dict[str, str] = {}    # normalized → occ:code

    def load(self) -> None:
        print("  Loading graph index...")
        t0 = time.time()
        self._load_edges()
        self._load_aliases()
        self._load_job_titles()
        self._load_skill_kinds()
        self._build_substring_vocab()
        print(f"  Loaded in {time.time()-t0:.1f}s")
        print(f"    Skills with HAS_SKILL: {len(self.skill_to_jobs):,}")
        print(f"    Occupations with jobs: {len(self.occ_to_jobs):,}")
        print(f"    CO_OCCURS pairs: {sum(len(v) for v in self.co_occurs.values()):,}")
        print(f"    Skill aliases: {len(self.skill_alias):,}")
        print(f"    Occupation aliases: {len(self.occ_alias):,}")
        print(f"    Substring vocab: {len(self.skill_substrings):,} skill / "
              f"{len(self.occ_substrings):,} occupation")
        if self.skill_kind:
            kinds = Counter(self.skill_kind.values())
            print(f"    skill_kind: {dict(kinds)}")

    def _load_skill_kinds(self) -> None:
        """
        Load skill_kind from nodes.csv so retrieval can weight by kind.

        Read from nodes.csv (not skill_dictionary.csv) because nodes.csv is the
        frozen Step 6 export whose node_id values are guaranteed to match the
        edge endpoints; the dictionary's registry_key has drifted from the
        graph before.
        """
        self.skill_kind: dict[str, str] = {}
        nodes_path = self.graph_dir / "nodes.csv"
        if not nodes_path.exists():
            return
        with nodes_path.open("r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("node_type") != "Skill":
                    continue
                kind = (row.get("skill_kind") or "").strip()
                if kind:
                    self.skill_kind[row["node_id"]] = kind

    def _build_substring_vocab(self) -> None:
        """
        Build length-descending vocabularies for substring resolution.

        Measured motivation: 50.3% of the top-3,000 query instances resolved to
        nothing at all, and the failures were dominated by terms like 司機 /
        作業員 / 清潔人員 that exist inside longer registry entries. Exact-key
        and exact-alias lookup alone cannot reach them.

        Latin and CJK are handled differently on purpose (see _substring_hit):
        CJK has no word delimiters so plain containment is correct, while a
        bare Latin containment check would let 'go' match 'google'.
        """
        skill_vocab: dict[str, str] = {}
        for alias_key, canonical in self.skill_alias.items():
            if canonical in self.skill_to_jobs and len(alias_key) >= 2:
                skill_vocab.setdefault(alias_key, canonical)
        for skill_id in self.skill_to_jobs:
            surface = skill_id.removeprefix("skill:").replace("_", " ").strip()
            if len(surface) >= 2:
                skill_vocab.setdefault(surface, skill_id)
        self.skill_substrings: list[tuple[str, str]] = sorted(
            skill_vocab.items(), key=lambda kv: (-len(kv[0]), kv[0])
        )

        occ_vocab: dict[str, str] = {}
        for alias_key, canonical in self.occ_alias.items():
            code = canonical.replace("occ:", "")
            if (code in self.occ_to_jobs or code in self.core_skills) and len(alias_key) >= 2:
                occ_vocab.setdefault(alias_key, canonical)
        self.occ_substrings: list[tuple[str, str]] = sorted(
            occ_vocab.items(), key=lambda kv: (-len(kv[0]), kv[0])
        )

    def _load_edges(self) -> None:
        edges_path = self.graph_dir / "edges.csv"
        with edges_path.open("r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                etype = row["edge_type"]
                src = row["source_id"]
                tgt = row["target_id"]

                if etype == "HAS_SKILL":
                    conf = float(row.get("confidence") or 0)
                    req = row.get("requirement_level", "unspecified")
                    self.skill_to_jobs[tgt].append((src, req, conf))
                elif etype == "REQUIRES_CREDENTIAL":
                    self.credential_to_jobs[tgt].append(src)
                elif etype == "IN_OCCUPATION":
                    occ = tgt.replace("occ:", "")
                    self.occ_to_jobs[occ].append(src)
                elif etype == "SUBCATEGORY_OF":
                    child = src.replace("occ:", "")
                    parent = tgt.replace("occ:", "")
                    self.occ_parent[child] = parent
                elif etype == "CO_OCCURS_WITH":
                    npmi = float(row.get("npmi") or 0)
                    self.co_occurs[src].append((tgt, npmi))
                    self.co_occurs[tgt].append((src, npmi))
                elif etype == "CORE_SKILL":
                    rate = float(row.get("rate") or 0)
                    occ = src.replace("occ:", "")
                    self.core_skills[occ].append((tgt, rate))

    def _load_aliases(self) -> None:
        # Skill aliases (from Step 3)
        skill_alias_path = self.graph_dir / "alias_dictionary.csv"
        if skill_alias_path.exists():
            with skill_alias_path.open("r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if row.get("ambiguity_status") == "unique":
                        self.skill_alias[row["alias_key"]] = row["canonical_id"]

        # Occupation aliases — unique go to occ_alias; ambiguous go to occ_alias_ambiguous
        self.occ_alias_ambiguous: dict[str, list[str]] = defaultdict(list)
        occ_alias_path = self.graph_dir / "alias_dictionary_occupation.csv"
        if occ_alias_path.exists():
            with occ_alias_path.open("r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    key = row["alias_key"]
                    canonical = row["canonical_id"]
                    if row.get("ambiguity_status") == "unique":
                        self.occ_alias[key] = canonical
                    else:
                        self.occ_alias_ambiguous[key].append(canonical)

    def _load_job_titles(self) -> None:
        """Load all job titles for display."""
        import duckdb
        con = duckdb.connect(":memory:")
        try:
            rows = con.execute(f"""
                SELECT job_id, title
                FROM read_parquet('{(self.graph_dir / "train_jobs.parquet").resolve().as_posix()}')
            """).fetchall()
            for job_id, title in rows:
                self.job_titles[f"job:{job_id}"] = title or ""
        finally:
            con.close()


# ─────────────────────────────────────────────────────────────────────────────
# Query resolver
# ─────────────────────────────────────────────────────────────────────────────

import unicodedata
import re


def normalize_query_token(token: str) -> str:
    """Same normalization as alias_key: NFKC + casefold + whitespace compress."""
    text = unicodedata.normalize("NFKC", token)
    text = text.casefold()
    text = re.sub(r"\s+", " ", text).strip()
    return text


# Separators common in train search logs (包裝員/作業員, 門市，櫃檯，op).
_QUERY_SEP_RE = re.compile(r"[/／、,，+＋|｜&＆;；]+")
# Trailing punctuation noise seen in logs (麵包二手/////).
_TRAILING_NOISE_RE = re.compile(r"[/\\._\-~…]+$")
# Full-query decorative wrappers 【爭鮮…】.
_WRAP_RE = re.compile(r"^[【\[](.+)[】\]]$")

# Job-title suffixes ranked by train-query frequency (longest first).
# Measured on train searchlog: 人員/助理/作業員/司機 dominate; whitespace
# splitting alone covers only 2.8% of queries because 97.2% have no spaces.
_JOB_SUFFIXES = (
    "護理師",
    "工程師",
    "設計師",
    "作業員",
    "管理員",
    "技術員",
    "包裝員",
    "服務員",
    "美容師",
    "保育員",
    "工讀生",
    "人員",
    "專員",
    "助理",
    "司機",
    "店員",
    "技師",
    "經理",
    "主管",
    "學徒",
    "工讀",
    "廚師",
    "倉管",
    "職員",
    "員工",
    "老師",
    "師傅",
)
# Too generic as standalone queries — reverse-suffix must stay strict.
_BROAD_ROLE_WORDS = frozenset({"人員", "員工", "職員", "專員"})
# High-frequency train standalone roles that are not title suffixes
# (保全 2,086 / 行政 3,398 / 清潔 431) but appear inside longer queries.
_STANDALONE_ROLES = (
    "保全",
    "行政",
    "清潔",
    "會計",
    "業務",
    "櫃檯",
    "包裝",
    "餐飲",
    "倉管",
    "護理",
)
_ROLE_WORDS = (frozenset(_JOB_SUFFIXES) | frozenset(_STANDALONE_ROLES)) - _BROAD_ROLE_WORDS


@dataclass
class PreprocessedQuery:
    """Index-free query expansion prior to skill/occupation resolution."""

    raw: str
    normalized: str
    tokens: list[str]
    steps: list[str] = field(default_factory=list)


def preprocess_query(query: str) -> PreprocessedQuery:
    """
    Expand a raw searchlog query into resolution candidate tokens.

    Driven by train-split searchlog facts (146k queries):
      - 97.2% have no whitespace → whitespace split barely helps
      - 98.3% contain CJK; top terms are short role words (司機, 行政, 作業員)
      - separators /／、, appear in ~6k queries (包裝員/作業員)
      - many hits are '<modifier><suffix>' (清潔人員, 小貨車司機, 行政助理)

    This function does NOT touch the graph. It only proposes surface forms for
    the resolver; OOV still maps to existing nodes or stays unresolved.
    """
    steps: list[str] = []
    normalized = normalize_query_token(query)
    if not normalized:
        return PreprocessedQuery(raw=query, normalized="", tokens=[], steps=steps)

    cleaned = _TRAILING_NOISE_RE.sub("", normalized).strip()
    if cleaned != normalized:
        steps.append(f"strip_trailing_noise: {normalized!r} → {cleaned!r}")
        normalized = cleaned

    wrapped = _WRAP_RE.fullmatch(normalized)
    if wrapped:
        inner = wrapped.group(1).strip()
        if inner:
            steps.append(f"unwrap_decoration: → {inner!r}")
            normalized = inner

    # Separator split first, then whitespace within each part.
    raw_parts = _QUERY_SEP_RE.split(normalized)
    parts = [p.strip() for p in raw_parts if p.strip()]
    if len(parts) > 1:
        steps.append(f"sep_split: {parts}")
    atoms: list[str] = []
    for part in parts or [normalized]:
        space_bits = part.split()
        if len(space_bits) > 1:
            steps.append(f"space_split: {space_bits}")
            atoms.extend(space_bits)
        else:
            atoms.append(part)

    tokens: list[str] = []
    seen: set[str] = set()

    def _emit(tok: str, reason: str) -> None:
        if not tok or tok in seen:
            return
        seen.add(tok)
        tokens.append(tok)
        if reason:
            steps.append(reason)

    # Keep the full normalized string as a candidate (multi-skill / full alias).
    _emit(normalized, "")

    for atom in atoms:
        _emit(atom, "")
        stem, suffix = _strip_job_suffix(atom)
        if suffix:
            if stem:
                _emit(stem, f"suffix_strip: {atom!r} → stem {stem!r}")
            # Role word itself is often the real occupation key (司機, 助理).
            _emit(suffix, f"suffix_keep: {atom!r} → role {suffix!r}")
        # Emit known role words embedded in a modifier+role blob (夜班保全).
        if len(atom) >= 3:
            for role in sorted(_ROLE_WORDS, key=len, reverse=True):
                if role != atom and role in atom:
                    _emit(role, f"role_in_token: {atom!r} → {role!r}")
                    break

    return PreprocessedQuery(
        raw=query, normalized=normalized, tokens=tokens, steps=steps
    )


def _strip_job_suffix(token: str) -> tuple[str, str]:
    """Return (stem, suffix) for the longest matching job-title suffix."""
    if not token or not re.search(r"[\u4e00-\u9fff]", token):
        return token, ""
    for suffix in _JOB_SUFFIXES:
        if token.endswith(suffix) and len(token) > len(suffix):
            stem = token[: -len(suffix)].strip()
            if stem:
                return stem, suffix
    return token, ""


@dataclass
class QueryResolution:
    raw_query: str
    resolved_skills: list[str] = field(default_factory=list)
    resolved_occupations: list[str] = field(default_factory=list)
    unresolved_terms: list[str] = field(default_factory=list)
    resolution_log: list[str] = field(default_factory=list)


def resolve_query(query: str, index: GraphIndex) -> QueryResolution:
    """
    Resolve a query string into skill/occupation anchors.

    Strategy:
      1. preprocess (separator split, job-suffix peel) — index-free
      2. full query exact skill / occupation
      3. each preprocessed token exact
      4. substring / reverse-suffix fallback on unresolved tokens
      5. whole-query substring as last resort
    """
    result = QueryResolution(raw_query=query)
    pre = preprocess_query(query)
    normalized = pre.normalized
    if not normalized:
        return result
    for step in pre.steps:
        result.resolution_log.append(f"preprocess: {step}")

    # 1. Try full query as skill
    skill_id = _resolve_skill(normalized, index)
    if skill_id:
        result.resolved_skills.append(skill_id)
        result.resolution_log.append(f"full_query → {skill_id}")
        return result

    # 2. Try full query as occupation
    occ_id = _resolve_occupation(normalized, index)
    if occ_id:
        result.resolved_occupations.append(occ_id)
        result.resolution_log.append(f"full_query → {occ_id}")
        return result

    # 3. Resolve each preprocessed token (exact only). Skip the full string —
    #    already tried above — so multi-anchor queries can accumulate.
    pending: list[str] = []
    for token in pre.tokens:
        if token == normalized:
            continue
        skill = _resolve_skill(token, index)
        if skill:
            if skill not in result.resolved_skills:
                result.resolved_skills.append(skill)
                result.resolution_log.append(f"token '{token}' → {skill}")
            continue
        occ = _resolve_occupation(token, index)
        if occ:
            if occ not in result.resolved_occupations:
                result.resolved_occupations.append(occ)
                result.resolution_log.append(f"token '{token}' → {occ}")
            continue
        pending.append(token)

    # 4. Substring / reverse-suffix fallback for tokens exact pass missed.
    for token in pending:
        skill = _resolve_skill(token, index, allow_substring=True)
        if skill:
            if skill not in result.resolved_skills:
                result.resolved_skills.append(skill)
                result.resolution_log.append(f"token '{token}' ~substring→ {skill}")
            continue
        occ = _resolve_occupation(token, index, allow_substring=True)
        if occ:
            if occ not in result.resolved_occupations:
                result.resolved_occupations.append(occ)
                result.resolution_log.append(f"token '{token}' ~substring→ {occ}")
            continue
        result.unresolved_terms.append(token)
        result.resolution_log.append(f"token '{token}' → unresolved")

    # 5. Whole-query substring / reverse-suffix as the very last resort.
    if not result.resolved_skills and not result.resolved_occupations:
        skill_id = _resolve_skill(normalized, index, allow_substring=True)
        if skill_id:
            result.resolved_skills.append(skill_id)
            result.resolution_log.append(f"full_query ~substring→ {skill_id}")
            return result
        occ_id = _resolve_occupation(normalized, index, allow_substring=True)
        if occ_id:
            result.resolved_occupations.append(occ_id)
            result.resolution_log.append(f"full_query ~substring→ {occ_id}")

    return result


_LATIN_RE = re.compile(r"[a-z0-9]")
_CJK_ONLY_RE = re.compile(r"^[\u4e00-\u9fff\u3400-\u4dbf]+$")


def _substring_hit(term: str, query: str) -> bool:
    """
    Containment test with script-aware boundaries.

    CJK terms use plain containment because Chinese has no word delimiters —
    that is exactly how 司機 should match 送貨司機.

    Terms containing Latin characters additionally require that the match is
    not glued to surrounding alphanumerics. Without this, the 2-character
    aliases harvested from parentheticals ('go' from Golang(Go), 'bi', 'vb')
    would fire on unrelated words — 'go' inside 'google', 'bi' inside
    'big data'. The boundary check keeps those aliases usable instead of
    forcing us to drop them.
    """
    start = query.find(term)
    if start < 0:
        return False
    if _CJK_ONLY_RE.fullmatch(term):
        return True
    end = start + len(term)
    before = query[start - 1] if start > 0 else ""
    after = query[end] if end < len(query) else ""
    if before and _LATIN_RE.fullmatch(before):
        return False
    if after and _LATIN_RE.fullmatch(after):
        return False
    return True


def _resolve_skill(
    normalized: str, index: GraphIndex, *, allow_substring: bool = False
) -> str | None:
    """
    Resolve a normalized token to a skill_id.

    allow_substring is off by default and enabled only in resolve_query's final
    pass. Letting substring matching run during the exact passes would make it
    short-circuit multi-anchor queries: 'Python 資料分析' resolved to
    skill:python by containment on the whole string and returned early, losing
    the occ:140400 anchor (and with it the occupation+skill boost) that token
    splitting used to find.
    """
    # Direct registry hit
    candidate = f"skill:{normalized.replace(' ', '_')}"
    if candidate in index.skill_to_jobs:
        return candidate
    # Alias lookup
    if normalized in index.skill_alias:
        canonical = index.skill_alias[normalized]
        if canonical in index.skill_to_jobs:
            return canonical
    # Try with dots preserved (node.js)
    candidate_dot = f"skill:{normalized}"
    if candidate_dot in index.skill_to_jobs:
        return candidate_dot
    if not allow_substring:
        return None
    # Substring fallback, longest term first
    for term, skill_id in getattr(index, "skill_substrings", ()):
        if len(term) > len(normalized):
            continue
        if _substring_hit(term, normalized):
            return skill_id
    return None


def _pick_occupation_from_candidates(
    candidates: list[str], index: GraphIndex
) -> str | None:
    """
    Collapse multiple occupation IDs to one anchor.

    Prefer a shared middle (4-digit) parent, then major (2-digit), else the
    candidate with the most jobs. Used for ambiguous aliases, prefix fan-out,
    and reverse-suffix fan-out (司機 → many *司機 aliases).
    """
    if not candidates:
        return None
    uniq = list(dict.fromkeys(candidates))
    if len(uniq) == 1:
        only = uniq[0]
        code = only.replace("occ:", "")
        if code in index.occ_to_jobs or code in index.core_skills:
            return only
        return None

    codes = [c.replace("occ:", "") for c in uniq]
    parents = {c[:4] + "00" for c in codes if len(c) >= 4}
    if len(parents) == 1:
        parent_code = parents.pop()
        if parent_code in index.occ_to_jobs or parent_code in index.core_skills:
            return f"occ:{parent_code}"

    major_parents = {c[:2] + "0000" for c in codes if len(c) >= 2}
    if len(major_parents) == 1:
        major_code = major_parents.pop()
        if major_code in index.occ_to_jobs or major_code in index.core_skills:
            return f"occ:{major_code}"

    parent_job_counts: dict[str, int] = {}
    for c in codes:
        if len(c) < 4:
            continue
        p = c[:4] + "00"
        parent_job_counts[p] = parent_job_counts.get(p, 0) + len(
            index.occ_to_jobs.get(c, [])
        )
    if parent_job_counts:
        best_parent = max(parent_job_counts, key=parent_job_counts.get)
        if best_parent in index.occ_to_jobs or best_parent in index.core_skills:
            return f"occ:{best_parent}"

    best = None
    best_count = -1
    for c in uniq:
        code = c.replace("occ:", "")
        count = len(index.occ_to_jobs.get(code, []))
        if count > best_count:
            best = c
            best_count = count
    return best


def _resolve_occupation(
    normalized: str, index: GraphIndex, *, allow_substring: bool = False
) -> str | None:
    """Try to resolve a normalized token to an occupation code."""
    # Unique alias
    if normalized in index.occ_alias:
        canonical = index.occ_alias[normalized]
        occ_code = canonical.replace("occ:", "")
        if occ_code in index.occ_to_jobs or occ_code in index.core_skills:
            return canonical
    # Ambiguous alias: resolve to parent (middle) that covers all candidates
    # This is the conservative strategy per Playbook: don't silently pick one
    if normalized in index.occ_alias_ambiguous:
        picked = _pick_occupation_from_candidates(
            index.occ_alias_ambiguous[normalized], index
        )
        if picked:
            return picked

    # Prefix fallback: find alias keys that start with the query token
    # (e.g. "會計" matches "會計人員", "會計師" etc.)
    if len(normalized) >= 2:
        prefix_matches: list[str] = []
        for alias_key in index.occ_alias:
            if alias_key.startswith(normalized):
                prefix_matches.append(index.occ_alias[alias_key])
        for alias_key in index.occ_alias_ambiguous:
            if alias_key.startswith(normalized):
                prefix_matches.extend(index.occ_alias_ambiguous[alias_key])
        if prefix_matches:
            picked = _pick_occupation_from_candidates(prefix_matches, index)
            if picked:
                return picked

    # Reverse-suffix: query is a short role word that many aliases END with.
    # Train-top term 「司機」(3,060) is not itself an alias_key, but 31 aliases
    # end with 司機 and 29/31 share middle parent 180200. Prefix match cannot
    # find these. Require a dominant parent so broad suffixes like 「人員」
    # (1,257 aliases across many middles) stay unresolved rather than wrong.
    if 2 <= len(normalized) <= 6 and _CJK_ONLY_RE.fullmatch(normalized):
        ending_matches: list[str] = []
        for alias_key, canonical in index.occ_alias.items():
            if alias_key != normalized and alias_key.endswith(normalized):
                ending_matches.append(canonical)
        for alias_key, cans in index.occ_alias_ambiguous.items():
            if alias_key != normalized and alias_key.endswith(normalized):
                ending_matches.extend(cans)
        # Known role words from train (作業員, 助理, …) may span several middles;
        # use job-weighted parent pick. Broad words (人員) stay on dominant gate.
        if normalized in _ROLE_WORDS:
            picked = _pick_occupation_from_candidates(ending_matches, index)
        else:
            picked = _pick_dominant_occupation(ending_matches, index)
        if picked:
            return picked

    if not allow_substring:
        return None

    # Substring fallback, longest term first. Catches the measured failure mode
    # where the query is a shorter form contained in a longer occupation alias.
    for term, occ_id in getattr(index, "occ_substrings", ()):
        if len(term) > len(normalized):
            continue
        if _substring_hit(term, normalized):
            return occ_id

    return None


def _pick_dominant_occupation(
    candidates: list[str], index: GraphIndex, *, min_share: float = 0.6
) -> str | None:
    """Like _pick_occupation_from_candidates but requires a dominant parent."""
    if not candidates:
        return None
    codes = [c.replace("occ:", "") for c in dict.fromkeys(candidates)]
    if len(codes) == 1:
        code = codes[0]
        if code in index.occ_to_jobs or code in index.core_skills:
            return f"occ:{code}"
        return None

    middle_counts: Counter[str] = Counter()
    for c in codes:
        if len(c) >= 4:
            middle_counts[c[:4] + "00"] += 1
    if middle_counts:
        parent, n = middle_counts.most_common(1)[0]
        if n / len(codes) >= min_share and (
            parent in index.occ_to_jobs or parent in index.core_skills
        ):
            return f"occ:{parent}"

    major_counts: Counter[str] = Counter()
    for c in codes:
        if len(c) >= 2:
            major_counts[c[:2] + "0000"] += 1
    if major_counts:
        parent, n = major_counts.most_common(1)[0]
        if n / len(codes) >= max(min_share, 0.75) and (
            parent in index.occ_to_jobs or parent in index.core_skills
        ):
            return f"occ:{parent}"
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Traversal + ranking
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class TraversalResult:
    query: str
    resolution: QueryResolution
    exact_hits: int = 0
    expanded_hits: int = 0
    occupation_hits: int = 0
    top_jobs: list[dict[str, Any]] = field(default_factory=list)
    trace_lines: list[str] = field(default_factory=list)
    latency_ms: float = 0.0


def _get_descendants(occ_code: str, index: GraphIndex) -> list[str]:
    """Get all descendant occupation codes via reverse parent lookup."""
    # Build child → parent is already in index.occ_parent
    # We need parent → children (reverse)
    children_map: dict[str, list[str]] = defaultdict(list)
    for child, parent in index.occ_parent.items():
        children_map[parent].append(child)

    descendants = []
    stack = [occ_code]
    while stack:
        current = stack.pop()
        for child in children_map.get(current, []):
            descendants.append(child)
            stack.append(child)
    return descendants


# skill_kind → score multiplier, applied only when use_llm_classification is on.
#
# Rationale: a tool/product name ('Kubernetes', 'AutoCAD') is far more
# discriminative of a job than a generic work-content phrase that Step 3 also
# labels 'technical' ('具備數字概念'). Before this, skill_kind was written into
# nodes.csv by Step 6 and read by nobody, so toggling classification produced
# byte-identical rankings and the ablation could not show any effect.
#
# soft is damped rather than dropped: the blacklist already removes soft skills
# that clear the statistical guard, so anything still labelled soft here was
# deliberately kept, and should count for something.
SKILL_KIND_WEIGHTS = {
    "tool": 1.3,
    "technical": 1.0,
    "soft": 0.5,
    "non_skill": 0.3,
}


def _skill_kind_multiplier(
    skill_id: str, index: GraphIndex, *, enabled: bool
) -> float:
    if not enabled:
        return 1.0
    kind = getattr(index, "skill_kind", {}).get(skill_id, "")
    return SKILL_KIND_WEIGHTS.get(kind, 1.0)


def traverse_and_rank(
    query: str,
    index: GraphIndex,
    *,
    top_k: int = 5,
    expand_top_n: int = 5,
    expand_min_npmi: float = 0.2,
    use_skill_kind_weights: bool = False,
) -> TraversalResult:
    """
    Execute graph traversal for a query. Strategy:
    - 0-hop: exact skill/occ/credential → jobs
    - 1-hop: top CO_OCCURS_WITH by NPMI → expanded jobs (lower weight)
    - Occupation: IN_OCCUPATION (+ descendants) → jobs
    - CORE_SKILL boost: jobs matching occupation's core skills get bonus
    - Credential path: credential mentions resolved from query → jobs
    - Multi-skill: jobs matching multiple query skills get intersection bonus
    """
    t0 = time.time()
    resolution = resolve_query(query, index)
    result = TraversalResult(query=query, resolution=resolution)

    # Score accumulator: job_id → score
    job_scores: dict[str, float] = defaultdict(float)
    job_paths: dict[str, list[str]] = defaultdict(list)

    # Track which skills each job matched (for intersection bonus)
    job_skill_matches: dict[str, set[str]] = defaultdict(set)

    # 0-hop: exact skill matches
    for skill_id in resolution.resolved_skills:
        jobs = index.skill_to_jobs.get(skill_id, [])
        result.exact_hits += len(jobs)
        kind_mult = _skill_kind_multiplier(
            skill_id, index, enabled=use_skill_kind_weights
        )
        kind_note = ""
        if use_skill_kind_weights and kind_mult != 1.0:
            kind_note = (
                f" [kind={getattr(index, 'skill_kind', {}).get(skill_id, '')}"
                f" x{kind_mult}]"
            )
        result.trace_lines.append(
            f"→ {skill_id} <-[HAS_SKILL]- {len(jobs):,} jobs (0-hop exact){kind_note}"
        )
        for job_id, req, conf in jobs:
            weight = 1.0 if req == "required" else (0.8 if req == "preferred" else 0.6)
            job_scores[job_id] += weight * kind_mult
            job_paths[job_id].append(f"exact:{skill_id}")
            job_skill_matches[job_id].add(skill_id)

    # Multi-skill intersection bonus: jobs matching ALL query skills get extra score
    if len(resolution.resolved_skills) > 1:
        all_skills_set = set(resolution.resolved_skills)
        intersection_count = 0
        for job_id, matched_skills in job_skill_matches.items():
            if matched_skills >= all_skills_set:
                job_scores[job_id] += 0.5 * len(all_skills_set)
                job_paths[job_id].append(f"intersection:{len(all_skills_set)}_skills")
                intersection_count += 1
        if intersection_count > 0:
            result.trace_lines.append(
                f"→ multi-skill intersection bonus: {intersection_count:,} jobs match all {len(all_skills_set)} skills"
            )

    # 1-hop: expand via CO_OCCURS_WITH (only if exact hits are sparse)
    # Skip expansion for supernodes (>50K edges) — their co-occurs are too noisy
    SUPERNODE_THRESHOLD = 50_000
    if resolution.resolved_skills and result.exact_hits < 100:
        for skill_id in resolution.resolved_skills:
            if len(index.skill_to_jobs.get(skill_id, [])) > SUPERNODE_THRESHOLD:
                result.trace_lines.append(
                    f"→ {skill_id}: supernode ({len(index.skill_to_jobs[skill_id]):,} edges), skip expansion"
                )
                continue
            co = index.co_occurs.get(skill_id, [])
            # Filter out supernodes from expansion targets too
            co_filtered = [(s, n) for s, n in co if len(index.skill_to_jobs.get(s, [])) <= SUPERNODE_THRESHOLD]
            top_co = sorted(co_filtered, key=lambda x: -x[1])[:expand_top_n]
            for related_skill, npmi in top_co:
                if npmi < expand_min_npmi:
                    continue
                expanded_jobs = index.skill_to_jobs.get(related_skill, [])
                result.expanded_hits += len(expanded_jobs)
                result.trace_lines.append(
                    f"→ {skill_id} -[CO_OCCURS {npmi:.3f}]-> {related_skill} "
                    f"<-[HAS_SKILL]- {len(expanded_jobs):,} jobs (1-hop)"
                )
                expand_mult = _skill_kind_multiplier(
                    related_skill, index, enabled=use_skill_kind_weights
                )
                for job_id, req, conf in expanded_jobs:
                    job_scores[job_id] += 0.3 * npmi * expand_mult
                    job_paths[job_id].append(f"expand:{related_skill}(npmi={npmi:.2f})")

    # Credential path: try resolving query tokens as credentials
    for skill_id in resolution.resolved_skills:
        # Check if this skill also exists as a credential
        cred_id = skill_id.replace("skill:", "credential:")
        cred_jobs = index.credential_to_jobs.get(cred_id, [])
        if cred_jobs:
            result.trace_lines.append(
                f"→ {cred_id} <-[REQUIRES_CREDENTIAL]- {len(cred_jobs):,} jobs"
            )
            for job_id in cred_jobs:
                job_scores[job_id] += 0.4
                job_paths[job_id].append(f"credential:{cred_id}")

    # Also try direct credential lookup for unresolved terms
    for term in resolution.unresolved_terms:
        norm = normalize_query_token(term).replace(" ", "_")
        cred_id = f"credential:{norm}"
        cred_jobs = index.credential_to_jobs.get(cred_id, [])
        if cred_jobs:
            result.trace_lines.append(
                f"→ {cred_id} <-[REQUIRES_CREDENTIAL]- {len(cred_jobs):,} jobs (from unresolved term)"
            )
            for job_id in cred_jobs:
                job_scores[job_id] += 0.4
                job_paths[job_id].append(f"credential:{cred_id}")

    # Occupation path (with hierarchy descendant expansion)
    # Collect CORE_SKILL set for boosting
    occ_core_skill_set: set[str] = set()
    for occ_id in resolution.resolved_occupations:
        occ_code = occ_id.replace("occ:", "")

        # Collect direct jobs + all descendant occupation jobs
        all_occ_codes = [occ_code]
        descendants = _get_descendants(occ_code, index)
        all_occ_codes.extend(descendants)

        jobs = []
        for code in all_occ_codes:
            jobs.extend(index.occ_to_jobs.get(code, []))
        jobs = list(set(jobs))

        result.occupation_hits += len(jobs)
        if descendants:
            result.trace_lines.append(
                f"→ {occ_id} + {len(descendants)} descendants <-[IN_OCCUPATION]- {len(jobs):,} jobs"
            )
        else:
            result.trace_lines.append(
                f"→ {occ_id} <-[IN_OCCUPATION]- {len(jobs):,} jobs"
            )
        for job_id in jobs:
            job_scores[job_id] += 0.5
            job_paths[job_id].append(f"occupation:{occ_id}")

        # Collect CORE_SKILL for this occupation (for boost below)
        core = index.core_skills.get(occ_code, [])
        if core:
            top_core = sorted(core, key=lambda x: -x[1])[:10]
            core_names = [f"{s}({r:.2f})" for s, r in top_core[:5]]
            result.trace_lines.append(
                f"→ {occ_id} -[CORE_SKILL]-> top: {', '.join(core_names)}"
            )
            for skill_id, rate in top_core:
                occ_core_skill_set.add(skill_id)

    # CORE_SKILL boost: only boost jobs that match BOTH occupation AND a query skill
    # (avoids O(n*m) scan and avoids rewarding unrelated skills)
    if resolution.resolved_occupations and resolution.resolved_skills:
        query_skill_set = set(resolution.resolved_skills)
        boosted = 0
        for job_id in list(job_scores.keys()):
            if any("occupation:" in p for p in job_paths.get(job_id, [])):
                matched = job_skill_matches.get(job_id, set()) & query_skill_set
                if matched:
                    job_scores[job_id] += 0.5 * len(matched)
                    job_paths[job_id].append(f"occ+skill_boost:{len(matched)}")
                    boosted += 1
        if boosted > 0:
            result.trace_lines.append(
                f"→ occ+skill intersection boost: {boosted:,} jobs"
            )

    # Rank and get top-K
    ranked = sorted(job_scores.items(), key=lambda x: -x[1])[:top_k]
    for job_id, score in ranked:
        title = index.job_titles.get(job_id, "")
        result.top_jobs.append({
            "job_id": job_id,
            "score": round(score, 3),
            "title": title[:60],
            "paths": job_paths[job_id][:4],
        })

    result.latency_ms = (time.time() - t0) * 1000
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

SMOKE_QUERIES = [
    "node.js",
    "後端工程師",
    "React 前端",
    "護理師",
    "會計",
    "reactjs",
    "k8s",
    "Python 資料分析",
]


# ─────────────────────────────────────────────────────────────────────────────
# Evaluation harness (P2.3: metrics.py integration)
# ─────────────────────────────────────────────────────────────────────────────


def _load_eval_labels(split: str, limit: int | None = None) -> tuple[Any, Any]:
    """
    Load relevance labels from dataset_1111 prepared artifacts.

    Uses browse (relevance=1) and apply (relevance=2) as graded relevance.
    Returns (queries_df, labels_df) from the specified split.
    """
    import duckdb as _duckdb

    dataset_dir = Path(__file__).parent / "data" / "raw"
    output_dir = Path(__file__).parent / "graph" / "eval_dataset"

    # Check if prepared artifacts exist; if not, prepare them
    labels_path = output_dir / "labels.parquet"
    queries_path = output_dir / "labeled_queries.parquet"

    if not labels_path.exists() or not queries_path.exists():
        print("  Preparing evaluation dataset (first run)...")
        _prepare_eval_dataset_inline(dataset_dir, output_dir)
        print("  Dataset prepared.")

    if not labels_path.exists():
        print("  ⚠ Labels not available, cannot run evaluation")
        import pandas as _pd
        return _pd.DataFrame(), _pd.DataFrame()

    con = _duckdb.connect(":memory:")
    try:
        # ORDER BY keeps --eval-limit samples stable across runs (DuckDB
        # otherwise may return an arbitrary 2000-row subset).
        queries_sql = f"""
            SELECT query_id, query, query_time
            FROM read_parquet('{queries_path.resolve().as_posix()}')
            WHERE data_split = '{split}'
            ORDER BY query_id
        """
        if limit:
            queries_sql += f" LIMIT {limit}"

        queries = con.execute(queries_sql).fetchdf()

        if queries.empty:
            return queries, queries.iloc[:0]

        query_ids = queries["query_id"].tolist()
        placeholders = ", ".join(f"'{qid}'" for qid in query_ids)

        labels = con.execute(f"""
            SELECT query_id, job_id, relevance, original_rank
            FROM read_parquet('{labels_path.resolve().as_posix()}')
            WHERE query_id IN ({placeholders})
        """).fetchdf()

        return queries, labels
    finally:
        con.close()


def _prepare_eval_dataset_inline(dataset_dir: Path, output_dir: Path) -> None:
    """Prepare eval dataset directly with DuckDB (avoids relative-import issues)."""
    import csv as _csv
    import duckdb as _duckdb

    output_dir.mkdir(parents=True, exist_ok=True)
    con = _duckdb.connect(":memory:")
    con.execute("SET threads=4")
    con.execute("SET memory_limit='8GB'")

    # Find CSVs by header signature
    csv_files = sorted(dataset_dir.glob("*.csv"))
    search_file = jobs_file = browse_file = apply_file = None

    for f in csv_files:
        with f.open("r", encoding="utf-8-sig") as fh:
            try:
                header = next(_csv.reader(fh))
            except StopIteration:
                continue
            cols = set(header)
            if {"talentNo", "ks", "search_time", "empStr"} <= cols:
                search_file = f
            elif {"organNo", "employeeNo", "dateIn", "talentNo"} <= cols:
                browse_file = f
            elif {"LogTitle", "empNo", "talentNo", "datein"} <= cols:
                apply_file = f
            elif "職缺編號" in cols and "職務名稱" in cols:
                jobs_file = f

    if not all([search_file, jobs_file, browse_file, apply_file]):
        print("  ⚠ Cannot find required CSVs for eval dataset")
        con.close()
        return

    def sql_path(p: Path) -> str:
        return p.resolve().as_posix().replace("'", "''")

    try:
        train_end = "2026-06-05T00:00:00"
        validation_end = "2026-06-06T00:00:00"

        con.execute(f"""
            CREATE TEMP TABLE raw_search AS
            SELECT * FROM read_csv('{sql_path(search_file)}',
                header=true, all_varchar=true, strict_mode=false,
                null_padding=true, parallel=false)
        """)
        con.execute(f"""
            CREATE TEMP TABLE raw_browse AS
            SELECT * FROM read_csv('{sql_path(browse_file)}',
                header=true, all_varchar=true, strict_mode=false)
        """)
        con.execute(f"""
            CREATE TEMP TABLE raw_apply AS
            SELECT * FROM read_csv('{sql_path(apply_file)}',
                header=true, all_varchar=true, strict_mode=false)
        """)

        # Build queries with splits
        con.execute(f"""
            CREATE TEMP TABLE queries_internal AS
            WITH typed AS (
                SELECT
                    row_number() OVER () AS source_row,
                    CASE WHEN trim(coalesce(talentNo, '')) IN ('', '0') THEN NULL
                         ELSE trim(talentNo) END AS talent_key,
                    trim(coalesce(ks, '')) AS query,
                    trim(coalesce(empStr, '')) AS exposed_jobs,
                    try_cast(search_time AS TIMESTAMP) AS query_time
                FROM raw_search
            ),
            identified AS (
                SELECT
                    'q_' || substr(md5(concat_ws('|',
                        cast(source_row AS VARCHAR),
                        coalesce(talent_key, 'anonymous'),
                        cast(query_time AS VARCHAR), query
                    )), 1, 20) AS query_id,
                    *
                FROM typed
                WHERE query_time IS NOT NULL AND query <> ''
            )
            SELECT *,
                CASE WHEN query_time < TIMESTAMP '{train_end}' THEN 'train'
                     WHEN query_time < TIMESTAMP '{validation_end}' THEN 'validation'
                     ELSE 'test' END AS data_split
            FROM identified
        """)

        # Attribution: browse within 60min, apply within 24h
        con.execute("""
            CREATE TEMP TABLE browse_events AS
            SELECT trim(talentNo) AS talent_key,
                   trim(employeeNo) AS job_id,
                   try_cast(dateIn AS TIMESTAMP) AS event_time
            FROM raw_browse
            WHERE trim(coalesce(talentNo, '')) NOT IN ('', '0')
              AND trim(coalesce(employeeNo, '')) <> ''
              AND try_cast(dateIn AS TIMESTAMP) IS NOT NULL
        """)
        con.execute("""
            CREATE TEMP TABLE apply_events AS
            SELECT trim(talentNo) AS talent_key,
                   trim(empNo) AS job_id,
                   try_cast(datein AS TIMESTAMP) AS event_time
            FROM raw_apply
            WHERE trim(coalesce(talentNo, '')) NOT IN ('', '0')
              AND trim(coalesce(empNo, '')) <> ''
              AND try_cast(datein AS TIMESTAMP) IS NOT NULL
        """)

        con.execute("""
            CREATE TEMP TABLE attributed_browse AS
            SELECT q.query_id, b.job_id, b.event_time, q.query_time
            FROM browse_events b
            ASOF JOIN queries_internal q
              ON b.talent_key = q.talent_key AND b.event_time >= q.query_time
            WHERE b.event_time <= q.query_time + INTERVAL 60 MINUTE
              AND list_contains(str_split(q.exposed_jobs, ','), b.job_id)
        """)
        con.execute("""
            CREATE TEMP TABLE attributed_apply AS
            SELECT q.query_id, a.job_id, a.event_time, q.query_time
            FROM apply_events a
            ASOF JOIN queries_internal q
              ON a.talent_key = q.talent_key AND a.event_time >= q.query_time
            WHERE a.event_time <= q.query_time + INTERVAL 24 HOUR
              AND list_contains(str_split(q.exposed_jobs, ','), a.job_id)
        """)

        con.execute("""
            CREATE TEMP TABLE positive_labels AS
            SELECT query_id, job_id,
                   max(relevance) AS relevance,
                   max(viewed) AS viewed,
                   max(applied) AS applied
            FROM (
                SELECT query_id, job_id, 1 AS relevance, 1 AS viewed, 0 AS applied
                FROM attributed_browse
                UNION ALL
                SELECT query_id, job_id, 2 AS relevance, 0 AS viewed, 1 AS applied
                FROM attributed_apply
            ) GROUP BY query_id, job_id
        """)

        # Build candidate_labels (exposures + positives, capped negatives)
        con.execute("""
            CREATE TEMP TABLE candidate_labels AS
            WITH exposures AS (
                SELECT q.query_id, trim(exposed.job_id) AS job_id,
                       cast(exposed.original_rank AS INTEGER) AS original_rank,
                       q.data_split
                FROM queries_internal q,
                UNNEST(str_split(q.exposed_jobs, ','))
                    WITH ORDINALITY AS exposed(job_id, original_rank)
                WHERE trim(exposed.job_id) <> ''
                  AND q.query_id IN (SELECT DISTINCT query_id FROM positive_labels)
            ),
            labeled AS (
                SELECT e.query_id, e.job_id,
                       coalesce(p.relevance, 0) AS relevance,
                       e.original_rank, e.data_split,
                       coalesce(p.viewed, 0) AS viewed,
                       coalesce(p.applied, 0) AS applied
                FROM exposures e
                LEFT JOIN positive_labels p
                  ON e.query_id = p.query_id AND e.job_id = p.job_id
            ),
            numbered AS (
                SELECT *,
                    sum(CASE WHEN relevance = 0 THEN 1 ELSE 0 END)
                    OVER (PARTITION BY query_id ORDER BY original_rank
                          ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
                    AS negative_ordinal
                FROM labeled
            )
            SELECT query_id, job_id, relevance, original_rank, data_split, viewed, applied
            FROM numbered
            WHERE relevance > 0 OR negative_ordinal <= 20
        """)

        # Export to parquet
        labels_out = output_dir / "labels.parquet"
        queries_out = output_dir / "labeled_queries.parquet"

        con.execute(f"""
            COPY (SELECT * FROM candidate_labels ORDER BY query_id, original_rank)
            TO '{sql_path(labels_out)}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """)
        con.execute(f"""
            COPY (
                SELECT DISTINCT q.query_id, q.query,
                       cast(q.query_time AS VARCHAR) AS query_time,
                       q.data_split
                FROM queries_internal q
                JOIN candidate_labels l USING (query_id)
                ORDER BY q.query_time, q.query_id
            ) TO '{sql_path(queries_out)}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """)

        # Report
        stats = con.execute("""
            SELECT count(DISTINCT query_id) AS queries,
                   count(*) AS labels,
                   sum(CASE WHEN relevance > 0 THEN 1 ELSE 0 END) AS positives
            FROM candidate_labels
        """).fetchone()
        print(f"    Queries with labels: {stats[0]:,}, total labels: {stats[1]:,}, positives: {stats[2]:,}")

    except Exception as e:
        print(f"  ⚠ Dataset preparation failed: {e}")
        import traceback
        traceback.print_exc()
    finally:
        con.close()


def _bm25_score(query: str, title: str, requirements: str) -> float:
    """Simple BM25-like term overlap scoring for baseline."""
    import unicodedata
    import re

    def tokenize(text: str) -> set[str]:
        text = unicodedata.normalize("NFKC", text).casefold()
        tokens = re.findall(r"[a-z0-9\u4e00-\u9fff\u3400-\u4dbf]+", text)
        return set(tokens)

    query_tokens = tokenize(query)
    if not query_tokens:
        return 0.0
    doc_tokens = tokenize(title + " " + requirements)
    overlap = query_tokens & doc_tokens
    return len(overlap) / len(query_tokens)


RRF_K = 60


def _rrf_fuse(ranked_lists: list[list[str]], k: int = RRF_K) -> dict[str, float]:
    """
    Reciprocal Rank Fusion: score(d) = Σ 1/(k + rank_i(d)), rank starting at 1.

    Why rank fusion instead of a weighted score sum. Measured on 300 test
    queries the two signals are on incompatible scales — BM25 p50=7.76 while
    graph p50=0.50, a 14.6x mean ratio — so 'graph + 0.3*bm25' let BM25 supply
    ~81% of the score. Renormalizing the weights to sum to 1 does not fix this:
    ranking metrics are invariant to multiplying the whole score by a constant,
    so only the ratio matters, and at these scales any fixed ratio either
    drowns the graph or drowns BM25.

    RRF also tolerates the graph's flat score distribution (p50 == p95 == 0.50,
    i.e. most jobs tie on the same occupation-hit bonus). Ties carry no
    information in a weighted sum but do get broken by position in a ranked
    list.

    k=60 is the value from the original Cormack et al. RRF paper; it damps the
    influence of the very top ranks so one confident list cannot dominate.
    """
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, doc_id in enumerate(ranked, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return scores


def run_evaluation(index: GraphIndex, config: Step8Config) -> dict[str, Any]:
    """
    Full-retrieval evaluation: for each query, retrieve top-K jobs from the
    entire graph (or BM25 index), then check how many of the user's actual
    browsed/applied jobs appear in the retrieved set.

    This measures the graph's ability to RECALL relevant jobs, not just re-rank
    an existing exposure list.
    """
    import pandas as pd
    from metrics import ndcg_at_k, reciprocal_rank, hit_at_k, evaluate_rankings

    print("\n" + "=" * 60)
    print("EVALUATION HARNESS (full retrieval)")
    print("=" * 60)
    print(f"  Split: {config.eval_split}")
    print(f"  Limit: {config.eval_limit or 'all'}")
    print(f"  Mode: {'graph + BM25 hybrid' if config.use_graph else 'baseline (BM25-only)'}")

    queries_df, labels_df = _load_eval_labels(config.eval_split, config.eval_limit)
    if queries_df.empty:
        print("  ⚠ No queries found for this split!")
        return {"error": "no_queries", "split": config.eval_split}

    print(f"  Queries loaded: {len(queries_df):,}")
    print(f"  Labels loaded: {len(labels_df):,}")

    # Build relevance lookup: query_id → {job_id: relevance}
    relevance_lookup: dict[str, dict[str, int]] = {}
    for _, row in labels_df.iterrows():
        qid = row["query_id"]
        if qid not in relevance_lookup:
            relevance_lookup[qid] = {}
        relevance_lookup[qid][str(row["job_id"])] = int(row["relevance"])

    # Build BM25 inverted index (term → set of job_ids with that term)
    import duckdb as _duckdb
    import unicodedata
    import re

    TOP_K = 50  # retrieve this many per query

    def tokenize(text: str) -> set[str]:
        text = unicodedata.normalize("NFKC", text).casefold()
        return set(re.findall(r"[a-z0-9\u4e00-\u9fff\u3400-\u4dbf]+", text))

    print("  Building BM25 inverted index...")
    t_idx = time.time()
    train_jobs_path = GRAPH_DIR / "train_jobs.parquet"
    inverted: dict[str, set[str]] = {}  # token → set of job_ids
    job_token_counts: dict[str, int] = {}  # job_id → number of unique tokens

    con = _duckdb.connect(":memory:")
    try:
        rows = con.execute(f"""
            SELECT job_id, title,
                   concat_ws(' ', computer_skills, work_skills,
                             certifications, additional_requirements) AS requirements
            FROM read_parquet('{train_jobs_path.resolve().as_posix()}')
        """).fetchall()
    finally:
        con.close()

    for job_id, title, requirements in rows:
        jid = str(job_id)
        tokens = tokenize((title or "") + " " + (requirements or ""))
        job_token_counts[jid] = len(tokens)
        for tok in tokens:
            if tok not in inverted:
                inverted[tok] = set()
            inverted[tok].add(jid)

    print(f"  Index built: {len(inverted):,} terms, {len(job_token_counts):,} jobs in {time.time()-t_idx:.1f}s")

    def bm25_retrieve(query: str, top_k: int = TOP_K) -> list[tuple[str, float]]:
        """Retrieve top-K jobs by BM25-like scoring from inverted index."""
        query_tokens = tokenize(query)
        if not query_tokens:
            return []
        # Score = sum of IDF-weighted term hits
        import math
        N = len(job_token_counts)
        scores: dict[str, float] = {}
        for tok in query_tokens:
            posting = inverted.get(tok)
            if not posting:
                continue
            idf = math.log((N - len(posting) + 0.5) / (len(posting) + 0.5) + 1.0)
            for jid in posting:
                scores[jid] = scores.get(jid, 0.0) + idf
        # Normalize by query length
        qlen = len(query_tokens)
        ranked = sorted(scores.items(), key=lambda x: -x[1])[:top_k]
        return [(jid, score / qlen) for jid, score in ranked]

    # Score each query via full retrieval
    ranking_rows: list[dict] = []
    incremental_rows: list[dict] = []
    t0 = time.time()
    query_count = 0

    for _, qrow in queries_df.iterrows():
        query_id = qrow["query_id"]
        query_text = qrow["query"]

        rel_map = relevance_lookup.get(query_id, {})
        if not rel_map:
            continue

        bm25_ranked = [jid for jid, _ in bm25_retrieve(query_text, top_k=TOP_K)]
        graph_ranked: list[str] = []

        if config.use_graph:
            result = traverse_and_rank(
                query_text,
                index,
                top_k=TOP_K,
                use_skill_kind_weights=config.use_llm_classification,
            )
            graph_ranked = [
                job["job_id"].removeprefix("job:") for job in result.top_jobs
            ]
            retrieved = _rrf_fuse([bm25_ranked, graph_ranked])
        else:
            # B0 baseline: BM25 alone, fused through the same function so the
            # only difference between arms is the presence of the graph list.
            retrieved = _rrf_fuse([bm25_ranked])

        ranked_jobs = sorted(retrieved.items(), key=lambda x: (-x[1], x[0]))[:TOP_K]
        for jid, score in ranked_jobs:
            ranking_rows.append({
                "query_id": query_id,
                "job_id": jid,
                "score": score,
                "relevance": rel_map.get(jid, 0),
            })

        # Incremental recall: positives the graph surfaced that BM25's top-K
        # missed entirely. This is the graph's distinctive contribution and it
        # stays visible even when aggregate NDCG does not move.
        if config.use_graph:
            positives = {jid for jid, rel in rel_map.items() if rel > 0}
            if positives:
                bm25_set = set(bm25_ranked)
                graph_only = (set(graph_ranked) - bm25_set) & positives
                incremental_rows.append({
                    "query_id": query_id,
                    "positives": len(positives),
                    "bm25_found": len(bm25_set & positives),
                    "graph_only_found": len(graph_only),
                })

        query_count += 1
        if query_count % 500 == 0:
            print(f"    Scored {query_count:,} queries...")

    eval_time = time.time() - t0
    print(f"  Retrieval done: {query_count:,} queries in {eval_time:.1f}s")

    if not ranking_rows:
        print("  ⚠ No ranking rows produced!")
        return {"error": "no_rankings", "split": config.eval_split}

    rankings_df = pd.DataFrame(ranking_rows)
    metrics_summary, per_query_df = evaluate_rankings(rankings_df, k=10)

    print(f"\n  ─── Metrics (k=10) ───")
    for metric_name, value in sorted(metrics_summary.items()):
        print(f"    {metric_name}: {value:.4f}")

    # Also compute Hit@1
    hit1_rows: list[float] = []
    for _, group in rankings_df.groupby("query_id"):
        ordered = group.sort_values("score", ascending=False)
        rels = ordered["relevance"].astype(float).tolist()
        hit1_rows.append(float(bool(rels) and rels[0] > 0))
    metrics_summary["hit@1"] = sum(hit1_rows) / len(hit1_rows) if hit1_rows else 0.0
    print(f"    hit@1: {metrics_summary['hit@1']:.4f}")

    # Recall: how many of the user's positive jobs appear in our top-K?
    retrieved_by_query: dict[str, set[str]] = defaultdict(set)
    for row in ranking_rows:
        retrieved_by_query[row["query_id"]].add(row["job_id"])
    recall_rows: list[float] = []
    for qid, retrieved_for_q in retrieved_by_query.items():
        positives = {
            jid for jid, rel in relevance_lookup.get(qid, {}).items() if rel > 0
        }
        if not positives:
            continue
        recall_rows.append(len(positives & retrieved_for_q) / len(positives))
    metrics_summary[f"recall@{TOP_K}"] = (
        sum(recall_rows) / len(recall_rows) if recall_rows else 0.0
    )
    print(f"    recall@{TOP_K}: {metrics_summary[f'recall@{TOP_K}']:.4f}")

    incremental: dict[str, Any] = {}
    if incremental_rows:
        total_pos = sum(r["positives"] for r in incremental_rows)
        graph_only = sum(r["graph_only_found"] for r in incremental_rows)
        bm25_found = sum(r["bm25_found"] for r in incremental_rows)
        queries_helped = sum(1 for r in incremental_rows if r["graph_only_found"] > 0)
        incremental = {
            "queries_evaluated": len(incremental_rows),
            "positives_total": total_pos,
            "positives_found_by_bm25": bm25_found,
            "positives_found_only_by_graph": graph_only,
            "queries_where_graph_added_a_positive": queries_helped,
            "incremental_recall_share": (
                graph_only / total_pos if total_pos else 0.0
            ),
        }
        print(f"\n  ─── Graph incremental contribution ───")
        print(f"    positives found only by graph: {graph_only:,} / {total_pos:,} "
              f"({incremental['incremental_recall_share']:.2%})")
        print(f"    queries helped: {queries_helped:,} / {len(incremental_rows):,}")

    eval_report = {
        "step": "step8_evaluation",
        "run_id": config.run_id or "default",
        "eval_mode": "full_retrieval",
        "fusion": {"method": "rrf", "k": RRF_K},
        "top_k": TOP_K,
        "graph_incremental": incremental,
        "feature_flags": config.feature_flags_dict(),
        "eval_config": {
            "split": config.eval_split,
            "limit": config.eval_limit,
            "query_count": query_count,
            "label_count": len(ranking_rows),
        },
        "metrics": metrics_summary,
        "eval_time_seconds": round(eval_time, 1),
        "per_query_summary": {
            "total": len(per_query_df),
            "with_hits": int(
                (per_query_df.get("hit@10", pd.Series([0])) > 0).sum()
            ) if not per_query_df.empty else 0,
        },
    }

    return eval_report


def main(argv: list[str] | None = None) -> None:
    config = parse_step8_args(argv)

    print("=" * 60)
    print("Step 8 — Retrieval Smoke Test")
    if config.run_eval:
        print("         + Evaluation Harness (NDCG/MRR/Hit)")
    print("=" * 60)
    print(f"  Feature flags: {config.feature_flags_dict()}")
    if config.run_id:
        print(f"  Run ID: {config.run_id}")

    index = GraphIndex()
    index.load()

    results = []
    print("\n" + "─" * 60)

    for query in SMOKE_QUERIES:
        result = traverse_and_rank(
            query, index, use_skill_kind_weights=config.use_llm_classification
        )
        results.append(result)

        print(f"\n  Query: \"{query}\"")
        print(f"  Resolution: {result.resolution.resolution_log}")
        print(f"  Hits: exact={result.exact_hits:,}, expanded={result.expanded_hits:,}, "
              f"occupation={result.occupation_hits:,}")
        print(f"  Latency: {result.latency_ms:.1f}ms")
        print(f"  Trace:")
        for line in result.trace_lines[:5]:
            print(f"    {line}")
        print(f"  Top {len(result.top_jobs)} jobs:")
        for job in result.top_jobs:
            print(f"    {job['job_id']} (score={job['score']}) \"{job['title']}\"")
            print(f"      paths: {job['paths']}")

    # Export trace report
    report: dict[str, Any] = {
        "step": "step8_retrieval_smoke",
        "schema_version": "v0.1",
        "run_id": config.run_id or "smoke",
        "feature_flags": config.feature_flags_dict(),
        "queries": [
            {
                "query": r.query,
                "resolution": {
                    "skills": r.resolution.resolved_skills,
                    "occupations": r.resolution.resolved_occupations,
                    "unresolved": r.resolution.unresolved_terms,
                    "log": r.resolution.resolution_log,
                },
                "hits": {
                    "exact": r.exact_hits,
                    "expanded": r.expanded_hits,
                    "occupation": r.occupation_hits,
                },
                "trace": r.trace_lines,
                "top_jobs": r.top_jobs,
                "latency_ms": round(r.latency_ms, 1),
            }
            for r in results
        ],
    }

    report_path = GRAPH_DIR / "retrieval_smoke_report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    resolved = sum(1 for r in results if r.exact_hits > 0 or r.occupation_hits > 0)
    print(f"  Queries with hits: {resolved}/{len(results)}")
    print(f"  Average latency: {sum(r.latency_ms for r in results)/len(results):.1f}ms")
    failed = [r.query for r in results if r.exact_hits == 0 and r.occupation_hits == 0]
    if failed:
        print(f"  ⚠ No hits: {failed}")
    else:
        print("  ✓ All queries returned results")
    print(f"\n  Report: {report_path}")

    # ─────────────────────────────────────────────────────────────────────────
    # Evaluation harness (--eval mode)
    # ─────────────────────────────────────────────────────────────────────────
    if config.run_eval:
        eval_report = run_evaluation(index, config)
        eval_path = GRAPH_DIR / f"eval_report_{config.run_id or 'default'}.json"
        eval_path.write_text(
            json.dumps(eval_report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n  Eval report: {eval_path}")

    print("\n✓ Step 8 complete.")


if __name__ == "__main__":
    main()
