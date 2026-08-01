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
- LLM 必須實際參與技能抽取、正規化、分類或關係判斷，並揭露失敗模式與防護；不能只是附加展示

**創意與安全原則：**

- 創意必須形成「創新假設 → feature flag → ablation → 指標差異 → traversal trace」的可驗證鏈條，不能只增加 edge type 或展示頁
- LLM 不得只憑模型常識直接寫入正式圖；新關係必須同時有 train 語料支持、evidence、版本與 verifier 結果
- test JD、test 行為與 test query 不得回寫節點、邊、alias、registry 或統計量；OOV query 只能在查詢期間對既有節點做保守解析或 fallback
- 安全閘門優先於創意功能：任何新設計若提高 leakage、誤連或不可解釋風險，預設關閉或留在 candidate / quarantine，不為創意分犧牲穩定性

### 1.1 權威資料來源與衝突處理

| 優先用途 | 來源 | 本文件採用方式 |
|----------|------|----------------|
| 比賽約束與交付物 | `1111 人力銀行 命題文件 - 2026 雲湧智生：臺灣生成式 AI 應用黑客松競賽.pdf` | 視為 graph train-only、LLM 核心角色、schema/trace、ablation 等約束的權威來源 |
| 實際資料欄位、API、評估口徑 | `1111 人力銀行 -黑客松企業數據工作坊簡報.pdf` | 使用工作坊公布的實際欄位與 `query/location_code/duty_code` API contract |
| 實作可用欄位與值域 | `dataset/*.csv`、`dataset/README.md` | 以實際 CSV header、資料型態與值域為準，不假設文件提到但未提供的欄位 |
| 現場補充 | 8/1 主辦單位正式說明 | 可補充實作細節，但不得自行推定會覆蓋命題文件的交付規格 |

已知文件差異：命題文件預告 JD 含「上架時間戳」，但實際 `職缺.csv` 與工作坊簡報僅提供 `職缺最後修改時間`；工作坊簡報也未列出明確 train/test cutoff。故本文件不得虛構 `posted_at` 或自行指定 cutoff，需在 manifest 中記錄依據與限制。

---

## 2. 開工前必做：Schema v0.1 已鎖定

**狀態：Schema v0.1 已凍結；Gate 0 為 `conditional_pass`。**

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
5. 鎖定 Innovation Contract、feature flags 與 ablation matrix
6. 產出本文件第 9 節「決策勾選表」的填寫版

### 2.2 Day 0 交付物

| 交付物 | 說明 |
|--------|------|
| Schema v0.1 | Node / Edge / 屬性 / ID 規則 |
| 抽取 JSON 契約 | A 交給 B 的唯一格式 |
| train cutoff | 時間切分規則、時區、邊界與依據（與主辦正式切分對齊） |
| 品質政策 | fail / quarantine / warn |
| Innovation Contract | H1 / H2、feature flags、安全邊界、評估矩陣 |
| Golden slice | 固定 100–1,000 Job IDs、預期 artifact hash 與 contract test |
| 分工表 | 誰負責什麼、誰有權改 schema |

**規則：Schema 升版需兩人同意。** 任一方不得私下新增 edge type 或改 ID 規則。

### 2.3 Gate 0 鎖定紀錄

```yaml
schema_version: v0.1
schema_status: frozen
gate0_status: conditional_pass
frozen_at: "2026-08-01"
change_control: two_person_approval
full_graph_allowed: false
full_graph_blocker: official_train_cutoff_unresolved
golden_slice_allowed: true
```

`conditional_pass` 不代表 Schema 未決，而是 Schema 已可供 A/B 開工，但正式 cutoff、執行模型、部分品質門檻、A/B 姓名與 golden slice IDs 尚待填入。這些項目不可以單方面改動 Schema；cutoff 未解決前只允許固定 smoke / golden slice，不允許宣稱全量 train graph 完成。

---

## 3. Schema v0.1（已凍結版）

以下為已凍結的 MVP。Schema 描述圖中的**語意事實**；`.72`、`.45` 等排序係數不屬於 schema，應放在可調整、可做 ablation 的 ranking config。任何增減都視為 schema change，必須由兩人同意、升版並記錄 migration impact。

### 3.1 節點（Nodes）

| 類型 | 範例 | 必要屬性 | MVP |
|------|------|----------|-----|
| `Job` | `job:1370179` | `job_id`, `title`, `last_modified_at`, `source_snapshot_id`, `content_hash` | 必做 |
| `Skill` | `skill:python` | `skill_id`, `canonical_name`, `skill_kind`, `dictionary_version`；`global_job_frequency` 為 Step 5 衍生屬性 | 必做 |
| `Occupation` | `occ:140200` | `occupation_code`, `name`, `level`（`major` / `middle` / `minor`）, `parent_code` | 必做 |
| `Credential` | `credential:高考護理師執照` | `credential_id`, `canonical_name`, `credential_type` | 必做 |
| `SkillAlias` | `alias:reactjs` | `normalized_alias` | 可選；MVP 預設使用版本化字典，不建節點 |
| `SkillCategory` | `cat:programming_language` | `name` | 後期 |

補充規則：

- 資料只有 `職缺最後修改時間`，沒有刊登時間；`posted_at` 不得列為必要欄位。
- 若時程極緊，`Credential` 可暫以 `Skill.skill_kind=credential` 實作，但不可把證照與一般技能無標記混在一起。
- `train_eligible` 只存在於 Step 1 的 `train_jobs` 中間表，供切分稽核使用。最終 `graph/nodes.csv` 只收錄 `train_eligible=true` 的 Job；非 train 職缺即使帶有 `false` 旗標也不得寫入最終圖。
- `global_job_frequency` 由 B 在 Step 5 使用同一份 `statistical_eligible` policy 計算，Step 1–4 可為空值；正式匯出前必須補齊。其分子分母不得與 `CORE_SKILL.rate` 使用不同口徑。

### 3.2 邊（Edges）

| 關係 | 方向 | 意義 | 必要屬性 | MVP |
|------|------|------|----------|-----|
| `HAS_SKILL` | Job → Skill | 職缺與技能關係 | `requirement_level`, `confidence`, `source_fields`, `evidence_refs`, `evidence_count`, `extractor_version` | 必做 |
| `IN_OCCUPATION` | Job → Occupation | 職缺所屬最細可解析職類 | `mapping_status` | 必做 |
| `SUBCATEGORY_OF` | Occupation → Occupation | 小類 → 中類 → 大類 | 無 | 必做 |
| `REQUIRES_CREDENTIAL` | Job → Credential | 職缺所列專業證照或資格 | `requirement_level`, `evidence_refs` | 必做 |
| `CO_OCCURS_WITH` | Skill ↔ Skill | train Job 中的技能共現 | `count`, `support`, `npmi`, `p_b_given_a`, `p_a_given_b`, `train_window` | 第二階段 |
| `CORE_SKILL` | Occupation → Skill | 職類核心技能 | `occupation_level`, `aggregation_scope`, `job_count`, `skill_job_count`, `rate`, `required_rate`, `train_window` | 第二階段 |
| `SEMANTICALLY_RELATED` | Skill → Skill 或 Skill ↔ Skill | 經 LLM 判斷且有 train 語料雙重驗證的技能關係 | `relation_type`, `directionality`, `corpus_support`, `evidence_refs`, `llm_model`, `prompt_version`, `verification_status`, `train_window` | 創意第二階段；預設關閉 |
| `IS_A` | Skill → SkillCategory | 技能分類 | `dictionary_version` | 後期 |

`HAS_SKILL.requirement_level` 僅允許 `required` / `preferred` / `unspecified`。若排序組需要舊式 `REQUIRES` / `PREFERS` / `MENTIONS` edge type，可由 `HAS_SKILL` 產生相容 view；不要在來源資料層維護兩套互相重複的邊。

Alias 預設是 query normalization 資產，不是圖內關係。若比賽明確要求 alias 出現在 graph schema，則必須同時把 `SkillAlias` 與 `ALIAS_OF` 升為 MVP，不可只在 trace 中畫出不存在的節點或邊。

`SEMANTICALLY_RELATED` 是受控的創意擴充，不是自由生成邊。`relation_type` 只允許 `complements` / `prerequisite` / `alternative`；`prerequisite` 為有向，其餘為對稱且只存 canonical pair 一次。只有 `verification_status=verified` 的關係能匯入正式 `edges.csv`；候選、弱證據與 `unrelated` 只留 audit。不得由 test query 觸發建邊或把 OOV 詞持久化成新 Skill。

