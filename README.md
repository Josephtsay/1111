核心程式都放在：

```text
C:\Hackathon\job_skill_graph
```

真正的功能實作位於：

```text
C:\Hackathon\job_skill_graph\src\job_skill_graph
```

`notebooks` 只是教學操作介面，會呼叫 `src` 裡的程式，不是另外一套邏輯。

## 一、整體目錄

```text
job_skill_graph/
├─ src/job_skill_graph/    核心 Python 程式
├─ config/                 資料、Graph、切分設定
├─ notebooks/              教學與展示 notebook
├─ tests/                  自動測試
├─ tests/fixtures/         測試用小型資料
├─ outputs/                實際執行產物
├─ README.md               專案完整說明
├─ COMPETITION_RUNBOOK.md  比賽 AWS 操作手冊
├─ pyproject.toml          Python 套件與指令設定
└─ requirements.txt        相依套件
```

---

# 二、程式實際執行流程

```text
六份原始 CSV
  ↓ dataset_1111.py
Canonical Parquet + Weak Labels
  ↓ structured_extraction.py / extraction.py
技能抽取結果 JSONL
  ↓ canonicalization.py
技能名稱正規化
  ↓ graph_builder.py + cooccurrence.py
Skill Graph CSV
  ↓ graph_validator.py
Graph 品質檢查
  ↓ retrieval.py
Graph query expansion / candidates
  ↓ graph_features.py
Query-Job features
  ↓ ltr.py + metrics.py
LambdaMART 訓練與評估
  ↓ search_service.py
全文搜尋 + Hybrid reranking
  ↓ api.py
官方搜尋 API
```

所有命令由 [cli.py](C:/Hackathon/job_skill_graph/src/job_skill_graph/cli.py:635) 串接。

---

# 三、各程式檔案的功能

## 1. `dataset_1111.py`：處理六份真實 CSV

位置：

[dataset_1111.py](C:/Hackathon/job_skill_graph/src/job_skill_graph/dataset_1111.py:181)

這是原始資料進入系統的第一站，負責：

- 依 CSV header 自動辨識六份檔案
- 用 DuckDB 處理大型 CSV
- 展開搜尋曝光 `empStr`
- 解析曝光職缺及原始 rank
- 城市名稱對應城市代碼
- 職務名稱對應職務代碼
- 時間切分 train／validation／test
- 搜尋、瀏覽、應徵事件歸因
- 產生 relevance 0／1／2
- 阻擋未來 JD 洩漏
- 產生 Parquet 與 manifest
- 移除線上不需要的 `talentNo`

主要類別：

```python
Dataset1111Config
Dataset1111Artifacts
Dataset1111Adapter
```

執行指令：

```powershell
job-skill-graph profile-1111-data ...
job-skill-graph prepare-1111-data ...
```

輸出：

```text
jobs.parquet
train_jobs.parquet
queries.parquet
labeled_queries.parquet
labels.parquet
cities.parquet
duties.parquet
dataset_manifest.json
dataset_profile.json
```

---

## 2. `schema.py`：資料格式與 leakage 檢查

位置：

[schema.py](C:/Hackathon/job_skill_graph/src/job_skill_graph/schema.py:114)

負責：

- 檢查必要欄位
- 檢查 duplicate ID
- 解析時間
- 檢查 relevance 是否為 0／1／2
- 檢查 train／validation／test 是否重疊
- 防止 test JD 被拿去抽技能或建圖
- 計算 SHA-256
- 讀取 YAML 欄位 mapping

重要函式：

```python
validate_dataframes()
assert_train_only_jobs()
load_and_validate_csvs()
```

若發現 test leakage，會直接丟出 `TestLeakageError`，不會繼續建圖。

---

## 3. `models.py`：所有資料契約

位置：

[models.py](C:/Hackathon/job_skill_graph/src/job_skill_graph/models.py:9)

這裡放 Pydantic models 與 Enum，例如：

```python
SkillType
Requirement
SkillMention
JobSkillExtraction
CanonicalizationAudit
QueryParseResult
TraversalTrace
GraphNode
GraphEdge
ValidationFinding
```

它的作用是規定資料必須長什麼樣子。

例如技能抽取結果必須包含：

```text
raw_mention
canonical_candidate
skill_type
requirement
importance
confidence
evidence
```

LLM 或 deterministic extractor 的輸出都必須符合這裡的 schema。

