# Skill Graph 建圖手冊（團隊共用）

> 適用對象：負責 Skill Graph 的兩位成員（以及需要對齊介面的排序 / API 成員）  
> 分支：`feat/skill-graph`  
> 目標：從 1111 職缺資料建立可用於檢索與排序的技能圖譜，並產出比賽要求的 schema 與 traversal trace  
> 文件性質：獨立於現有程式架構；描述「應做什麼」，不綁定特定檔名或類別

---

## 1. 這份文件要解決什麼

Skill Graph 是本命題的核心資產：把職缺文字中的技能抽成結構化節點與關係，讓搜尋時能做技能擴展與可解釋匹配。

本文件定義：

1. 建圖的完整步驟與交付物
2. 每一階段的關鍵決策點（開會時直接勾選）
3. 兩人分工、介面契約、時程建議
4. 開工前必須共同鎖定的 Schema v0.1

**硬性原則（比賽）：**

- Graph **只能使用 train 期 JD** 建構
- **不得**使用 test 期間 JD 建圖（違者相關指標可能不計分）
- 提案須包含 **graph schema** 與至少一個 **遍歷 / 聚合 trace** 範例
- 最終需能做「有圖譜 vs 無圖譜」的 ablation

---

## 2. 開工前必做：兩人先鎖定 Schema v0.1

**答案：是，必須先討論 Schema。**

Schema 是兩人共用的合約。若未先對齊就分頭實作，常見後果是：

- Node / Edge 類型不一致，無法合併
- ID 規則不同，無法 join
- 抽取 JSON 欄位對不上，組裝腳本反覆炸裂
- 一邊做 recall 一邊做 precision，品質標準互相打架

### 2.1 Day 0 共同會議（建議 60–90 分鐘）

議程：

1. 確認 MVP 節點 / 邊類型（本文件第 3 節）
2. 拍板 ID、時間切分、抽取 JSON 契約
3. 指定角色 A / B 與介面負責人
4. 決定品質閘門：什麼錯誤會 fail
5. 產出本文件第 9 節「決策勾選表」的填寫版

### 2.2 Day 0 交付物

| 交付物 | 說明 |
|--------|------|
| Schema v0.1 | Node / Edge / 屬性 / ID 規則 |
| 抽取 JSON 契約 | A 交給 B 的唯一格式 |
| train cutoff | 時間切分規則（與比賽公告對齊） |
| 品質政策 | fail / quarantine / warn |
| 分工表 | 誰負責什麼、誰有權改 schema |

**規則：Schema 升版需兩人同意。** 任一方不得私下新增 edge type 或改 ID 規則。

---

## 3. Schema v0.1（建議起點）

以下為建議的 MVP。兩人可增減，但必須寫進共同文件並版本化。

### 3.1 節點（Nodes）

| 類型 | 範例 | 必要屬性 | MVP |
|------|------|----------|-----|
| `Job` | `job:1370179` | `job_id`, `title`, `posted_at` / `last_modified_at` | 必做 |
| `Skill` | `skill:python` | `canonical_name`, `skill_type`（可選） | 必做 |
| `Occupation` | `occ:140200` | `occupation_code`, `name`（大/中/小類） | 必做 |
| `SkillAlias` | `alias:reactjs` | `normalized_alias` | 可選（MVP 可用字典代替） |
| `SkillCategory` | `cat:programming_language` | `name` | 後期 |

### 3.2 邊（Edges）

| 關係 | 方向 | 意義 | 建議權重來源 | MVP |
|------|------|------|--------------|-----|
| `REQUIRES` | Job → Skill | 必備技能 | 抽取判定；預設 weight `1.0` | 必做 |
| `PREFERS` | Job → Skill | 加分技能 | 預設 `0.72` | 必做 |
| `MENTIONS` | Job → Skill | 僅提及 | 預設 `0.45` | 必做 |
| `INSTANCE_OF` | Job → Occupation | 職缺所屬職類 | 職缺分類欄位；`1.0` | 必做 |
| `ALIAS_OF` | Alias → Skill 或字典映射 | 同義寫法 | 規則字典 | 必做（字典即可） |
| `CO_OCCURS_WITH` | Skill ↔ Skill | 共現關聯 | NPMI / P(B\|A) | 第二階段 |
| `CORE_SKILL` | Skill → Occupation | 職類核心技能 | 職類內出現率 | 第二階段 |
| `IS_A` | Skill → Category | 技能分類 | 字典 | 後期 |

