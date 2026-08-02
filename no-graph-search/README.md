# no-graph-search

無圖譜版 hybrid search API（BM25 + kNN），獨立可部署。

## 結構

```
no-graph-search/
├── src/
│   ├── api.py              # FastAPI app（GET/POST /search, /health）
│   ├── lambda_handler.py   # Mangum adapter for Lambda
│   ├── search.py           # Query DSL builder + JobSearch class
│   ├── lookup.py           # 地區代碼升級（區→城市）
│   ├── clients.py          # OpenSearch + Bedrock client factory
│   ├── create_index.py     # 常數（VECTOR_FIELD, PIPELINE_ID）
│   └── textnorm.py         # 文本正規化
├── config/
│   └── settings.py         # 集中設定
├── cache/
│   └── lookups.pkl         # 預建好的對照表快取（不需要 CSV）
├── requirements.txt
├── template.yaml           # SAM 部署模板
├── .env.example
└── DEPLOY.md               # 部署步驟
```

## 本地跑

```bash
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env  # 填入 OpenSearch endpoint 等
.venv/bin/uvicorn src.api:app --port 8000
```

## 部署到 AWS

```bash
sam build && sam deploy --guided
```

## API 用法

```bash
# GET
curl "https://{url}/search?ks=水電&c0=100100&top_k=10"

# POST
curl -X POST "https://{url}/search" \
  -H "Content-Type: application/json" \
  -d '{"ks": "會計", "c0": "100221,100100", "salary_min": 35000}'
```

公開端點，無需 API key。