---

## 4. `structured_extraction.py`：不用 LLM 的技能抽取

位置：

[structured_extraction.py](C:/Hackathon/job_skill_graph/src/job_skill_graph/structured_extraction.py:152)

這是目前已經實際使用的技能抽取器。

資料來源：

- `computerSkill`
- `certification`
- `workSkill`
- train JD 中已知技能的 phrase matching

主要功能：

- 拆解結構化技能欄位
- 技能類型分類
- 建立 train-only skill lexicon
- 使用 Aho–Corasick 搜尋 JD 文字
- 保留逐字 evidence
- 產生 strict JSONL
- 統計 coverage

重要類別：

```python
StructuredSkillExtractor
SkillPhraseMatcher
```

目前需要改善的 `REQUIRES`／`PREFERS` 判斷，也是在這個檔案加入。

執行指令：

```powershell
job-skill-graph extract-structured-skills ...
```

---

## 5. `extraction.py`：Bedrock／LLM 抽取介面

位置：

[extraction.py](C:/Hackathon/job_skill_graph/src/job_skill_graph/extraction.py:39)

這支目前已做好介面，但尚未呼叫正式 AWS。

負責：

- 建立 LLM extraction prompt
- 建立 Bedrock request body
- strict JSON schema
- 解析模型回覆
- 限制 JSON repair
- retry
- cache
- failed-record 隔離
- Bedrock provider
- Mock provider

重要類別：

```python
MockExtractionProvider
BedrockExtractionProvider
ExtractionCache
```

比賽前只能使用：

```text
prepare-extraction --dry-run
```

拿到 AWS 後才會讓 `BedrockExtractionProvider` 實際呼叫 Bedrock，補抽沒有結構化技能的 JD。

---

## 6. `canonicalization.py`：技能名稱正規化

位置：

[canonicalization.py](C:/Hackathon/job_skill_graph/src/job_skill_graph/canonicalization.py:128)

負責把不同寫法對應到 canonical skill。

例如：

```text
ReactJS
react.js
react js
→ React
```

流程：

```text
NFKC normalization
→ alias dictionary
→ exact match
→ embedding candidates
→ binary verifier
→ canonicalization audit
```

也保護容易誤合併的技能：

```text
Java ≠ JavaScript
C ≠ C++
C++ ≠ C#
React ≠ React Native
SQL ≠ MySQL
AWS ≠ Azure
```

重要類別：

```python
CanonicalizationPipeline
RuleBasedVerifier
DeterministicFakeEmbedding
```

未來 Query alias resolver 也可以共用這裡的正規化邏輯。

---

## 7. `cooccurrence.py`：技能共現與職類核心技能

位置：

[cooccurrence.py](C:/Hackathon/job_skill_graph/src/job_skill_graph/cooccurrence.py:21)

負責計算：

### 技能共現

```text
Python ↔ SQL
React ↔ TypeScript
Docker ↔ Kubernetes
```

包含：

- count
- support
- PMI
- NPMI
- `P(B|A)`
- `P(A|B)`

### 職類核心技能

```text
後端工程師 → Python
DevOps → Kubernetes
前端工程師 → React
```

重要函式：

```python
compute_cooccurrence()
compute_core_skills()
```

未來要改善：

- occupation specificity
- skill IDF
- generic skill 降權
- top-N related skills
- supernode 控制

主要會修改這個檔案。

---

## 8. `graph_builder.py`：建立完整 Skill Graph

位置：

[graph_builder.py](C:/Hackathon/job_skill_graph/src/job_skill_graph/graph_builder.py:98)

這是 Graph 的主要建構器。

負責建立：

### Nodes

```text
Job
Skill
Occupation
SkillCategory
SkillAlias
```

### Edges

```text
REQUIRES
PREFERS
MENTIONS
INSTANCE_OF
ALIAS_OF
IS_A
CO_OCCURS_WITH
CORE_SKILL
```

也負責：

- deterministic node ID
- deterministic edge ID
- source hash
- train range
- evidence
- confidence
- extraction model version
- schema version
- Neptune CSV 格式

輸出：

```text
nodes_plain.csv
edges_plain.csv
nodes_neptune.csv
edges_neptune.csv
graph_manifest.json
```

執行指令：

```powershell
job-skill-graph build-graph ...
```

