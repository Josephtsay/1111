# Skill Graph 新計劃與分工（2026-08-01 修訂版 r2）

> 對象：Role A（Content Graph）／Role B（Structure Graph）
> 基準：`feat/skill-graph`；B 最新 `a28646b`，A 最新 `e68c347`（blacklist v0.2 + bake-off）
> 本文件補充而非取代 `docs/SKILL_GRAPH_PLAYBOOK.md`
> 定案路線：**職缺主路徑 = 2A／2B 斷詞與結構化；LLM = 技能分類（必做）＋可選職務對照表擴詞；禁止職類常識直接寫入 HAS_SKILL。**

---

## 0. r2 修訂了什麼（只讀這段也夠）

r1 方向正確，但有兩個會讓交付出問題的地方：

| # | r1 的問題 | 實測依據 | r2 的修正 |
|---|-----------|----------|-----------|
| 1 | **分類的 ablation 會是空的**。`skill_kind` 沒有任何下游消費者——只有 step3 寫死 `technical`、`step6:113` 搬進 `nodes.csv`；step5／step8 都不讀它。「開／關分類」會得到完全相同的檢索結果，LLM 又變成裝飾（playbook 陷阱 #12） | 全 repo grep `skill_kind` 僅 3 處，全為寫入或搬運 | **分類必須接到既有唯一消費點**：`skill_kind ∈ {soft, non_skill}` → 過統計守衛 → 併入 `soft_skill_blacklist_v0.3.csv` → step5 已會吃最新版 → 影響 CO_OCCURS／CORE_SKILL／global DF → step8 才會真的變。已實作於 `step_a6` |
| 2 | **ablation matrix 被低估**。step8 是 smoke test（只有命中數與 latency），沒有 relevance label、沒有 B0 baseline；`feature_flags` 在 `step6:321`／`step8:563` 是**寫死的字面值**，不是開關 | `metrics.py` 已有 `ndcg_at_k`／`mrr`／`hit_at_k`，但 step8 完全沒引用 | 升為 **Phase 2（B 主線）**，優先於對照表擴詞。時間不足時**砍擴詞、保 harness** |
| 3 | 把「分類 vs blacklist 衝突」當例外處理 | 兩份 soft 定義會長期分歧 | 改為**單一來源**：分類產生清單，A5 統計訊號當守衛層 |
| 4 | 未處理 `dictionary_version` 升版一致性 | `extractions.jsonl` 每個 mention 內嵌 `dictionary_version: v0.1` | **不升 `dictionary_version`**；分類版本另存 `skill_kind_version` / `skill_kind_source` / `skill_kind_confidence`（step6 依欄名讀，多欄位無影響） |
| 5 | 「用 bake-off 選分類模型」 | Gate 1 量的是**抽取**任務（strict JSON／grounding／offset／assertion），不可轉用 | 分類需自己的人工評測集（`--make-eval-set`，1,437 抽 100 筆分層） |
| 6 | 「v0.1 幾乎 0 hit」 | 實測**恰好 0 hit** | 已更正 |
| 7 | 「A 修了 step5 的 `csv_mod` NameError」 | B 已在 `b8bcb6b` 自行修好（頂層 import） | A 的重複 local import 已刪；A 對 step5 只剩 loader 預設路徑一項改動 |

**預期管理**：v0.2 只封鎖 1 筆，B 重跑 step5 幾乎看不出差別。這是正常的——技能詞彙表被限制在 1,437 個結構化欄位值，本來就幾乎沒有純軟技能。要有可見效果得靠 Phase 1 把 soft 集合擴大。

---

## 1. 現況（已驗證，非推測）

### 1.1 A 已 commit（`e68c347`，**尚未 push**）

| 檔案 | 內容 |
|------|------|
| `fixtures/soft_skill_blacklist_v0.2.csv` | 全量 121.8 萬職缺統計導出；封鎖 1 筆（`skill:具備溝通協調能力`，DF 1,040、跨 20 大類） |
| `step_a5_soft_skill_blacklist.py` | dual-signal（詞彙 + DF／熵／salience）；Excel/Word/PPT/Outlook 列 `retained_downweight` 不封鎖 |
| `llm_client.py` | Bedrock Converse（botocore SigV4；此環境無 boto3） |
| `step_a0b_llm_bakeoff_run.py` | bake-off：mock／dry-run／live／rescore |
| `configs/model_registry.yaml` | Gate 1 實測；`frozen: false`；flags 全 false |
| `step5_statistical_edges.py` | loader 預設吃最新 `soft_skill_blacklist_v*.csv`（**需 B ack**） |

