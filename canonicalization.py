from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Protocol, Sequence

import numpy as np

from .models import (
    CanonicalizationAudit,
    VerificationDecision,
    VerificationResult,
)


DEFAULT_ALIASES = {
    "reactjs": "React",
    "react.js": "React",
    "react js": "React",
    "vuejs": "Vue.js",
    "vue.js": "Vue.js",
    "nodejs": "Node.js",
    "node js": "Node.js",
    "typescript": "TypeScript",
    "amazon web services": "AWS",
    "k8s": "Kubernetes",
}

DEFAULT_PROTECTED_PAIRS = {
    frozenset(("java", "javascript")),
    frozenset(("c", "c++")),
    frozenset(("c", "c#")),
    frozenset(("c++", "c#")),
    frozenset(("react", "react native")),
    frozenset(("sql", "mysql")),
    frozenset(("aws", "azure")),
    frozenset(("tensorflow", "pytorch")),
    frozenset(("node.js", "javascript")),
}


def normalize_skill_text(text: str) -> str:
    """NFKC and whitespace normalization while preserving meaningful symbols."""
    value = unicodedata.normalize("NFKC", str(text))
    value = value.replace("，", ",").replace("。", ".").replace("＃", "#")
    value = re.sub(r"\s+", " ", value.strip())
    return value.casefold()


def display_name(text: str) -> str:
    return unicodedata.normalize("NFKC", str(text)).strip()


class EmbeddingProvider(Protocol):
    dimensions: int
    model_id: str

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


@dataclass
class DeterministicFakeEmbedding:
    dimensions: int = 32
    model_id: str = "deterministic-sha256-v1"

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            tokens = re.findall(r"[\w.+#-]+", normalize_skill_text(text))
            vector = np.zeros(self.dimensions, dtype=float)
            for token in tokens or [normalize_skill_text(text)]:
                digest = hashlib.sha256(token.encode("utf-8")).digest()
                for i in range(self.dimensions):
                    vector[i] += (digest[i % len(digest)] / 127.5) - 1.0
            norm = float(np.linalg.norm(vector))
            if norm:
                vector /= norm
            vectors.append(vector.tolist())
        return vectors


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    a, b = np.asarray(left, dtype=float), np.asarray(right, dtype=float)
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(np.dot(a, b) / denom) if denom else 0.0


class BinaryVerifier(Protocol):
    def verify(
        self, raw_skill: str, candidate_skill: str, *, context: str = ""
    ) -> VerificationResult: ...


@dataclass
class RuleBasedVerifier:
    protected_pairs: set[frozenset[str]] = field(
        default_factory=lambda: set(DEFAULT_PROTECTED_PAIRS)
    )

    def verify(
        self, raw_skill: str, candidate_skill: str, *, context: str = ""
    ) -> VerificationResult:
        raw = normalize_skill_text(raw_skill)
        candidate = normalize_skill_text(candidate_skill)
        if frozenset((raw, candidate)) in self.protected_pairs:
            return VerificationResult(
                decision=VerificationDecision.RELATED_BUT_DISTINCT,
                confidence=1.0,
                reason="protected distinction rule",
            )
        if raw == candidate:
            return VerificationResult(
                decision=VerificationDecision.SAME_SKILL,
                confidence=1.0,
                reason="identical normalized text",
            )
        return VerificationResult(
            decision=VerificationDecision.AMBIGUOUS,
            confidence=0.5,
            reason="offline verifier has no high-precision proof",
        )