未來要加入 IDF edge weight 或 occupation-specific core score，會同時修改：

- `cooccurrence.py`
- `graph_builder.py`
- `config/graph_schema.yaml`

---

## 9. `graph_validator.py`：Graph 品質閘門

位置：

[graph_validator.py](C:/Hackathon/job_skill_graph/src/job_skill_graph/graph_validator.py:29)

負責檢查：

- duplicate node／edge ID
- dangling edge
- orphan Skill
- 非法 node／edge type
- 必要 property 缺漏
- confidence／weight 合法性
- evidence 是否真的存在於 JD
- source job 是否存在
- test leakage
- protected distinction violation
- supernode
- low-support co-occurrence

嚴重錯誤可以：

- fail
- quarantine
- warn

不會默默刪除資料。

執行指令：

```powershell
job-skill-graph validate-graph ...
```

目前報告：

[graph_quality_report.json](C:/Hackathon/job_skill_graph/outputs/1111_smoke_10k/quality/graph_quality_report.json)

---

## 10. `retrieval.py`：Query 解析與 Graph traversal

位置：

[retrieval.py](C:/Hackathon/job_skill_graph/src/job_skill_graph/retrieval.py:34)

這是 Graph 線上查詢的核心。

主要流程：

```python
parse_query()
retrieve()
```

`parse_query()` 負責：

- Query 正規化
- 技能詞匹配
- alias 匹配
- occupation 匹配
- unresolved term 保存

`retrieve()` 負責：

```text
Skill → Job
Skill → Related Skill → Job
Occupation → Job
```

並輸出：

```python
Candidate
TraversalTrace
```

每個 candidate 都包含：

- job ID
- graph score
- matched skills
- traversal path
- edge weights
- explanation

目前最需要改善的 Query Understanding、職務名稱 mapping、Graph expansion，就是修改這支檔案。

---

## 11. `graph_features.py`：產生 LambdaMART 特徵

位置：

[graph_features.py](C:/Hackathon/job_skill_graph/src/job_skill_graph/graph_features.py:65)

負責把 Query＋Job＋Graph 轉成模型可以使用的數值。

### 非圖 features

- BM25
- deterministic dense score
- title overlap
- description overlap
- location match
- duty match
- freshness
- salary min/max

### Graph features

- graph score
- exact skill count
- one-hop skill count
- required match weight
- preferred match weight
- mentioned match weight
- occupation match
- best path score
- mean path score

重要函式：

```python
compute_pair_features()
add_structured_pair_features()
compute_labeled_feature_frame()
select_feature_set()
```

`compute_labeled_feature_frame()` 只計算真正觀測到的 query-job candidates，不建立數百萬職缺的 Cartesian product。

未來新增 weighted Jaccard、rare skill match、skill coverage 等 features，會放在這支檔案。

---

## 12. `search_service.py`：全文召回與 Hybrid reranking

位置：

[search_service.py](C:/Hackathon/job_skill_graph/src/job_skill_graph/search_service.py:91)

分為兩部分。

### SQLite FTS5

```python
build_sqlite_search_index()
SQLiteFTSSearchBackend
```

負責：

- 中文 unigram／bigram／trigram
- 英文 token
- BM25
- strict AND
- broad OR fallback
- location boost
- duty boost

目前用它取代尚未取得的 OpenSearch。

### Hybrid backend

```python
HybridRerankBackend
```

流程：

```text
SQLite/BM25 candidates
+ Graph candidates
→ 去重
→ 計算 features
→ LambdaMART predict
→ 排序
```

拿到 AWS 後，主要會新增一個：

```python
OpenSearchSearchBackend
```

取代 SQLite，但保留相同 `SearchBackend` 介面，Graph 和 LambdaMART 不需要整套重寫。

---

## 13. `ltr.py`：LambdaMART 訓練與消融

位置：

[ltr.py](C:/Hackathon/job_skill_graph/src/job_skill_graph/ltr.py:71)

負責：

- BM25 baseline
- LightGBM LambdaMART
- query group 建立
- model training
- model predict
- model save/load
- A/B/C/D 消融
- feature importance
- Graph win/loss/tie
- 評估報告輸出

重要內容：

```python
BM25
LambdaMARTRanker
evaluate_ablation()
save_ablation_outputs()
```

輸出：

```text
ablation_results.csv
ablation_results.json
per_query_metrics.csv
ablation_details.json
ablation_report.md
lambdamart_C.txt
lambdamart_D.txt
```

