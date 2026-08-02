"""
Query-time LLM skill resolver (closed-set → existing skill nodes).

Playbook §Step 8:
  deterministic / alias first; LLM only when no skill anchor was resolved;
  map only to existing nodes; never write back alias / registry / edges.

Usage from step8:
  skills = resolve_skills_with_llm(query, resolution, index, ...)
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROMPT = ROOT / "prompts" / "llm_query_skill_resolve_v0.1.txt"
DEFAULT_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

_SKILL_ID_RE = re.compile(r"^skill:[^\s]+$")
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)
_SEP_RE = re.compile(r"[/／、,，+＋|｜&＆;；\s]+")


def _norm(token: str) -> str:
    text = unicodedata.normalize("NFKC", token)
    text = text.casefold()
    return re.sub(r"\s+", " ", text).strip()


class SupportsGraphIndex(Protocol):
    skill_to_jobs: dict[str, Any]
    core_skills: dict[str, list[tuple[str, float]]]
    occ_parent: dict[str, str]
    occ_alias: dict[str, str]
    skill_alias: dict[str, str]

    def __getattr__(self, name: str) -> Any: ...


@dataclass
class LLMSkillResolveResult:
    skill_ids: list[str] = field(default_factory=list)
    confidence: float = 0.0
    reason: str = ""
    candidate_count: int = 0
    prompt_chars: int = 0
    latency_ms: float = 0.0
    source: str = "llm"  # llm | mock | skipped | error
    error: str = ""


def load_prompt_template(path: Path = DEFAULT_PROMPT) -> str:
    if not path.exists():
        raise FileNotFoundError(path)
    return path.read_text(encoding="utf-8")


def _skill_display(skill_id: str, index: SupportsGraphIndex) -> str:
    key = skill_id.removeprefix("skill:")
    # Prefer a short alias surface if one points here
    for alias, canonical in getattr(index, "skill_alias", {}).items():
        if canonical == skill_id and len(alias) <= 40:
            return alias
    return key.replace("_", " ")


def _global_top_skills(index: SupportsGraphIndex, limit: int) -> list[str]:
    """Rank by HAS_SKILL job count (proxy for DF)."""
    ranked = sorted(
        index.skill_to_jobs.items(),
        key=lambda kv: -len(kv[1]),
    )
    return [sid for sid, _ in ranked[:limit]]


def _core_skills_for_occ(index: SupportsGraphIndex, occ_code: str, top_n: int) -> list[str]:
    code = occ_code.removeprefix("occ:")
    out: list[str] = []
    seen: set[str] = set()
    # Walk leaf → parent so major-class CORE_SKILL can fill gaps
    cur: str | None = code
    while cur:
        for skill_id, _rate in index.core_skills.get(cur, [])[:top_n]:
            if skill_id in index.skill_to_jobs and skill_id not in seen:
                seen.add(skill_id)
                out.append(skill_id)
        cur = index.occ_parent.get(cur)
        if len(out) >= top_n:
            break
    return out[:top_n]


def build_candidate_skills(
    query: str,
    resolved_occupations: list[str],
    index: SupportsGraphIndex,
    *,
    max_candidates: int = 60,
    core_top_n: int = 12,
) -> tuple[list[str], str]:
    """
    Build closed candidate set + occupation context string.

    Priority:
      1. CORE_SKILL of resolved occupations (and parents)
      2. Light occupation guess via occ_alias on query tokens
      3. Global high-DF skills
    """
    candidates: list[str] = []
    seen: set[str] = set()
    occ_notes: list[str] = []

    def add_many(ids: list[str]) -> None:
        for sid in ids:
            if sid in index.skill_to_jobs and sid not in seen:
                seen.add(sid)
                candidates.append(sid)

    for occ in resolved_occupations:
        occ_notes.append(occ)
        add_many(_core_skills_for_occ(index, occ, core_top_n))

    if not resolved_occupations:
        normalized = _norm(query)
        tokens = [normalized] + [t for t in _SEP_RE.split(normalized) if t]
        for tok in tokens:
            if not tok:
                continue
            for key in (tok, tok.replace(" ", "_")):
                occ = index.occ_alias.get(key)
                if occ:
                    occ_notes.append(f"{occ} (guessed:{key})")
                    add_many(_core_skills_for_occ(index, occ, core_top_n))
                    break

    if len(candidates) < max_candidates:
        add_many(_global_top_skills(index, max_candidates))

    candidates = candidates[:max_candidates]
    ctx = "; ".join(occ_notes[:5]) if occ_notes else "(none)"
    return candidates, ctx


def render_prompt(
    template: str,
    *,
    query: str,
    occupation_context: str,
    candidates: list[str],
    index: SupportsGraphIndex,
    max_skills: int = 5,
) -> str:
    lines = [
        f"{sid} | {_skill_display(sid, index)}"
        for sid in candidates
    ]
    return (
        template
        .replace("{{QUERY}}", query.strip())
        .replace("{{OCCUPATION_CONTEXT}}", occupation_context)
        .replace("{{MAX_SKILLS}}", str(max_skills))
        .replace("{{CANDIDATE_SKILLS}}", "\n".join(lines))
    )


def parse_llm_skill_response(
    text: str,
    allowed: set[str],
    *,
    max_skills: int = 5,
) -> tuple[list[str], float, str]:
    cleaned = _FENCE_RE.sub("", (text or "").strip()).strip()
    # Recover outermost JSON object if model added junk
    if not cleaned.startswith("{"):
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            cleaned = cleaned[start : end + 1]
    data = json.loads(cleaned)
    raw_ids = data.get("skill_ids") or []
    if not isinstance(raw_ids, list):
        raw_ids = []
    out: list[str] = []
    for item in raw_ids:
        sid = str(item).strip()
        if not _SKILL_ID_RE.match(sid):
            continue
        if sid not in allowed:
            continue
        if sid not in out:
            out.append(sid)
        if len(out) >= max_skills:
            break
    conf = data.get("confidence", 0.0)
    try:
        confidence = float(conf)
    except (TypeError, ValueError):
        confidence = 0.0
    reason = str(data.get("reason") or "")[:80]
    return out, confidence, reason


def mock_resolve_skills(
    query: str,
    candidates: list[str],
    *,
    max_skills: int = 5,
    has_occupation_context: bool = False,
) -> LLMSkillResolveResult:
    """
    Deterministic offline stand-in for Bedrock (plumbing / no-credential runs).

    1. Prefer candidates whose surface form is contained in the query.
    2. Else, if occupation context produced the shortlist, take the leading
       CORE_SKILL-ordered candidates (same closed set the LLM would see).
    """
    q = query.casefold()
    hits: list[str] = []
    for sid in candidates:
        surface = sid.removeprefix("skill:").replace("_", " ").casefold()
        key = sid.removeprefix("skill:").casefold()
        if len(surface) >= 3 and surface in q:
            hits.append(sid)
        elif len(key) >= 3 and key in q.replace(" ", "_"):
            hits.append(sid)
        if len(hits) >= max_skills:
            break
    reason = "mock_substring"
    if not hits and has_occupation_context and candidates:
        hits = candidates[: max(1, min(3, max_skills))]
        reason = "mock_core_skill_shortlist"
    return LLMSkillResolveResult(
        skill_ids=hits,
        confidence=0.4 if hits else 0.0,
        reason=reason,
        candidate_count=len(candidates),
        source="mock",
    )


def resolve_skills_with_llm(
    query: str,
    resolved_occupations: list[str],
    index: SupportsGraphIndex,
    *,
    model_id: str = DEFAULT_MODEL,
    max_skills: int = 5,
    max_candidates: int = 60,
    prompt_path: Path = DEFAULT_PROMPT,
    mock: bool = False,
    client_invoke: Any | None = None,
) -> LLMSkillResolveResult:
    candidates, occ_ctx = build_candidate_skills(
        query,
        resolved_occupations,
        index,
        max_candidates=max_candidates,
    )
    if not candidates:
        return LLMSkillResolveResult(source="skipped", error="no_candidates")

    if mock:
        result = mock_resolve_skills(
            query,
            candidates,
            max_skills=max_skills,
            has_occupation_context=(occ_ctx != "(none)"),
        )
        result.candidate_count = len(candidates)
        return result

    template = load_prompt_template(prompt_path)
    prompt = render_prompt(
        template,
        query=query,
        occupation_context=occ_ctx,
        candidates=candidates,
        index=index,
        max_skills=max_skills,
    )
    invoke = client_invoke
    if invoke is None:
        from llm_client import invoke as bedrock_invoke, load_env

        load_env()
        invoke = bedrock_invoke

    try:
        # Keep eval / smoke from hanging on a single Bedrock stall
        # (default llm_client timeout is 300s × 3 attempts).
        resp = invoke(
            model_id,
            prompt,
            max_tokens=512,
            temperature=0.0,
            timeout=30.0,
            max_attempts=2,
        )
    except Exception as e:  # noqa: BLE001 — surface as soft failure
        return LLMSkillResolveResult(
            candidate_count=len(candidates),
            prompt_chars=len(prompt),
            source="error",
            error=str(e)[:200],
        )

    text = resp.get("text") or ""
    try:
        skills, conf, reason = parse_llm_skill_response(
            text, set(candidates), max_skills=max_skills
        )
    except Exception as e:  # noqa: BLE001
        return LLMSkillResolveResult(
            candidate_count=len(candidates),
            prompt_chars=len(prompt),
            latency_ms=float(resp.get("latency_ms") or 0.0),
            source="error",
            error=f"parse: {e}"[:200],
        )

    return LLMSkillResolveResult(
        skill_ids=skills,
        confidence=conf,
        reason=reason,
        candidate_count=len(candidates),
        prompt_chars=len(prompt),
        latency_ms=float(resp.get("latency_ms") or 0.0),
        source="llm",
    )
