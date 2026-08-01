# Step 9 交付文件 — A 段落（Content Graph）

> 角色：A（Content Graph）
> 基準 commit：`feat/skill-graph@47e543d`（含 B 的 `ddc273e` Phase 2 與 A 的 `47e543d` registry_key 修正）
> 對應 playbook：`docs/SKILL_GRAPH_PLAYBOOK.md` Step 9「LLM 在技能抽取／正規化／分類／關係建構中的必要角色」與「LLM／抽取失敗模式與防護說明」
> 狀態：A 段落草稿完成；B 段落（schema 正式版、traversal trace）另行提供。ablation 指標已由 B 實測，見 A1.5
>
> **本文件只寫已實測的數字。** 未量化的項目一律寫「未量化」並附取得方式，不填估計值。
>
> **artifact 位置提醒：** `.gitignore:51` 忽略整個 `graph/`，所以所有圖產物都是**各自機器本地的**，不隨 git 傳遞。本文引用的 B 端指標來自 `ddc273e` 的 commit message 與 B 機器上的 `eval_report_*.json`；A 端數字來自本機 `graph/`。兩邊要對齊必須交換 artifact hash 或各自重跑，不能假設 pull 完就有圖（見 A4.5）。

---

## A0. 引用來源與重現指令

文中每個數字都可由下列 artifact 重跑取得。

| 數字群 | 來源 artifact | 重現指令 |
|--------|---------------|----------|
| Gate 1 模型比較（4 模型 × 25 筆） | `graph/llm_bakeoff_v0.1/bakeoff_summary_live.json`、`configs/model_registry.yaml` | `python step_a0b_llm_bakeoff_run.py --mode live --limit 25` |
| 規則式 assertion 偵測器基線 | `graph/assertion_challenge_report.json` | `python step2c_assertion_challenge.py` |
| 技能分類結果與逐筆稽核 | `graph/skill_classification_audit.csv`、`graph/skill_classification_manifest.json`、`graph/a6_full_run.log`（v0.1 prompt）、`graph/a6_full_run_v2.log`（v0.2 prompt） | `python step_a6_skill_classification.py --mode live` |
| 黑名單 | `fixtures/soft_skill_blacklist_v0.2.csv`（規則法）、`v0.3.csv`（LLM + dual-signal） | 同上 |
| 資料組成揭露比例 | `graph/data_composition_manifest.json` | `python step_a7_data_composition.py` |
| 抽取覆蓋率 | `graph/extraction_structured_manifest.json`、`graph/extraction_phrase_manifest.json` | `python step2a_structured_extraction.py` / `step2b_phrase_extraction.py` |
| Ablation 指標 B0 / G1 / G2 | `graph/eval_report_{B0,G1,G2}.json`、`graph/ablation_comparison.json`（**B 機器**；A 本機未產出） | `python step9_ablation.py --ablation` |
| Step 7 品質閘門 | `graph/graph_quality_report.json`（**B 機器**） | `python step7_quality_gate.py` |
| registry_key 壞 key 計數 | `graph/skill_dictionary.csv` | `awk -F, 'NR>1 && $1 ~ /[()]/' graph/skill_dictionary.csv` |
| skill alias 筆數 | `graph/alias_dictionary.csv` | `python step3_canonicalization.py` |

模型憑證由環境變數（`.env`）提供，Provider = AWS Bedrock，Region = `us-west-2`，呼叫介面為 `bedrock-runtime Converse`（`llm_client.py`，botocore SigV4）。憑證未寫入任何 artifact 或 manifest。

---

## A1. LLM 在建圖中的必要角色

### A1.1 命題約束與我們的選擇

命題要求 LLM 必須實際參與抽取、正規化、分類或關係判斷之一，且必須可關閉做 ablation，不能只是附加展示。

我們選定的必要角色是 **Step 3 之後的技能分類（`skill_kind`）**，由 `step_a6_skill_classification.py` 執行：LLM 對 `graph/skill_dictionary.csv` 的 1,437 個 Skill 節點指定 `technical` / `tool` / `soft` / `non_skill`。

| 項目 | 值 |
|------|-----|
| 模型 | `us.anthropic.claude-haiku-4-5-20251001-v1:0`（Gate 1 preferred） |
| Prompt | `prompts/llm_skill_classification_v0.2.txt`，sha256 `9bc0edb6…2606f` |
| 分類版本 | `skill_kind_v0.2`（`dictionary_version` 保持 `v0.1` 不動） |
| 規模 | 1,437 筆 → 批次 40 → 36 次呼叫 |
| 實測 token | 輸入 91,527、輸出 85,025 |
| 覆蓋率 | 1,419 / 1,437 = 98.7% |
| 結果分布 | `technical` 1,030、`tool` 388、`soft` 1（分母為取得回覆的 1,419 筆；含未回覆者的字典分布見 A3.5） |
| 與規則法分歧 | 0（`both_not_soft` 1,418、`both_soft` 1） |

### A1.2 為什麼不是全量 JD 的 LLM 抽取

這是成本與時程問題，不是資料權限問題（主辦方已確認全部職缺可用於建圖）。

Gate 1 實測吞吐量最高者為 haiku 4.5 的 **365.7 jobs/hour**。以此推算 1,218,635 筆職缺：

```
1,218,635 ÷ 365.7 ≈ 3,332 小時 ≈ 139 天（單執行緒、不含重試與失敗重跑）
```

其餘候選更慢（sonnet 173.6、nova-lite 297.6、qwen3-32b 259.4 jobs/hour）。加上 A2.2 列出的四個未解 blocking issue，`use_llm_extraction` 維持 `false`。

分類任務的規模差了四個數量級：技能字典只有 1,437 筆，36 次呼叫、數分鐘、成本以美分計。**同樣是「LLM 實際參與建圖」，但落在可負擔且可驗證的位置。**

### A1.3 關鍵設計：分類必須接到既有消費點，否則 ablation 是空的

`skill_kind` 原本在 repo 內**沒有任何下游消費者**。實際檢查結果：

| 位置 | 行為 |
|------|------|
| `step3_canonicalization.py:229` | 寫死 `"skill_kind": "technical"` |
| `step6_graph_export.py:113` | `row.get("skill_kind", "technical")` 搬進 `nodes.csv` |
| `step2c_assertion_challenge.py:87` | 只讀取，不影響判定 |
| `step5_statistical_edges.py` / `step8_retrieval_smoke.py` | **完全沒有引用** |

也就是說，若分類只寫進 `nodes.csv`，「開／關 LLM 分類」會得到**完全相同的檢索結果**，LLM 等於裝飾（playbook §10 陷阱 #12）。

