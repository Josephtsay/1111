from __future__ import annotations

import json
import re
from collections import Counter, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

from .canonicalization import normalize_skill_text
from .models import JobSkillExtraction, Requirement, SkillMention, SkillType
from .schema import stable_json_hash


PROGRAMMING_LANGUAGES = {
    "python",
    "java",
    "javascript",
    "typescript",
    "c",
    "c++",
    "c#",
    "go",
    "golang",
    "rust",
    "ruby",
    "php",
    "kotlin",
    "swift",
    "scala",
    "r",
    "matlab",
    "objective-c",
    "visual basic",
    "vb.net",
}
FRAMEWORKS = {
    "react",
    "react native",
    "angular",
    "vue",
    "vue.js",
    "django",
    "flask",
    "fastapi",
    "spring",
    "spring boot",
    ".net",
    ".net core",
    "node.js",
    "nestjs",
    "next.js",
    "laravel",
}
DATABASES = {
    "sql",
    "mysql",
    "postgresql",
    "postgres",
    "oracle",
    "sql server",
    "mongodb",
    "redis",
    "elasticsearch",
    "sqlite",
}
CLOUD_PLATFORMS = {
    "aws",
    "amazon web services",
    "azure",
    "gcp",
    "google cloud",
}
DEVOPS_TOOLS = {
    "docker",
    "kubernetes",
    "k8s",
    "jenkins",
    "gitlab ci",
    "github actions",
    "terraform",
    "ansible",
    "git",
}
AI_ML_SKILLS = {
    "tensorflow",
    "pytorch",
    "scikit-learn",
    "keras",
    "machine learning",
    "deep learning",
    "llm",
}
DATA_TOOLS = {
    "excel",
    "power bi",
    "tableau",
    "spark",
    "hadoop",
    "airflow",
    "kafka",
}
OPERATING_SYSTEMS = {"linux", "windows", "unix", "macos", "android", "ios"}
IGNORED_VALUES = {"", "null", "none", "無", "不拘", "其他", "略"}


def classify_structured_skill(
    skill: str, *, source_column: str
) -> SkillType:
    normalized = normalize_skill_text(skill)
    if source_column == "certifications":
        return SkillType.CERTIFICATION
    if source_column == "work_skills":
        return SkillType.BUSINESS_SKILL
    if normalized in PROGRAMMING_LANGUAGES:
        return SkillType.PROGRAMMING_LANGUAGE
    if normalized in FRAMEWORKS:
        return SkillType.FRAMEWORK
    if normalized in DATABASES:
        return SkillType.DATABASE
    if normalized in CLOUD_PLATFORMS:
        return SkillType.CLOUD_PLATFORM
    if normalized in DEVOPS_TOOLS:
        return SkillType.DEVOPS_TOOL
    if normalized in AI_ML_SKILLS:
        return SkillType.AI_ML
    if normalized in DATA_TOOLS:
        return SkillType.DATA_TOOL
    if normalized in OPERATING_SYSTEMS:
        return SkillType.OPERATING_SYSTEM
    return SkillType.OTHER


def split_structured_values(value: object) -> list[str]:
    if value is None:
        return []
    text = str(value).strip()
    if normalize_skill_text(text) in IGNORED_VALUES:
        return []
    values = []
    for item in re.split(r"[,，;；\n\r]+", text):
        cleaned = re.sub(r"\s+", " ", item).strip()
        normalized = normalize_skill_text(cleaned)
        if normalized not in IGNORED_VALUES and 1 <= len(cleaned) <= 120:
            values.append(cleaned)
    return list(dict.fromkeys(values))