### 3.3 ID 規則（必須 deterministic）

```text
job:<職缺編號>                 → job:1370179
skill:<normalized_canonical>   → skill:python
occ:<職務代碼或正規化職類名>    → occ:140200
alias:<normalized_alias>       → alias:reactjs   （若採用節點）
```

要求：

- 同一輸入重跑，ID 必須相同
- 正規化字串規則兩人共用（建議 NFKC + casefold + 空白壓縮）
- 禁止用「執行當下流水號」當 ID

### 3.4 抽取結果契約（A → B 唯一介面）

每份 JD 對應一筆 JSONL：

```json
{
  "job_id": "1370179",
  "skills": [
    {
      "raw_mention": "ReactJS",
      "canonical_candidate": "React",
      "requirement": "required",
      "confidence": 0.9,
      "evidence": "熟悉 ReactJS 開發",
      "method": "structured"
    }
  ],
  "extraction_version": "v0.1"
}
```

欄位約束：

| 欄位 | 說明 |
|------|------|
| `requirement` | 僅允許 `required` / `preferred` / `mentioned` |
| `evidence` | **必填**；必須能在原 JD 找到對應片段 |
| `method` | `structured` / `phrase` / `llm` |
| `canonical_candidate` | 抽取端初步建議；最終 canonical 以正規化流水線為準 |

---

## 4. 使用哪些資料

### 4.1 建圖必用

| 資料 | 用途 |
|------|------|
| `職缺.csv`（**train 期**） | 建圖主來源：標題、內容、技能欄位、職務分類、時間戳 |
| `職務對照表.csv` | Occupation 代碼 / 名稱 / `CodeAlike`（相似職稱） |

### 4.2 建圖輔助（非圖結構本體）

| 資料 | 用途 |
|------|------|
| `城市對照表.csv` | 搜尋條件解析；通常不進 Skill Graph |
| `userSearchLog` | 觀察高頻 query 詞；驗證 retrieval，不拿來建 test 洩漏圖 |
| 瀏覽 / 應徵 | 下游排序標籤；**不直接當 graph edge** |

### 4.3 職缺欄位優先序（建圖視角）

1. **高可靠**：`電腦技能資料`、`工作技能`、`專業證照`
2. **高覆蓋**：`職務名稱`、`職務內容`、`附加條件`
3. **結構邊**：`職務大類` / `中類` / `小類`、`職缺編號`、`職缺最後修改時間`

---

## 5. 完整建圖步驟

```text
0. Schema & 切分規則（兩人共同）
1. 資料準備（train JD only）
2. 技能抽取
3. 技能正規化（canonicalization）
4. Job–Skill / Job–Occupation 邊組裝
5. Skill–Skill 共現與 Occupation 核心技能
6. Graph 組裝匯出
7. 品質檢查
8. Retrieval smoke test（證明圖能用）
9. 文件定稿：schema + traversal trace
```

---

### Step 0 — Schema 與規則鎖定

**負責人：** 兩人共同  
**輸入：** 比賽命題、資料欄位說明  
**輸出：** Schema v0.1、抽取 JSON 契約、train cutoff、品質政策  

**完成定義（DoD）：**

- [ ] 節點 / 邊清單已勾選
- [ ] ID 規則已寫死
- [ ] A/B 介面格式已示例
- [ ] fail 條件已列出

---

### Step 1 — 資料準備（train-only）

**負責人：** B（結構側）為主；A 複核欄位  
**輸入：** `dataset/職缺.csv`、職務對照表  
**輸出：**

- `train_jobs`（parquet/csv）
- 切分報告：筆數、時間範圍、是否含非 train

