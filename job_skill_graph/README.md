# `job_skill_graph/` — 初版上傳的 package

這裡是 **Josephtsay 於 2026-07-26 一次性上傳**的程式（commit `1d0dc54` "Add files via upload"，
`8ae3861` 初始化，`97287d8` 補 README）。原本 19 個檔案全部平鋪在 repo 根目錄，
2026-08-02 整理時整組移進本資料夾。

移動的理由與作法：這些檔案彼此用**相對匯入**（`from .models import ...`），
必須整組待在同一個 package 目錄才 import 得起來，不能逐檔拆散。
目錄取名 `job_skill_graph` 是因為 package 本來就叫這個名字
（見 `__init__.py` docstring 與初版 README 描述的 `src/job_skill_graph`）。
副作用是 `tests/test_location_mask_mock.py` 原本為了繞開「目錄名 `1111` 不是合法 Python identifier」
而寫的 `importlib.spec_from_file_location` 別名 hack 可以拿掉，現在 `import job_skill_graph.x` 直接可用。

---

## 這組檔案有兩種性質，不要混為一談

使用者問的「有些像按 PDF 建的、有些像沒被採納的初版 graph」——兩者都對，各佔一部分。
以下依**實際量到的證據**分類，不是靠檔名猜測。

### A. 仍在線上的服務層（有被後續工作實際使用／修改）

| 檔案 | 證據 |
|------|------|
| `metrics.py` | **playbook pipeline 唯一實際 import 的舊模組**：`pipeline/step8_retrieval_smoke.py` 取 `ndcg_at_k` / `reciprocal_rank` / `hit_at_k` / `evaluate_rankings` |
| `location_mask.py` | **不是舊檔**。playbook 期產出（`fe7a0c7`, 2026-08-02, 陳奕蓁），放這裡是因為被 `cli.py` / `retrieval.py` / `search_service.py` 以相對匯入使用 |
| `retrieval.py` | `97287d8` 之後被改 +97 行（location allowlist 整合） |
| `search_service.py` | `97287d8` 之後被改 +141 行 |
| `cli.py` | `97287d8` 之後被改 +29 行 |
| `api.py`, `graph_features.py`, `ltr.py` | 排序 / API 層，playbook §1 明列的「需要對齊介面的排序 / API 成員」surface；未被 playbook step 取代，但 playbook 期也沒動過 |

### B. 已被 playbook pipeline 取代的初版建圖流程

對照關係如下。這是**功能對應**，不是自動遷移——兩套各自獨立實作：

| 初版（本資料夾） | 取代者（`pipeline/`） |
|------------------|----------------------|
| `dataset_1111.py` | `pipeline/step1_train_data_prep.py` |
| `structured_extraction.py` | `pipeline/step2a_structured_extraction.py`、`pipeline/step2b_phrase_extraction.py` |
| `extraction.py`（Bedrock 介面） | `pipeline/llm_client.py`、`pipeline/step_a0b_llm_bakeoff_run.py` |
| `canonicalization.py` | `pipeline/step3_canonicalization.py`、`pipeline/step3b_derive_aliases.py` |
| `graph_builder.py` | `pipeline/step4_edge_assembler.py` + `pipeline/step6_graph_export.py` |
| `cooccurrence.py` | `pipeline/step5_statistical_edges.py` |
| `graph_validator.py` | `pipeline/step7_quality_gate.py` |
| `schema.py`、`models.py` | Schema v0.1（playbook §3）＋ §3.4 extraction JSONL 契約 |
| `embeddings.py` | 無對應；playbook pipeline 未使用 |

**「初版沒被採納」的直接證據是 edge 詞彙表不相交：**

- 初版 `graph_builder.py`：`REQUIRES` / `PREFERS` / `MENTIONS`（:305-307）、`INSTANCE_OF`、`ALIAS_OF`、`IS_A`、`CO_OCCURS_WITH`、`CORE_SKILL`
- playbook `pipeline/step4_edge_assembler.py` / `pipeline/step5_statistical_edges.py`：`HAS_SKILL`、`IN_OCCUPATION`、`SUBCATEGORY_OF`、`REQUIRES_CREDENTIAL`、`CO_OCCURS_WITH`、`CORE_SKILL`

playbook §3.2 明文把初版那三種降級為相容 view，而不是資料層來源：
「若排序組需要舊式 `REQUIRES` / `PREFERS` / `MENTIONS` edge type，可由 `HAS_SKILL` 產生相容 view」。
§3.1 也把 `SkillAlias` 節點排除在 MVP 外，而初版 `graph_builder.py:389` 會建 `ALIAS_OF` 邊。

---

## 注意事項

1. **本 package 不產生正式交付的圖。** 正式圖由 `pipeline/step1`→`step9` 產生，輸出在 `graph/`
   （gitignore 內，不隨 git 傳遞）。不要用 `graph_builder.py` 的輸出去對 Step 7 品質閘門。
2. **A 組（已被取代）的檔案沒有測試涵蓋，也沒有在目前資料上重跑過。** 只驗證了 import 成功，
   未驗證輸出正確性。要沿用任何一支之前請自行確認。
3. `canonicalization.py` 在 `.kiro/hooks/scripts/schema_guard.py` 的 `FROZEN_SURFACE` 名單內
   （比對 basename，移動資料夾不影響），改它仍會要求確認。
4. 初版 README 描述的 `src/job_skill_graph/`、`config/`、`notebooks/`、`tests/`、`outputs/`、
   `pyproject.toml`、`requirements.txt` **在本 repo 從未存在**——當初只上傳了這 19 個 `.py`。
   它提到的 `config/dataset_1111.yaml`、`config/graph_schema.yaml`、9 本 notebook、
   11 支 pytest、`job-skill-graph` CLI entry point 也都不在 repo 裡，
   所以「70 個測試全部通過」無法在此驗證。
   該檔的逐模組說明對上表 A 組仍有參考價值，原文保留在 git 歷史（`git show 97287d8:README.md`），
   但其中所有 `C:\Hackathon\...` 路徑一律無效。
