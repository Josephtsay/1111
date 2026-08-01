---
inclusion: fileMatch
fileMatchPattern: "docs/*.md"
---

# 交付文件撰寫規則

處理 `docs/` 下的文件時自動載入。這些規則是為了讓評審隨時能重跑我們宣稱的每個數字。

## 每個揭露數字都要能重現

- 文件裡引用的比例、計數、指標，都必須指得出**產生它的腳本或 artifact 路徑**
- 不可用 ad-hoc 一次性計算後只把結果寫進文件 —— 規則要寫死在腳本裡並 commit
- 反例（真實踩過）：`SKILL_GRAPH_NEXT_PLAN.md` r2 寫「37.4%（DF 佔 52.1%）是純動作職責描述」，
  但沒留腳本，事後無法重現；最接近的操作化只有 27.9% DF。
  對照 `14.1%` 可重現（`step_a7_data_composition.py`，規則 `能力|技巧|知識|技能`）
- 現有可重現腳本：
  - `step_a7_data_composition.py` → 資料組成比例
  - `step_a7b_key_change_impact.py` → registry_key 變更影響
  - `step9_ablation.py --ablation` → B0/G1/G2 指標

## 誠實原則（優先於好看）

- 未量化就寫「未量化」並附取得方式，**不填估計值、不填佔位數字**
- 分母不同的兩組數字要同時列出並解釋，不要只挑有利的那組
- 負面結果照寫：規則法贏過 LLM、ablation 為零增益、指標退步，都要留在文件裡
- 一個結果有多種解讀時全部列出。例：LLM 分類與規則法「零分歧」可以是獨立複驗成功，
  也可以是 prompt 被調到只複製規則法答案 —— 沒有人工標註前不能只講前者
- 區分「已驗證」與「假設」。假設要標明未驗證及驗證方法

## traversal trace 要求

- 必須用**真實** `job_id` 與真實 edge properties
- 不可出現 `...` 或省略節點
- 需可由 `graph/retrieval_smoke_report.json` 的 `trace` 欄位對照

## 引用格式

- 提到程式行為時附檔名與行號（如 `step6_graph_export.py:113`）
- 提到 artifact 時附相對路徑，並註明是哪台機器產生的（`graph/` 不進 git）
- 提到 commit 時用短 hash（如 `47e543d`）