---

## 14. `metrics.py`：官方評估指標

位置：

[metrics.py](C:/Hackathon/job_skill_graph/src/job_skill_graph/metrics.py:9)

實作：

- DCG
- NDCG@10
- Precision@10
- Top-1 relevance
- MRR
- Hit@10
- Paired bootstrap

重要函式：

```python
ndcg_at_k()
precision_at_k()
top1_relevance()
reciprocal_rank()
evaluate_rankings()
paired_bootstrap_difference()
```

這支只計算評估，不負責模型訓練。

---

## 15. `api.py`：官方 FastAPI

位置：

[api.py](C:/Hackathon/job_skill_graph/src/job_skill_graph/api.py:63)

負責：

- Request schema
- Response schema
- `/health`
- `POST /api/v1/jobs/search`
- request ID
- rank 連續性
- job ID 唯一性
- Pydantic validation

主要模型：

```python
JobSearchRequest
RankedJob
JobSearchResponse
```

API 本身不決定使用 SQLite、OpenSearch 或 Graph；它只呼叫抽象的 `SearchBackend`。

因此比賽後替換 AWS backend 時，不需要改官方 API 格式。

---

## 16. `embeddings.py`：Embedding 與 Neptune dry-run

位置：

[embeddings.py](C:/Hackathon/job_skill_graph/src/job_skill_graph/embeddings.py:24)

目前提供：

- deterministic embedding
- Bedrock Titan embedding provider
- Skill embedding text
- Occupation embedding text
- embedding 儲存
- Neptune import plan
- AWS write guard

重要類別與函式：

```python
DeterministicEmbeddingProvider
BedrockTitanEmbeddingProvider
embed_graph_nodes()
build_neptune_import_plan()
execute_neptune_import()
```

`execute_neptune_import()` 有安全限制。現在不會真的上傳或呼叫 Neptune loader。

拿到 AWS 後才會：

- 選擇正式 embedding model
- 建立 OpenSearch kNN index
- 視需要執行 Neptune import

---

## 17. `cli.py`：所有命令的入口

位置：

[cli.py](C:/Hackathon/job_skill_graph/src/job_skill_graph/cli.py:778)

安裝專案後：

```powershell
job-skill-graph ...
```

實際會執行：

```python
job_skill_graph.cli:main
```

這由 [pyproject.toml](C:/Hackathon/job_skill_graph/pyproject.toml:34) 設定。

目前指令：

```text
profile-1111-data
prepare-1111-data
extract-structured-skills
prepare-extraction
import-extraction
canonicalize
build-graph
validate-graph
build-features
evaluate-ablation
build-search-index
serve-api
synthetic-demo
```

`cli.py` 負責串流程，但演算法邏輯仍放在各自模組，不應把新演算法全部寫進 CLI。

---

# 四、設定檔放在哪裡

## `config/dataset_1111.yaml`

位置：

[dataset_1111.yaml](C:/Hackathon/job_skill_graph/config/dataset_1111.yaml)

記錄：

- train／validation／test cutoff
- 瀏覽 attribution window
- 應徵 attribution window
- negative cap
- leakage policy
- privacy policy

## `config/graph_schema.yaml`

位置：

[graph_schema.yaml](C:/Hackathon/job_skill_graph/config/graph_schema.yaml)

記錄：

- node types
- edge types
- requirement weights
- alias dictionary
- protected distinctions
- co-occurrence 門檻
- retrieval hops
- hop decay
- embedding 設定
- Graph quality policy

未來調整 Graph edge weight、supernode threshold、NPMI threshold，主要改這裡。

## `column_mapping.*.yaml`

用途是把外部 CSV 欄位名稱映射成 canonical 欄位。

真實六份 1111 CSV 已有專用 adapter，所以主要是 synthetic demo 和其他外部資料會用到。

---

# 五、Notebook 放在哪裡

位置：

```text
C:\Hackathon\job_skill_graph\notebooks
```

順序：