### 3.3 ID 規則（必須 deterministic）

```text
job:<職缺編號>                 → job:1370179
skill:<registry_key>           → skill:python
occ:<職務代碼>                 → occ:140200
credential:<registry_key>      → credential:高考護理師執照
alias:<normalized_alias>       → alias:reactjs   （僅在採用 Alias 節點時）
```

要求：

- 同一輸入重跑，ID 必須相同
- `Occupation` 一律使用 `職務對照表.CodeNo`，禁止用顯示名稱產生 ID
- Skill / Credential ID 由版本化 registry 產生；顯示名稱可以改，ID 不跟著改
- 正規化規則兩人共用（建議 NFKC + casefold + 空白壓縮），但不得移除具有語意的標點，例如 `C++`、`C#`、`.NET`、`Node.js`
- 必須定義 normalized key collision 的 fail / quarantine 規則
- 禁止用「執行當下流水號」當 ID

Edge deterministic key：

```text
Job–Skill              → (job_id, skill_id)
Job–Occupation         → (job_id, occupation_code)
Occupation hierarchy   → (child_occ_code, parent_occ_code)
Job–Credential         → (job_id, credential_id)
Skill co-occurrence     → (min(skill_a, skill_b), max(skill_a, skill_b))
Occupation core skill  → (occupation_code, skill_id, aggregation_scope)
Semantic prerequisite  → (relation_type, skill_a, skill_b)
Semantic symmetric     → (relation_type, min(skill_a, skill_b), max(skill_a, skill_b))
```

### 3.4 抽取結果契約（A → B 唯一介面）

每份 JD 對應一筆 JSONL：

```json
{
  "job_id": "3945298",
  "skills": [
    {
      "mention_id": "mention:3945298:職務內容:15:17:v0.1",
      "raw_mention": "護理",
      "canonical_candidate": "skill:nursing_care",
      "source_field": "職務內容",
      "start_offset": 15,
      "end_offset": 17,
      "requirement_level": "unspecified",
      "assertion_status": "affirmed",
      "confidence": 0.9,
      "evidence": "護理人員工作",
      "method": "phrase",
      "extractor_version": "v0.1"
    }
  ],
  "credentials": [
    {
      "mention_id": "mention:3945298:專業證照:1:v0.1",
      "raw_mention": "高考護理師執照",
      "canonical_candidate": "credential:高考護理師執照",
      "source_field": "專業證照",
      "start_offset": null,
      "end_offset": null,
      "requirement_level": "unspecified",
      "assertion_status": "affirmed",
      "confidence": 1.0,
      "evidence": "高考護理師執照",
      "method": "structured",
      "extractor_version": "v0.1"
    }
  ],
  "extraction_version": "v0.1"
}
```

欄位約束：

| 欄位 | 說明 |
|------|------|
| `mention_id` | deterministic；建議由 `job_id + source_field + offsets + extractor_version` 產生 |
| `source_field` | 原始來源欄位，例如 `電腦技能資料`、`工作技能`、`專業證照`、`職務內容`、`附加條件` |
| `start_offset` / `end_offset` | 非結構化文字建議必填；結構化欄位可為 `null` |
| `requirement_level` | 僅允許 `required` / `preferred` / `unspecified` |
| `assertion_status` | 僅允許 `affirmed` / `negated` / `uncertain`；MVP 至少支援 `affirmed` |
| `confidence` | `[0, 1]`；代表抽取可信度，不代表職缺相關性；不得直接作 ranking weight |
| `evidence` | **必填**；必須能在 `source_field` 對應原文找到片段 |
| `method` | `structured` / `phrase` / `llm` |
| `extractor_version` | 規則、phrase matcher 或模型版本；用於比較不同方法的 confidence |
| `canonical_candidate` | 抽取端初步建議；最終 canonical 以版本化 registry 為準 |

同一 `(job, skill)` 或 `(job, credential)` 的多筆 mention **不可在抽取階段刪除**。B 在組邊時才物化一條 `HAS_SKILL` / `REQUIRES_CREDENTIAL`，並保留全部 `evidence_refs`、來源欄位與 evidence count。

#### Confidence policy（Day 0 建議預設）

1. `confidence` 只表示 extraction reliability，用於 accept / quarantine；**不直接進 ranking score**。
2. 門檻依 `method` 分開定義於版本化 `extraction_config.yaml`，禁止用單一全域門檻比較 `structured`、`phrase`、`llm`。
3. `structured` 只有在 source-specific parser 精確命中且通過 registry 驗證時可給 `1.0`。
4. `phrase` / `llm` confidence 由規則驗證、詞表匹配、evidence grounding 等外部訊號產生；不得直接採用 LLM 自評分數。
5. 低於該 method 門檻的 mention 進 quarantine，不得物化為 graph edge。
6. 同一 edge 有多筆 accepted mentions 時，`HAS_SKILL.confidence = max(mention.confidence)`；支持強度另以 `evidence_count` 表達，不使用假設 mentions 相互獨立的機率公式。
7. ranking 若要使用 extraction confidence，必須另做校準與 ablation，並在 `ranking_config.yaml` 明示；v0.1 預設不使用。

### 3.5 Alias 字典契約

MVP 的 Skill / Occupation alias 採圖外版本化 `alias_dictionary.csv`，共用以下 schema：

| 欄位 | 說明 |
|------|------|
| `alias_key` | 正規化後 alias；NFKC + casefold + 空白壓縮，但保留語意標點 |
| `raw_alias` | 原始 alias，供 audit 與展示 |
| `entity_type` | `skill` / `occupation` |
| `canonical_id` | `skill:<registry_key>` 或 `occ:<CodeNo>` |
| `source` | `manual` / `CodeAlike` / `CodeName` / `CodeNameEN` / `extracted` |
| `language` | `zh` / `en` / `mixed` / `unknown` |
| `ambiguity_status` | `unique` / `ambiguous` / `quarantined` |
| `dictionary_version` | alias dictionary 版本 |

`CodeAlike` ingestion 規則：

1. 將 `<br>`、`<br/>`、`<br />` 與換行視為 alias delimiter；不得無條件以 `/`、`／` 切分。
2. `CodeAlike`、`CodeNameA`、`CodeNameEN` 對到該列 `CodeNo`；`CodeNameB/C` 必須對到解析後的中類 / 大類 parent CodeNo，**不可**把父層名稱映射到每個 descendant leaf。所有項目都保留原始 `source`。
3. 同一 `alias_key` 對到多個 `canonical_id` 時保留多筆候選並標記 `ambiguous`；query resolver 可利用 `duty_code`、其他 query token 或層級做 disambiguation，不得靜默任選或合併 Occupation。
4. Alias dictionary 的輸入 hash、版本與 ambiguity 計數寫入 manifest。

---

## 4. 使用哪些資料

### 4.1 建圖必用

| 資料 | 用途 |
|------|------|
| `職缺.csv`（**train 期**） | 建圖主來源：標題、內容、技能欄位、職務分類、時間戳 |
| `職務對照表.csv` | Occupation 代碼 / 名稱 / `CodeAlike`（相似職稱） |

`職缺.csv` 是 2026-06-01～06-07 資料包中的主檔，但其 `職缺最後修改時間` 範圍超過該週；**資料包期間不等於 train window**。在主辦正式 cutoff 未確認前，只可做資料剖析與 smoke，不可宣稱已完成 train-only 全量建圖。

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

### 4.4 實際資料盤點（Step 0 基線）

以下數字來自對 `dataset/` 內 CSV 的逐列掃描，供 schema 與品質門檻設計；若資料檔更新，必須重跑並更新本節或另存 audit report。

