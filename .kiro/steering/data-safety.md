---
inclusion: auto
---

# 資料安全規則

## 禁止事項

- 不得把 `graph/`、`*.parquet`、大型 CSV 加入 git（已在 .gitignore）
- 不得把 `data/raw/` 的原始資料 echo 到 chat 超過 5 行 sample
- 不得捏造資料或統計數字；若不確定，停下來詢問
- 不得擅自決定 cutoff、模型選擇、品質門檻等需要雙人同意的事項

## Git 規範

- 分支：`feat/skill-graph`
- Commit 前確認沒有 stage 到大檔或 .gitignore 內的檔案
- 只 commit 腳本、config、小型 metadata（< 100KB）