A6 因此把分類結果接到 repo 內既有的唯一消費點 —— soft-skill blacklist：

```text
skill_kind ∈ {soft, non_skill}
  → A5 全量統計守衛（dual-signal）
  → fixtures/soft_skill_blacklist_v0.3.csv
  → step5 自動載入最新版 soft_skill_blacklist_v*.csv
  → CO_OCCURS_WITH / CORE_SKILL / global_job_frequency
  → step8 檢索結果
```

這條路徑不需要 B 改任何程式：`step6` 依欄名讀 `skill_kind`，`step5` 的 loader 已預設取最新版黑名單。

### A1.4 合規邊界

| 邊界 | 實作位置 |
|------|----------|
| LLM 只能對**已存在 registry** 的技能指定 kind，不得新增／改寫／刪除 `registry_key` | `classify_batch()` 的 allowlist：`if key not in requested or kind not in VALID_KINDS: rejected` |
| 被封鎖的技能仍來自職缺原文（2A／2B evidence），不是模型憑常識造出來的 | 黑名單只收 `skill_dictionary.csv` 內既有 ID |
| 封鎖需 dual-signal：LLM 語意判斷 **且** A5 全量統計訊號 | `statistical_guard()` |
| 模型自評 confidence 不得作為通過依據（playbook §3.4 #4） | confidence 只寫入 `skill_classification_audit.csv`，不進字典、不參與 guard |
| 未取得回覆者保留 step3 預設 `technical` | 保守：不封鎖 |
| 部分執行（`--limit`）不得覆寫正式 v0.3 | 改寫 `…_partial_N.csv.txt`；否則 step5「取最新版」會撈到一份只含少數項目的黑名單，等於悄悄關掉過濾 |
| 換版不得回退：v0.3 必須是 v0.2 的超集 | `load_prior_blacklist()` 把規則法已封鎖項目 carry forward |
| `dictionary_version` 不升版 | `extractions.jsonl` 的每個 mention 內嵌 `dictionary_version: v0.1`，升版會造成 join 不一致；分類版本記於 manifest |

`skill_kind` 詞彙表固定四值，**明確不設 `task` 類別**。曾考慮增設，因為 v0.1 prompt 把「電話接聽與人員接待」等真實工作內容判成 `soft`（見 A2.3）；但「任務 vs 技能」邊界本身模糊——雇主把「櫃檯收銀服務」填進 `工作技能` 欄，對他而言那就是求職者要會的事。多一類的收益不足以抵銷雙人 sign-off 與全量重跑成本。改為在 prompt 寫死判準。`Task` 節點列為 schema v0.2 候選，本階段不做。

### A1.5 Ablation 定義與預期效果（不誇大）

| Run | graph | LLM 分類 | 黑名單 |
|-----|-------|----------|--------|
| B0 | off | off | — |
| G1 | on | off | 空黑名單 |
| G2 | on | on | `v0.3`（LLM + dual-signal） |

**必須誠實揭露的一點：G1 的對照組不能用 `v0.2`。** v0.2（規則法）與 v0.3（LLM）封鎖的是**同一筆**技能（`skill:具備溝通協調能力`，DF 1,040、eligible DF 1,037），兩者相減為零。要得到非退化的對照，G1 必須使用空黑名單。

即使如此，可測量的 delta 也只有 1,437 個節點中的 1 個。這個結果本身是資料事實：技能詞彙表被限制在兩個結構化欄位的 1,437 個值，本來就幾乎沒有純軟技能（見 A3）。

#### 實測結果（B，`ddc273e`，test split 2,000 queries）

| 場景 | Run | NDCG@10 | MRR | Recall@50 |
|------|-----|---------|-----|-----------|
| re-ranking（曝光候選內重排） | B0（無圖 / BM25） | 0.4308 | 0.3958 | — |
| | G1 | **0.4347** | **0.3985** | — |
| | G2 | **0.4347** | **0.3985** | — |
| full retrieval（121.8 萬職缺召回 top-50） | B0 | **0.0127** | — | 0.029 |
| | G1 | 0.0125 | — | 0.029 |
| | G2 | 0.0125 | — | 0.029 |

三個可驗證的結論：

1. **G1 與 G2 逐位數相同。** 這證實了上面的預測 —— LLM 分類目前對檢索指標的貢獻是 **0.0000**。原因有兩層：blacklist 只有 1 筆生效且 v0.2 已封鎖同一筆；`skill_kind` 的 `tool` / `technical` 區分仍然沒有下游消費者。
2. **圖在 re-ranking 有小幅正增益（+0.0039 NDCG@10、+0.0027 MRR），在 full retrieval 反而略降（−0.0002）。** 兩者都不足以稱為顯著改善。
3. **B 的診斷指出根本原因不在分類，而在 skill 錨點幾乎沒被觸發**：top 3,000 query（163,286 個實例）中只有 **0.1%** 解析到 skill 錨點、49.6% 解析到 occupation、50.3% 完全未命中；`HAS_SKILL` 只覆蓋 **30.4%** 的職缺；`alias_dictionary.csv` 只有 **37 筆** skill alias（全部 `source=manual`、`ambiguity_status=unique`、零 occupation alias）。

第 3 點直接落在 A 的責任範圍：**檢索用不到技能圖，主因是 A 端的技能覆蓋與 alias 太薄，不是 B 的 traversal 或 LLM 分類。** 37 筆手工 alias 對上 1,437 個節點，等於絕大多數技能沒有任何查詢入口。這是 Step 9 必須揭露的限制，也是 A 之後最該補的地方（見 A4.1 第 7 項）。

因此 **LLM 分類在本階段不帶來指標增益，我們也不這樣宣稱。** 它的實際價值在兩件可查證的事：

1. **獨立複驗**：LLM 在 1,419 筆上與規則法零分歧，其中包含規則法唯一封鎖項的一致確認。這比單一訊號的黑名單更有辯護力。
2. **擋下不可逆的品質損失**：v0.1→v0.2 的迭代攔下 8 筆誤判封鎖，這 8 筆合計 25,550 個 job-level 技能歸屬，若進了黑名單會從 `CO_OCCURS_WITH` / `CORE_SKILL` / `global_job_frequency` 中被靜默移除（見 A2.3）。

---

## A2. LLM 失敗模式與防護

### A2.1 Gate 1 bake-off 概況

25 筆 golden slice × 4 候選模型，任務是 Step 2B/2C 的非結構化抽取與 assertion。**未 freeze**（`model_registry.yaml: frozen: false`，四個 feature flag 全 `false`），本輪未產生任何進圖 artifact。

