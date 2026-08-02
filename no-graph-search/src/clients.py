"""AWS Bedrock 與 OpenSearch client 工廠，以及 embedding 呼叫封裝。"""

from __future__ import annotations

import json
import logging
import random
import time
from typing import Any, Iterable, Sequence

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from config.settings import settings

logger = logging.getLogger(__name__)

# 這些錯誤可以重試（節流、暫時性服務問題）
RETRYABLE_ERROR_CODES = {
    "ThrottlingException",
    "TooManyRequestsException",
    "ModelTimeoutException",
    "ServiceUnavailableException",
    "InternalServerException",
    "ModelNotReadyException",
}

# 這些錯誤重試也不會好，而且會讓每一批都失敗 —— 必須立刻中止整輪並明確報告，
# 否則會表現成「進度條停住、CPU 0%、看起來卡死」。實際發生過：臨時 STS 憑證
# 過期後每批在 0.6 秒內失敗，但錯誤訊息只剩「ClientError」五個字。
FATAL_ERROR_CODES = {
    "ExpiredTokenException",
    "ExpiredToken",
    "InvalidSignatureException",
    "UnrecognizedClientException",
    "AccessDeniedException",
    "InvalidClientTokenId",
    "SignatureDoesNotMatch",
    "ValidationException",
    "ResourceNotFoundException",
}

# 各錯誤的具體處理建議，直接印給使用者
FATAL_HINTS = {
    "ExpiredTokenException": (
        "AWS 臨時憑證已過期。請重新取得一份 env.txt，然後執行：\n"
        "  .venv/bin/python scripts/make_env.py"
    ),
    "ExpiredToken": "AWS 臨時憑證已過期，請重貼 env.txt 後跑 scripts/make_env.py",
    "AccessDeniedException": (
        "沒有呼叫此 model 的權限。請確認 Bedrock console 已開通 model access，"
        "且 IAM 政策允許 bedrock:InvokeModel"
    ),
    "ValidationException": (
        "請求參數有誤。最常見是 model ID 不對 —— 新模型多半只支援 "
        "INFERENCE_PROFILE，必須用 us. 或 global. 前綴的 profile ID"
    ),
    "ResourceNotFoundException": "找不到指定的 model，請用 scripts/list_bedrock_models.py 確認",
}


class FatalBedrockError(RuntimeError):
    """不可重試的致命錯誤，呼叫端應立刻中止整輪作業。"""

    def __init__(self, code: str, message: str) -> None:
        hint = FATAL_HINTS.get(code, "")
        text = f"{code}: {message}"
        if hint:
            text += f"\n\n處理方式：{hint}"
        super().__init__(text)
        self.code = code

# input_type 決定 Cohere 對 query 和 document 的非對稱編碼
INPUT_TYPE_DOCUMENT = "search_document"
INPUT_TYPE_QUERY = "search_query"


def boto_session() -> boto3.Session:
    """建立 boto3 session，有設 AWS_PROFILE 就用 named profile。"""
    if settings.aws_profile:
        return boto3.Session(profile_name=settings.aws_profile, region_name=settings.aws_region)
    return boto3.Session(region_name=settings.aws_region)


def bedrock_runtime_client():
    """bedrock-runtime client。關閉 botocore 內建 retry，改由呼叫端統一控制 backoff。"""
    settings.validate_bedrock()
    return boto_session().client(
        "bedrock-runtime",
        config=BotoConfig(
            retries={"max_attempts": 1, "mode": "standard"},
            read_timeout=120,
            connect_timeout=10,
        ),
    )


def bedrock_control_client():
    """bedrock（control plane）client，用來列出可用 model。"""
    return boto_session().client("bedrock")


def opensearch_client():
    """依 OPENSEARCH_AUTH 設定建立 OpenSearch client。"""
    from opensearchpy import AWSV4SignerAuth, OpenSearch, RequestsHttpConnection

    settings.validate_opensearch()

    endpoint = settings.opensearch_endpoint.rstrip("/")
    host = endpoint.split("://", 1)[-1]
    use_ssl = not endpoint.startswith("http://")
    port = 443 if use_ssl else 9200
    if ":" in host:
        host, port_str = host.rsplit(":", 1)
        port = int(port_str)

    kwargs: dict[str, Any] = {
        "hosts": [{"host": host, "port": port}],
        "use_ssl": use_ssl,
        "verify_certs": settings.opensearch_verify_certs,
        "ssl_show_warn": settings.opensearch_verify_certs,
        "connection_class": RequestsHttpConnection,
        "timeout": 60,
        "max_retries": 3,
        "retry_on_timeout": True,
        # bulk body 有約三分之二是向量，1536 個 float 以 JSON 文字表示每個約
        # 11 bytes（binary 只要 4），gzip 實測壓 3.54x：全量 44.6 GB → 12.6 GB。
        # 從本機上傳到 us-west-2 時上傳頻寬是實際瓶頸（threads 1→8 只從 39 進到
        # 42 筆/秒，說明管道已滿而非延遲問題），所以這個壓縮直接換成時間。
        "http_compress": True,
        # parallel_bulk 用多執行緒共用連線池，預設 pool_maxsize=10 會讓
        # 執行緒數超過 10 時互相排隊。
        "pool_maxsize": 16,
    }

    if settings.opensearch_auth == "aws_iam":
        credentials = boto_session().get_credentials()
        if credentials is None:
            raise RuntimeError(
                "找不到 AWS credentials，無法對 OpenSearch 做 SigV4 簽章。"
                "請設定 AWS_PROFILE 或執行 aws configure。"
            )
        kwargs["http_auth"] = AWSV4SignerAuth(
            credentials, settings.aws_region, settings.opensearch_service
        )
    elif settings.opensearch_auth == "basic":
        kwargs["http_auth"] = (settings.opensearch_username, settings.opensearch_password)

    return OpenSearch(**kwargs)


