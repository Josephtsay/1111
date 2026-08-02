"""集中管理 pipeline 設定，全部從環境變數讀取（可用 .env 覆寫）。

用法：
    from config.settings import settings
    print(settings.bedrock_embed_model_id)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv 未安裝時不強制失敗
    def load_dotenv(*_args, **_kwargs):  # type: ignore[misc]
        return False


PROJECT_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env")

# .env 常留下 KEY= 這種空值。botocore 會把 AWS_PROFILE="" 當成名為 "" 的 profile
# 而丟 ProfileNotFound，所以空值一律從 environ 移除，讓預設 credential chain 生效。
for _key in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_SESSION_TOKEN", "AWS_ACCESS_KEY_ID"):
    if _key in os.environ and not os.environ[_key].strip():
        del os.environ[_key]


def _env(key: str, default: str = "") -> str:
    """讀取環境變數並去除頭尾空白，空字串視為未設定。"""
    return (os.environ.get(key) or default).strip()


def _env_int(key: str, default: int) -> int:
    raw = _env(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"環境變數 {key} 必須是整數，實際為 {raw!r}") from exc


def _env_bool(key: str, default: bool) -> bool:
    raw = _env(key).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class Settings:
    # === 路徑 ===
    project_root: Path = PROJECT_ROOT
    output_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "output")
    cache_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "cache")

    # === 原始資料 ===
    jobs_csv: Path = field(default_factory=lambda: PROJECT_ROOT / "職缺.csv")
    job_category_csv: Path = field(default_factory=lambda: PROJECT_ROOT / "職務對照表.csv")
    city_csv: Path = field(default_factory=lambda: PROJECT_ROOT / "城市對照表.csv")

    # === 中間產物 ===
    enriched_jsonl: Path = field(default_factory=lambda: PROJECT_ROOT / "output" / "enriched.jsonl")
    summaries_jsonl: Path = field(default_factory=lambda: PROJECT_ROOT / "output" / "summaries.jsonl")
    documents_jsonl: Path = field(
        default_factory=lambda: PROJECT_ROOT / "output" / "search_documents.jsonl"
    )
    vectors_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "output" / "vectors")

    # === AWS ===
    aws_region: str = field(default_factory=lambda: _env("AWS_REGION", "us-east-1"))
    aws_profile: str = field(default_factory=lambda: _env("AWS_PROFILE"))

    # === Bedrock / embedding ===
    bedrock_embed_model_id: str = field(
        default_factory=lambda: _env("BEDROCK_EMBED_MODEL_ID", "cohere.embed-v4")
    )
    embed_dim: int = field(default_factory=lambda: _env_int("EMBED_DIM", 1536))
    embed_batch_size: int = field(default_factory=lambda: _env_int("EMBED_BATCH_SIZE", 96))

    # === Task 4 摘要（由並行 agent 使用）===
    summarize_model_id: str = field(default_factory=lambda: _env("SUMMARIZE_MODEL_ID"))
    summarize_concurrency: int = field(
        default_factory=lambda: _env_int("SUMMARIZE_CONCURRENCY", 8)
    )
    # content 超過這個長度就需要 LLM 摘要
    summary_threshold_chars: int = 1000
    # 不需摘要者，embedding 取 content 前 N 字
    embed_content_prefix_chars: int = 200

    # === OpenSearch ===
    opensearch_endpoint: str = field(default_factory=lambda: _env("OPENSEARCH_ENDPOINT"))
    opensearch_index: str = field(default_factory=lambda: _env("OPENSEARCH_INDEX", "jobs_v1"))
    opensearch_auth: str = field(default_factory=lambda: _env("OPENSEARCH_AUTH", "aws_iam"))
    opensearch_username: str = field(default_factory=lambda: _env("OPENSEARCH_USERNAME"))
    opensearch_password: str = field(default_factory=lambda: _env("OPENSEARCH_PASSWORD"))
    opensearch_service: str = field(default_factory=lambda: _env("OPENSEARCH_SERVICE", "es"))
    opensearch_verify_certs: bool = field(
        default_factory=lambda: _env_bool("OPENSEARCH_VERIFY_CERTS", True)
    )
    opensearch_bulk_batch_size: int = field(
        default_factory=lambda: _env_int("OPENSEARCH_BULK_BATCH_SIZE", 500)
    )

    # === ETL ===
    # 讀 職缺.csv 的 chunk 大小（1.2GB / 120 萬筆，避免一次載入）
    csv_chunk_size: int = 50_000
    # embedding 每個分檔存幾筆
    vectors_per_shard: int = 100_000

    def ensure_dirs(self) -> None:
        """建立所有輸出目錄。"""
        for path in (self.output_dir, self.cache_dir, self.vectors_dir):
            path.mkdir(parents=True, exist_ok=True)

    def validate_bedrock(self) -> None:
        if not self.bedrock_embed_model_id:
            raise ValueError("BEDROCK_EMBED_MODEL_ID 未設定，請填入 .env")
        if not self.aws_region:
            raise ValueError("AWS_REGION 未設定，請填入 .env")

    def validate_opensearch(self) -> None:
        if not self.opensearch_endpoint:
            raise ValueError("OPENSEARCH_ENDPOINT 未設定，請填入 .env")
        if self.opensearch_auth not in {"aws_iam", "basic", "none"}:
            raise ValueError(
                f"OPENSEARCH_AUTH 必須是 aws_iam / basic / none，實際為 {self.opensearch_auth!r}"
            )
        if self.opensearch_auth == "basic" and not (
            self.opensearch_username and self.opensearch_password
        ):
            raise ValueError("OPENSEARCH_AUTH=basic 需同時設定 OPENSEARCH_USERNAME 與 OPENSEARCH_PASSWORD")


settings = Settings()