| 指標 | haiku45 | sonnet45 | nova-lite | qwen3-32b |
|------|---------|----------|-----------|-----------|
| `strict_json_success_rate` | 0.0 | 0.0 | 0.0 | 0.2 |
| `contract_valid_rate` | **1.0** | 0.88 | 0.92 | **1.0** |
| `evidence_grounded_rate` | 0.9771 | **1.0** | 0.8953 | 0.9618 |
| `hallucinated_mention_rate` | 0.0229 | **0.0** | **0.1047** | 0.0382 |
| `offset_exact_rate` | 0.0351 | 0.0497 | 0.0 | 0.0 |
| `assertion_accuracy` | **0.80** | 0.7667 | 0.60 | 0.6667 |
| `protected_pair_violations` | 0 | 0 | 0 | 0 |
| `unparseable_json` | 0 | 3 | 0 | 0 |
| throughput (jobs/h) | **365.7** | 173.6 | 297.6 | 259.4 |
| latency p50 / p95 (ms) | 8,542 / 15,567 | 13,176 / 32,430 | 8,201 / 17,939 | 11,658 / 24,093 |

Gate 1 建議：preferred `cand_haiku45`、fallback `cand_sonnet45`、不建議 `cand_nova_lite`（10.5% mention 的 evidence 在原文找不到，assertion 僅 0.60，違反 precision-first 政策）。`chosen` / `fallback` 仍為 `null`，待 Gate 2 雙人 sign-off。

### A2.2 四個 blocking issue

#### ① strict JSON 全數失敗 — 且 prompt 修正**沒有**解決

現象：四個模型的 `strict_json_success_rate` 為 0.0（qwen 0.2）。`json_repair_notes` 顯示 Claude 系列 25/25、22/25 回應被 code fence 包住。

初判根因：prompt 缺陷而非模型缺陷 —— `llm_extraction_v0.1` 寫了「no Markdown」但沒有明確禁止 code fence。

**後續實測推翻了這個樂觀預期。** 分類 prompt v0.2 已逐字加上 `No code fences. No backtick characters.` 並要求「Your entire reply must start with { and end with }」，但 A6 全量執行的 `strict_json_batches` 仍為 **0 / 36**。

結論與防護：**prompt 層的格式指令不是可靠的控制手段。** 真正承重的是 deterministic 的解析層 —— `parse_strict_json()` 的 `limited_json_repair_only` 策略（去 code fence、取第一個 `{...}`，不做語意修補）。這一層讓 36/36 批次全部成功解析、零 `batch_errors`。交付時應把「strict JSON 率」理解為 prompt 遵從度指標，而非可用性指標；可用性由解析層保證。

#### ② 模型自報 offset 不可用

現象：`offset_exact_rate` 全部 ≤ 0.0497，nova 與 qwen 為 **0.0**。而 playbook §3.4 建議非結構化 mention 必填 `start_offset` / `end_offset`。

防護：LLM 抽取管線**不得信任模型 offset**；必須用 `evidence` 字串在對應 `source_field` 上 deterministic 重新定位，定位失敗者 quarantine。這正是 2B phrase 抽取已採用的做法（`extraction_phrase_manifest.json` 的 `contract_compliance.start_end_offset_on_original_text: true`、`normalize_alignment_map: true`，且對齊失敗的命中「丟棄而非輸出壞 evidence」）。

#### ③ assertion 準確率全部低於門檻，規則法反而滿分

`extraction_config` 的 `assertion_challenge.minimum_pass_rate = 0.85`。四個模型為 0.60–0.80，全部未達標。同一份 48 題 challenge set（30 題有 assertion label）上，規則式偵測器 `rules_v0.1` 的表現是：

| 指標 | rules_v0.1 | 最佳 LLM（haiku） |
|------|-----------|------------------|
| `assertion_accuracy` | **1.00**（30/30） | 0.80（24/30） |
| `requirement_accuracy` | **1.00**（30/30） | 0.80 |
| `protected_pair_accuracy` | **1.00**（13/13） | — |
| `abbreviation_accuracy` | **1.00**（6/6） | — |

防護：`assertion_status` 繼續由規則偵測器負責，LLM 只用於補抽 mention。若要讓 LLM 決定 assertion，必須先達 0.85，否則其 mention 一律 quarantine。

這是本次 bake-off 最重要的負面結論：**在有明確語言線索的判定任務上，規則法勝過四個 LLM。** 我們據此把 LLM 移到規則法做不到的位置（語意分類），而不是硬塞進規則法已經做得更好的位置。

#### ④ 錯誤集中在條件語氣，否定語氣則全對

依類別分解 assertion 正確數：

| 類別 | 題數 | haiku | sonnet | nova | qwen |
|------|------|-------|--------|------|------|
| negation（不需 Python 經驗） | 12 | **12** | **12** | **12** | **12** |
| conditional（若熟悉 K8s 佳） | 12 | 9 | 6 | 3 | 5 |
| confusion（寵物 Python） | 5 | 2 | 4 | 2 | 2 |
| abbreviation | 1 | 1 | 1 | 1 | 1 |

兩個方向的錯誤風險不對稱：

- conditional 的失敗**全部**是 `affirmed → uncertain`（過度保守）。後果是漏抽，可接受。
- confusion 的失敗包含 `uncertain → affirmed`（`conf_001` 四個模型全錯）。這是**危險方向**：把語意混淆案例當成肯定技能寫進圖。

防護：`negated` / `uncertain` 一律不得物化成 `HAS_SKILL`（playbook Step 4 規則 2、Step 7 fail 條件），已由 B 的組邊層強制。prompt v0.2 需補強條件語氣示例、challenge set 需擴充 confusion 類題目 —— 兩者皆**未完成**，列入 A4 待補。

### A2.3 分類誤判 8 筆：根因與 dual-signal 防護的實際能力

#### 發生了什麼

v0.1 分類 prompt 的全量執行結果（`graph/a6_full_run.log`）：LLM 判定 10 筆 `soft` + 1 筆 `non_skill`，共 **11 筆**進入黑名單候選。dual-signal 統計守衛擋掉 2 筆，**9 筆**寫進 `soft_skill_blacklist_v0.3.csv`。

這 9 筆中只有 1 筆（`具備溝通協調能力`）是正確判定 —— **其餘 8 筆是誤判**。