| 項目 | 觀察值 | Schema / 流程影響 |
|------|--------|-------------------|
| 職缺數 | 1,218,635；`職缺編號` 全部唯一 | `job:<職缺編號>` 可直接作 deterministic ID |
| 最後修改時間 | 2024-01-01 00:12:09.827 ～ 2026-06-24 14:59:55.470 | 必須記錄 cutoff、時區、snapshot；不可把資料包週期當 cutoff |
| 2026-06-07 後修改 | 249,894 筆 | 未確認 train policy 前不得納入 train graph |
| 任一結構化能力欄非空 | 382,758 筆（31.41%） | 結構化抽取適合先做，但無法單獨提供足夠 recall |
| `電腦技能資料` | 257,193 筆（21.11%）；約 405 個逗號切分值 | 適合建立高 precision seed dictionary；需控制 Office 類 supernode |
| `專業證照` | 111,907 筆（9.18%）；約 1,637 個逗號切分值 | 應建 `Credential` 或至少用 `skill_kind=credential` 隔離 |
| `工作技能` | 151,263 筆（12.41%）；約 1,032 個逗號切分值 | 需 source-specific parser；不可把所有中文標點一律切開 |
| Occupation | 1,217,912 筆可由大/中/小類唯一映射 CodeNo；10 筆雙重匹配；713 筆全空 | 以完整三級 tuple 映射；定義 ambiguity 與缺值政策 |
| Occupation 層級 | 1,217,452 筆映射小類；460 筆只映射中類 | `IN_OCCUPATION` 必須指向「最細可解析層級」，不可假設全是 leaf |
| 高頻泛用技能 | Excel 208,508、Word 202,720、PowerPoint 124,779 | 保留供 query 使用，但以 DF/IDF、top-N 與職類內 salience 控制權重 |

---

## 5. 完整建圖步驟

```text
0. Schema、切分、Innovation Contract（兩人共同）
1. 資料準備（train JD only）
2. 技能抽取
3. 技能正規化（canonicalization）
4. HAS_SKILL / IN_OCCUPATION / hierarchy 邊組裝
5. 共現、Occupation 核心技能、可選 verified semantic relation
6. Graph 組裝匯出
7. 品質檢查
8. Query-adaptive retrieval smoke test（證明圖能用）
9. 文件定稿：schema + traversal trace + ablation matrix
```

---

### Step 0 — Schema 與規則鎖定

**負責人：** 兩人共同  
**輸入：** 比賽命題 PDF、工作坊簡報、實際 CSV / README、主辦現場補充

**輸出：** Schema v0.1、抽取 JSON 契約、來源權威矩陣、train cutoff 決策、品質政策、Innovation Contract

#### Innovation Contract（Day 0 一起鎖定）

創意主軸定義為 **Evidence-Calibrated, Query-Adaptive Skill Graph**：LLM 從 train JD 建立有 evidence、可驗證的技能語意；查詢端依意圖選擇最小必要 traversal，而不是對所有 query 固定擴展。這直接回應命題的創意與 GenAI 主題契合評分，但以主指標、安全與可解釋性作啟用條件。

| 項目 | 建議鎖定內容 |
|------|--------------|
| 創新假設 H1 | LLM 抽取 / 正規化驗證能改善 alias、縮寫與 OOV query 的 recall，且不明顯降低 precision |
| 創新假設 H2 | query-adaptive traversal 比固定 1-hop 擴展有更好的 NDCG@10，並降低錯誤擴展與 latency |
| Feature flags | `use_graph`, `use_llm_extraction`, `use_llm_relations`, `use_adaptive_traversal` |
| 安全限制 | train-only；LLM 關係需 dual-signal verification；query-time OOV 不回寫圖；負向條件不得轉成正向 skill |
| 評估 | NDCG@10、MRR、Hit@1、Hit@10、latency、錯誤擴展率；每個 flag 都有可重現 ablation |

`use_llm_relations=false` 為 MVP 預設；只有 Step 5C 的品質閘門與 Step 9 ablation 證明有益時才開啟。這讓團隊可以爭取創意分，但不把第二階段實驗綁成核心依賴。

#### Model Decision Contract（何時決定模型）

模型選擇重要，但**不是 Schema 決策，也不應在 Gate 0 只憑品牌或單次示例拍板**。Gate 0 鎖定模型的輸入 / 輸出、評估集與紀錄格式；A 在 Gate 1 用 1k golden slice 做候選模型比較，Gate 2 才凍結執行模型與 fallback。

| 使用位置 | 模型影響 | 決策時點 | 建議準則 |
|----------|----------|----------|----------|
| Step 2 非結構化抽取 / assertion | **最高**；直接決定漏抽、幻覺與否定誤判，會形成 `HAS_SKILL` 基礎 | Gate 1 比較，Gate 2 freeze | strict JSON 成功率、mention precision/recall、assertion accuracy、成本、吞吐 |
| Step 3 canonical verifier | **高**；錯誤合併會污染所有下游統計 | Gate 1–2 | protected-pair error 優先於 recall；必須可 abstain / quarantine |
| Step 5C semantic relation | 中高但屬實驗；會影響擴展品質 | Gate 3 前 | relation precision、evidence grounding、G4 ablation；不合格保持 off |
| Step 8 query intent / OOV fallback | 影響線上 latency 與解析 recall，但不得改圖 | Gate 3–4 | deterministic / alias 優先，小模型只處理未解析案例；量 p50/p95 latency |
| Embedding candidate generation | 影響 Step 3 / 5C 候選召回，不直接決定入圖 | Gate 1–2 | protected pairs、Top-K recall、版本可重現；仍須 verifier gate |

不要假設「一個最大模型包辦全部」最好。離線建圖可使用品質較高但較慢的模型；線上 query resolver 應優先 deterministic 規則或低延遲模型。B 的切分、ID、edge assembly、統計與 quality gate 必須保持 deterministic、model-agnostic，避免換模型就改壞 Schema。

版本化 `model_registry.yaml` 至少記錄：

- `task_name`, `provider`, `model_id`, `model_revision` / snapshot date
- `prompt_version`, `response_schema_version`, decoding / seed（若支援）
- candidate model、chosen model、fallback、retry 與 quarantine policy
- golden set 版本、品質指標、平均成本、throughput、p50 / p95 latency

正式 graph manifest 引用 `model_registry_version`。換模型、prompt 或 embedding model 後必須產生新 extraction / dictionary 版本並重跑受影響的 Gate，不得沿用舊 audit 假裝結果等價。

**Schema freeze 完成定義（DoD）：**

- [x] 節點 / 邊清單已鎖定
- [x] Node / edge deterministic ID 或 key 規則已寫死
- [x] A/B 介面格式已示例
- [x] cutoff 尚未取得正式依據，已記為 `cutoff_status=unresolved`、暫定時區與 snapshot 限制；禁止全量 train graph
- [x] fail 條件已列出
- [x] LLM 在圖譜建構中的必要角色與 ablation 已定義
- [x] H1 / H2、四個 feature flags、評估指標與 query-time 不回寫政策已寫入 Innovation Contract
- [x] model I/O / registry contract 已鎖定；候選模型與品質門檻於 Gate 1 比較、Gate 2 freeze

---

### Step 1 — 資料準備（train-only）

**負責人：** B（結構側）為主；A 複核欄位  
**輸入：** `dataset/職缺.csv`、職務對照表  
**輸出：**

- `train_jobs`（parquet/csv）
- 切分報告：筆數、時間範圍、是否含非 train

**工作內容：**

1. 解析 `職缺最後修改時間`
2. 依主辦正式統一切分取出 train JD；資料包的 2026-06-01～06-07 範圍不可直接當 cutoff
3. 將 naive timestamp 依已決時區轉為 timezone-aware timestamp
4. 保留建圖必要欄位並計算 `content_hash`、`source_snapshot_id`、`train_eligible`
5. 以完整職務三級 tuple 對齊 Occupation `CodeNo`
6. 輸出 cutoff 前後筆數、最早/最晚時間與排除原因

**關鍵決策：**

| 決策 | 建議 |
|------|------|
| cutoff 以哪個時間欄為準 | 以主辦正式切分規則為準；目前實際資料只提供 `職缺最後修改時間`，不可假設 `posted_at` |
| cutoff 尚未公布 | 只做固定 job_id 的 smoke / profiling；不得宣稱 train-only 全量圖完成 |
| 時區 | 主辦未指定時暫記假設 `Asia/Taipei`，並在 manifest 明示 |
| 先全量還是 smoke | 先 1k / 10k smoke，再全量 |
| jobs scope | 優先「有可用文字內容的 train JD」 |

**風險：** 切分錯誤 = 整張圖可能因洩漏作廢。且目前只有最新 JD 快照、沒有版本歷史；即使排除 `last_modified_at > cutoff`，也要在文件中揭露無法完全重建 cutoff 當時 JD 內容的限制。

---

### Step 2 — 技能抽取

**負責人：** A（內容側）  
**輸入：** `train_jobs`  
**輸出：** `extractions.jsonl`（符合第 3.4 節契約）

#### 2A. 結構化抽取（先做）