Gate 1 實測（25 筆 golden × 4 模型）的 4 個 blocking issues 已記在 registry：
`strict_json=0`（prompt 沒禁 code fence）、`offset_exact ≤ 0.05`（模型 offset 不可用）、
`assertion 0.60–0.80 < 0.85 門檻`（規則法同題 1.0）、錯誤集中在 conditional 語氣。

### 1.2 A 已寫、待 commit

| 檔案 | 狀態 |
|------|------|
| `prompts/llm_skill_classification_v0.1.txt` | 分類 prompt；已明確禁止 code fence |
| `step_a6_skill_classification.py` | 分類腳本；80 筆試跑 100% 正常；全量執行中 |

### 1.3 B 已完成（遠端 `a28646b`）

`step4_edge_assembler.py`、`step5_statistical_edges.py`、`step6_graph_export.py`、
`step7_quality_gate.py`、`step8_retrieval_smoke.py`；CORE_SKILL boost redesign、edge dedup、csv import 修復、perf。

### 1.4 資料流與介面凍結點

```text
dataset/職缺.csv + 職務對照表.csv
        ├─► [A] 2A 結構化 ─┐
        └─► [A] 2B 斷詞   ─┴─► [A] Step3 ─► graph/extractions.jsonl + dictionaries
                                                    │
                    [A] step_a6 分類 ──► skill_dictionary.csv (skill_kind)
                                    └──► fixtures/soft_skill_blacklist_v0.3.csv
                                                    │
        [B] Step4 組邊 ─► [B] Step5 統計邊（吃 blacklist）─► [B] Step6 匯出
                                                    │
                        [B] Step7 quality ─► [共同] Step8 smoke + traces
```

- **A → B 凍結點**：`graph/extractions.jsonl` + `skill_dictionary.csv` + `credential_dictionary.csv` + `alias_dictionary.csv`
- **A → Step5**：`fixtures/soft_skill_blacklist_v*.csv`（contract：`canonical_id` + `status`；只有 `status=rejected` 被忽略）
- **A → Step6**：`skill_dictionary.csv` 的 `skill_kind` 欄（step6 依欄名讀，B 無需改碼）

---

## 2. Phase 1 — LLM 技能分類（A 主線）

目標：讓 LLM 進入正式建圖、**且真的有下游效果**，同時不掃 122 萬 JD。
規模：1,437 筆 → 批次 40 → 36 次呼叫、約 41k input tokens、數分鐘、數美分。

| ID | 任務 | 負責人 | Done when |
|----|------|--------|-----------|
| P1.1 | `step_a6` + 分類 prompt | **A** ✅ | dry-run／live 可跑 |
| P1.2 | 全量分類 1,437 筆，寫回 `skill_kind` | **A** 🔄 | coverage ≥ 90%；dictionary 更新 |
| P1.3 | 產出 `soft_skill_blacklist_v0.3.csv`（v0.2 超集） | **A** 🔄 | step5 載入筆數 > v0.2 |
| P1.4 | 人工評測集 100 筆 → 分類準確率、LLM vs 規則法對比 | **A 產檔、需人標** | `--eval` 有數字 |
| P1.5 | commit + push、通知 B | **A** | 遠端可見 |
| P1.6 | `use_llm_classification` 進 step6／step8 manifest 且可切換 | **B**（A 提規格） | true/false 兩份 manifest |

### 分類法定案（2026-08-01 人工裁定，已凍結）

`skill_kind` 只有四個值：`technical` / `tool` / `soft` / `non_skill`。

**明確不設 `task` 類別。** 曾考慮過，因為 LLM 用初版 prompt 時把
「電話接聽與人員接待」（DF 14,456）、「維護辦公室環境清潔」、「達成產能與出貨目標」
判成 `soft`，差一步就把真實工作內容從統計邊移除。但「任務 vs 技能」邊界本身模糊——
雇主把「櫃檯收銀服務」填進 `工作技能` 欄，對他而言那就是求職者要會的能力。
多一類的收益不足以抵銷 sign-off 與全量重跑成本。

**改為在 prompt 寫死判準**（`prompts/llm_skill_classification_v0.2.txt`）：
工作內容型敘述一律 `technical`；名稱含「溝通／表達／協調／服務」不代表 `soft`；
只有完全沒有專業或工作內容指向的純特質才是 `soft`；不確定一律給 `technical`。