**工作內容：**

1. 解析 `職缺最後修改時間`
2. 依比賽 / 工作坊統一切分取出 train JD
3. 保留建圖必要欄位
4. 對齊 Occupation 代碼或名稱

**關鍵決策：**

| 決策 | 建議 |
|------|------|
| cutoff 以哪個時間欄為準 | 預設 `職缺最後修改時間`；若主辦另有定義則跟公告 |
| 先全量還是 smoke | 先 1k / 10k smoke，再全量 |
| jobs scope | 優先「有可用文字內容的 train JD」 |

**風險：** 切分錯誤 = 整張圖可能因洩漏作廢。

---

### Step 2 — 技能抽取

**負責人：** A（內容側）  
**輸入：** `train_jobs`  
**輸出：** `extractions.jsonl`（符合第 3.4 節契約）

#### 2A. 結構化抽取（先做）

來源：`電腦技能資料`、`工作技能`、`專業證照`

- 依逗號 / 分號 / 換行切分
- 清洗空白與 `NULL`
- 初步標記 `requirement`（結構化欄位預設可先 `mentioned` 或依欄位語意調整）

#### 2B. 非結構化抽取（第二波）

來源：`職務名稱` + `職務內容` + `附加條件`

可選技術：

1. **Phrase matching**：用 skill 詞表掃全文（高可控）
2. **LLM 結構化抽取**：輸出 strict JSON；需記錄失敗模式（幻覺、誤合併、層級錯置）

#### REQUIRES / PREFERS / MENTIONS 判定建議

| 訊號 | 判定 |
|------|------|
| 必備、需熟悉、具備、條件 | `required` |
| 加分、佳、優先、歡迎 | `preferred` |
| 僅列出或正文順帶提及、不確定 | `mentioned` |

**關鍵決策：**

| 決策 | 建議 |
|------|------|
| 是否一開始就上 LLM | 先規則 + 結構化；LLM 補漏 |
| evidence 是否必填 | 必填 |
| 批次策略 | 1k → 10k → 全量 |

**完成定義：**

- [ ] 每筆都有 `job_id` + `skills[]`
- [ ] 隨機抽 50 筆人工抽查通過率可接受
- [ ] 已知失敗案例有清單（供比賽說明）

---

### Step 3 — 技能正規化（Canonicalization）

**負責人：** A 主編 alias；B 實作合併管線與 audit 匯出（可協作）  
**輸入：** `extractions.jsonl`  
**輸出：**

- `skill_dictionary`
- `canonicalization_audit`
- alias 對照（字典或 `ALIAS_OF` 邊）

**建議流程：**

```text
raw_mention
  → NFKC / casefold / 空白正規化
  → alias 字典 exact match
  →（可選）embedding 候選
  → verifier（保護對、門檻）
  → canonical skill
```

**保護對（不可合併）示例：**

- Java ≠ JavaScript
- C ≠ C++ ≠ C#
- React ≠ React Native
- SQL ≠ MySQL
- AWS ≠ Azure
- TensorFlow ≠ PyTorch

**關鍵決策：**

| 決策 | 建議 |
|------|------|
| 合併策略 | **precision-first：寧可漏併，不要誤併** |
| 泛用技能（溝通、認真負責） | 不進圖，或極低權重且不進 CORE_SKILL |
| 中英混寫 | alias 字典收斂到單一 canonical |

---

### Step 4 — Job–Skill / Job–Occupation 邊組裝

**負責人：** B  
**輸入：** 正規化後的 mentions + `train_jobs`  
**輸出：** Job–Skill 邊、Job–Occupation 邊（中間表或直接進 edges）

**規則建議：**

1. 同一 `(job, skill)` 多次出現 → 取最強 requirement：  
   `required > preferred > mentioned`
2. 邊保留：`weight`, `evidence`, `confidence`, `method`, `source_job_id`
3. Occupation：由職務分類建立 `INSTANCE_OF`

**完成定義：**

- [ ] 每條 Job–Skill 邊都能追溯 evidence
- [ ] 無指向不存在 Skill / Job 的邊