來源：`電腦技能資料`、`工作技能`、`專業證照`

- 依來源欄位使用不同 parser；ASCII comma 是主要 delimiter，但不可把所有 `、`、`／`、全形逗號無條件切開
- 清洗空白與 `NULL`
- `電腦技能資料` / `工作技能` 先建立高 precision Skill mention
- `專業證照` 路由到 `Credential`；不得與一般 Skill 無標記混合
- 結構化欄位無法判定必要程度時標記 `requirement_level=unspecified`

#### 2B. 非結構化抽取（第二波）

來源：`職務名稱` + `職務內容` + `附加條件`

建議技術：

1. **Phrase matching**：用 skill 詞表掃全文（高可控）
2. **LLM 結構化抽取 / 驗證**：輸出 strict JSON；需記錄失敗模式（幻覺、誤合併、層級錯置、中英縮寫歧義）

LLM 是命題要求的圖譜核心模組，不能只當展示。MVP 可先用規則取得 seed，再由 LLM 負責至少一項必要能力，例如非結構化技能抽取、同義詞候選驗證、技能分類或關係判斷，並預留「關閉 LLM 圖譜步驟」的 ablation。

LLM 輸出除第 3.4 節欄位外，可另輸出 `qualifier`、`failure_tags` 與 `relation_candidates[]`；這些欄位只作 audit / Step 5C 候選來源，不得繞過 canonicalization 與品質閘門直接建邊。`relation_candidates[]` 至少要引用同一份 train JD 內的 `mention_id`，禁止以模型常識補不存在於 evidence 的技能。

#### 2C. Assertion / extraction challenge set（全量前必過）

建立版本化的小型 golden set，至少涵蓋：

- 否定：「不需 Python 經驗」、「無須具備證照」
- 條件 / 不確定：「若熟悉 Kubernetes 佳」、「可能接觸 AWS」
- protected pairs：Java / JavaScript、React / React Native、C / C++ / C#
- 中英縮寫與 OOV：K8s / Kubernetes、Node / Node.js
- 技能 / 產品 / 職稱混淆，以及中文、英文、混寫案例

至少分開報告 mention precision、assertion accuracy 與 protected-pair error；不得只以「隨機 50 筆看起來正常」取代負向測試。若否定 / 不確定偵測未達團隊門檻，非結構化 mention 只能 quarantine，不能預設全部 `affirmed` 後入圖。

#### requirement_level 判定建議

| 訊號 | 判定 |
|------|------|
| 必備、需熟悉、具備、條件 | `required` |
| 加分、佳、優先、歡迎 | `preferred` |
| 僅列出、正文順帶提及或無法確定 | `unspecified` |

「是否必要」與「是否只是被提到」不可共用同一 enum；來源位置由 `source_field` 表達，否定或不確定語氣由 `assertion_status` 表達。

**關鍵決策：**

| 決策 | 建議 |
|------|------|
| LLM 使用順序 | 規則 / 結構化先建立 seed，LLM 負責必要的補抽、驗證、分類或關係判斷 |
| evidence 是否必填 | 必填 |
| 批次策略 | 1k → 10k → 全量 |

**完成定義：**

- [ ] 每筆都有 `job_id` + `skills[]`，證照另有 `credentials[]` 或明確 type
- [ ] 每個 mention 都有 `mention_id`、`source_field`、evidence、extractor version
- [ ] 隨機抽 50 筆人工抽查通過率可接受
- [ ] LLM 與非 LLM 抽取的差異可被量化
- [ ] `assertion_status` 是否已實作真實的否定 / 不確定偵測（而非全部預設 `affirmed`）已明確記錄；若未實作，須列為已知限制並在 manifest / 失敗模式說明中揭露
- [ ] assertion / extraction challenge set 通過；未通過的來源或 method 已被 quarantine，而非預設 `affirmed`
- [ ] 已知失敗案例與防護有清單（供比賽說明）

---

### Step 3 — 技能正規化（Canonicalization）

**負責人：** A 主編 alias；B 實作合併管線與 audit 匯出（可協作）  
**輸入：** `extractions.jsonl`  
**輸出：**

- `skill_dictionary`
- `canonicalization_audit`
- `credential_dictionary`
- 版本化 alias 對照字典（MVP 預設不建 Alias node）
- normalized mentions：補上 `canonical_id`, `canonicalization_status`（`accepted` / `quarantined` / `rejected`）, `dictionary_version`

**建議流程：**

```text
raw_mention
  → NFKC / casefold / 空白正規化
  → alias 字典 exact match
  → deterministic / embedding candidate generation
  → LLM verifier：選擇候選或拒絕，不可自行跳號建 ID
  → protected-pair + evidence + corpus gate
  → accepted / quarantined / rejected
```

正規化不得刪除 `C++`、`C#`、`.NET`、`Node.js` 等語意標點。若 registry key 衝突，禁止自動覆蓋；需 fail 或 quarantine 並輸出 audit。

LLM verifier 只能在已產生的 canonical candidates 中選擇、要求人工審核或拒絕；新增 registry entry 必須走另一個有審核與版本升版的流程。模型自評 confidence 不作通過依據；決策需保留候選集合、evidence、prompt/model 版本與 verifier 結果。

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
| 證照 | 路由至 Credential registry；不得與一般 Skill 靜默合併 |

---

### Step 4 — 語意邊與職類階層組裝

**負責人：** B  
**輸入：** 正規化後的 mentions + `train_jobs`  
**輸出：** `HAS_SKILL`、`IN_OCCUPATION`、`SUBCATEGORY_OF`、`REQUIRES_CREDENTIAL`（中間表或直接進 edges）

**規則建議：**

1. 同一 `(job, skill)` 多次出現 → 物化一條 `HAS_SKILL`，聚合後 requirement 採 `required > preferred > unspecified`
2. 不刪除原始 mentions；只聚合 `assertion_status == affirmed` 且 `canonicalization_status == accepted` 的 mentions（negated / uncertain 一律不得物化成 `HAS_SKILL`），邊保留 `evidence_refs`、`evidence_count`、`source_fields`、`max(confidence)` 與 extractor version
3. 語意邊不寫死 `.72` / `.45` 等 ranking weight；權重放在 ranking config
4. Occupation 以 `(職務大類, 職務中類, 職務小類)` 對 `CodeNameC/B/A` exact match，連到最細可解析 `CodeNo`
5. 職務三級 tuple 對到多個 `CodeNo` 時，只有已由兩人審核、版本化的 `occupation_collision_overrides.csv` 才可解決；已知案例可記 `mapping_status=resolved_by_override`。未知碰撞不得自動套用「選較具體」規則，應 quarantine、輸出候選與原因並阻擋全量圖通過 Gate；審核後再升版 overrides
6. 三級分類全空時不建立 `IN_OCCUPATION`；列 warn，不建立 `occ:unknown` supernode
7. 依 `CodeNo` 建立小類 → 中類 → 大類的 `SUBCATEGORY_OF`

**完成定義：**

- [ ] 每條 `HAS_SKILL` / `REQUIRES_CREDENTIAL` 都能追溯 evidence
- [ ] 每條 `HAS_SKILL` 的來源 mentions 皆為 `assertion_status == affirmed`；抽查確認沒有 negated / uncertain mention 被物化
- [ ] 每條 `IN_OCCUPATION` 都能追溯 tuple → CodeNo 映射結果
- [ ] 所有已知職務三級 tuple 多重匹配案例都能在 `occupation_collision_overrides.csv` 找到已審核規則；未知碰撞為零
- [ ] 無指向不存在 Job / Skill / Credential / Occupation 的邊

---

### Step 5 — 統計邊（共現 / 核心技能）

**負責人：** B；A 協助審核泛用技能黑名單  
**輸入：** train-only 的 `HAS_SKILL` 歸屬
**輸出：** `CO_OCCURS_WITH`、`CORE_SKILL`；可選的 verified `SEMANTICALLY_RELATED`

#### Statistical eligibility（共現與核心技能共用）

統計邊不得直接使用所有 `HAS_SKILL`。先在 accepted mentions 上套用同一個 `statistical_eligible` policy；同一 `(job, skill)` 只要有至少一筆 eligible mention，該 skill 才進入該 Job 的統計 skill set。

```text
statistical_eligible =
  assertion_status == affirmed
  AND canonicalization_status == accepted
  AND confidence >= extraction_config.threshold_by_method[method]
  AND (
    requirement_level IN {required, preferred}
    OR (
      requirement_level == unspecified
      AND source_field IN {電腦技能資料, 工作技能}
    )
  )
```