| # | 技能 | DF | 跨大類 | 熵 | salience | v0.1 判定 | v0.2 判定 |
|---|------|-----|--------|-----|----------|-----------|-----------|
| 1 | 電話接聽與人員接待 | 14,456 | 18 | 0.5234 | 0.1273 | soft ✗ | technical ✓ |
| 2 | 維持場所的整潔與美觀 | 3,432 | 18 | 0.4570 | 0.0446 | soft ✗ | technical ✓ |
| 3 | 維護辦公室環境清潔 | 2,590 | 19 | 0.5379 | 0.0437 | soft ✗ | technical ✓ |
| 4 | 協商談判技巧 | 2,018 | 19 | 0.6596 | 0.0679 | soft ✗ | technical ✓ |
| 5 | 電話開發能力 | 1,550 | 18 | 0.2228 | 0.0419 | soft ✗ | technical ✓ |
| 6 | 達成產能與出貨目標 | 1,253 | 11 | 0.3628 | 0.0652 | soft ✗ | technical ✓ |
| 7 | 具有衝突處理的能力 | 201 | 12 | 0.2525 | 0.0195 | soft ✗ | technical ✓ |
| 8 | 具備口語及肢體表達能力 | 50 | 8 | 0.4059 | 0.0356 | soft ✗ | technical ✓ |
| — | **具備溝通協調能力** | 1,040 | 20 | 0.8827 | 0.0220 | soft ✓ | soft ✓ |

8 筆誤判合計 **25,550 個 job-level 技能歸屬**。若未攔下，這些會從 `CO_OCCURS_WITH`、`CORE_SKILL`、`global_job_frequency` 中被靜默移除 —— 而且 `HAS_SKILL` 邊仍在，使用者不會看到任何錯誤訊息，只會發現「電話接聽」這類技能在職類核心技能中憑空消失。

#### 根因（兩層）

**第一層：來源欄位的分類法與 taxonomy 不對齊。**
`工作技能` 欄位混合了三種性質不同的東西（見 A3.3）。v0.1 prompt 把 `technical` 定義為「可習得、可驗證的專業能力或作業技術」、`soft` 定義為「泛用人格特質、態度、人際或自我管理特質」。「維護辦公室環境清潔」兩邊都不完全符合：它不是「專業技術」，也不是「人格特質」。模型在定義的縫隙中把它推向 `soft`。

**第二層：詞面觸發，加上未說明的不對稱代價。**
8 筆中有 4 筆（協商談判技巧、電話開發能力、具備口語及肢體表達能力、具有衝突處理的能力）名稱含「溝通／表達／協調／衝突／談判」。v0.1 prompt 的 rule 3 只處理了複合技能的主體判定，沒有直接否證「含這些詞就是 soft」。rule 8 寫了「不確定時傾向 technical」，但**沒有說明誤判成 soft 是不可逆的統計移除**，模型沒有理由把保守偏誤設得那麼強。

**模型自評 confidence 在誤判上是 0.92–0.99。** 修正後同樣這 8 筆判為 `technical`，confidence 仍是 0.92–0.99。同一組項目、相反答案、相同高信心 —— 這是 playbook §3.4 #4（模型自評分數不得作為通過依據）最直接的實證，也是我們把 confidence 只留在 audit、不讓它參與任何 gate 的理由。

#### dual-signal 守衛實際攔住了什麼，沒攔住什麼

守衛條件（`statistical_guard()`，沿用 A5 v0.2 門檻）：

```text
DF >= 50
AND (normalized_entropy >= 0.55 OR distinct_major >= 8)
AND max_occupation_salience <= 0.50
```

**攔住的 2 筆**：

| 技能 | 擋下原因 |
|------|----------|
| `熟悉國際社交禮儀` | DF=1 < 50（統計支持不足） |
| `空間魔法師` | DF=57 過關，但 entropy 0.438 < 0.55 且 majors 7 < 8（分散度不足） |

**沒攔住的 8 筆**：全部通過三個條件。原因是結構性的 —— **高頻跨行業的工作內容，在統計上與泛用人格特質無法區分**。兩者都是高 DF、高分散度、低 salience。以 `電話接聽與人員接待` 為例：DF 14,456、18 個大類、salience 0.1273，每一項都比正確封鎖的 `具備溝通協調能力`（DF 1,040）更「像」一個該封鎖的泛用詞。

salience 上限確實有保護作用，但保護的是另一群對象：Excel（0.6108）、Word（0.6138）、PowerPoint（0.5223）都在 0.50 之上，**結構上不可能被封鎖**，只會被 A5 標記 `retained_downweight`。這正是 playbook §5 對 Office 類「保留 query 能力、依 salience 降權、不一律刪除」政策的實作。

**誠實結論：dual-signal 是必要但不充分的。** 它過濾低支持度雜訊、保護職類核心技能，但無法替代 taxonomy 正確性。把它宣稱為「LLM 誤判的防護網」會是誤導。

#### 真正攔住這 8 筆的是什麼

1. **強制輸出可稽核的排序清單**：A6 每次執行都印出依 DF 排序的 blocked 清單（含理由），並為 1,437 筆全部寫一列 `skill_classification_audit.csv`。`電話接聽與人員接待` 以 DF 14,456 排在第一行 —— 一個 DF 一萬四的項目被判為「泛用人格特質」，在排序清單上無法忽視。
2. **人工複核作為進 B 之前的閘門**：v0.3 產出後、B 消費前的人工審核（2026-08-01）攔下全部 8 筆。
3. **taxonomy 人工裁定 + prompt 升版**：v0.2 prompt 寫死判準 —— 工作內容型敘述一律 `technical`；「低技術門檻不等於 soft，掃地、接電話、收銀都是雇主要求會做的事」；名稱含「溝通／表達／協調／衝突／服務」不代表 soft，並逐字列出這 8 筆作為 `technical` 範例；不確定一律 `technical`，並說明「誤判成 soft 是不可逆的品質損失」；明示預期 soft 只有個位數到十幾筆。
4. **全量重跑**：v0.2 結果為 `soft` 1 筆、blocked 1 筆，與規則法零分歧。

#### 這個修正的一個副作用要一併揭露

v0.2 的 prompt 把判準寫得很緊，等於用 8 個具體反例引導模型。**它同時也讓 soft 偵測幾乎失去獨立性** —— 最終結果與規則法完全一致（`both_soft` 1、`both_not_soft` 1,418、零分歧）。「零分歧」可以讀成兩種故事：獨立複驗成功，或 prompt 被調到只會複製規則法的答案。目前**沒有人工標註可以區分這兩種解讀**（見 A2.6）。交付時應同時陳述這兩種可能，不可只挑有利的說法。

### A2.4 分層防護清單（依承重程度排序）

