# 1111 Skill Graph — 2026 雲湧智生：臺灣生成式 AI 應用黑客松

分支：`feat/skill-graph`

從 1111 職缺資料建立技能圖譜，用於檢索與排序，並產出命題要求的 graph schema、
traversal trace 與「有圖 vs 無圖」ablation。

**先讀這三份，再動手：**

| 文件 | 用途 |
|------|------|
| `docs/SKILL_GRAPH_PLAYBOOK.md` | 建圖手冊、Schema v0.1（已凍結）、品質閘門、分工 |
| `.kiro/steering/project-state.md` | 目前進度、**已量過的數字**、10 個已知地雷 |
| 根目錄兩份 `*.pdf` | 命題文件 + 工作坊簡報（權威來源，見 playbook §1.1；`*.pdf` 已 gitignore） |

數字不要在這份 README 裡複述——一律以 `project-state.md` 與各 manifest 為準，
避免出現兩個版本。撰寫交付文件的規則見 `.kiro/steering/deliverable-rules.md`。

---

## 目錄結構

```text
.
├─ pipeline/                       ← playbook 建圖 pipeline（正式交付路線）
│   ├─ step1…step9                    主線：資料 → 抽取 → 正規化 → 邊 → 匯出 → 閘門 → 檢索 → ablation
│   ├─ step_a0…step_a7b               A 支線：LLM bake-off、blacklist、分類、可重現統計
│   ├─ assertion_detection.py         step2b / step2c 共用的 assertion 判定（規則法）
│   ├─ build_phrase_lexicon.py        step2b 的片語詞典建置
│   ├─ llm_client.py                  Bedrock 呼叫層（step_a0b / step_a6）
│   ├─ ab_preprocess_eval.py          query preprocess A/B 全檢索評估
│   └─ probe_query_preprocess.py      preprocess / resolve 覆蓋率探測
│
├─ job_skill_graph/                ← 初版上傳的 package（見該資料夾 README）
├─ tests/                          ← mock 測試（不需要全量 artifact）
│
├─ docs/                           ← playbook、下一步計畫、Step 9 交付段落
├─ configs/model_registry.yaml     ← 模型註冊表（Gate 2；目前 frozen: false）
├─ prompts/                        ← LLM prompt 版本檔
├─ fixtures/                       ← 進 git 的小型種子檔與評估集
│
├─ *.pdf                           ← 命題文件 + 工作坊簡報（gitignore，留在根目錄）
├─ dataset/                        ← 六份原始 CSV（3.8GB，gitignore）
├─ graph/                          ← pipeline 產物（gitignore，每台機器各自本地）
└─ graph_track_a_compare/          ← Track A 對照 eval 報告（**只放結果**，進 git）
```

慣例：**腳本一律放 `pipeline/`，產出的報告放 `graph_track_a_compare/`。**
`ab_preprocess_eval.py` 搬進 `pipeline/` 後仍寫回 `graph_track_a_compare/ab_preprocess_eval.json`。

**`graph/` 不進 git。** pull 完不等於有圖；要對齊就交換 artifact hash 或各自重跑。

---

## 兩套建圖程式並存，只有一套是正式的

repo 裡有兩份功能重疊的建圖實作，這是歷史造成的，不是重複造輪子：

- **正式路線**：`pipeline/step1`→`step9`（+ `step_a*` 支線）。playbook 定義的 Schema v0.1，
  edge 為 `HAS_SKILL` / `IN_OCCUPATION` / `SUBCATEGORY_OF` / `REQUIRES_CREDENTIAL` /
  `CO_OCCURS_WITH` / `CORE_SKILL`。品質以 `pipeline/step7_quality_gate.py` 為準。
- **初版**：`job_skill_graph/`（Josephtsay 2026-07-26 上傳）。edge 為
  `REQUIRES` / `PREFERS` / `MENTIONS` / `ALIAS_OF` / `INSTANCE_OF` / `IS_A` …，
  與 Schema v0.1 不相交，playbook §3.2 已把前三者降級為相容 view。