一律排除：

- `negated` / `uncertain`、quarantined mention
- 非結構化 `職務名稱` / `職務內容` / `附加條件` 中的 `unspecified`
- `Credential`
- 已決的泛用軟技能黑名單

此 policy 與 method thresholds 必須版本化並寫入 manifest；`CO_OCCURS_WITH` 與 `CORE_SKILL` 不得各自實作不同 eligibility 規則。

#### 5A. Skill–Skill 共現

對每個 train Job 的 skill set 做 pair count，再算：

- `count`, `support`
- `PMI`, `NPMI`
- `P(B|A)`, `P(A|B)`

建議起步門檻（可調）：

- `count >= 5`
- `NPMI >= 0.1`
- 每個 skill 只保留 top-N 共現（控制 supernode）

`CO_OCCURS_WITH` 邏輯上是無向邊；匯出時使用 `(min(skill_a, skill_b), max(skill_a, skill_b))` 作唯一 key，並同時保存兩個方向的 conditional probability。

#### 5B. Occupation 核心技能

使用 `SUBCATEGORY_OF` 對 Occupation 做多層統計：

- `minor`：`aggregation_scope=direct`，使用直接以 `IN_OCCUPATION` 歸屬該節點的 Jobs。
- `middle` / `major`：`aggregation_scope=descendants`，使用直接歸屬該節點的 Jobs，加上所有 descendant Occupation 的 Jobs；同一 Job 去重一次。
- 若未來同一 Occupation 同時輸出 direct 與 descendants 兩種結果，edge key 必須包含 `aggregation_scope`。

在每個 scope 內計算 skill 出現率：

```text
core_skill_rate = (# train jobs in occ with skill) / (# train jobs in occ)
```

以 `Occupation → Skill` 儲存，並同時保留 `occupation_level`、`aggregation_scope`、`job_count`、`skill_job_count`、`required_rate`、`train_window`；只有比例而沒有分母會讓小樣本職類產生誤導性的 1.0。

同一份 policy 亦用於計算每個 Skill 的 `global_job_frequency`，由 B 在 Step 5 產生並於 Step 6 補入 Skill nodes；不得從所有 mentions 另算一份不同口徑的 DF。

#### 5C. Verified semantic relation（創意第二階段，可關閉）

目標是在 Skill Graph 內表達「互補、先備、替代」而不讓 LLM 自由幻想關係。流程固定為：

```text
train-only eligible skill pairs
  → deterministic candidates（共現 / 同職類核心技能 / 已有 evidence）
  → LLM label：complements / prerequisite / alternative / unrelated
  → dual-signal verifier（corpus support + evidence grounding + protected-pair policy）
  → verified edge 或 audit-only quarantine
```

安全限制：

1. 候選兩端必須已是 accepted Skill，且候選來源只能是 train-only 統計或 train JD evidence。
2. LLM 必須輸出 relation type、方向、evidence refs 與理由；模型自評分數不等於 verification。
3. 只有 LLM 判斷與 corpus support 同時通過的關係可標為 `verified`；一般常識但資料無支持者不入圖。
4. `prerequisite` 須有方向證據；證據不足時降級為 `complements` 候選或 quarantine，不可猜方向。
5. 匯出前做人工抽樣與 ablation；若 precision 或 ranking 指標未達 Day 0 門檻，維持 `use_llm_relations=false`。
6. test query 的 OOV mapping 只可暫時指向既有 Skill 候選，不得建立或更新本關係。

**關鍵決策：**

| 決策 | 建議 |
|------|------|
| 門檻要多嚴 | 先保守；圖太稀疏再放寬 |
| supernode | 必做 top-N 或 IDF 降權 |
| Excel / Office | 保留 query 能力；依 global DF/IDF 與職類內 salience 降權，不一律刪除 |
| LLM 語意關係 | dual-signal verified 才入圖；未證明增益前 feature flag 預設關閉 |

---

### Step 6 — Graph 組裝匯出

**負責人：** B  
**輸出建議：**

```text
graph/
  nodes.csv
  edges.csv
  graph_manifest.json
  mentions.jsonl
  skill_dictionary.csv
  credential_dictionary.csv
  alias_dictionary.csv
  extraction_config.yaml
  model_registry.yaml
  innovation_contract.yaml
  assertion_challenge_set.jsonl
  assertion_challenge_report.json
  canonicalization_audit.csv
  occupation_mapping_audit.csv
  occupation_collision_overrides.csv
  semantic_relation_audit.jsonl
  ranking_config.yaml
```

`graph_manifest.json` 至少包含：

- schema / extraction / dictionary 版本
- confidence policy、method thresholds 與 statistical eligibility policy 版本
- `cutoff_status`, `train_cutoff`, cutoff 邊界、時區與正式依據
- `source_snapshot_id` 與 snapshot 限制
- train 時間區間、納入 / 排除筆數及原因
- node / edge 計數（依 type）
- `occupation_collision_overrides`、semantic relation verifier 與四個 feature flags 的版本 / 啟用狀態
- 每個輸入檔的 hash、檔名版本與相關比賽文件版本
- `contains_post_cutoff_jd` 的實測結果；只有驗證為 `false` 才可宣稱 train-only
- `model_registry_version`，以及 LLM / embedding model、prompt、inference config、fallback 與 random seed（若適用）

儲存策略（Hackathon）：

1. **先 CSV / Parquet + typed adjacency / inverted index**（最快驗證）
2. 目前有 121 萬以上 Job，不建議把全圖直接載入 NetworkX Python object graph
3. 有餘力再做 Neptune / 圖資料庫匯入計畫

---

### Step 7 — 品質檢查（Gate）

**負責人：** B 執行；A 複核內容錯誤  
**輸出：** `graph_quality_report.json`

| 檢查項 | 建議處置 |
|--------|----------|
| test JD 出現在圖中 | **fail** |
| cutoff 未決卻宣稱 train-only 全量圖完成 | **fail** |
| manifest 缺 cutoff 邊界、時區、snapshot 或輸入 hash | **fail** |
| dangling edge | **fail** |
| duplicate node/edge ID | **fail** |
| normalized key collision 未處理 | **fail** |
| `requirement_level` / `assertion_status` 非法值 | **fail** |
| `confidence` 不在 `[0, 1]` 或 method threshold 未版本化 | **fail** |
| 低於 threshold / quarantined mention 被物化成 graph edge | **fail** |
| `assertion_status` 為 negated / uncertain 的 mention 被物化成 `HAS_SKILL` | **fail** |
| assertion challenge set 缺失、未版本化或未達 Day 0 門檻 | **fail**；相關非結構化 method 不得全量入圖 |
| protected pair 被合併 | **fail** 或 quarantine |
| evidence 不在指定 `source_field` 原文 | quarantine |
| Occupation 無法匹配 | quarantine / warn；不得建立 `occ:unknown` supernode |
| 未知 Occupation 多重匹配或未版本化 override 被套用 | **fail** |
| `CO_OCCURS_WITH` self-loop、NaN / inf 或重複 pair | **fail** |
| CO_OCCURS / CORE_SKILL 使用不同或未記錄的 statistical eligibility policy | **fail** |
| `global_job_frequency` 未補齊或使用不同 statistical eligibility policy | **fail** |
| 未驗證、無 train evidence 或由 test query 觸發的 `SEMANTICALLY_RELATED` 入圖 | **fail** |
| test query / OOV 解析回寫圖、alias 或 registry | **fail** |
| orphan Skill | warn |
| supernode 超標 | warn + 強制截斷後重跑 |
| LLM 輸出不符合 JSON schema / 幻覺 mention | quarantine + 記錄失敗模式 |

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

#### Query-adaptive traversal policy

先分類 query，再選擇「最小必要」路徑；不得為了展示圖而對所有 query 無條件做 1-hop 擴展。

| Query 類型 | 預設策略 | 安全邊界 |
|------------|----------|----------|
| 明確 Skill | exact `HAS_SKILL` 為主；必要時才用高門檻 `CO_OCCURS_WITH` | exact 結果不應被弱擴展淹沒 |
| Occupation | `IN_OCCUPATION` + hierarchy + `CORE_SKILL` | 記錄使用 minor / middle / major 哪一層 |
| 多 Skill | 先交集 / coverage，再以 verified expansion 補 recall | 不因單一 supernode 支配排序 |
| Alias / OOV | alias exact → ambiguous resolver → 保守 semantic fallback | 只映射既有節點；不回寫 Skill、alias 或 edge |
| 含負向條件 | 將負向 skill 放入 exclusion set | 不建立正向 traversal 或正向 `HAS_SKILL` 解釋 |

