"""
最小 AWS Bedrock 客戶端（Role A / LLM bake-off 用）

為什麼不用 boto3：
  這台機器的 base conda env 只有 botocore 1.29.76（2023-02），沒有 boto3，
  且該 botocore 版本沒有 bedrock / bedrock-runtime 的 service model。
  升級 botocore 會牽動 aiobotocore / s3fs 的 pin，風險大於必要。
  → 用 botocore 既有的 SigV4 signer 直接打 HTTPS，不動環境。

憑證來源：.env（AWS_DEFAULT_REGION / AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY /
AWS_SESSION_TOKEN）。憑證只讀入 process env，不寫入任何輸出檔、不列印值。

支援：
  list_foundation_models()                 # bedrock control plane
  list_inference_profiles()
  invoke(model_id, prompt, ...)            # bedrock-runtime Converse API
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent  # pipeline/ -> repo root; graph/ dataset/ fixtures/ 都掛在根目錄
DEFAULT_ENV_FILE = ROOT / ".env"


# ─────────────────────────────────────────────────────────────────────────────
# .env loading（只讀入 process env，不列印值）
# ─────────────────────────────────────────────────────────────────────────────
def load_env(path: Path = DEFAULT_ENV_FILE, *, override: bool = False) -> list[str]:
    """讀入 .env，回傳「載入到的 key 名稱」（不含值）。"""
    loaded: list[str] = []
    if not path.exists():
        return loaded
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and (override or not os.environ.get(key)):
            os.environ[key] = val
        if key:
            loaded.append(key)
    return loaded


def region() -> str:
    return (
        os.environ.get("AWS_DEFAULT_REGION")
        or os.environ.get("AWS_REGION")
        or "us-east-1"
    )


def credentials_present() -> dict[str, bool]:
    """只回報 key 是否存在，不回報值。"""
    return {
        "AWS_DEFAULT_REGION": bool(os.environ.get("AWS_DEFAULT_REGION")),
        "AWS_ACCESS_KEY_ID": bool(os.environ.get("AWS_ACCESS_KEY_ID")),
        "AWS_SECRET_ACCESS_KEY": bool(os.environ.get("AWS_SECRET_ACCESS_KEY")),
        "AWS_SESSION_TOKEN": bool(os.environ.get("AWS_SESSION_TOKEN")),
    }


class BedrockError(RuntimeError):
    def __init__(self, status: int, message: str, *, retryable: bool = False) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message
        self.retryable = retryable


# ─────────────────────────────────────────────────────────────────────────────
# SigV4 signed request
# ─────────────────────────────────────────────────────────────────────────────
def _signed_request(
    *,
    method: str,
    host_prefix: str,
    path: str,
    body: bytes | None = None,
    timeout: float = 300.0,
) -> tuple[int, bytes]:
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials

    try:
        creds = Credentials(
            access_key=os.environ["AWS_ACCESS_KEY_ID"],
            secret_key=os.environ["AWS_SECRET_ACCESS_KEY"],
            token=os.environ.get("AWS_SESSION_TOKEN"),
        )
    except KeyError as e:
        raise BedrockError(0, f"missing credential env var: {e.args[0]}") from e

    rgn = region()
    url = f"https://{host_prefix}.{rgn}.amazonaws.com{path}"
    headers = {"Content-Type": "application/json"} if body is not None else {}
    aws_req = AWSRequest(method=method, url=url, data=body, headers=headers)
    SigV4Auth(creds, "bedrock", rgn).add_auth(aws_req)

    req = urllib.request.Request(
        url, data=body, method=method, headers=dict(aws_req.headers)
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except urllib.error.URLError as e:
        raise BedrockError(0, f"network error: {e.reason}", retryable=True) from e


def _get_json(path: str, *, host_prefix: str = "bedrock") -> dict[str, Any]:
    status, payload = _signed_request(method="GET", host_prefix=host_prefix, path=path)
    text = payload.decode("utf-8", errors="replace")
    if status != 200:
        raise BedrockError(status, text[:500], retryable=status in (429, 500, 503))
    return json.loads(text)


def list_foundation_models() -> list[dict[str, Any]]:
    return _get_json("/foundation-models").get("modelSummaries", [])


def list_inference_profiles() -> list[dict[str, Any]]:
    return _get_json("/inference-profiles?maxResults=200").get(
        "inferenceProfileSummaries", []
    )


# ─────────────────────────────────────────────────────────────────────────────
# Invoke（Converse API：跨 provider 統一 I/O，含 usage tokens）
# ─────────────────────────────────────────────────────────────────────────────
def invoke(
    model_id: str,
    prompt: str,
    *,
    max_tokens: int = 4096,
    temperature: float = 0.0,
    timeout: float = 300.0,
    max_attempts: int = 3,
) -> dict[str, Any]:
    """
    回傳 {text, input_tokens, output_tokens, latency_ms, attempts, stop_reason}。
    失敗丟 BedrockError（含 status），由呼叫端決定計入哪個失敗類別。
    """
    body = json.dumps(
        {
            "messages": [{"role": "user", "content": [{"text": prompt}]}],
            "inferenceConfig": {"maxTokens": max_tokens, "temperature": temperature},
        }
    ).encode("utf-8")
    path = f"/model/{urllib.parse.quote(model_id, safe='')}/converse"

    last: BedrockError | None = None
    for attempt in range(1, max_attempts + 1):
        t0 = time.perf_counter()
        try:
            status, payload = _signed_request(
                method="POST",
                host_prefix="bedrock-runtime",
                path=path,
                body=body,
                timeout=timeout,
            )
        except BedrockError as e:
            last = e
            if e.retryable and attempt < max_attempts:
                time.sleep(min(2 ** attempt, 8))
                continue
            raise
        latency_ms = (time.perf_counter() - t0) * 1000.0
        text = payload.decode("utf-8", errors="replace")

        if status == 200:
            data = json.loads(text)
            parts = data.get("output", {}).get("message", {}).get("content", []) or []
            usage = data.get("usage", {}) or {}
            return {
                "text": "".join(p.get("text", "") for p in parts),
                "input_tokens": usage.get("inputTokens"),
                "output_tokens": usage.get("outputTokens"),
                "latency_ms": latency_ms,
                "attempts": attempt,
                "stop_reason": data.get("stopReason"),
            }

        retryable = status in (429, 500, 503)
        last = BedrockError(status, text[:400], retryable=retryable)
        if retryable and attempt < max_attempts:
            time.sleep(min(2 ** attempt, 8))
            continue
        raise last

    assert last is not None
    raise last


def main() -> None:
    """探測可用模型（只呼叫 control plane，不做推理、不產生 token 費用）。"""
    keys = load_env()
    print(f"  .env keys loaded: {sorted(set(keys))}")
    print(f"  credentials present: {credentials_present()}")
    print(f"  region: {region()}")

    try:
        models = list_foundation_models()
    except BedrockError as e:
        print(f"\n  [FAIL] list_foundation_models: {e}")
        return

    active_text = [
        m
        for m in models
        if "TEXT" in (m.get("outputModalities") or [])
        and (m.get("modelLifecycle") or {}).get("status") == "ACTIVE"
    ]
    print(f"\n  foundation models visible: {len(models)}")
    print(f"  ACTIVE text-output models: {len(active_text)}\n")
    for m in sorted(active_text, key=lambda x: str(x.get("modelId"))):
        types = ",".join(m.get("inferenceTypesSupported") or [])
        print(
            f"    {str(m.get('modelId')):<62} "
            f"{str(m.get('providerName')):<12} [{types}]"
        )

    try:
        profiles = list_inference_profiles()
        print(f"\n  inference profiles: {len(profiles)}")
        for p in profiles:
            print(f"    {p.get('inferenceProfileId')}")
    except BedrockError as e:
        print(f"\n  inference profiles unavailable: {e}")


if __name__ == "__main__":
    main()