該 package 內仍有在線上的部分（`metrics.py`、`location_mask.py`、`retrieval.py`、
`search_service.py`、`api.py`、`ltr.py`、`graph_features.py`），
逐檔的「仍在用 / 已被取代」對照表在 `job_skill_graph/README.md`。

---

## pipeline 執行順序

**一律從 repo 根目錄執行**，例如 `python pipeline/step3_canonicalization.py`。

腳本內用 `_REPO_ROOT = Path(__file__).resolve().parent.parent` 錨定根目錄，
所以 `graph/`、`dataset/`、`fixtures/`、`prompts/`、`configs/`、`.env` 仍然讀根目錄那份，
不會跑到 `pipeline/` 底下。**要再搬動 `pipeline/` 的層級，就得同步改這行。**

```text
pipeline/step1_train_data_prep.py        職缺載入、職務對照、golden slice
pipeline/step1b_occupation_alias.py      職類 alias
        ↓
pipeline/step2a_structured_extraction.py 結構化欄位抽取
pipeline/step2b_phrase_extraction.py     片語比對抽取（詞典來自 build_phrase_lexicon.py）
pipeline/step2c_assertion_challenge.py   assertion 挑戰集（規則法勝過 LLM，見地雷 #7）
        ↓
pipeline/step3_canonicalization.py       registry_key / canonical ID / alias 綁定
pipeline/step3b_derive_aliases.py        括號別名萃取
        ↓
pipeline/step4_edge_assembler.py         HAS_SKILL / IN_OCCUPATION / REQUIRES_CREDENTIAL
pipeline/step5_statistical_edges.py      CO_OCCURS_WITH / CORE_SKILL / global_job_frequency
        ↓
pipeline/step6_graph_export.py           nodes.csv / edges.csv
pipeline/step7_quality_gate.py           品質閘門（無 FAIL 才算過）
        ↓
pipeline/step8_retrieval_smoke.py        檢索 smoke + 評估 harness
pipeline/step9_ablation.py --ablation    B0 / G1 / G2
```

A 支線（LLM）：`step_a0`/`step_a0b` bake-off → `step_a5` soft-skill blacklist →
`step_a6` skill 分類 → `step_a7`/`step_a7b` 可重現統計。

### 執行注意

- **全量步驟不要前景跑**（1,218,635 筆職缺）。預設 120s timeout 一定不夠，
  用背景執行再追 log。`.kiro/hooks/pipeline-run-guard.json` 會提醒。
- **重跑 `step3` 會改 registry ID**，必須連帶重跑 `step_a6` 與 Step 4→8。
  先跑 `python pipeline/step_a7b_key_change_impact.py` 確認影響範圍。
- `exit 0` 不是成功證據，以 Step 7 結果為準。
- `step8` 的評估 harness 會 import `job_skill_graph.metrics`；該 package 在根目錄，
  不是 `pipeline/` 的 sibling，靠 `pipeline/step8_retrieval_smoke.py` 開頭的 `sys.path` 插入解決。

---

## 測試

不需要全量 artifact，可直接跑：

```bash
python3 tests/test_location_mask_mock.py   # 24 checks
python3 tests/test_step4_mock.py
python3 tests/test_step5_mock.py
python3 .kiro/hooks/scripts/selftest.py    # 24 checks，驗 .kiro hooks
```

---

## `.kiro/` 設定

- `steering/` — 每次對話自動載入的專案規則（資料安全、schema 規則、交付文件規則、目前進度）
- `hooks/` — `git-safety`（阻擋誤 commit artifact / `.env`）、
  `schema-freeze-guard`（改到 Schema v0.1 凍結範圍時要求確認）、
  `pipeline-run-guard`（全量步驟與 live LLM 成本提醒）
