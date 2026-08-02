# 部署指南 — Search API (Lambda + API Gateway)

## 架構

```
評審瀏覽器 / curl
      │
      ▼
API Gateway (HTTP API, 公開無 auth)
      │
      ▼
Lambda (FastAPI + Mangum)
      │
      ├── Bedrock (Cohere Embed v4) — 產生 query 向量
      │
      └── OpenSearch (hybrid search) — 回傳排序結果
```

## 前置需求

- AWS SAM CLI (`brew install aws-sam-cli`)
- Python 3.13
- 已建好的 OpenSearch domain（已灌入資料）
- Bedrock model access 已開通 (cohere.embed-v4)

## 本地測試

```bash
# 安裝依賴
.venv/bin/pip install -r requirements.txt

# 啟動 dev server
.venv/bin/uvicorn src.api:app --reload --port 8000

# 測試
curl "http://localhost:8000/search?ks=水電"
curl -X POST http://localhost:8000/search \
  -H "Content-Type: application/json" \
  -d '{"ks": "會計", "c0": "100100", "top_k": 5}'
```

## 部署到 AWS

```bash
# 1. Build（安裝 Lambda 需要的依賴到 .aws-sam/）
sam build

# 2. 首次部署（互動式設定參數）
sam deploy --guided

# 後續部署
sam deploy

# 部署時會問你填入的參數：
#   OpenSearchEndpoint = https://search-xxx.us-east-1.es.amazonaws.com
#   OpenSearchAuth     = basic
#   OpenSearchUsername  = (你的 FGAC 使用者)
#   OpenSearchPassword  = (你的密碼)
```

部署完成後會輸出：
```
Outputs:
  ApiUrl: https://xxxxxxxxxx.execute-api.us-east-1.amazonaws.com
```

把這個 URL 給評審就好。

## 端點

| Method | Path      | 說明 |
|--------|-----------|------|
| GET    | /health   | 存活確認 |
| GET    | /search?ks=水電&c0=100100 | 查詢（瀏覽器友好） |
| POST   | /search   | 查詢（JSON body） |

## POST /search 範例

```json
{
  "ks": "水電",
  "c0": "100221,100100",
  "top_k": 10,
  "mode": "hybrid",
  "salary_min": 35000,
  "job_types": ["全職"]
}
```

## 回應格式

```json
{
  "mode": "hybrid",
  "query": "水電",
  "took_ms": 42,
  "total": 1583,
  "filter_notes": {
    "100221": "新北市新莊區 → 升級為 新北市（100200）"
  },
  "hits": [
    {
      "score": 0.87,
      "job_id": "12345678",
      "title": "水電技師",
      "city": "新北市",
      "salary_text": "月薪‧40000‧55000",
      ...
    }
  ]
}
```

## 注意事項

- Lambda 的 IAM role 需要 `bedrock:InvokeModel` 權限（SAM template 已設定）
- OpenSearch 若用 FGAC basic auth，帳密透過環境變數傳入
- 若用 aws_iam auth，Lambda 的 execution role 需要被加入 OpenSearch 的 backend role
- Cold start 約 3-5 秒（載入 lookup 對照表 + 建立 client），warm 後 < 500ms
- Memory 建議 512MB 以上（lookup 表 + opensearch-py）