| # | 防護 | 類型 | 是否可被 LLM 繞過 | 實證 |
|---|------|------|------------------|------|
| 1 | registry_key allowlist | 結構性 | **否** | `classify_batch()` 丟棄不在 batch 內的 key |
| 2 | 四值 taxonomy 白名單 | 結構性 | **否** | `kind not in VALID_KINDS` → 丟棄 |
| 3 | deterministic JSON 解析層 | 結構性 | **否** | 36/36 批次解析成功，strict JSON 率 0/36 |
| 4 | v0.3 ⊇ v0.2 超集保證 | 結構性 | **否** | `load_prior_blacklist()` carry forward |
| 5 | 部分執行不覆寫正式黑名單 | 結構性 | **否** | `--limit` → `…_partial_N.csv.txt` |
| 6 | 未回覆保留 `technical` | 保守預設 | 否 | 18 筆全數未封鎖 |
| 7 | dual-signal 統計守衛 | 統計 | **部分** | 攔 2/11；8 筆誤判通過 |
| 8 | salience ≤ 0.50 上限 | 統計 | 否（對高 salience） | Excel/Word/PPT 結構上不可封鎖 |
| 9 | 排序稽核清單 + 人工複核 | 流程 | 否 | 攔下全部 8 筆 |
| 10 | 模型自評 confidence | **不採用** | — | 誤判與正解同為 0.92–0.99 |

第 7 項是唯一「部分有效」的自動防護，第 9 項才是實際攔下錯誤的那一層。這代表**目前的分類管線不是全自動安全的**，它依賴一次人工 sign-off。這一點必須寫進交付文件，不能讓評審誤以為 dual-signal 是自動閘門。

### A2.5 未取得分類回覆的 18 筆

覆蓋率 1,419/1,437 = 98.7%，18 筆未取得回覆（`graph/skill_classification_manifest.json` 的 `unclassified_registry_keys`）。全部保留 step3 預設 `technical`，因此**不會被封鎖**，最壞後果只是「少降權」而非「錯誤移除」。

受影響的最高 DF 項目：`產品/設備故障排除檢修`（2,124）、`繪製2d╱3d模型設計圖`（1,571）、`產品外型/包裝設計`（834）。

18 筆中有 **14 筆**的 `registry_key` 含分隔符或括號字元（`/`、`╱`、`、`、`,`、`(`、`)`、`&`、`_`、`-`）。假設是批次回應中的 key 無法逐字對回原始 key，因而被 allowlist 守衛丟棄。**此假設未經驗證** —— A6 目前把 `rejected_items` 留在呼叫層的 meta，沒有持久化到 manifest（`batch_errors` 為空，因為這些不是 HTTP 或解析錯誤）。剩餘 4 筆（`緊急甦醒術cpr施作`、`偉盟製造業groerp`、`聯合資訊mf2000`、`力冠erp`）不含特殊字元，多為冷門廠商 ERP 名稱，可能是模型改寫了 key。

修法（A4 待補）：把 `rejected_items` 寫入 manifest，即可確認假設並針對性重試。

### A2.6 尚未量化的缺口（不可用估計值填補）

| 缺口 | 現況 | 取得方式 |
|------|------|----------|
| mention precision / recall | **不可得**。golden slice 無人工 mention 標註，且這 25 筆的 phrase baseline 恰好為 0 mentions（phrase 全量覆蓋率僅 11.79%） | 重新分層 golden slice 納入有 phrase 命中的職缺，或建立人工 mention 標註 |
| 分類準確率 | **未量化**。`fixtures/skill_kind_eval_set_v0.1.csv` 已產出 100 筆分層樣本，但 `expected_skill_kind` 欄**全部為空**，尚未人工標註 | 人工填答後 `python step_a6_skill_classification.py --eval` |
| LLM vs 規則法的 soft 偵測 precision / recall | 未量化（需上一項的人工標籤） | 同上；`run_eval()` 已實作 |
| 每筆成本 (USD) | `avg_cost_usd_per_job` 全部為 `null`。已量到實際 token 數，但未取得本帳號 rate card | 由帳號擁有者填入 us-west-2 on-demand 費率；不採用第三方轉述數字 |
| canonical verifier 模型比較 | 未測（`model_registry.yaml` 的 `canonical_verifier.candidates: []`） | 須另量 protected-pair 錯誤率，且 verifier 必須能 abstain |
| conditional / confusion 補強 | prompt v0.2 未補條件語氣示例；challenge set 未擴充 confusion 類 | 見 A4 |

Gate 1 的 assertion 只有 30 題有標註，只夠做候選篩選，不足以作 Gate 2 freeze 依據。

### A2.7 非 LLM 失敗模式：括號別名洩漏進 registry_key（`47e543d` 已修）

這一項不是模型造成的，是 A 自己的正規化程式缺陷，但它汙染的是 ID 這種最難回溯的東西，所以必須一起揭露。

**現象**：`normalize_key()` 原本用 `str.strip("()")` 清括號，而 `str.strip` 只移除字串**兩端**的字元。像 `Kubernetes(K8S)` 這種值會掉尾括號、卻留下中間的 `(`，於是壞 ID 被直接寫進 `skill_dictionary.csv`：

```text
skill:kubernetes(k8s
skill:amazon_web_services_(aws
skill:docker(docker_compose
skill:eda(exploratory_data_analysis
```

**規模（實測，`step_a7b_key_change_impact.py`）**：受影響的不只技能字典 —— commit message 只舉了技能的例子，實際上證照字典受影響的數量是技能的 6.4 倍。

| 字典 | 總筆數 | key 值會改變 | 佔比 |
|------|--------|--------------|------|
| `skill_dictionary.csv` | 1,437 | **40** | 2.8% |
| `credential_dictionary.csv` | 1,637 | **256** | 15.6% |
| 合計 | 3,074 | **296 個 registry ID** | 9.6% |

證照名稱大量使用「英文縮寫(中文全名)」格式，所以命中率遠高於技能，例如
`acls(高級心臟救命術` → `acls_高級心臟救命術`、
`bec中高級(劍橋商務英語認證` → `bec中高級_劍橋商務英語認證`、
`british_council_englishscore_(300-399` → `british_council_englishscore_300-399`。

**為什麼嚴重**：playbook §3.3 要求 ID deterministic 且可 join。壞 key 會讓
（a）query 端無法用乾淨字串命中節點；
（b）`HAS_SKILL` 的 `target_id` 與字典若在不同時間產生就對不起來，Step 7 的 dangling edge 檢查會 fail。

**修法（`47e543d`）**：新增 `sanitize_registry_key()`，把 `(` 換成 `_`、刪除 `)`，**而不是在 `(` 處截斷**。這個選擇很關鍵 —— 截斷會讓共用前綴的不同技能靜默碰撞：`EDA(Exploratory Data Analysis)` 與 `EDA(Electronic Design Automation)` 都會塌成 `eda`，變成一次無聲的誤併，正好違反 §Step 3 的 precision-first。以 `_` 相接可保持每個 key 決定性且不碰撞；真要合併仍須走 alias dictionary 與雙人複核。