只有 `use_adaptive_traversal=true` 時啟用此 policy；固定 0-hop / 1-hop 仍保留為可重現 baseline。

對每個 query 記錄：

```text
1. 解析到哪些 Skill / Occupation
2. 0-hop：由 Skill / Occupation 反向走到 Job 的命中數
3. intent 類型、選用 policy、0-hop / 1-hop 擴展節點數與截斷原因
4. Top 5 job_id + path 解釋
5. 明顯錯誤案例
6. path score、latency、feature flags 與 graph version
7. 同一 query 關閉 graph / LLM relation / adaptive traversal 後的排序差異
```

**比賽交付用 trace 範例（格式可調整）：**

```text
query: "node.js 後端"
→ query normalization（alias dictionary）: "node.js" → skill:nodejs
→ occupation normalization（CodeName / CodeAlike）: "後端" → occ:140215（後端工程師）
→ intent policy: multi-skill + occupation；exact-first，最多擴展 1-hop
→ skill:nodejs <-[HAS_SKILL {requirement_level: required}]- job:57745782
→ skill:nodejs -[CO_OCCURS_WITH {npmi: ...}]-> skill:typescript
→ skill:typescript <-[HAS_SKILL]- job:75669246
→ occ:140215 -[CORE_SKILL {aggregation_scope: direct, rate: ...}]-> skill:nodejs
→ occ:140215 <-[IN_OCCUPATION]- job:...
→ ranker 聚合 exact_skill、verified_expanded_skill、occupation_match、path_score
→ ranked jobs: [57745782, 75669246, ...]
```

上例中的 alias normalization 是圖外前處理，因此不畫 `ALIAS_OF`。`HAS_SKILL` / `IN_OCCUPATION` 的 schema 方向是 Job → Skill / Occupation；從 query 節點找 Job 時以 `<-` 明確表示反向 traversal。實際交付 trace 必須使用通過 train cutoff 的 Job，填入真實 edge properties、policy / flag 與截斷資訊，不可保留 `...`。若使用 `SEMANTICALLY_RELATED`，trace 還須顯示 `relation_type`、corpus support 與 evidence refs。

---

### Step 9 — 文件與交接

**負責人：** 兩人共同  
**輸出：**

- Schema 正式版（可附 yaml）
- 至少 1 個完整 traversal / aggregation trace
- LLM 在技能抽取 / 正規化 / 分類 / 關係建構中的必要角色
- LLM / 抽取失敗模式與防護說明
- 「有圖譜 vs 無圖譜」與「關閉 LLM 圖譜步驟」的可重現 ablation
- 給排序組的介面說明：圖特徵有哪些、如何讀 edges

#### Step 9 評估矩陣（創意要用數據證明）

至少保留以下 run；若時程不足，先完成前四列，後兩列屬第二階段：

| Run | Graph | LLM extraction / normalization | Verified semantic relation | Adaptive traversal | 用途 |
|-----|-------|--------------------------------|----------------------------|--------------------|------|
| B0 | off | off | off | off | BM25 / 既有 baseline |
| G1 | on | off | off | off | 純結構化 Skill Graph 增益 |
| G2 | on | on | off | off | 命題核心：LLM 建圖增益 |
| G3 | on | on | off | on | H2：adaptive traversal 增益 |
| G4 | on | on | on | off | verified relation 單獨貢獻 |
| FULL | on | on | on | on | 完整創意方案；僅在品質 Gate 通過時提交 |

每列記錄 NDCG@10、MRR、Hit@1、Hit@10、p50 / p95 latency 與錯誤擴展率，並挑 3–5 個 query 做 counterfactual trace。若 G4 / FULL 低於 G2 / G3，正式方案應關閉 semantic relation；不要為了創意敘事犧牲主指標。

---

## 6. 關鍵決策點總表

下表為 Schema v0.1 已決政策；實際數值仍待校準者會明示於建議欄，不影響 Schema freeze。

| # | 決策 | 選項 | 建議預設 | 已決 |
|---|------|------|----------|------|
| 1 | MVP 節點範圍 | Job / Skill / Occupation / Credential / Alias | Job+Skill+Occupation+Credential；Alias 用字典 | v0.1 已決 |
| 2 | MVP edge | 多 edge type / 單一屬性邊 | `HAS_SKILL` + `IN_OCCUPATION` + `SUBCATEGORY_OF` + `REQUIRES_CREDENTIAL` | v0.1 已決 |
| 3 | Alias 實作 | 字典 / Alias 節點 | 版本化字典；Skill / Occupation 共用 schema，query normalization 在圖外 | v0.1 已決 |
| 4 | ID 規則 | 顯示名 / registry / hash | Job 用原 ID、Occupation 用 CodeNo、Skill/Credential 用 registry key | v0.1 已決 |
| 5 | train cutoff | 日期時間、邊界、時區、依據 | 跟主辦正式切分；目前 `unresolved` 且禁止全量建圖 | v0.1 政策已決 |
| 6 | Confidence 用途 | ranking / quarantine / 兩者 | extraction accept/quarantine；不直接進 ranking；threshold by method | v0.1 已決 |
| 7 | Edge confidence 聚合 | max / mean / 機率合併 | accepted mentions 取 `max`，支持數另存 `evidence_count` | v0.1 已決 |
| 8 | 抽取策略 | 規則優先 / LLM 優先 / 混合 | 規則 seed + LLM 必要抽取/驗證/分類 + ablation | v0.1 已決 |
| 9 | requirement 不確定時 | required / preferred / unspecified | `unspecified` | v0.1 已決 |
| 10 | Statistical eligibility | 全部技能 / requirement 篩選 / source-aware | affirmed + accepted；required/preferred，或高可信結構化 unspecified | v0.1 已決 |
| 11 | Occupation 聚合 | 只算 leaf / 多層 direct / descendants | minor=direct；middle/major=direct + descendants 去重 | v0.1 已決 |
| 12 | 合併策略 | precision-first / recall-first | precision-first | v0.1 已決 |
| 13 | 共現門檻 | count / NPMI / top-N | count≥5, NPMI≥0.1, top-N 待 smoke 校準 | v0.1 起始值已決 |
| 14 | 泛用技能 | 進圖 / 降權 / 排除 | 保留但依 DF/IDF 與職類 salience 降權 | v0.1 已決 |
| 15 | 圖儲存 | CSV / Parquet / NetworkX / Neptune | CSV/Parquet + typed adjacency；避免全量 NetworkX | v0.1 已決 |
| 16 | fail 條件 | 見 Step 7 | 洩漏、未決 cutoff 誤宣稱、dangling、collision 必 fail | v0.1 已決 |
| 17 | schema 變更權 | 單人 / 雙人同意 | **雙人同意** | v0.1 已決 |
| 18 | Occupation 未知碰撞 | 自動選子層 / quarantine | 只接受版本化 override；未知碰撞 quarantine 並阻擋全量 Gate | v0.1 已決 |
| 19 | Assertion gate | 全部 affirmed / challenge set | 負向 golden challenge set 必過；未過的非結構化來源 quarantine | v0.1 已決 |
| 20 | 創意主軸 | 多加邊 / evidence-calibrated + adaptive | 可量測的 H1/H2；所有能力以 feature flag 控制 | v0.1 已決 |
| 21 | LLM relation | 直接入圖 / dual-signal verified | train corpus + evidence + verifier 同時通過；預設關閉 | v0.1 已決 |
| 22 | OOV query | 寫回圖 / query-time mapping | 只暫時映射既有 Skill；不回寫任何圖資產 | v0.1 已決 |

---

## 7. 兩人分工

### 7.1 是否可以兩人分別開工

**可以，但不是兩人各自從 Step 1 跑到 Step 9。** 建議採「共同契約與 golden vertical slice → 內容 / 結構雙軌並行 → 關鍵 Gate 合流」。這樣兩人能同時產出，又不會到 Step 4 才發現 JSON、ID 或 eligibility 無法合併。

開工條件：

