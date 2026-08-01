#!/usr/bin/env python3
"""
PreToolUse guard for fs_write / str_replace — protects the Schema v0.1 freeze.

Why a command hook instead of an agent prompt:
  The previous version used an `agent` action with matcher "fs_write|str_replace".
  PreToolUse matchers only test the TOOL NAME, so it fired on EVERY write —
  including markdown docs and read-only analysis scripts — and asked the model
  to reason about schema impact each time. Pure overhead for most writes.

  This script scopes the guard by FILE PATH: only the files that actually
  define node/edge types, ID rules, or quality policy trigger a confirmation.
  Everything else passes silently.

Behaviour:
  - path not on the frozen surface  -> exit 0, silent
  - path on the frozen surface      -> exit 0 + "ask" decision, user confirms

"ask" rather than exit 2 (hard block), because editing these files is
legitimate and frequent; the point is that a human sees it, not that it is
forbidden.

Fails open: any unexpected error exits 0.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Files whose contents define the frozen contract. Explicit list, not a glob,
# so new analysis scripts (step_a7*.py etc.) do not trip the guard.
FROZEN_SURFACE = {
    "step3_canonicalization.py": "registry_key / canonical ID 規則、alias 綁定、protected pairs",
    "step4_edge_assembler.py": "HAS_SKILL / IN_OCCUPATION / REQUIRES_CREDENTIAL 物化規則",
    "step5_statistical_edges.py": "CO_OCCURS_WITH / CORE_SKILL / statistical_eligible 政策",
    "step6_graph_export.py": "nodes.csv / edges.csv 欄位契約",
    "step7_quality_gate.py": "品質閘門門檻",
    "step2a_structured_extraction.py": "§3.4 extraction JSONL 契約",
    "step2b_phrase_extraction.py": "§3.4 extraction JSONL 契約",
    "step2c_assertion_challenge.py": "assertion_status 判定",
    "assertion_detection.py": "assertion_status 判定",
    "canonicalization.py": "canonical ID 規則",
    "step_a6_skill_classification.py": "skill_kind 詞彙表、blacklist 政策、dual-signal 門檻",
    "extraction_config.yaml": "method thresholds、assertion 門檻",
    "model_registry.yaml": "feature flags、chosen/fallback model（Gate 2）",
}

CHECKLIST = (
    "這個檔案屬於 Schema v0.1 凍結範圍（{reason}）。\n"
    "請確認這次修改是否會："
    "\n  - 新增或移除 node type（Job/Skill/Occupation/Credential）或 edge type"
    "\n  - 改變 deterministic ID 規則（job:<id> / skill:<key> / occ:<CodeNo> / credential:<key>）"
    "\n  - 改動 confidence 政策、assertion_status 處理、requirement_level 列舉值"
    "\n  - 改動 §3.4 extraction JSONL 契約"
    "\n  - 改動 statistical_eligible 政策"
    "\n若有任何一項成立，需 A/B 雙人同意並升版後才可進行。"
)


def main() -> int:
    raw = sys.stdin.read()
    if not raw.strip():
        return 0
    payload = json.loads(raw)
    tool_input = payload.get("tool_input") or {}
    path = tool_input.get("path") or tool_input.get("targetFile") or ""
    if not path:
        return 0

    reason = FROZEN_SURFACE.get(Path(path).name)
    if not reason:
        return 0  # ordinary file - say nothing

    print(json.dumps({
        "hookSpecificOutput": {
            "permissionDecision": "ask",
            "permissionDecisionReason": CHECKLIST.format(reason=reason),
        }
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)  # fail open
