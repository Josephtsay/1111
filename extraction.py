from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from pydantic import ValidationError

from .models import JobSkillExtraction, RetryPolicy
from .schema import stable_json_hash


PROMPT_VERSION = "job-skill-extraction-v1"
DEFAULT_MODEL_ID = "global.anthropic.claude-sonnet-4-5-20250929-v1:0"


SYSTEM_RULES = """You extract skills from a job description into strict JSON.
Rules:
1. Extract only skills that appear verbatim in the supplied JD text.
2. Never add a skill from general knowledge.
3. Every skill must include a verbatim evidence substring.
4. Separate required, preferred, and mentioned requirements.
5. Lower confidence when it is uncertain whether a phrase is a skill.
6. Java is not JavaScript.
7. C is not C++ or C#.
8. React is not automatically React Native.
9. SQL is not automatically MySQL.
10. AWS is not Azure.
11. canonical_candidate is only a suggestion; do not decide final merging.
12. Return JSON only, without Markdown.
"""


def extraction_json_schema() -> dict[str, Any]:
    return JobSkillExtraction.model_json_schema()


def build_extraction_prompt(
    *,
    job_id: str,
    title: str,
    requirements: str,
    description: str,
    prompt_version: str = PROMPT_VERSION,
) -> str:
    source = "\n".join(
        [
            f"TITLE:\n{title or ''}",
            f"REQUIREMENTS:\n{requirements or ''}",
            f"DESCRIPTION:\n{description or ''}",
        ]
    )
    return (
        f"{SYSTEM_RULES}\nPROMPT_VERSION: {prompt_version}\n"
        f"JOB_ID: {job_id}\n\nJD SOURCE:\n{source}\n\n"
        f"JSON SCHEMA:\n{json.dumps(extraction_json_schema(), ensure_ascii=False)}"
    )


def build_bedrock_request(
    prompt: str,
    *,
    max_tokens: int = 4096,
    temperature: float = 0.0,
) -> dict[str, Any]:
    return {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [{"role": "user", "content": prompt}],
    }


def _extract_text_from_provider_response(response: Any) -> str:
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        if {"job_id", "skills", "extraction_prompt_version"} <= set(response):
            return json.dumps(response, ensure_ascii=False)
        if "output" in response and isinstance(response["output"], dict):
            message = response["output"].get("message", {})
            content = message.get("content", [])
            if content and isinstance(content[0], dict) and "text" in content[0]:
                return str(content[0]["text"])
        content = response.get("content")
        if isinstance(content, list) and content and isinstance(content[0], dict):
            if "text" in content[0]:
                return str(content[0]["text"])
        if "body" in response and isinstance(response["body"], str):
            return response["body"]
    raise ValueError("Unsupported extraction provider response shape")


def limited_json_repair(text: str) -> str:
    """Repair only wrappers/trailing commas; never invent fields or values."""
    candidate = text.strip().lstrip("\ufeff")
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate, flags=re.IGNORECASE)
        candidate = re.sub(r"\s*```$", "", candidate)
    first = candidate.find("{")
    last = candidate.rfind("}")
    if first < 0 or last < first:
        raise ValueError("No JSON object found")
    candidate = candidate[first : last + 1]
    return re.sub(r",\s*([}\]])", r"\1", candidate)


def parse_extraction_response(
    response: Any,
    *,
    source_text: str | None = None,
    expected_job_id: str | None = None,
) -> JobSkillExtraction:
    raw = _extract_text_from_provider_response(response)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = json.loads(limited_json_repair(raw))
    try:
        parsed = JobSkillExtraction.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"Extraction response failed JSON schema validation: {exc}") from exc
    if expected_job_id is not None and parsed.job_id != str(expected_job_id):
        raise ValueError(
            f"Extraction job_id mismatch: expected {expected_job_id}, got {parsed.job_id}"
        )
    if source_text is not None:
        for mention in parsed.skills:
            if mention.evidence not in source_text:
                raise ValueError(
                    f"Evidence is not a verbatim JD substring for {mention.raw_mention!r}: "
                    f"{mention.evidence!r}"
                )
            if mention.raw_mention not in mention.evidence and mention.raw_mention not in source_text:
                raise ValueError(
                    f"Raw mention not found in source for {mention.raw_mention!r}"
                )
    return parsed


class ExtractionProvider(Protocol):
    name: str
    model_id: str

    def invoke(self, request: dict[str, Any]) -> Any: ...