---

### Step 5 — 統計邊（共現 / 核心技能）

**負責人：** B；A 協助審核泛用技能黑名單  
**輸入：** train-only 的 Job–Skill 歸屬  
**輸出：** `CO_OCCURS_WITH`、`CORE_SKILL`

#### 5A. Skill–Skill 共現

對每個 train Job 的 skill set 做 pair count，再算：

- `count`, `support`
- `PMI`, `NPMI`
- 可選 `P(B|A)`, `P(A|B)`

建議起步門檻（可調）：

- `count >= 5`
- `NPMI >= 0.1`
- 每個 skill 只保留 top-N 共現（控制 supernode）

#### 5B. Occupation 核心技能

在同一 Occupation 內計算 skill 出現率：

```text
core_skill_weight = (# jobs in occ with skill) / (# jobs in occ)
```

可再拆 `required_rate` / `preferred_rate`。

**關鍵決策：**

| 決策 | 建議 |
|------|------|
| 門檻要多嚴 | 先保守；圖太稀疏再放寬 |
| supernode | 必做 top-N 或 IDF 降權 |
| Excel / Office / 中文 | 多數情況降權或排除 CORE_SKILL |

---

### Step 6 — Graph 組裝匯出

**負責人：** B  
**輸出建議：**

```text
graph/
  nodes.csv
  edges.csv
  graph_manifest.json
  skill_dictionary.csv
  canonicalization_audit.csv
```

`graph_manifest.json` 至少包含：

- schema 版本
- train 時間區間
- node / edge 計數（依 type）
- 輸入資料 hash 或檔名版本
- 是否宣稱 `contains_test_jd: false`

儲存策略（Hackathon）：

1. **先 CSV + in-memory retrieval**（最快驗證）
2. 有餘力再做 Neptune / 圖資料庫匯入計畫

---

### Step 7 — 品質檢查（Gate）

**負責人：** B 執行；A 複核內容錯誤  
**輸出：** `graph_quality_report.json`

| 檢查項 | 建議處置 |
|--------|----------|
| test JD 出現在圖中 | **fail** |
| dangling edge | **fail** |
| duplicate node/edge ID | **fail** |
| protected pair 被合併 | **fail** 或 quarantine |
| evidence 不在原文 | quarantine / warn |
| orphan Skill | warn |
| supernode 超標 | warn + 強制截斷後重跑 |

**完成定義：** 無 fail 項，方可進入 Step 8。

---

### Step 8 — Retrieval smoke test（證明圖能用）

**負責人：** 兩人共同（建議 pair session）  
**目的：** 圖不是死檔案；必須能從 query 走到 job，並留下可解釋 path。

建議固定 8–10 個 smoke query，例如：

- `node.js`
- `後端工程師`
- `React 前端`
- `護理師`
- `會計`（測非 IT）
- 拼字 / 別名：`reactjs`、`k8s`

對每個 query 記錄：

```text
1. 解析到哪些 Skill / Occupation
2. 0-hop：Skill → Job 命中數
3. 1-hop：CO_OCCURS / CORE_SKILL 擴展
4. Top 5 job_id + path 解釋
5. 明顯錯誤案例
```

**比賽交付用 trace 範例（格式可調整）：**

```text
query: "node.js 後端"
→ alias "node.js" ALIAS_OF Skill:Node.js
→ Skill:Node.js -REQUIRES→ Job:132045128 (weight=1.0)
→ Skill:Node.js -CO_OCCURS_WITH→ Skill:TypeScript -PREFERS→ Job:132111693
→ Occupation:後端工程師 boost via INSTANCE_OF
→ ranked jobs: [132045128, 132111693, ...]
```

---

### Step 9 — 文件與交接

**負責人：** 兩人共同  
**輸出：**

- Schema 正式版（可附 yaml）
- 至少 1 個完整 traversal / aggregation trace
- LLM / 抽取失敗模式與防護說明（若有用生成式 AI）
- 給排序組的介面說明：圖特徵有哪些、如何讀 edges

---

