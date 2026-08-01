---
inclusion: auto
---

# Skill Graph 團隊工作規則

本專案正在建構 Skill Graph（技能圖譜），以下規則適用於所有成員，在每次 Kiro 互動中自動生效。

## Schema v0.1（已凍結 2026-08-01）

- **Node types:** Job, Skill, Occupation, Credential（Alias 用圖外版本化字典）
- **Core edges:** HAS_SKILL, IN_OCCUPATION, SUBCATEGORY_OF, REQUIRES_CREDENTIAL
- **Second phase:** CO_OCCURS_WITH, CORE_SKILL
- **創意實驗:** SEMANTICALLY_RELATED — 預設 `use_llm_relations=false`，需 dual-signal verified
- **Schema 變更需雙人同意並升版，任一方不得私下新增 edge type 或改 ID 規則**

## 分工

| 角色 | 核心職責 |
|------|----------|
| **A — Content Graph** | Step 2 抽取、Step 3 正規化、alias/registry 內容、assertion challenge set、LLM prompt/verifier、model bake-off |
| **B — Structure Graph** | Step 1 資料準備、Step 4 edge assembly、Step 5 統計邊、Step 6 匯出、Step 7 品質檢查 |
| **共同** | Step 0 Schema、Step 8 retrieval smoke、Step 9 文件/ablation、Gate sign-off |

## 硬性約束

1. **建圖資料範圍:** 全部職缺皆可用於建圖，不需切分 train/test（主辦方口頭確認，書面來源待補 §1.1）
2. query/行為資料仍依 `dataset_1111.py` 切分 train/validation/test；test 期查詢/行為不得回寫圖
3. ID 必須 deterministic：
   - `job:<職缺編號>` → `job:1370179`
   - `skill:<registry_key>` → `skill:python`
   - `occ:<CodeNo>` → `occ:140200`
   - `credential:<registry_key>` → `credential:高考護理師執照`
4. 不得擅自新增 edge type 或改 ID 規則
5. `HAS_SKILL` 只能物化 `assertion_status=affirmed` 且 `canonicalization_status=accepted` 的 mentions
6. Confidence 只用於 extraction accept/quarantine（threshold by method），不直接進 ranking
7. 合併策略：**precision-first**（寧可漏併，不要誤併）
8. test query / OOV 不回寫 graph / alias / registry / 統計量
9. LLM 必須在建圖中有必要角色（非展示），並保留 ablation
10. 不得捏造資料或統計數字；若不確定，停下來詢問使用者

## 介面契約

```
A 輸出 → extractions.jsonl（§3.4 格式）+ extraction_config.yaml
         + skill_dictionary.csv + credential_dictionary.csv + alias_dictionary.csv
B 輸出 → graph/nodes.csv + edges.csv + graph_manifest.json
         + occupation_mapping_audit.csv + canonicalization_audit.csv + quality_report.json
共同   → golden_slice contract test + traversal trace + ablation matrix
```

交接時必附 `schema_version`、輸入 artifact hash、row count、quarantine count。

## 關鍵文件參照

- Playbook: #[[file:docs/SKILL_GRAPH_PLAYBOOK.md]]
- Golden slice IDs: `graph/golden_slice_ids.json`（500 筆固定 Job IDs，seed=2026）
- Step 1 manifest: `graph/step1_manifest.json`

## Innovation Contract

- **H1:** LLM 抽取/正規化改善 alias、縮寫與 OOV query 的 recall
- **H2:** query-adaptive traversal 比固定 1-hop 有更好的 NDCG@10
- **Feature flags:** `use_graph`, `use_llm_extraction`, `use_llm_relations`, `use_adaptive_traversal`
- 每個 flag 都有可重現 ablation；實驗功能退步就關閉

## Gate 流程

| Gate | 條件 |
|------|------|
| Gate 0 | Schema + JSON + 建圖資料範圍（全量，來源待補）+ Innovation Contract 已鎖定 ✓ |
| Gate 1 | Golden slice 500 jobs Step 1–4 跑通；A 的 JSONL 可被 B 原樣讀入 |
| Gate 2 | 10k extraction + dictionary frozen；model chosen |
| Gate 3 | Step 5–7 smoke 無 fail；DF/supernode 合理 |
| Gate 4 | 建圖來源已附佐證 + trace + ablation 可重現 |