**碰撞驗證（實測）**：對兩份字典全量套用新規則後檢查，結果支持上述設計 ——

| 檢查 | skill | credential |
|------|-------|-----------|
| 不同舊 key 映射到同一新 key（靜默誤併） | **0** | **0** |
| 新 key 撞到既有的乾淨 key | **0** | **0** |
| 新 key 反被 `is_clean_registry_key` 拒收 | **0** | **0** |
| 未改變的 key 因新規則被拒收 | **0** | **0** |

前述兩個 EDA 確實維持相異（`eda_exploratory_data_analysis` vs `eda_electronic_design_automation`）。

**最關鍵的一處：alias 重新綁定沒有斷。** 這 40 個變動的技能 key 中，有 **20 個被 `alias_dictionary.csv` 的 canonical_id 指向**，而且正好是查詢價值最高的那一批：`k8s` / `kubernetes` / `kubernete` / `k8s_cluster` → `kubernetes(k8s`；`react` / `reactjs` / `react.js` / `react_js` → `react(reactjs`；`node` / `nodejs` / `node.js` / `node_js` → `node.js(node/nodejs`；還有 `vue` / `vuejs` / `vue.js`、`aws` / `amazon_web_services`、`azure`、`mssql` / `sql_server`。這些正是 playbook §Step 8 指定的 smoke query。

實測結果：新舊都綁定 **37 筆，零損失、零新增**。這完全依賴 commit 裡「alias seed 比對前先 sanitize target」那一行 —— 因為 `fixtures/skill_alias_seed_v0.1.csv` 仍存舊格式 target（如 `kubernetes(k8s`）。**反事實檢查：若當初漏掉這一行，綁定會從 37 掉到 17**，也就是上述 20 筆全部靜默解綁，`node.js` / `react` / `k8s` 這些查詢會直接失去技能入口，而且不會有任何錯誤訊息。這是一次真正的近失事故，也說明為什麼 alias fixture 應該存**穩定 ID 或原始顯示名**，而不是存正規化後的中間產物。

**`DOT_PREFIX_ALLOWLIST` 對本資料集無作用（誠實說明）**：字典裡沒有任何 key 以 `.` 開頭（skill 0 筆、credential 0 筆），`.net` 只以非開頭形式出現（`asp.net`、`asp.net_mvc`、`c#.net`、`c++.net`、`vb.net(...)`）。這與 `canonicalization_manifest.json` 的 `seed_rejected: {}`、`quarantined_skills: 0` 一致 —— 舊版一筆都沒因 `.` 開頭被拒收。所以這個 allowlist 是**防禦性程式碼**，符合 playbook 對 `.NET` 標點具語意的要求，但在目前資料上不改變任何輸出。不應把它算成本次修正的成果。

同一 commit 另外收緊了三處：`parse_canonical_candidate` 對前綴後的 key 也做 sanitize；`is_clean_registry_key` 明確拒收任何仍含 `(` / `)` 的 key；開頭為 `.` 預設拒收（offset/parse 垃圾），但 `DOT_PREFIX_ALLOWLIST` 保留 `.net` / `.net_core`，因為 playbook 要求 `.NET` 的標點具語意。alias seed 比對前先 sanitize target，因為 `fixtures/skill_alias_seed_v0.1.csv` 仍列舊格式（如 `kubernetes(k8s`），否則會全部匹配不到。

**治理註記（待補）**：這個修正改變了 `skill:<registry_key>` 的產生規則。依 playbook §2.2 與 §3.3，ID 規則屬 Schema v0.1 凍結範圍，變更需**雙人同意並升版**。目前 commit message 有完整理由與驗證紀錄，但沒有記錄雙人 sign-off，也沒有升 `dictionary_version`（仍為 `v0.1`，理由見 A1.4）。建議補一筆 sign-off 紀錄，否則評審若追問「凍結後的 ID 規則為何被改」會缺根據。

**現況與風險**：修正只改變 Step 3 **未來**的產出。**本機 `graph/skill_dictionary.csv` 仍是修正前的版本（40 個壞 key 還在）**，因為 `graph/` 不進 git。任何人重跑 Step 3 之後，必須連帶重跑 Step 4→8，否則 `nodes.csv` 與 `edges.csv` 會出現 dangling reference。這條也是 A4.5 的核心。

**這件事對交付敘事的意義**：我們在 A2.2–A2.3 花了很多篇幅談 LLM 的失敗模式，但這次品質損害最大、最難察覺的一個 bug 出在**規則式字串處理**，不是模型。防護的結論一致：無論訊號來自模型還是規則，都要有 deterministic 的 key 驗證層（`is_clean_registry_key`）與可稽核的產出，不能靠「看起來對」。

---

## A3. 資料組成揭露

### A3.1 Skill 節點只來自兩個結構化欄位

`graph/skill_dictionary.csv` 的 1,437 個 Skill 節點，`source` 欄只有兩個值：

| 來源欄位 | 節點數 | `skill_kind` 分布 |
|----------|--------|-------------------|
| `工作技能` | 1,032 | technical 1,025、tool 6、soft 1 |
| `電腦技能資料` | 405 | tool 382、technical 23 |

非結構化欄位（`職務名稱` / `職務內容` / `附加條件`）在 v0.1 中**不產生新的 Skill 節點**：2B phrase 抽取用的是由這兩個結構化欄位統計出的 lexicon，只新增 mention、不新增字典條目。`extraction_phrase_manifest.json` 已載明此限制（「lexicon 來自結構化欄位統計，非結構化文字中的 OOV 技能不在涵蓋範圍」）。

### A3.2 語料覆蓋率

| 項目 | 值 |
|------|-----|
| 職缺總數 | 1,218,635 |
| 任一結構化能力欄非空 | 382,758（31.41%） |
| `電腦技能資料` 非空 | 257,193（21.11%） |
| `工作技能` 非空 | 151,263（12.41%） |
| 2A 結構化抽取：有技能 mention 的職缺 | 320,685（26.32%），共 1,539,648 skill mentions |
| 2A：有證照 mention 的職缺 | 111,907（9.18%），共 206,647 credential mentions |
| 2B phrase 抽取：有命中的職缺 | 143,658（11.79%），共 211,128 skill + 77,071 credential mentions |

Skill 節點來自語料的少數部分。這限制了圖的技能覆蓋，也解釋了為什麼詞彙表只有 1,437 個值。

### A3.3 `工作技能` 1,032 個節點的命名組成