1. Gate 0 的 Schema、cutoff 狀態、抽取 JSON、quality policy 與 Innovation Contract 已鎖定。
2. 兩人共同挑同一批 100–1,000 個固定 train / smoke Job，完整走過 Step 1–4。
3. A 的 JSONL 能被 B 的 assembler 無人工修改讀入，且產生的邊可回指 mention / evidence。
4. 若 cutoff 未決，只能在固定 smoke IDs 並行；不可各自建立或宣稱全量 train graph。

### 7.2 角色定義與工作包

| 角色 | 暱稱 | 核心職責 |
|------|------|----------|
| **A — Content Graph** | 內容側 | Step 2；Step 3 的 alias / registry 內容；assertion challenge set；模型 bake-off / `model_registry`；LLM prompt / verifier；evidence 品質；relation candidate 審核；LLM ablation |
| **B — Structure Graph** | 結構側 | Step 1、4、5、6、7；Occupation / overrides；ID / edge key；組裝與統計；quality gate；typed index；retrieval 實作 |

可直接開 issue 的工作包：

| 工作包 | Owner | 輸入 | 完成輸出 | 何時需同步 |
|--------|-------|------|----------|------------|
| A0 模型比較與 freeze | A 主、B review latency / interface | golden set、model I/O contract | `model_registry.yaml`、品質 / 成本 / latency 比較 | Gate 1 review；Gate 2 freeze chosen / fallback model |
| A1 結構化 / phrase / LLM 抽取 | A | 固定 smoke `train_jobs`，之後為全量 train | `extractions.jsonl`, `extraction_config.yaml`, model bake-off / failure report | JSON schema、模型或 enum 要改時先停在 Gate |
| A2 Canonical registry / alias | A 主、B review | extractions、CodeAlike、protected pairs | dictionaries、normalized mentions、audit | 新 ID、合併或 collision policy 需雙人同意 |
| A3 LLM semantic verifier | A 主 | B 提供的 train-only candidates | `semantic_relation_audit.jsonl` | verifier threshold / relation type 變更需同步 |
| B1 Train / Occupation substrate | B | 原始 CSV、cutoff、職務對照表 | `train_jobs`, hierarchy, mapping audit, overrides | cutoff 或未知碰撞立刻同步 |
| B2 Edge assembler / statistics | B | A 的 frozen JSONL + dictionaries | core edges、CO_OCCURS、CORE_SKILL、global DF | A 的契約 hash 改變不得靜默續跑 |
| B3 Export / quality / retrieval | B | 完整 graph tables | graph package、quality report、trace runner | fail、新 supernode 或 latency 超標時同步 |
| J1 Golden slice / evaluation | 共同 | 同一固定樣本與 query set | contract test、ablation matrix、3–5 traces | 每個 Gate 一起 sign-off |

### 7.3 責任邊界（避免踩線）

| 可以做 | 不要做 |
|--------|--------|
| A 改抽取邏輯與 alias 內容 | A 直接手改 `edges.csv` 的 type 定義 |
| B 改組裝、統計邊、品質腳本 | B 擅自改抽取 JSON 欄位名稱 |
| 任一方提 schema 變更 | 未雙方同意就升版上線 |

### 7.4 介面契約

```text
A 主要輸出 ──► extractions.jsonl + extraction_config + skill/credential/alias dictionaries（契約見 3.4–3.5）
B 主要輸出 ──► graph/nodes + edges + manifest + occupation overrides + mapping/canonicalization audit + quality report
共同輸出 ──► golden slice contract test + schema 文件 + traversal trace + feature-flag ablation matrix
```

交接時必附 `schema_version`、輸入 artifact hash、row count、quarantine count 與產生指令；接收方先跑 contract test，禁止用手改 CSV 讓流程「暫時能跑」。

### 7.5 同步閘門（Gate）

| Gate | 時點 | 兩人共同確認 | 未通過時 |
|------|------|--------------|----------|
| Gate 0 — Contract freeze | 開工前 | Schema、JSON、ID、cutoff、quality、H1/H2、feature flags | 不開始獨立全量工作 |
| Gate 1 — Golden ingestion | 100–1,000 Job Step 1–4 後 | A artifact 可被 B 原樣讀入；模型 bake-off；evidence / edge 可追溯；未知 occupation collision 為零 | 修契約、模型候選或 quarantine，不擴至 10k |
| Gate 2 — Content / model freeze | 10k 抽取 / canonicalize 後 | chosen / fallback model、prompt、challenge set、protected pairs、dictionary diff、quarantine rate | 不啟動全量統計 |
| Gate 3 — Pre-full graph | Step 5–7 smoke 後 | 無 fail、DF / supernode 合理、semantic relation precision 合格 | 關閉實驗功能或回退 config |
| Gate 4 — Submission candidate | Step 8–9 | train-only、trace、ablation、指標與 latency 可重現 | 以 G2 / G3 等較穩版本提交，不勉強上 FULL |

### 7.6 建議時程（可依比賽日程壓縮）

```text
Day 0（共同 90 分）— Gate 0
  權威來源矩陣 + Schema v0.1 + JSON 契約 + cutoff + Innovation Contract + 工作包確認

Day 1（共同）— Gate 1
  同一批 100–1,000 Job 跑 Step 1–4；先證明 A/B artifact 能直接串接

Day 2–3（並行）
  A: A0/A1/A2 + assertion challenge set；B: B1/B2 + Occupation audit
  10k 合流 → Gate 2

Day 4–5（並行）
  A: A3 + LLM failure / relation audit；B: 統計邊 + global DF + export / quality
  smoke graph 合流 → Gate 3

Day 6（cutoff 已決才全量）
  B 跑 frozen artifacts；A 抽查 quarantine / relation / failure cases

Day 7（共同）— Gate 4
  quality report → adaptive traversal → ablation matrix → trace / 文件定稿
```

### 7.7 每日同步（15 分鐘）

固定對齊：

1. Schema 是否有變更提案
2. A 今日產出筆數 / 抽查問題
3. B 品質報告新增 fail/warn
4. artifact version / hash 是否已變更、下一個 Gate 是否可進入
5. 明日 blocker

---

## 8. 端到端流程圖

```text
Gate 0: Schema + JSON + cutoff + Innovation Contract（共同）
        │
        ▼
Gate 1: 同一 golden slice 跑通 Step 1–4（共同）
        │
        ▼
A 軌: Step 2–3 Content Graph        B 軌: Step 1/4 Structure Graph
     extraction / assertion             train / occupation / assembler
     registry / alias / verifier         hierarchy / overrides / quality
        │                                      │
        └────────── Gate 2: frozen artifacts ─┘
                           │
                           ▼
Step 5: CO_OCCURS / CORE_SKILL / global DF（B）
        + verified semantic relation candidate（A 審、B 匯出；預設 off）
        │
        ▼
Gate 3: Step 6–7 export + quality（B 執行、A 複核）
        │
        ▼
Gate 4: adaptive traversal + ablation + docs（共同）
```

---

## 9. Gate 0 鎖定與待填清單

> `[x]` 為 Schema v0.1 已鎖定政策；`[ ]` 是執行參數或人員資料，須在相應 Gate 補齊，不代表 Schema 未凍結。

- [x] Node types：Job / Skill / Occupation / Credential；Alias 採圖外字典
- [x] Core edge types：`HAS_SKILL` / `IN_OCCUPATION` / `SUBCATEGORY_OF` / `REQUIRES_CREDENTIAL`
- [x] 第二階段 edge：`CO_OCCURS_WITH` / `CORE_SKILL`；`SEMANTICALLY_RELATED` 預設 off
- [x] Node / edge deterministic key：依第 3.3 節
- [ ] train cutoff：待主辦正式依據；邊界：待定；時區：暫記 `Asia/Taipei`
- [x] cutoff 狀態：`unresolved`；只阻擋全量建圖，允許固定 golden slice
- [ ] source snapshot / hash：______________________________
- [x] 抽取 JSON 版本：`v0.1`
- [x] confidence 用途：accept/quarantine；直接進 ranking：否
- [ ] method thresholds：structured=___, phrase=___, llm=___；config version：________
- [x] edge confidence 聚合：accepted mentions 的 `max`
- [x] LLM 必要角色：至少負責非結構化抽取或 canonical verifier；保留 ablation
- [x] Innovation H1 / H2 與四個 feature flags 已鎖定
- [x] test query / OOV 不回寫 graph / alias / registry
- [x] requirement enum：`required` / `preferred` / `unspecified`
- [x] Credential：獨立節點；若降級實作仍須以 `skill_kind` 隔離
- [x] Alias：圖外版本化字典
- [x] CodeAlike delimiter / ambiguity policy 已確認
- [x] 未知職類碰撞一律 quarantine / fail；只接受版本化 override
- [x] `HAS_SKILL` 同時要求 `assertion_status == affirmed` 與 `canonicalization_status == accepted`
- [ ] assertion / extraction challenge set 與通過門檻：______________________________
- [x] 合併策略：precision-first
- [x] statistical_eligible policy：依 Step 5，共用單一版本
- [x] Occupation aggregation：minor=direct；middle/major=direct+descendants
- [ ] 共現校準：起始 count=5、NPMI=0.1；top-N=___
- [x] semantic relation：預設 off，dual-signal verified 才能入圖
- [ ] semantic relation precision 門檻：______________________________
- [x] 圖輸出路徑：`graph/`
- [x] fail 條件：依 Step 7
- [ ] 候選模型與 embedding model：______________________________
- [ ] `model_registry_version`、chosen / fallback model（Gate 2）：______________________________
- [ ] A 負責人：________
- [ ] B 負責人：________
- [ ] Golden slice IDs / artifact hash：______________________________
- [x] Schema 變更需雙人同意並升版

