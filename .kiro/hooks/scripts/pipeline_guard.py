#!/usr/bin/env python3
"""
PreToolUse guard for execute_bash — full-corpus pipeline runs.

Replaces the old `post-step-verify` hook, which never fired: it used
trigger=PostToolUse with a matcher of "step.*\\.py", but PostToolUse matchers
are tested against the TOOL NAME (execute_bash), never the command text or a
filename. It had been dead since the day it was created.

Two things this catches, both learned the hard way:

  1. Full-corpus steps (1.22M jobs, 279MB parquet, 1.1GB extractions) exceed the
     120s default bash timeout. They must run in the background, otherwise the
     call returns a truncated log and the run state becomes unclear.

  2. `exit 0` is not evidence of success. Step 7 is the authority on graph
     validity, and a re-run of Step 3 invalidates every downstream artifact.

Emits guidance on stdout (forwarded for PreToolUse) only when a pipeline script
is actually being invoked. Silent otherwise. Never blocks.
"""

from __future__ import annotations

import json
import re
import sys

# Steps that read the full corpus. step_a7* are read-only reporting, excluded.
HEAVY = re.compile(
    r"\b(step1_train_data_prep|step2a_structured_extraction|step2b_phrase_extraction"
    r"|step3_canonicalization|step4_edge_assembler|step5_statistical_edges"
    r"|step6_graph_export|step8_retrieval_smoke|step9_ablation)\.py"
)
STEP3 = re.compile(r"\bstep3_canonicalization\.py")
LIVE_LLM = re.compile(r"--mode\s+live")


def main() -> int:
    raw = sys.stdin.read()
    if not raw.strip():
        return 0
    payload = json.loads(raw)
    tool_input = payload.get("tool_input") or {}
    cmd = tool_input.get("command") or ""
    background = bool(tool_input.get("run_in_background"))

    if not HEAVY.search(cmd):
        return 0

    notes: list[str] = []

    if not background:
        notes.append(
            "這是全量步驟（1,218,635 筆職缺）。預設 bash timeout 120s 不夠，"
            "請用 run_in_background=true 或 control_bash_process，"
            "再用 get_process_output 追進度。"
        )

    if STEP3.search(cmd):
        notes.append(
            "重跑 Step 3 會重新產生 registry_key（47e543d 之後有 296 個 ID 會變："
            "skill 40 + credential 256）。必須連帶重跑 step_a6 與 Step 4→8，"
            "否則舊 edges 指向已不存在的 key，Step 7 dangling edge 檢查會 FAIL。"
            "可先跑 `python step_a7b_key_change_impact.py` 確認影響範圍。"
        )

    if LIVE_LLM.search(cmd):
        notes.append(
            "--mode live 會真的呼叫 Bedrock 並產生費用。"
            "確認這不是可以先用 --mode dry-run 或 --limit 驗證的情況。"
        )

    notes.append(
        "完成後不要只看 exit code。要驗：manifest 的 row count 與 artifact hash 是否更新、"
        "有無重複 node/edge ID、有無 dangling edge、test 期行為資料未流入圖，"
        "並以 step7_quality_gate.py 的結果為準（無 FAIL 才算過）。"
    )

    print("[pipeline-guard]")
    for n in notes:
        print(f"  - {n}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)  # fail open
