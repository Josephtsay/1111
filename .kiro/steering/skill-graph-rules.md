---
inclusion: auto
---

# Skill Graph — Role B 工作規則

本專案正在建構 Skill Graph（技能圖譜），以下規則在所有互動中自動生效。

## 角色

我是 **Role B（Structure Graph）**，負責：
- Step 1: 資料準備（已完成）
- Step 4: Edge assembly（HAS_SKILL, IN_OCCUPATION, SUBCATEGORY_OF, REQUIRES_CREDENTIAL）
- Step 5: 統計邊（CO_OCCURS_WITH, CORE_SKILL）
- Step 6: Graph 組裝匯出
- Step 7: 品質檢查
- Step 8: Retrieval smoke test（與 A 共同）

## Schema v0.1（已凍結）

- **Node types:** Job, Skill, Occupation, Credential（Alias 用圖外字典）
- **Core edges:** HAS_SKILL, IN_OCCUPATION, SUBCATEGORY_OF, REQUIRES_CREDENTIAL
- **Second phase:** CO_OCCURS_WITH, CORE_SKILL; SEMANTICALLY_RELATED 預設 off
- **Schema 變更需雙人同意並升版**

## 硬性約束

1. 所有職缺皆為 train（不需 time-based cutoff）
2. 不得使用 test 期間 JD 建圖
3. ID 必須 deterministic：`job:<職缺編號>`, `skill:<registry_key>`, `occ:<CodeNo>`, `credential:<registry_key>`
4. 不得擅自新增 edge type 或改 ID 規則
5. `HAS_SKILL` 只能物化 `assertion_status=affirmed` 且 `canonicalization_status=accepted` 的 mentions
6. Confidence 只用於 accept/quarantine，不直接進 ranking
7. 合併策略：precision-first（寧可漏併不要誤併）
8. test query / OOV 不回寫 graph / alias / registry

## 介面契約

- A 交付：`extractions.jsonl`（§3.4 格式）+ skill/credential/alias dictionaries
- B 交付：`graph/nodes.csv` + `edges.csv` + manifest + audit + quality report
- Golden slice：500 固定 Job IDs（`graph/golden_slice_ids.json`）

## 路徑慣例

- 原始資料：`data/raw/`
- Graph 產出：`graph/`（在 .gitignore 中，不進 git）
- 腳本：專案根目錄 `step*.py`