## 6. 關鍵決策點總表

開會時逐項勾選，寫入「已決」欄。

| # | 決策 | 選項 | 建議預設 | 已決 |
|---|------|------|----------|------|
| 1 | MVP 節點範圍 | 極簡 Job+Skill / +Occupation / +Alias+Category | Job+Skill+Occupation | |
| 2 | Alias 實作 | 字典 / Alias 節點 | 先字典 | |
| 3 | ID 規則 | 顯示名 / hash | `type:normalized` | |
| 4 | train cutoff | 日期時間 | 跟比賽公告 | |
| 5 | 抽取策略 | 規則優先 / LLM 優先 / 混合 | 規則→LLM 補漏 | |
| 6 | requirement 不確定時 | required / preferred / mentioned | mentioned | |
| 7 | 合併策略 | precision-first / recall-first | precision-first | |
| 8 | 共現門檻 | count / NPMI / top-N | count≥5, NPMI≥0.1, top-N | |
| 9 | 泛用技能 | 進圖 / 降權 / 排除 | 排除 CORE；慎進共現 | |
| 10 | 圖儲存 | CSV / NetworkX / Neptune | CSV 先 | |
| 11 | fail 條件 | 見 Step 7 | 洩漏與 dangling 必 fail | |
| 12 | schema 變更權 | 單人 / 雙人同意 | **雙人同意** | |

---

## 7. 兩人分工

### 7.1 角色定義

| 角色 | 暱稱 | 核心職責 |
|------|------|----------|
| **A — Content Graph** | 內容側 | 抽取、requirement 判定、alias 字典、evidence 品質、LLM prompt / 失敗模式 |
| **B — Structure Graph** | 結構側 | train 切分、ID、邊組裝、共現/CORE_SKILL、品質閘門、匯出、in-memory retrieval |

### 7.2 責任邊界（避免踩線）

| 可以做 | 不要做 |
|--------|--------|
| A 改抽取邏輯與 alias 內容 | A 直接手改 `edges.csv` 的 type 定義 |
| B 改組裝、統計邊、品質腳本 | B 擅自改抽取 JSON 欄位名稱 |
| 任一方提 schema 變更 | 未雙方同意就升版上線 |

### 7.3 介面契約

```text
A 唯一輸出 ──► extractions.jsonl（契約見 3.4）
B 唯一輸出 ──► graph/nodes.csv + edges.csv + manifest + quality report
共同輸出 ──► schema 文件 + traversal trace
```

### 7.4 建議時程（可依比賽日程壓縮）

```text
Day 0（共同 90 分）
  Schema v0.1 + JSON 契約 + cutoff + 分工確認

Day 1–2
  A: 結構化抽取 + 1k smoke extractions
  B: train_jobs 切分 + graph skeleton（可 ingest A 的 JSON）

Day 3–4
  A: 全文 phrase / LLM 補抽 + alias 字典擴充
  B: Job–Skill / Occupation 邊 + 品質檢查 v1

Day 5–6
  A: protected pairs 審核 + 失敗案例分析
  B: 共現 / CORE_SKILL + supernode 控制

Day 7（共同）
  全量建圖 → quality report → 10 query smoke → 文件定稿
```

### 7.5 每日同步（15 分鐘）

固定對齊：

1. Schema 是否有變更提案
2. A 今日產出筆數 / 抽查問題
3. B 品質報告新增 fail/warn
4. 明日 blocker

---

## 8. 端到端流程圖

```text
Day 0: Schema v0.1（兩人共同鎖定）
        │
        ▼
Step 1: train_jobs（B）
        │
        ▼
Step 2: extractions.jsonl（A）
        structured → phrase → LLM
        │
        ▼
Step 3: canonicalize（A+B）
        alias + protected pairs + audit
        │
        ▼
Step 4: Job–Skill / Job–Occupation（B）
        │
        ▼
Step 5: CO_OCCURS / CORE_SKILL（B）
        │
        ▼
Step 6–7: export + quality gate（B）
        │
        ▼
Step 8–9: smoke traversal + docs（A+B）
```