1. [資料 schema](C:/Hackathon/job_skill_graph/notebooks/step_1_2_job_skill_schema.ipynb)
2. [技能抽取](C:/Hackathon/job_skill_graph/notebooks/step_1_3_job_skill_extraction.ipynb)
3. [Canonicalization](C:/Hackathon/job_skill_graph/notebooks/step_1_3_5_skill_canonicalization.ipynb)
4. [建圖](C:/Hackathon/job_skill_graph/notebooks/step_1_4_job_skill_graph_building.ipynb)
5. [Graph 品質](C:/Hackathon/job_skill_graph/notebooks/step_1_5_job_skill_graph_evaluation.ipynb)
6. [Embedding](C:/Hackathon/job_skill_graph/notebooks/step_2_1_skill_embedding.ipynb)
7. [Neptune dry-run](C:/Hackathon/job_skill_graph/notebooks/step_2_2_neptune_import.ipynb)
8. [Graph retrieval/features](C:/Hackathon/job_skill_graph/notebooks/step_2_3_graph_retrieval_and_features.ipynb)
9. [LTR ablation](C:/Hackathon/job_skill_graph/notebooks/step_3_1_ltr_ablation.ipynb)

Notebook 的定位是：

- 解釋
- 展示資料
- 顯示結果
- Demo

正式 pipeline 應使用 CLI 或直接 import `src` 模組，不依賴 notebook 執行順序。

---

# 六、測試放在哪裡

位置：

```text
C:\Hackathon\job_skill_graph\tests
```

主要測試：

| 檔案 | 測試內容 |
|---|---|
| `test_dataset_1111.py` | 六份 CSV adapter、時間切分、歸因 |
| `test_structured_extraction.py` | 結構化技能抽取 |
| `test_extraction.py` | LLM extraction 契約 |
| `test_canonicalization.py` | alias 與 protected pairs |
| `test_graph_builder.py` | Graph 建構 |
| `test_graph_validator.py` | Graph 品質檢查 |
| `test_no_test_leakage.py` | 防止 test JD 洩漏 |
| `test_graph_features.py` | Graph features |
| `test_retrieval_features_metrics.py` | retrieval、features、metrics |
| `test_ltr_persistence.py` | 模型保存與讀取 |
| `test_search_api.py` | 官方 API |

執行：

```powershell
cd C:\Hackathon\job_skill_graph
.\.venv\Scripts\python.exe -m pytest -q
```

目前 70 個測試全部通過。

---

# 七、實際執行結果放在哪裡

真實 10k smoke run：

```text
C:\Hackathon\job_skill_graph\outputs\1111_smoke_10k
```

重要產物：

| 路徑 | 功能 |
|---|---|
| `jobs.parquet` | 83,326 個曝光職缺 |
| `train_jobs.parquet` | train-only JD |
| `queries.parquet` | 10k 搜尋 |
| `labels.parquet` | relevance 標籤 |
| `structured_extractions.jsonl` | 技能抽取結果 |
| `graph/` | Graph nodes／edges |
| `quality/` | Graph 品質報告 |
| `features.parquet` | LTR features |
| `evaluation/` | A/B/C/D 評估與模型 |
| `jobs_fts.sqlite` | 本機全文搜尋 index |

這些是產物，不是原始碼。修改演算法時應修改 `src`，再重新產生 `outputs`，不要直接手改輸出檔。

---

# 八、之後要改進時分別修改哪裡

| 要改的功能 | 主要程式 |
|---|---|
| Query alias／職務解析 | `retrieval.py` |
| 拼字修正 | 新增 `query_understanding.py` |
| `REQUIRES`／`PREFERS` | `structured_extraction.py` |
| Bedrock 技能抽取 | `extraction.py` |
| Entity resolution | `canonicalization.py` |
| Skill IDF | `cooccurrence.py`、`graph_builder.py` |
| CORE_SKILL 改善 | `cooccurrence.py` |
| Supernode 降權 | `graph_builder.py`、`graph_schema.yaml` |
| 新 Graph features | `graph_features.py` |
| Vector recall | 新增 `vector_search.py` 或 OpenSearch backend |
| OpenSearch | `search_service.py` 新增 backend |
| LTR 模型／loss | `ltr.py` |
| 評估指標 | `metrics.py` |
| API 欄位 | `api.py` |
| Neptune import | `embeddings.py`、Neptune notebook |
| CLI 新指令 | `cli.py` |
| 防洩漏規則 | `schema.py`、`dataset_1111.py` |

最重要的原則是：資料處理、Graph、排序、API 各自分開。未來替換 OpenSearch、Bedrock 或 Neptune 時，只替換對應模組，不需要把整個專案重寫。