@dataclass
class MockExtractionProvider:
    responses: dict[str, Any]
    name: str = "offline-mock"
    model_id: str = "offline-mock-v1"
    calls: int = 0

    def invoke(self, request: dict[str, Any]) -> Any:
        self.calls += 1
        prompt = request["messages"][0]["content"]
        match = re.search(r"^JOB_ID:\s*(.+)$", prompt, flags=re.MULTILINE)
        if not match:
            raise ValueError("Mock request did not contain JOB_ID")
        job_id = match.group(1).strip()
        if job_id not in self.responses:
            raise KeyError(f"No mock response configured for job_id={job_id}")
        value = self.responses[job_id]
        return value.model_dump_json() if isinstance(value, JobSkillExtraction) else value


@dataclass
class BedrockExtractionProvider:
    """Paid provider. No client is created until invoke and explicit opt-in."""

    model_id: str = DEFAULT_MODEL_ID
    region: str = "us-east-1"
    allow_paid_api: bool = False
    name: str = "amazon-bedrock"

    def invoke(self, request: dict[str, Any]) -> Any:
        if not self.allow_paid_api:
            raise RuntimeError(
                "Bedrock call blocked. Set allow_paid_api=True only after an explicit decision."
            )
        import boto3  # Optional dependency and delayed AWS client construction.

        client = boto3.client("bedrock-runtime", region_name=self.region)
        response = client.invoke_model(
            modelId=self.model_id,
            body=json.dumps(request),
            contentType="application/json",
            accept="application/json",
        )
        return json.loads(response["body"].read())


class ExtractionCache:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def key_for(self, request: dict[str, Any], model_id: str) -> str:
        return stable_json_hash({"model_id": model_id, "request": request})

    def get(self, key: str) -> Any | None:
        path = self.root / f"{key}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def put(self, key: str, value: Any) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{key}.json"
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        return path


def invoke_with_retry(
    provider: ExtractionProvider,
    request: dict[str, Any],
    retry_policy: RetryPolicy | None = None,
    *,
    sleep: Callable[[float], None] = time.sleep,
    random_seed: int = 42,
) -> Any:
    policy = retry_policy or RetryPolicy()
    rng = random.Random(random_seed)
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return provider.invoke(request)
        except Exception as exc:
            name = exc.__class__.__name__
            retryable = name in policy.retryable_error_names or "throttl" in str(exc).lower()
            if attempt >= policy.max_attempts or not retryable:
                raise
            base = min(
                policy.initial_delay_seconds * (2 ** (attempt - 1)),
                policy.max_delay_seconds,
            )
            sleep(base + rng.random() * min(0.25, base))
    raise AssertionError("unreachable")


def extract_job(
    *,
    job: dict[str, Any],
    provider: ExtractionProvider,
    cache: ExtractionCache | None = None,
    prompt_version: str = PROMPT_VERSION,
    retry_policy: RetryPolicy | None = None,
) -> tuple[JobSkillExtraction, dict[str, Any]]:
    job_id = str(job["job_id"])
    source_text = "\n".join(
        str(job.get(field, "") or "") for field in ("title", "requirements", "description")
    )
    prompt = build_extraction_prompt(
        job_id=job_id,
        title=str(job.get("title", "") or ""),
        requirements=str(job.get("requirements", "") or ""),
        description=str(job.get("description", "") or ""),
        prompt_version=prompt_version,
    )
    request = build_bedrock_request(prompt)
    cache_key = cache.key_for(request, provider.model_id) if cache else None
    cached = cache.get(cache_key) if cache and cache_key else None
    raw = cached if cached is not None else invoke_with_retry(provider, request, retry_policy)
    parsed = parse_extraction_response(
        raw, source_text=source_text, expected_job_id=job_id
    )
    if cache and cache_key and cached is None:
        cache.put(cache_key, parsed.model_dump(mode="json"))
    manifest = {
        "job_id": job_id,
        "source_hash": stable_json_hash(source_text),
        "request_hash": stable_json_hash(request),
        "cache_key": cache_key,
        "cache_hit": cached is not None,
        "provider": provider.name,
        "model_id": provider.model_id,
        "prompt_version": prompt_version,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "success",
    }
    return parsed, manifest


def write_failed_record(
    path: str | Path, *, job_id: str, error: Exception, request_hash: str | None = None
) -> None:
    record = {
        "job_id": job_id,
        "error_type": error.__class__.__name__,
        "error": str(error),
        "request_hash": request_hash,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