def _sleep_backoff(attempt: int, base: float = 2.0, cap: float = 60.0) -> None:
    """exponential backoff with jitter。"""
    delay = min(cap, base**attempt) + random.uniform(0, 1)
    logger.warning("retry attempt=%d，等待 %.1fs", attempt, delay)
    time.sleep(delay)


def embed_texts(
    texts: Sequence[str],
    *,
    input_type: str = INPUT_TYPE_DOCUMENT,
    client=None,
    max_retries: int = 5,
    last_token_count: list[int | None] | None = None,
) -> list[list[float]]:
    """呼叫 Cohere Embed v4 取得 float embedding。

    Args:
        texts: 待 embed 的文本，長度不可超過 EMBED_BATCH_SIZE。
        input_type: search_document（索引用）或 search_query（查詢用）。
        client: 可傳入既有的 bedrock-runtime client 重複使用。
        max_retries: 可重試錯誤的最大重試次數。

        last_token_count: 可傳入單元素 list，呼叫後會寫入 Bedrock 回報的實際
            input token 數（取自 header），供限速器校正估算誤差。

    Returns:
        與 texts 順序一致的向量 list，每個向量長度為 EMBED_DIM。
    """
    if last_token_count is None:
        last_token_count = [None]
    if not texts:
        return []
    if len(texts) > settings.embed_batch_size:
        raise ValueError(
            f"單次最多 {settings.embed_batch_size} 筆，收到 {len(texts)} 筆。請先切批。"
        )
    if input_type not in {INPUT_TYPE_DOCUMENT, INPUT_TYPE_QUERY}:
        raise ValueError(f"input_type 必須是 {INPUT_TYPE_DOCUMENT} 或 {INPUT_TYPE_QUERY}")

    client = client or bedrock_runtime_client()
    body = json.dumps(
        {
            "input_type": input_type,
            "texts": list(texts),
            "truncate": "RIGHT",
        },
        ensure_ascii=False,
    )

    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            response = client.invoke_model(
                modelId=settings.bedrock_embed_model_id,
                body=body,
                accept="application/json",
                contentType="application/json",
            )
            # Bedrock 用 header 回報實際計費的 input token 數，這是限速器
            # 校正估算誤差的權威來源（回應 body 本身不含用量資訊）
            headers = response.get("ResponseMetadata", {}).get("HTTPHeaders", {})
            raw_count = headers.get("x-amzn-bedrock-input-token-count")
            last_token_count[0] = int(raw_count) if raw_count else None

            payload = json.loads(response["body"].read())
            return _extract_embeddings(payload, expected=len(texts))
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in FATAL_ERROR_CODES:
                # 重試無意義且每批都會失敗，包成專門的例外讓呼叫端中止整輪
                message = exc.response.get("Error", {}).get("Message", str(exc))
                raise FatalBedrockError(code, message) from exc
            if code not in RETRYABLE_ERROR_CODES:
                raise
            last_exc = exc
            _sleep_backoff(attempt)
        except Exception as exc:  # 連線層錯誤（timeout / reset）也重試
            if type(exc).__name__ not in {
                "ReadTimeoutError",
                "ConnectTimeoutError",
                "ConnectionError",
                "EndpointConnectionError",
            }:
                raise
            last_exc = exc
            _sleep_backoff(attempt)

    detail = f"{type(last_exc).__name__}: {last_exc}" if last_exc else "未知原因"
    raise RuntimeError(
        f"embed_texts 重試 {max_retries} 次仍失敗（最後一次：{detail}）"
    ) from last_exc


def _extract_embeddings(payload: dict[str, Any], *, expected: int) -> list[list[float]]:
    """從 Bedrock 回應取出 float 向量。

    Cohere v4 text-only 回傳 {"response_type": "embeddings_floats", "embeddings": [[...]]}，
    若指定 embedding_types 則回傳 {"embeddings": {"float": [[...]]}}。兩種都處理。
    """
    embeddings = payload.get("embeddings")
    if isinstance(embeddings, dict):
        for key in ("float", "floats"):
            if key in embeddings:
                embeddings = embeddings[key]
                break
        else:
            raise ValueError(f"回應中找不到 float embedding，keys={list(embeddings)}")

    if not isinstance(embeddings, list):
        raise ValueError(f"無法解析 embeddings，型別為 {type(embeddings)}")
    if len(embeddings) != expected:
        raise ValueError(f"預期 {expected} 個向量，實際收到 {len(embeddings)} 個")

    dim = len(embeddings[0])
    if dim != settings.embed_dim:
        raise ValueError(f"向量維度為 {dim}，預期 {settings.embed_dim}（檢查 EMBED_DIM 設定）")
    return embeddings


def embed_in_batches(
    texts: Iterable[str],
    *,
    input_type: str = INPUT_TYPE_DOCUMENT,
    client=None,
) -> Iterable[list[list[float]]]:
    """把 texts 切成 EMBED_BATCH_SIZE 大小逐批 embed，yield 每批的向量。"""
    client = client or bedrock_runtime_client()
    batch: list[str] = []
    for text in texts:
        batch.append(text)
        if len(batch) >= settings.embed_batch_size:
            yield embed_texts(batch, input_type=input_type, client=client)
            batch = []
    if batch:
        yield embed_texts(batch, input_type=input_type, client=client)