規則寫死於 `step_a7_data_composition.py`（`data_composition_rule_v0.1`），依序判定、先命中者為準；純字面規則、不呼叫模型。DF 取自 A5 全量統計。

該欄位 job-level 歸屬總量 DF = 722,610。

| 分組 | 判定規則 | 節點數 | 節點占比 | DF 占比 | 例 |
|------|----------|--------|----------|---------|-----|
| `ability_marked` | 名稱含「能力／技巧／知識／技能」 | 145 | **14.1%** | 16.9% | ERP管理與維護能力(612)、PCB Layout軟體操作能力(237) |
| `duty_action_phrase` | 無能力標記且以動詞開頭 | 141 | 13.7% | 12.2% | 使用適當的處理技術準備食物(4,574)、使用合適的工具達成份量控制(833) |
| `named_tool_or_standard` | 無能力標記、非動詞開頭，含拉丁字母或數字 | 27 | 2.6% | 0.9% | CNC加工機與相關機械操作(1,204)、CAD技術應用(758) |
| `other_noun_phrase` | 以上皆非 | 719 | **69.7%** | **70.0%** | 不動產經紀業務(319)、一般獸醫臨床及住院照料(59) |

**核心揭露：1,032 個節點中只有 145 個（14.1%）在名稱上標示自己是一種「能力」，這 145 個只涵蓋該欄位 16.9% 的 job-level 歸屬量。其餘 887 個（85.9%，DF 83.1%）名稱裡沒有任何能力字樣。**

最大的一群不是動詞開頭的職責敘述，而是**領域工作內容名詞片語**（69.7%、DF 70.0%），例如「不動產經紀業務」、「一般保險之公證」。這類名稱既不是具名工具，也不宣稱是一種能力，而是在描述「這份工作在做什麼」。

### A3.4 `skill_kind` 的資訊增益有限（誠實揭露）

把 A3.1 的分布對照可以看出：`skill_kind` 幾乎在重述 `source` 欄位。

- `電腦技能資料` → 405 筆中 382 筆（94.3%）判為 `tool`
- `工作技能` → 1,032 筆中 1,025 筆（99.3%）判為 `technical`

換言之，`tool` / `technical` 的切分主要由來源欄位決定，LLM 在 `工作技能` 這 1,032 筆**內部**幾乎沒有做出區分（只挑出 6 個 tool、1 個 soft）。**分類的實質資訊增益集中在那唯一一個 soft 封鎖決定上，而不是一個豐富的重新分割。** 這與 A1.5 的預期一致，也是為什麼我們不把 `skill_kind` 宣稱為主要創新。

### A3.5 兩組看似矛盾的分布數字（先解釋，免得評審誤判）

| 出處 | 分布 | 分母 |
|------|------|------|
| `skill_classification_manifest.json` 的 `kind_distribution` | tool 388、technical **1,030**、soft 1 | **1,419**（僅取得模型回覆者） |
| `skill_dictionary.csv` / `skill_classification_audit.csv` 實際內容 | tool 388、technical **1,048**、soft 1 | **1,437**（全字典） |

兩者相差的 18 筆就是 A2.5 的未回覆項目，它們保留 step3 預設 `technical`。**兩個數字都正確，只是分母不同。** 進入 `nodes.csv` 的是後者（1,048 / 388 / 1）。

### A3.6 `SKILL_GRAPH_NEXT_PLAN.md` r2 的一個數字無法重現

r2 §2 寫「37.4%（DF 佔 52.1%）是純動作職責描述」。這一組數字**沒有留下產生它的腳本，也無法從現有 artifact 重現**。最接近的節點占比是「長度 ≥ 10 字且無能力標記」= 37.6%，但該群只涵蓋 27.9% 的 DF，與 52.1% 差距過大。

處置建議：以 A3.3 的可重現分組取代該句，或補上原始腳本。這不是無關緊要的細節 —— 它是一個**揭露性數字**，評審有權要求重跑。`14.1%` 這個數字則可重現（`能力|技巧|知識|技能` 規則，n=145）。

### A3.7 這對圖的解讀意味什麼

1. **`HAS_SKILL` 指向 `工作技能` 衍生節點時，不代表「求職者須具備一項具名能力」**，更常是「這份 JD 列出了這項工作內容」。下游排序與解釋文案不應把它一律呈現為「技能要求」。
2. **這是來源欄位本身的分類法造成的，不是抽取錯誤。** 雇主在同一個欄位裡混填具名工具、可遷移能力與職務工作內容。我們用 `skill_kind` 誠實標記，不新增節點型別。
3. **`Task` 節點列為 schema v0.2 候選，本階段不實作。** 需雙人同意升版並全鏈重跑（2B → Step 3 → Step 4–8），成本高於本階段收益。
4. **泛用技能不刪除、只降權。** Excel（DF 220,977、salience 0.6108）、Word（211,979、0.6138）、PowerPoint（129,026、0.5223）、Outlook（71,727、0.3532）標記為 `retained_downweight`，保留 query 可解析性，由 DF/IDF 與職類內 salience 控制權重。前三者的 salience 高於 0.50 的封鎖上限，結構上不可能被任何分類結果封鎖。

---

## A4. 待補與交接

### A4.1 A 自己的待補

| # | 項目 | 阻擋什麼 |
|---|------|----------|
| 1 | 人工標註 `fixtures/skill_kind_eval_set_v0.1.csv`（100 筆，`expected_skill_kind` 全空），再跑 `--eval` | 分類準確率、LLM vs 規則法 precision/recall；也是唯一能區分 A2.3 末段「獨立複驗 vs 複製規則法」兩種解讀的方法 |
| 2 | A6 把 `rejected_items` 持久化到 manifest | 驗證 A2.5 的 key 對回失敗假設 |
| 3 | prompt v0.2 補條件語氣示例；challenge set 擴充 confusion 類 | Gate 1 blocking issue ④ 未解 |
| 4 | 修正 `SKILL_GRAPH_NEXT_PLAN.md` r2 的 37.4%/52.1%（見 A3.6） | 揭露數字可重現性 |
| 5 | 若要 Gate 2 freeze 抽取模型：重新分層 golden slice 或建人工 mention 標註，並提高 sonnet 的 `max_tokens` 重測 | `chosen` / `fallback` 目前為 `null` |
| 6 | 取得帳號 rate card 填入成本 | `avg_cost_usd_per_job` 為 `null` |
| 7 | **擴充 skill alias（目前僅 37 筆對 1,437 節點）與提高 `HAS_SKILL` 職缺覆蓋（目前 30.4%）** | 這是 ablation 指標不動的**主因**（A1.5 結論 3）；比再調分類更有效 |
| 8 | 補 `47e543d` 的 ID 規則變更雙人 sign-off 紀錄 | Schema v0.1 凍結範圍的變更缺核准依據（A2.7 治理註記） |

