---
inclusion: auto
---

# 資料安全與 Git 規範

## 資料安全

- 不得把 `graph/`、`*.parquet`、大型 CSV、`dataset/` 加入 git（已在 .gitignore）
- 不得在 chat 中 echo `dataset/` 原始資料超過 5 行 sample
- 原始資料實際位於 **`dataset/`**（`data/raw/` 不存在，勿再引用）
- 不得捏造資料、統計數字或假設未取得的資訊；若不確定，停下來詢問
- 不得擅自決定需要雙人同意的事項（模型選擇、品質門檻、Schema 變更）
- 不得在非結構化 mention 全部預設 `affirmed`——assertion gate 必須如實運作
- 全量職缺可用於建圖（主辦方口頭確認），但 query/行為資料的 test 切分仍在：test 期查詢、點擊、應徵不得流入圖節點、邊、alias 或統計量
- 「全量可用」的書面來源尚待補齊（見 Playbook §1.1）；提交前必須附上可查證依據

## Git 規範

- 工作分支：`feat/skill-graph`
- Commit 前確認沒有 stage 到 .gitignore 內的檔案或超過 100KB 的檔案
- 只 commit：腳本 (*.py)、config (*.yaml, *.json < 100KB)、文件 (*.md)
- Commit message 格式：`<type>(<scope>): <description>`
  - type: feat / fix / chore / docs
  - scope: step1 / step2 / step3 / step4 / step5 / step6 / step7 / schema / infra

## 路徑慣例

| 用途 | 路徑 |
|------|------|
| 原始資料（不進 git） | `dataset/` |
| Graph 產出（不進 git） | `graph/` |
| Pipeline 腳本 | 根目錄 `step*.py` |
| 團隊文件 | `docs/` |
| Kiro config | `.kiro/` |
