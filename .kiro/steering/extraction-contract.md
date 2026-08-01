---
inclusion: fileMatch
fileMatchPattern: "*extract*"
---

# 抽取結果契約（A → B 介面）

當處理 extraction 相關檔案時，自動載入以下規範。

## JSONL 格式（每筆 JD 一行）

```json
{
  "job_id": "3945298",
  "skills": [
    {
      "mention_id": "mention:<job_id>:<source_field>:<start>:<end>:<version>",
      "raw_mention": "護理",
      "canonical_candidate": "skill:nursing_care",
      "source_field": "職務內容",
      "start_offset": 15,
      "end_offset": 17,
      "requirement_level": "required|preferred|unspecified",
      "assertion_status": "affirmed|negated|uncertain",
      "confidence": 0.9,
      "evidence": "護理人員工作",
      "method": "structured|phrase|llm",
      "extractor_version": "v0.1"
    }
  ],
  "credentials": [...],
  "extraction_version": "v0.1"
}
```

## 欄位規則

- `mention_id`: deterministic（job_id + source_field + offsets + version）
- `requirement_level`: 僅允許 `required` / `preferred` / `unspecified`
- `assertion_status`: 僅允許 `affirmed` / `negated` / `uncertain`
- `confidence`: `[0, 1]`，代表抽取可信度，不代表相關性
- `evidence`: **必填**，必須能在 source_field 原文找到
- `source_field`: `電腦技能資料` / `工作技能` / `專業證照` / `職務內容` / `附加條件` / `職務名稱`
- 同一 (job, skill) 多筆 mention **不可在抽取階段刪除**

## Edge 物化規則（B 執行）

- 只聚合 `assertion_status == affirmed` 且 `canonicalization_status == accepted`
- `HAS_SKILL.confidence = max(accepted mentions' confidence)`
- `HAS_SKILL.evidence_count = count(accepted mentions)`
- `requirement_level` 聚合：`required > preferred > unspecified`