**資料背景（交付文件需揭露）**：`工作技能` 衍生的 1,032 個 Skill 節點中，
僅 14.1% 的名稱帶「能力／技巧／知識」標記，37.4%（DF 佔 52.1%）是純動作職責描述。
節點集合本質上混合了「具名工具」「可遷移能力」「職務工作內容」三種性質；
這來自來源欄位本身的分類法，不是抽取錯誤。我們用 `skill_kind` 誠實標記，
不新增節點型別（`Task` 節點列為 schema v0.2 候選，本階段不做）。

已知殘留：16 筆未取得模型回覆，保留 step3 預設 `technical`（`skill_kind_source=step3_default`）；
2 筆仍帶 `skill_kind_v0.1` 標記。三者皆非 `soft`，不影響黑名單。

### 分類合規邊界（已寫進 manifest）

1. LLM 只能對**已存在 registry 的技能**指定 kind；不得新增／改寫／刪除 `registry_key`（腳本擋掉不在 batch 內的 key）。
2. 被封鎖的技能仍來自職缺原文（2A／2B evidence），不是模型憑常識造出來的。
3. 封鎖需 **dual-signal**：LLM 語意判斷 **+** A5 全量統計（DF ≥ 50、熵 ≥ 0.55 或跨 ≥ 8 大類、salience ≤ 0.5）。單靠模型不入圖。
4. `skill_kind` 詞彙表 `technical / tool / soft / non_skill` 由 A 提出，**需 A/B 雙人 sign-off**（會出現在 B 的 `nodes.csv`）；`credential` 保留給證照降級路徑。
5. 未回覆的技能保留 `technical`（保守，不封鎖）。
6. **部分執行（`--limit`）不得覆寫正式 v0.3**——step5 取最新版，一份只含少數項目的 v0.3 等於悄悄關掉黑名單。腳本已有防護。

---

## 3. Phase 2 — 指標 harness 與真開關（**B 主線，優先於擴詞**）

r2 最重要的調整。目前無法產出 playbook §Step 9 要求的 NDCG／MRR ablation。

| ID | 任務 | 負責人 | 接點 | Done when |
|----|------|--------|------|-----------|
| P2.1 | pull `e68c347` + A 分類 commit，重跑 Step5 → 6 | **B** | blacklist v0.3 | CORE_SKILL／nodes 反映 v0.3 與 `skill_kind` |
| P2.2 | `feature_flags` 由寫死改為 CLI／config 參數 | **B** | `step6:321`、`step8:563` | 可 `--no-graph`、`--blacklist <path>` 切換 |
| P2.3 | 接 `metrics.py` 到 step8：用 `dataset_1111.py` 切分，以瀏覽／應徵當 relevance label | **B** | `metrics.py` 已有 ndcg／mrr／hit | 產出 NDCG@10／MRR／Hit@1／Hit@10 |
| P2.4 | B0 baseline（無圖，BM25 或既有檢索） | **B** | — | 有對照組數字 |
| P2.5 | Step7 quality gate 跑過 | **B 執行、A 複核內容** | `step7_quality_gate.py` | 無 fail |

**安全紅線（playbook §10 #1）**：test 期 query／點擊／應徵**只能當評測 label**，不得回寫節點、邊、alias、registry 或統計量。

若 P2.3／P2.4 做不完：ablation 退為「命中數 + latency + trace 的定性對照」，並在交付文件**明確標註沒有 NDCG**，不可假裝有。

---

## 4. Phase 3 — Ablation 與 trace（共同）

| Run | graph | LLM 分類 | 用途 |
|-----|-------|----------|------|
| B0 | off | off | baseline |
| G1 | on | off | 純結構化圖增益（blacklist 用 v0.2 或空） |
| G2 | on | on | **命題核心：LLM 建圖增益**（v0.3 + `skill_kind`） |

另需：至少 1 條完整 traversal trace（真實 job_id、真實 edge properties、無 `...`）＋ 3–5 個 query 的 counterfactual。
B 跑管線與指標；A 負責 trace 內容正確性與失敗模式敘事。
`use_llm_extraction`／`use_llm_relations`／`use_adaptive_traversal` 維持 **false**。

---

## 5. Phase 4 — 職務對照表擴詞（可選，時間不足就砍）

| ID | 任務 | 負責人 | Done when |
|----|------|--------|-----------|
| P4.1 | LLM 讀 `職務對照表.csv` → 候選技能／別名（版本化） | **A** | `graph/occupation_skill_candidates_*.jsonl` |
| P4.2 | 過濾後併入 phrase lexicon（升版） | **A** | lexicon v0.2 |
| P4.3 | 重跑 2B → Step3 | **A** | evidence 仍來自職缺原文 |
| P4.4 | 通知 B 重跑 4→5→6→7→8 | **B** | Gate 再過 |

