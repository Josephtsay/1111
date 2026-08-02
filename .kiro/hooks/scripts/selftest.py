#!/usr/bin/env python3
"""
Self-test for the .kiro hook guards. Run after editing any guard script:

    python3 .kiro/hooks/scripts/selftest.py

Feeds synthetic PreToolUse payloads to each guard and asserts the exit code and
whether output was produced. Payloads are built in-process, so this file can
contain git-like strings without a guard tripping on the test command itself.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
GIT = HERE / "git_guard.py"
SCHEMA = HERE / "schema_guard.py"
PIPELINE = HERE / "pipeline_guard.py"

ALLOW, BLOCK = 0, 2


def run(script: Path, payload: dict) -> tuple[int, str, str]:
    p = subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
    )
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def bash(command: str, background: bool = False) -> dict:
    return {"tool_input": {"command": command, "run_in_background": background}}


def write(path: str) -> dict:
    return {"tool_input": {"path": path}}


CASES: list[tuple[str, Path, dict, int, bool]] = [
    # label, script, payload, expected exit, expect any output
    ("ls is irrelevant",            GIT, bash("ls -la"), ALLOW, False),
    ("git log is read-only",        GIT, bash("g" + "it log --oneline"), ALLOW, False),
    ("status is read-only",         GIT, bash("g" + "it status --short"), ALLOW, False),
    ("explicit md file is fine",    GIT, bash("g" + "it add docs/a.md"), ALLOW, False),
    ("blanket dot add",             GIT, bash("g" + "it add ."), BLOCK, True),
    ("blanket -A add",              GIT, bash("g" + "it add -A"), BLOCK, True),
    ("graph artifact",              GIT, bash("g" + "it add graph/nodes.csv"), BLOCK, True),
    ("dotenv",                      GIT, bash("g" + "it add .env"), BLOCK, True),
    ("forced ignored path",         GIT, bash("g" + "it add -f dataset/x.csv"), BLOCK, True),
    ("parquet suffix",              GIT, bash("g" + "it add graph/train.parquet"), BLOCK, True),
    ("commit -a",                   GIT, bash("g" + "it commit -am x"), BLOCK, True),
    ("chained after &&",            GIT, bash("ls && g" + "it add ."), BLOCK, True),
    # the false positive that broke the first version: quoted mention only
    ("quoted mention not a run",    GIT, bash('echo "g' + 'it add ."'), ALLOW, False),
    ("grep for the phrase",         GIT, bash('grep -r "g' + 'it commit" docs/'), ALLOW, False),

    ("doc write is free",           SCHEMA, write("docs/SKILL_GRAPH_STEP9_SECTION_A.md"), ALLOW, False),
    ("analysis script is free",     SCHEMA, write("pipeline/step_a7_data_composition.py"), ALLOW, False),
    ("step3 needs confirm",         SCHEMA, write("pipeline/step3_canonicalization.py"), ALLOW, True),
    ("model_registry needs confirm", SCHEMA, write("configs/model_registry.yaml"), ALLOW, True),
    ("step_a6 needs confirm",       SCHEMA, write("pipeline/step_a6_skill_classification.py"), ALLOW, True),

    ("light script is free",        PIPELINE, bash("python pipeline/step_a7b_key_change_impact.py"), ALLOW, False),
    ("heavy in foreground warns",   PIPELINE, bash("python pipeline/step4_edge_assembler.py"), ALLOW, True),
    ("heavy in background ok",      PIPELINE, bash("python pipeline/step4_edge_assembler.py", True), ALLOW, True),
    ("step3 rerun warns",           PIPELINE, bash("python pipeline/step3_canonicalization.py", True), ALLOW, True),
]


def main() -> int:
    failures = 0
    for label, script, payload, want_code, want_output in CASES:
        code, out, err = run(script, payload)
        produced = bool(out or err)
        ok = (code == want_code) and (produced == want_output)
        if not ok:
            failures += 1
        status = "ok  " if ok else "FAIL"
        print(f"  [{status}] {script.name:<18} {label:<30} exit={code} output={produced}")
        if not ok:
            print(f"           expected exit={want_code} output={want_output}")
            if err:
                print(f"           stderr: {err.splitlines()[0][:100]}")

    # schema guard must emit a valid "ask" decision
    code, out, _ = run(SCHEMA, write("pipeline/step3_canonicalization.py"))
    try:
        decision = json.loads(out)["hookSpecificOutput"]["permissionDecision"]
        if decision != "ask":
            raise ValueError(decision)
        print("  [ok  ] schema_guard.py     emits permissionDecision=ask")
    except Exception as exc:
        failures += 1
        print(f"  [FAIL] schema_guard.py     bad decision payload: {exc}")

    print()
    print(f"  {len(CASES) + 1} checks, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
