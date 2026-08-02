---
inclusion: auto
---

# 目前進度與已知地雷

**基準 commit：`feat/skill-graph@47e543d`（2026-08-02）**

這份檔案記錄「已經量過、不必再算」的事實，以及踩過的坑。數字有變請更新，不要讓它過期。

## 建圖狀態

Step 1→9 已在 **B 的機器**跑完，Step 7 品質閘門 **13 PASS / 0 FAIL / 2 WARN**。

**`graph/` 在 .gitignore 內，所以圖產物不隨 git 傳遞 —— 每台機器各自本地。**
pull 完不等於有圖。要對齊就交換 artifact hash 或各自重跑，不要假設對方的檔案在你這裡。
A 的機器目前只有 Step 1/2/3 + A5/A6 產物，Step 4→9 沒有輸出。

## 已量測結果（test split, 2,000 queries）

| 場景 | B0（無圖） | G1（有圖） | G2（+LLM 分類） |
|---|---|---|---|
| re-ranking NDCG@10 | 0.4308 | 0.4347 | 0.4347 |
| re-ranking MRR | 0.3958 | 0.3985 | 0.3985 |
| full retrieval NDCG@10 | 0.0127 | 0.0125 | 0.0125 |
| full retrieval Recall@50 | 0.029 | 0.029 | 0.029 |

**G1 = G2 逐位數相同，LLM 分類對指標貢獻 0.0000。** 別再說「分類帶來增益」。

**指標不動的根因（已定位，不是 LLM、也不是 traversal）：**
top 3,000 query（163,286 實例）只有 **0.1%** 解析到 skill 錨點、49.6% 到 occupation、50.3% 完全未命中；
`HAS_SKILL` 只覆蓋 **30.4%** 職缺；`alias_dictionary.csv` 只有 **37 筆** skill alias 對 1,437 個節點。
要拉指標就補 alias 與技能覆蓋，調 prompt 或 traversal 不會動。

## 常用數字（免得重算）

- 職缺總數 1,218,635；任一結構化能力欄非空 382,758（31.41%）
- Skill 節點 1,437 = `工作技能` 1,032 + `電腦技能資料` 405（**只來自這兩個結構化欄位**）
- `skill_kind`：technical 1,048 / tool 388 / soft 1（全字典 1,437）
  manifest 的 `kind_distribution` 是 1,030/388/1，分母只算取得回覆的 1,419 筆 —— 兩者都對，別當成矛盾
- Credential 節點 1,637；alias 37 筆（全 manual、全 unique、無 occupation alias）
- blacklist v0.2 與 v0.3 都只封鎖同一筆 `skill:具備溝通協調能力`（DF 1,040）
- `工作技能` 1,032 節點中只有 145 個（14.1%）帶「能力/技巧/知識/技能」字樣，佔該欄位 DF 16.9%

## 已知地雷

1. **`graph/` 不進 git** —— 見上。最常犯的誤判。
2. **重跑 Step 3 會改 296 個 registry ID**（skill 40 + credential 256，`47e543d` 之後）。
   必須連帶重跑 `step_a6` 與 Step 4→8，否則 Step 7 dangling edge 會 FAIL。
   先跑 `python pipeline/step_a7b_key_change_impact.py` 確認。
3. **`fixtures/skill_alias_seed_v0.1.csv` 存的是正規化後的舊格式 target**（如 `kubernetes(k8s`）。
   靠 `step3` 比對前 sanitize 才沒斷；少了那一行綁定會從 37 掉到 17，
   `node.js` / `react` / `k8s` 會靜默失去入口。normalize 規則再動就會再踩。
4. **strict JSON 率恆為 0**（36/36 批次）。prompt 寫「no code fences」也沒用。
   可用性靠 `parse_strict_json()` 的 deterministic 修補層，不是靠 prompt。
5. **模型自評 confidence 不是證據。** 同一批 8 筆技能，誤判與正解的 confidence 都是 0.92–0.99。
   只寫進 audit，不參與任何 gate（playbook §3.4 #4）。
6. **LLM offset 不可用**（`offset_exact_rate` ≤ 0.0497）。一律用 evidence 字串重新定位。
7. **assertion 判定規則法勝過所有 LLM**（rules_v0.1 = 1.00 vs 最佳 LLM 0.80）。
   `assertion_status` 維持規則法，別換成 LLM。
8. **G1 對照組必須是空黑名單，不能用 v0.2** —— v0.2 與 v0.3 封鎖同一筆，相減為零。
9. **`lstrip("./")` / `strip("()")` 這類寫法會按字元集刪除**，不是刪前綴。
   `".env".lstrip("./")` == `"env"`。已在 `step3` 與 `git_guard` 各踩一次。
10. **全量步驟不要前景跑**（1.22M 筆、279MB parquet、1.1GB extractions）。
    用 `run_in_background=true`，預設 120s timeout 一定不夠。
11. **`data/raw/` 這個目錄不存在，但有四支腳本指著它**（2026-08-02 整理時發現，**與搬檔無關**）：
    `pipeline/step1_train_data_prep.py:34`、`pipeline/step1b_occupation_alias.py`、
    `pipeline/step6_graph_export.py:238`、`pipeline/step8_retrieval_smoke.py:837`。
    實際資料在 `dataset/`，而 `pipeline/step2a`、`step2b` 指的就是 `dataset/`。
    證據：`graph/step1_manifest.json` 的 `source_files.jobs_csv.path` 記錄的是
    `dataset/職缺.csv`，但現在 code 讀 `data/raw/職缺.csv`，且 `JOBS_CSV` 沒有 CLI 覆寫。
    **代表 step1 現在跑不起來、無法重現自己的 manifest。** 修法是把這四處改成 `dataset/`，
    但這會動到 step6（凍結範圍），需 A/B 雙人同意，所以先記錄不逕行修改。

## 目錄結構（2026-08-02 整理後）

- `pipeline/` — playbook 建圖 pipeline（step1→9、step_a*、`llm_client.py` 等 22 支）
- `job_skill_graph/` — Josephtsay 初版上傳的 package；含仍在用的 `metrics.py`／`location_mask.py`
  與已被取代的初版建圖模組，逐檔對照見 `job_skill_graph/README.md`
- `tests/` — mock 測試
- **腳本一律從 repo 根目錄執行**（`python pipeline/stepX.py`）。
  腳本內用 `Path(__file__).resolve().parent.parent` 錨定根目錄，
  所以 `graph/`／`dataset/`／`fixtures/`／`prompts/`／`configs/`／`.env` 都還是讀根目錄那份。
  再搬動 `pipeline/` 的層級就要同步改這行。

## 尚未完成（會影響評分）

- **`playbook §1.1` 的「建圖資料範圍決策來源」仍空白。** 命題文件明文規定圖譜只能用 train 期 JD，
  **違者該指標項不計分**；目前只有口頭確認，沒有書面佐證。這是風險最高的未決項，且與程式無關。
- Gate 0 清單還有 10 項空白（source hash、method thresholds、負責人、golden slice hash…）
- `model_registry.yaml` 仍 `frozen: false`，`chosen`/`fallback` 為 `null` —— Gate 2 從未正式 freeze
- 分類準確率無數字：`fixtures/skill_kind_eval_set_v0.1.csv` 100 筆 `expected_skill_kind` 全空
- `avg_cost_usd_per_job` 全 `null`（缺帳號 rate card）

詳細待補清單見 #[[file:docs/SKILL_GRAPH_STEP9_SECTION_A.md]] 的 A4 節。