**禁止**：對照表技能直接當 `HAS_SKILL`（無 JD evidence）。**證照**維持 2A `專業證照`。
**成本警告**：這條路強迫全鏈重跑（2B 全量 + Step3 + B 的 4→8），是目前最貴的動作。

---

## 6. 明確不做

| 項目 | 決定 |
|------|------|
| 全量職缺 LLM 抽取 | 不做；`use_llm_extraction=false`（吞吐／成本，且 Gate 1 有 4 個未解 blocking issue） |
| `SEMANTICALLY_RELATED` | 預設關；非提交必要不開 |
| 斷詞結果再丟 LLM「總結後抽取」 | 不做（evidence／合規風險） |
| 改 Schema v0.1 核心節點／邊 | 本階段不改；需雙人同意升版 |
| 升 `dictionary_version` | 不升（會與 extractions.jsonl 內嵌版本不一致） |

---

## 7. 分工速查

| 角色 | 主責 | 不要做 |
|------|------|--------|
| **A** | 分類與 blacklist、prompt、評測集、trace 內容與失敗模式、可選擴詞 | 改 Step4/5/6/8 核心邏輯、全量 JD LLM 抽取 |
| **B** | pull 後重跑 5→6→7→8、feature flag 真開關、metrics harness、B0 baseline、CORE_SKILL 抽樣 | 改 extraction 格式、自寫技能 mention |
| **共同** | `skill_kind` 詞彙表 sign-off、Gate sign-off、ablation、trace、Step9 文件 | 單方面改 schema／介面契約 |

---

## 8. 重跑決策表

| 變更 | A 重跑 | B 重跑 |
|------|--------|--------|
| 只換 blacklist（v0.2→v0.3） | — | Step5 → 6（→ 7 → 8） |
| 只換 `skill_kind`（分類） | `step_a6` | Step6（→ 8）；**不必** Step4/5 |
| 擴 lexicon／改 2B | 2B → Step3 | Step4 → 5 → 6 → 7 → 8 |
| 改 assertion／canonical 規則 | 視情況 2B/3 | 4→5→6→7→8 |
| 只改 step8 query／flag／指標 | — | Step8 |

---

## 9. 交接檢查清單（A → B）

B pull 後應看到：

- [ ] `fixtures/soft_skill_blacklist_v0.2.csv` 與 `v0.3.csv`；`load_soft_skill_blacklist()` 回傳 v0.3 且筆數 > 1
- [ ] `graph/skill_dictionary.csv` 的 `skill_kind` 有多值（非全 technical）＋ 3 個新欄位；`dictionary_version` 仍為 v0.1
- [ ] `graph/skill_classification_audit.csv`、`skill_classification_manifest.json`（模型、prompt hash、與規則法分歧統計）
- [ ] `configs/model_registry.yaml`：`frozen: false`、flags 全 false、4 個 blocking issues
- [ ] `graph/skill_dictionary_pre_classification.csv`（回退用）

B 回報給 A：

- [ ] Step5 重跑後 CORE_SKILL 是否仍被泛用技能主導（抽樣）
- [ ] Step7 是否有新的 fail／warn
- [ ] Step6 `nodes.csv` 的 `skill_kind` 分布是否合理

---

## 10. 相關路徑

| 路徑 | 說明 |
|------|------|
| `docs/SKILL_GRAPH_PLAYBOOK.md` | 權威 schema／步驟／Gate |
| `docs/SKILL_GRAPH_NEXT_PLAN.md` | 本文件 |
| `step_a5_soft_skill_blacklist.py` / `step_a6_skill_classification.py` | A 的 blacklist 與分類 |
| `fixtures/soft_skill_blacklist_v*.csv` | Step5 過濾（自動取最新版） |
| `fixtures/skill_kind_eval_set_v0.1.csv` | 分類人工評測集 |
| `configs/model_registry.yaml` | 模型與 flag 狀態 |
| `metrics.py` | 已有 ndcg／mrr／hit，待接進 step8 |

---

## 11. 變更紀錄

| 日期 | 說明 |
|------|------|
| 2026-08-01 r1 | 初版：定案 LLM=分類＋可選擴詞 |
| 2026-08-01 r2 | 分類必須接 blacklist 才有 ablation 效果；指標 harness 升為 Phase 2 且優先於擴詞；blacklist 單一來源；不升 dictionary_version；分類需自己的評測集；更正 v0.1 為恰好 0 hit 與 csv_mod 歸屬 |