---

## 9. Day 0 決策勾選表（複本，開會填）

> 複製本節到 issue / Notion / 另一份 `SCHEMA_V0.md` 填寫。

- [ ] Node types：______________________________
- [ ] Edge types：______________________________
- [ ] ID 規則：______________________________
- [ ] train cutoff：______________________________
- [ ] 抽取 JSON 版本：`v0.1`
- [ ] 合併策略：precision-first / 其他：________
- [ ] 共現門檻：count=___, NPMI=___, top-N=___
- [ ] 圖輸出路徑：`graph/` 或其他：________
- [ ] fail 條件已確認
- [ ] A 負責人：________
- [ ] B 負責人：________
- [ ] Schema 變更需雙人同意：是

---

## 10. 常見陷阱

1. **用 test JD 建圖** → 洩漏，可能整項不計分  
2. **未先定 schema 就分頭寫** → 合併成本爆炸  
3. **alias 過度合併** → Java / JavaScript 變成同一個 skill  
4. **泛用技能變成 supernode** → 圖失去辨識力  
5. **沒有 evidence** → 無法答辯、無法做品質檢查  
6. **建完圖不做 retrieval smoke** → 圖是死資產，NDCG 不會動  
7. **A/B 搶改同一份 edges** → 用介面契約隔離  
8. **把瀏覽/應徵直接當 graph 邊** → 行為訊號應留給排序模組，避免把偏差寫進圖結構  

---

## 11. 與下游（排序 / API）的交接

Skill Graph 完成後，排序模組通常需要：

| 項目 | 說明 |
|------|------|
| 圖檔 | `nodes.csv` / `edges.csv` |
| Query → Skill 解析 | alias / occupation 對照 |
| Graph features | 如 exact skill count、path score、occupation match |
| Ablation | 關掉 graph features 仍可跑 baseline |

API 層不需要知道圖內部細節；只要最終能對 `query` / `location_code` / `duty_code` 回傳排序後的 `job_id`。

---

## 12. 分支與協作約定

- 工作分支：`feat/skill-graph`
- 大檔與產物不進 git：見根目錄 `.gitignore`（含 `dataset/`、`*.pdf`、`graphify-out/`、`outputs/`、`__pycache__/` 等）
- Schema 變更開短 PR 或至少在同步會議記錄
- 合併回主分支前：quality report 無 fail，且至少 1 個 traversal trace 可重現

---

## 13. 一頁總結

| 問題 | 答案 |
|------|------|
| 要不要先討論 schema？ | **要，Day 0 鎖定 v0.1** |
| 主要用什麼資料？ | train 期 `職缺.csv` + `職務對照表` |
| 步驟？ | Schema → train 資料 → 抽取 → 正規化 → 組邊 → 統計邊 → 匯出 → 品質 → smoke → 文件 |
| 怎麼分工？ | A 內容抽取與 alias；B 結構組裝、統計邊、品質與匯出；共同負責 schema 與 smoke |
| 何時算建圖完成？ | 品質閘門通過 + 固定 query 能產出可解釋 traversal |

---

## 附錄 A — 最小可行路徑（若時間極緊）

若只能做一條最短路徑：

1. Day 0 鎖定 Job / Skill / Occupation + REQUIRES/PREFERS/MENTIONS/INSTANCE_OF  
2. 只用三個結構化技能欄位抽取  
3. 小 alias 字典 + protected pairs  
4. 匯出 CSV  
5. 不做共現也先做 0-hop retrieval smoke  
6. 有時間再加 CO_OCCURS / CORE_SKILL / LLM  

---

## 附錄 B — 文件維護

| 項目 | 說明 |
|------|------|
| 維護者 | Skill Graph 兩位成員共同 |
| 更新時機 | Schema 升版、分工調整、品質政策變更 |
| 相關路徑 | `docs/SKILL_GRAPH_PLAYBOOK.md`（本文件） |

若本文件與比賽現場補充說明衝突，**以主辦單位現場 / 正式公告為準**，並回頭更新本文件的 cutoff 與交付條款。