### A4.2 需要 A/B 雙人 sign-off

| 項目 | 說明 |
|------|------|
| `skill_kind` 四值詞彙表 | 會出現在 B 的 `nodes.csv`；`credential` 保留給證照降級路徑，本批次未使用 |
| G1 對照組定義 | **必須是空黑名單，不是 v0.2**（見 A1.5），否則 ablation 恆等於零 |
| `Task` 節點 | schema v0.2 候選，本階段不做 |

### A4.3 給 B 的提醒

- **`47e543d` 的 key 變更影響 296 個 registry ID，不只 commit message 舉例的技能**：skill 40 筆、**credential 256 筆（15.6%）**。若你的 `graph/` 是在該 commit 之前產生的，`edges.csv` 的 `REQUIRES_CREDENTIAL` target 會比 `HAS_SKILL` 受影響更廣。請用 `python step_a7b_key_change_impact.py` 對自己機器的字典確認，再決定要不要重跑。
- `step5` loader 預設取最新版 `fixtures/soft_skill_blacklist_v*.csv`，目前會拿到 `v0.3`（1 筆）。此改動在 `step5_statistical_edges.py`，**需要 B ack**。
- 重跑後請回報：CORE_SKILL 是否仍被泛用技能主導（抽樣）、Step7 是否有新 fail/warn、`nodes.csv` 的 `skill_kind` 分布是否為 technical 1,048 / tool 388 / soft 1。
- 只換 `skill_kind` 時只需重跑 Step 6（→ 8）；只換黑名單需 Step 5 → 6（→ 7 → 8）。

### A4.4 建圖完成狀態（`47e543d`，A 的本機）

`graph/` 不進 git，所以「pull 完」不等於「有圖」。A 本機的實際狀態：

| 階段 | 產物 | A 本機 |
|------|------|--------|
| Step 1 | `train_jobs.parquet`、`occupation_hierarchy.csv`、`step1_manifest.json` | 有 |
| Step 2A/2B | `extractions_structured.jsonl`、`extractions_phrase.jsonl` | 有 |
| Step 3 | `extractions.jsonl`、`skill_dictionary.csv`、`credential_dictionary.csv`、`alias_dictionary.csv`、`canonicalization_audit.csv` | 有，但為 `47e543d` **修正前**產出（含 40 個壞 key） |
| A5 / A6 | blacklist v0.2 / v0.3、`skill_classification_audit.csv` | 有 |
| Step 4 | `edges_core.csv`、`step4_manifest.json` | **無** |
| Step 5 | `co_occurs_edges.csv`、`core_skill_edges.csv`、`global_job_frequency.json`、`edges_final.csv` | **無** |
| Step 6 | `nodes.csv`、`edges.csv`、`mentions.jsonl`、`graph_manifest.json` | **無** |
| Step 7 | `graph_quality_report.json` | **無** |
| Step 8 / 9 | `retrieval_smoke_report.json`、`eval_report_*.json`、`ablation_comparison.json` | **無** |

**結論：圖在 B 的機器上已建完並量測（Step 7 為 13 PASS / 0 FAIL / 2 WARN），在 A 的機器上只做到 Step 3 + 分類。** A 若要獨立重現全圖，順序是：

```text
step3（因 47e543d 改了 registry_key，必須重跑）
  → step_a6（字典換了，分類要重跑；會再花一次 LLM 呼叫）
  → step4 → step5 → step6 → step7 → step8 / step9
```

重跑 Step 3 之後**不可只跑一半**：舊 `edges_*` 與新字典的 key 不一致會產生 dangling edge，Step 7 會 fail。若只是要複核 B 的數字，較省的做法是向 B 取 `nodes.csv` / `edges.csv` / `graph_manifest.json` 與 artifact hash，而不是本地重跑全鏈。

### A4.5 A 段落一頁總結

| 問題 | 答案 |
|------|------|
| LLM 在圖裡做什麼？ | 技能分類 `skill_kind`（1,437 節點、36 次呼叫），並經 dual-signal 接到 soft-skill blacklist → step5 統計邊 → step8 |
| 為什麼不做全量抽取？ | 最快實測吞吐 365.7 jobs/h，121.8 萬筆需 ~139 天；且 Gate 1 有 4 個未解 blocking issue，`use_llm_extraction=false` |
| 分類會不會只是裝飾？ | 已接到 repo 內唯一消費點，但**實測 G1 = G2 逐位數相同**，對指標貢獻為 0.0000。不宣稱增益 |
| 圖本身有用嗎？ | re-ranking +0.0039 NDCG@10（0.4308→0.4347）；full retrieval −0.0002（0.0127→0.0125）。都不顯著 |
| 指標為什麼不動？ | 不是分類的問題：只有 0.1% query 解析到 skill 錨點、`HAS_SKILL` 僅覆蓋 30.4% 職缺、skill alias 只有 37 筆。**A 端覆蓋太薄才是主因** |
| 圖建完了嗎？ | B 機器上完成且 Step 7 全過（13 PASS / 0 FAIL / 2 WARN）；**A 機器上只到 Step 3 + 分類**，Step 4–9 未產出（`graph/` 不進 git，見 A4.4） |
| 最重要的失敗模式？ | ① prompt 層格式指令無效（strict JSON 仍 0/36），靠 deterministic 解析層；② 模型 offset 不可用；③ assertion 全模型輸給規則法（1.00 vs 0.80）；④ 誤判集中在 conditional，危險方向在 confusion |
| 8 筆分類誤判怎麼發生的？ | 來源欄位混合三種性質 + prompt taxonomy 有縫隙 + 詞面觸發；模型在誤判與正解上都是 0.92–0.99 高信心 |
| dual-signal 攔住了嗎？ | 只攔 2/11；8 筆誤判全部通過。高頻跨行業工作內容與泛用特質在統計上不可區分。**實際攔下的是排序稽核清單 + 人工複核** |
| 最重要的資料揭露？ | `工作技能` 衍生的 1,032 個節點只有 14.1%（145 個，DF 16.9%）在名稱上標示為「能力」；最大一群是領域工作內容名詞片語（69.7%、DF 70.0%） |
| 現在能宣稱什麼？ | 分類覆蓋 98.7%、與規則法零分歧、擋下 8 筆誤判封鎖（25,550 個 job-level 歸屬） |
| 現在**不能**宣稱什麼？ | 分類準確率（無人工標籤）、mention precision/recall、每筆成本、LLM 分類帶來 NDCG 增益、dual-signal 是自動安全閘門 |