@dataclass
class CanonicalizationPipeline:
    aliases: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_ALIASES))
    embedding_provider: EmbeddingProvider = field(
        default_factory=DeterministicFakeEmbedding
    )
    verifier: BinaryVerifier = field(default_factory=RuleBasedVerifier)
    verifier_threshold: float = 0.90
    top_k: int = 5
    protected_pairs: set[frozenset[str]] = field(
        default_factory=lambda: set(DEFAULT_PROTECTED_PAIRS)
    )

    def __post_init__(self) -> None:
        self.aliases = {
            normalize_skill_text(alias): canonical
            for alias, canonical in self.aliases.items()
        }

    def _is_protected(self, left: str, right: str) -> bool:
        return (
            frozenset((normalize_skill_text(left), normalize_skill_text(right)))
            in self.protected_pairs
        )

    def candidates(
        self, raw_skill: str, canonical_skills: Sequence[str]
    ) -> list[tuple[str, float]]:
        unique = list(dict.fromkeys(canonical_skills))
        if not unique:
            return []
        vectors = self.embedding_provider.embed([raw_skill, *unique])
        scored = [
            (skill, cosine_similarity(vectors[0], vector))
            for skill, vector in zip(unique, vectors[1:])
        ]
        return sorted(scored, key=lambda item: (-item[1], item[0]))[: self.top_k]

    def canonicalize(
        self,
        raw_skill: str,
        *,
        canonical_skills: Sequence[str],
        canonical_index: dict[str, str] | None = None,
        source_job_id: str,
        context: str = "",
    ) -> tuple[str | None, CanonicalizationAudit]:
        normalized = normalize_skill_text(raw_skill)
        exact_canonicals = canonical_index or {
            normalize_skill_text(skill): skill for skill in canonical_skills
        }
        if normalized in self.aliases:
            selected = self.aliases[normalized]
            audit = CanonicalizationAudit(
                raw_mention=raw_skill,
                normalized_text=normalized,
                selected_skill=selected,
                candidate_list=[selected],
                similarity_scores=[1.0],
                verifier_decision=VerificationDecision.SAME_SKILL,
                verifier_confidence=1.0,
                rule_used="high_precision_alias_dictionary",
                source_job_id=source_job_id,
            )
            return selected, audit
        if normalized in exact_canonicals:
            selected = exact_canonicals[normalized]
            audit = CanonicalizationAudit(
                raw_mention=raw_skill,
                normalized_text=normalized,
                selected_skill=selected,
                candidate_list=[selected],
                similarity_scores=[1.0],
                verifier_decision=VerificationDecision.SAME_SKILL,
                verifier_confidence=1.0,
                rule_used="exact_normalized_match",
                source_job_id=source_job_id,
            )
            return selected, audit

        # The offline verifier can only prove exact/protected distinctions. Avoid
        # an O(mentions × dictionary) embedding scan that cannot approve a merge.
        candidates = (
            []
            if isinstance(self.verifier, RuleBasedVerifier)
            else self.candidates(raw_skill, canonical_skills)
        )
        selected: str | None = None
        decision = None
        confidence = None
        rule = "unresolved"
        for candidate, _ in candidates:
            if self._is_protected(raw_skill, candidate):
                continue
            result = self.verifier.verify(raw_skill, candidate, context=context)
            decision, confidence = result.decision, result.confidence
            if (
                result.decision == VerificationDecision.SAME_SKILL
                and result.confidence >= self.verifier_threshold
            ):
                selected = candidate
                rule = "embedding_plus_binary_verifier"
                break

        audit = CanonicalizationAudit(
            raw_mention=raw_skill,
            normalized_text=normalized,
            selected_skill=selected,
            candidate_list=[name for name, _ in candidates],
            similarity_scores=[round(score, 8) for _, score in candidates],
            verifier_decision=decision,
            verifier_confidence=confidence,
            rule_used=rule,
            source_job_id=source_job_id,
        )
        return selected, audit


def assert_protected_distinctions(
    alias_map: dict[str, str],
    protected_pairs: set[frozenset[str]] | None = None,
) -> None:
    pairs = protected_pairs or DEFAULT_PROTECTED_PAIRS
    normalized_map = {
        normalize_skill_text(alias): normalize_skill_text(target)
        for alias, target in alias_map.items()
    }
    for pair in pairs:
        left, right = sorted(pair)
        left_target = normalized_map.get(left, left)
        right_target = normalized_map.get(right, right)
        if left_target == right_target:
            raise ValueError(f"Protected skills were merged: {left!r} and {right!r}")