---

## 10. 常見陷阱

1. **用 test JD 建圖** → 洩漏，可能整項不計分  
2. **把資料包 6/1～6/7 當成 train window** → 主辦尚未公布 cutoff，不得自行推定
3. **把最後修改時間當成刊登時間** → 實際資料沒有 `posted_at`
4. **未先定 schema 就分頭寫** → 合併成本爆炸
5. **alias 過度合併或移除語意標點** → Java / JavaScript、C / C++、Node / Node.js 被誤合併
6. **把證照當一般 Skill** → 護理師執照與 Python 的語意及 traversal 混亂
7. **泛用技能變成 supernode** → 圖失去辨識力
8. **只留聚合邊、不留 mentions/evidence** → 無法答辯、重算或做品質檢查
9. **把 extraction confidence 直接乘進 ranking** → 抽取不確定性與職缺相關性混為一談
10. **所有 HAS_SKILL 都進共現統計** → 弱訊號、否定與正文 unspecified 稀釋真正關聯
11. **trace 方向與 schema 相反** → 反向 traversal 必須用 `<-` 明示
12. **LLM 只是裝飾** → 不符命題精神，也無法證明 graph/LLM 對 NDCG 的必要性
13. **建完圖不做 retrieval smoke / ablation** → 圖是死資產，無法證明對排序有用
14. **A/B 搶改同一份 edges** → 用介面契約隔離
15. **把瀏覽/應徵直接當 graph 邊** → 行為訊號應留給排序模組，避免把曝光偏差寫進圖結構
16. **只過濾 `canonicalization_status`、忘記同時過濾 `assertion_status`** → 「不需要 Python 經驗」這類否定語氣被物化成正向 `HAS_SKILL`，使用者搜尋會撈到明講不需要該技能的職缺
17. **職務三級 tuple 多重匹配只補救單一案例** → 已知案例需收斂到版本化 override；新碰撞不得沿用猜測規則，應 quarantine 後審核
18. **`train_eligible` 誤植為最終圖節點的必要屬性** → 非 train 職缺若連同旗標寫進 `graph/nodes.csv`，即使標示為 `false` 仍算「test JD 出現在圖中」
19. **未知 Occupation collision 自動選最細層** → 可能穩定地產生錯誤 `IN_OCCUPATION`；只接受已審核 override
20. **所有非結構化 mention 預設 `affirmed`** → assertion gate 名義存在、實際失效；負向 challenge set 未過不得全量入圖
21. **LLM 憑常識自由補 semantic edge** → 關係無 train evidence、難以答辯且可能誤擴展；只接受 dual-signal verified edge
22. **OOV query 寫回 registry / graph** → test-time leakage 與圖版本漂移；只做 ephemeral mapping
23. **為每個 query 固定擴展全部鄰居** → supernode、latency 與誤連放大；使用 intent policy 與 top-N / threshold
24. **為了完整創意方案忽略 ablation 退步** → 若 G4 / FULL 不如 G2 / G3，正式提交應關閉該功能

---

## 11. 與下游（排序 / API）的交接

Skill Graph 完成後，排序模組通常需要：

| 項目 | 說明 |
|------|------|
| 圖檔 | `nodes.csv` / `edges.csv` |
| Query → Skill / Occupation 解析 | 版本化 alias dictionary、`CodeName` / `CodeAlike` 對照 |
| Graph features | exact skill count、expanded skill、path score、occupation match、credential match |
| Query policy | intent type、selected traversal、expansion cap、exclusion set、feature flags |
| Feature provenance | 每個分數能回指 traversal path、edge properties 與 graph version |
| Ablation | B0 / G1 / G2 / G3 / G4 / FULL 的 config、指標與 latency 可重現 |

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
| Schema 狀態？ | **v0.1 已凍結；Gate 0=`conditional_pass`，可做 golden slice，正式 cutoff 未決前不可全量建圖** |
| 主要用什麼資料？ | 主辦正式 train 期 `職缺.csv` + `職務對照表`；資料包週期不等於 train cutoff |
| 步驟？ | Schema → train 資料 → 抽取 → 正規化 → 組邊 → 統計邊 → 匯出 → 品質 → smoke → 文件 |
| Schema 核心？ | Job / Skill / Occupation / Credential；HAS_SKILL / IN_OCCUPATION / SUBCATEGORY_OF / REQUIRES_CREDENTIAL |
| 創意主軸？ | Evidence-Calibrated, Query-Adaptive Skill Graph；LLM 關係需 train evidence + verifier，查詢採最小必要 traversal |
| 模型何時決定？ | Gate 0 鎖 I/O 與評估契約；Gate 1 做 1k bake-off；Gate 2 freeze extraction / verifier / embedding 的 chosen 與 fallback model |
| Confidence 怎麼用？ | 只做 extraction accept/quarantine，門檻依 method 版本化；v0.1 不直接進 ranking |
| 統計邊吃哪些技能？ | 共用 `statistical_eligible`：affirmed + accepted + 達門檻，並依 requirement / source 篩選 |
| Occupation 怎麼聚合？ | minor 用 direct jobs；middle / major 使用 direct + descendant jobs 去重 |
| 怎麼分工？ | 先共同跑 golden slice；A 做 Content Graph，B 做 Structure Graph；在 Gate 0–4 交換 frozen artifact / hash 並共同 sign-off |
| 何時算建圖完成？ | cutoff 已決 + 品質閘門通過 + query policy 有可解釋 trace + feature-flag ablation 可重現；實驗功能退步就關閉 |

---

## 附錄 A — 最小可行路徑（若時間極緊）

若只能做一條最短路徑：

1. Day 0 鎖定 Job / Skill / Occupation / Credential + HAS_SKILL / IN_OCCUPATION / SUBCATEGORY_OF / REQUIRES_CREDENTIAL
2. cutoff 未決只做固定 job_id smoke；cutoff 已決才建全量 train graph
3. 先抽取三個結構化能力欄，證照獨立路由
4. 鎖定 method-specific confidence thresholds 與共用 `statistical_eligible` policy
5. 用 LLM 驗證同義詞或補抽非結構化技能，保留 prompt/version/evidence 與失敗案例
6. 小型版本化 alias / registry + protected pairs；CodeAlike 使用同一 alias schema
7. 匯出 CSV/Parquet + extraction config + manifest + quality report
8. 先做 0-hop retrieval trace 與 B0 / G1 / G2 ablation
9. 有時間再加 `CO_OCCURS_WITH` / `CORE_SKILL` 與 adaptive traversal（G3）
10. `SEMANTICALLY_RELATED`（G4 / FULL）最後做；未通過 dual-signal、precision 或 ranking gate 就保持關閉

---

## 附錄 B — 文件維護

| 項目 | 說明 |
|------|------|
| 維護者 | Skill Graph 兩位成員共同 |
| 更新時機 | Schema 升版、分工調整、品質政策變更 |
| 相關路徑 | `docs/SKILL_GRAPH_PLAYBOOK.md`（本文件） |

若來源互相衝突，先依第 1.1 節的用途分工處理：交付硬性規格以命題文件為準，實際資料欄位與 API 以工作坊 / CSV 為準；8/1 正式補充可補足細節，但不得在沒有明文時自行推定其覆蓋命題文件。任何 cutoff 或交付條款變更都要附來源並回寫本文件。