@dataclass
class StructuredSkillExtractor:
    prompt_version: str = "1111-structured-fields-v2"
    max_skills_per_job: int = 40

    def extract(
        self,
        job: dict[str, Any],
        *,
        text_candidates: list[tuple[str, str, SkillType]] | None = None,
    ) -> JobSkillExtraction:
        sources = (
            ("computer_skills", 0.90),
            ("certifications", 0.85),
            ("work_skills", 0.80),
        )
        mentions: list[SkillMention] = []
        seen: set[str] = set()
        for source_column, importance in sources:
            for value in split_structured_values(job.get(source_column, "")):
                normalized = normalize_skill_text(value)
                if normalized in seen:
                    continue
                seen.add(normalized)
                mentions.append(
                    SkillMention(
                        raw_mention=value,
                        canonical_candidate=value,
                        skill_type=classify_structured_skill(
                            value, source_column=source_column
                        ),
                        requirement=Requirement.MENTIONED,
                        importance=importance,
                        confidence=1.0,
                        evidence=value,
                    )
                )
                if len(mentions) >= self.max_skills_per_job:
                    break
            if len(mentions) >= self.max_skills_per_job:
                break
        for evidence, canonical, skill_type in text_candidates or []:
            normalized = normalize_skill_text(canonical)
            if normalized in seen:
                continue
            seen.add(normalized)
            mentions.append(
                SkillMention(
                    raw_mention=evidence,
                    canonical_candidate=canonical,
                    skill_type=skill_type,
                    requirement=Requirement.MENTIONED,
                    importance=0.70,
                    confidence=0.90,
                    evidence=evidence,
                )
            )
            if len(mentions) >= self.max_skills_per_job:
                break
        return JobSkillExtraction(
            job_id=str(job["job_id"]),
            occupation_candidate=str(job.get("occupation_code", "") or "") or None,
            skills=mentions,
            extraction_prompt_version=self.prompt_version,
        )


class SkillPhraseMatcher:
    """Aho-Corasick matcher built only from train structured fields."""

    def __init__(self, phrases: dict[str, tuple[str, SkillType]]) -> None:
        self.transitions: list[dict[str, int]] = [{}]
        self.failures: list[int] = [0]
        self.outputs: list[list[str]] = [[]]
        self.phrases = phrases
        for normalized in phrases:
            state = 0
            for character in normalized:
                if character not in self.transitions[state]:
                    self.transitions[state][character] = len(self.transitions)
                    self.transitions.append({})
                    self.failures.append(0)
                    self.outputs.append([])
                state = self.transitions[state][character]
            self.outputs[state].append(normalized)
        queue: deque[int] = deque(self.transitions[0].values())
        while queue:
            current = queue.popleft()
            for character, nxt in self.transitions[current].items():
                queue.append(nxt)
                fallback = self.failures[current]
                while fallback and character not in self.transitions[fallback]:
                    fallback = self.failures[fallback]
                self.failures[nxt] = self.transitions[fallback].get(character, 0)
                self.outputs[nxt].extend(self.outputs[self.failures[nxt]])

    @staticmethod
    def _latin_boundary(text: str, start: int, end: int, phrase: str) -> bool:
        if not re.search(r"[a-z0-9]", phrase):
            return True
        left_ok = start == 0 or not text[start - 1].isalnum()
        right_ok = end == len(text) or not text[end].isalnum()
        return left_ok and right_ok

    def find(
        self, source_text: str, *, max_matches: int = 40
    ) -> list[tuple[str, str, SkillType]]:
        normalized_source = normalize_skill_text(source_text)
        state = 0
        matches: list[tuple[int, int, str]] = []
        for index, character in enumerate(normalized_source):
            while state and character not in self.transitions[state]:
                state = self.failures[state]
            state = self.transitions[state].get(character, 0)
            for phrase in self.outputs[state]:
                start = index - len(phrase) + 1
                end = index + 1
                if self._latin_boundary(normalized_source, start, end, phrase):
                    matches.append((start, end, phrase))
        selected: list[tuple[int, int, str]] = []
        occupied: list[tuple[int, int]] = []
        for start, end, phrase in sorted(
            matches, key=lambda item: (item[0], -(item[1] - item[0]), item[2])
        ):
            if any(start < right and end > left for left, right in occupied):
                continue
            occupied.append((start, end))
            selected.append((start, end, phrase))
            if len(selected) >= max_matches:
                break
        output = []
        for start, end, phrase in selected:
            display, skill_type = self.phrases[phrase]
            evidence = source_text[start:end]
            if normalize_skill_text(evidence) != phrase:
                found = re.search(re.escape(display), source_text, flags=re.IGNORECASE)
                if not found:
                    continue
                evidence = found.group(0)
            output.append((evidence, display, skill_type))
        return output


def _build_train_lexicon(
    connection: duckdb.DuckDBPyConnection,
    source_path: Path,
    *,
    split: str,
    min_frequency: int,
) -> dict[str, tuple[str, SkillType]]:
    escaped = source_path.resolve().as_posix().replace("'", "''")
    cursor = connection.execute(
        f"""
        SELECT computer_skills, certifications, work_skills
        FROM read_parquet('{escaped}')
        WHERE data_split = ?
        """,
        [split],
    )
    counts: Counter[str] = Counter()
    metadata: dict[str, tuple[str, SkillType]] = {}
    while rows := cursor.fetchmany(5000):
        for computer, certifications, work in rows:
            per_job: set[str] = set()
            for source_column, value in (
                ("computer_skills", computer),
                ("certifications", certifications),
                ("work_skills", work),
            ):
                for phrase in split_structured_values(value):
                    normalized = normalize_skill_text(phrase)
                    if len(normalized) < 2:
                        continue
                    per_job.add(normalized)
                    metadata.setdefault(
                        normalized,
                        (
                            phrase,
                            classify_structured_skill(
                                phrase, source_column=source_column
                            ),
                        ),
                    )
            counts.update(per_job)
    curated = (
        PROGRAMMING_LANGUAGES
        | FRAMEWORKS
        | DATABASES
        | CLOUD_PLATFORMS
        | DEVOPS_TOOLS
        | AI_ML_SKILLS
        | DATA_TOOLS
        | OPERATING_SYSTEMS
    )
    return {
        normalized: metadata[normalized]
        for normalized, count in counts.items()
        if count >= min_frequency or normalized in curated
    }


def extract_structured_skills_from_parquet(
    jobs_parquet: str | Path,
    output_jsonl: str | Path,
    *,
    split: str = "train",
    limit: int | None = None,
    batch_size: int = 5000,
    lexicon_min_frequency: int = 2,
) -> dict[str, object]:
    source = Path(jobs_parquet)
    target = Path(output_jsonl)
    if not source.is_file():
        raise FileNotFoundError(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    escaped = source.resolve().as_posix().replace("'", "''")
    limit_sql = f"LIMIT {int(limit)}" if limit is not None else ""
    extractor = StructuredSkillExtractor()
    connection = duckdb.connect()
    lexicon = _build_train_lexicon(
        connection,
        source,
        split=split,
        min_frequency=lexicon_min_frequency,
    )
    matcher = SkillPhraseMatcher(lexicon)
    cursor = connection.execute(
        f"""
        SELECT
            job_id,
            occupation_code,
            title,
            description,
            requirements,
            computer_skills,
            certifications,
            work_skills
        FROM read_parquet('{escaped}')
        WHERE data_split = ?
        ORDER BY job_id
        {limit_sql}
        """,
        [split],
    )
    job_count = 0
    mention_count = 0
    jobs_without_skills = 0
    try:
        with target.open("w", encoding="utf-8") as handle:
            while rows := cursor.fetchmany(batch_size):
                for (
                    job_id,
                    occupation_code,
                    title,
                    description,
                    requirements,
                    computer_skills,
                    certifications,
                    work_skills,
                ) in rows:
                    source_text = "\n".join(
                        str(value or "")
                        for value in (title, requirements, description)
                    )
                    extraction = extractor.extract(
                        {
                            "job_id": job_id,
                            "occupation_code": occupation_code,
                            "computer_skills": computer_skills,
                            "certifications": certifications,
                            "work_skills": work_skills,
                        },
                        text_candidates=matcher.find(source_text),
                    )
                    handle.write(extraction.model_dump_json() + "\n")
                    job_count += 1
                    mention_count += len(extraction.skills)
                    if not extraction.skills:
                        jobs_without_skills += 1
    finally:
        connection.close()
    manifest = {
        "provider": "deterministic-1111-structured-fields",
        "prompt_version": extractor.prompt_version,
        "input": str(source.resolve()),
        "input_fingerprint": stable_json_hash(
            {
                "size": source.stat().st_size,
                "mtime_ns": source.stat().st_mtime_ns,
            }
        ),
        "split": split,
        "job_count": job_count,
        "mention_count": mention_count,
        "jobs_without_structured_skills": jobs_without_skills,
        "coverage": (
            (job_count - jobs_without_skills) / job_count if job_count else 0.0
        ),
        "train_lexicon_size": len(lexicon),
        "lexicon_min_frequency": lexicon_min_frequency,
        "paid_api_calls": 0,
    }
    manifest_path = target.with_suffix(target.suffix + ".manifest.json")
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest
