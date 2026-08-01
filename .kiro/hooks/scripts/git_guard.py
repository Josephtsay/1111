#!/usr/bin/env python3
"""
PreToolUse guard for execute_bash — blocks unsafe `git add` / `git commit`.

Why a command hook instead of an agent prompt:
  The previous version used an `agent` action with matcher "execute_bash".
  PreToolUse matchers are tested against the TOOL NAME only, so it fired on
  every single bash call (cat, ls, grep, python ...) and asked the model to
  self-police. That burned a round trip per command and enforced nothing.

  This script reads the tool input on stdin, exits 0 silently when the command
  has nothing to do with git staging, and only speaks up for real violations.

Command-position parsing (important):
  A naive `re.search("git add", cmd)` also fires on commands that merely
  MENTION the text — `echo "git add ."`, `grep "git commit" file`, or a test
  harness passing JSON payloads. So we split the command on shell separators
  and only inspect segments that actually START with `git`. Quoted occurrences
  therefore no longer trigger a block.

Exit codes:
  0 = allow (silent)
  2 = block; stderr is shown to the agent

Fails open: any unexpected error exits 0, so a bug here can never wedge work.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

MAX_BYTES = 100 * 1024

# Paths that must never enter git. Kept in sync with .gitignore.
FORBIDDEN = (
    "graph/", "dataset/", "datasets/", "data/raw/", "outputs/", "artifacts/",
    ".env", "credentials.json",
)
FORBIDDEN_SUFFIX = (
    ".parquet", ".pdf", ".pkl", ".joblib", ".sqlite", ".db", ".lgb",
    ".booster", ".pem", ".key", ".log",
)

# Split on shell command separators so we can tell a real invocation from a
# quoted mention. Deliberately simple: over-splitting is safe here.
SEPARATORS = re.compile(r"(?:\|\||&&|[;\n|&])")


def command_segments(cmd: str) -> list[str]:
    return [seg.strip() for seg in SEPARATORS.split(cmd) if seg.strip()]


def git_subcommand(segment: str) -> str | None:
    """Return 'add'/'commit' if this segment really invokes it, else None."""
    try:
        tokens = shlex.split(segment)
    except ValueError:
        tokens = segment.split()
    if not tokens:
        return None
    # allow env prefixes and simple wrappers
    i = 0
    while i < len(tokens) and ("=" in tokens[i] and not tokens[i].startswith("-")):
        i += 1
    if i >= len(tokens) or Path(tokens[i]).name != "git":
        return None
    for tok in tokens[i + 1:]:
        if tok.startswith("-"):
            continue
        return tok if tok in ("add", "commit") else None
    return None


def has_short_flag(flags: list[str], letter: str) -> bool:
    """`-a` must also be found inside combined short flags like `-am`."""
    for f in flags:
        if f.startswith("--"):
            continue
        if letter in f[1:].split("=")[0]:
            return True
    return False


def is_forbidden(path: str) -> str | None:
    # NOTE: use removeprefix, NOT lstrip("./") — lstrip strips by CHARACTER SET,
    # so ".env".lstrip("./") == "env" and the dotfile silently passed the check.
    # Same failure mode as the str.strip("()") bug fixed in step3 (see 47e543d).
    p = path.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    for bad in FORBIDDEN:
        if p == bad.rstrip("/") or p.startswith(bad):
            return f"路徑在禁止清單：{bad}"
    for suf in FORBIDDEN_SUFFIX:
        if p.endswith(suf):
            return f"副檔名在禁止清單：{suf}"
    return None


def git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=10
        ).stdout
    except Exception:
        return ""


def main() -> int:
    raw = sys.stdin.read()
    if not raw.strip():
        return 0
    payload = json.loads(raw)
    cmd = (payload.get("tool_input") or {}).get("command") or ""

    relevant = [(seg, sub) for seg in command_segments(cmd)
                if (sub := git_subcommand(seg))]
    if not relevant:
        return 0  # nothing to do with staging -> stay silent

    problems: list[str] = []
    touches_index = False

    for segment, sub in relevant:
        try:
            tokens = shlex.split(segment)
        except ValueError:
            tokens = segment.split()
        flags = [t for t in tokens if t.startswith("-")]
        operands = [t for t in tokens[tokens.index(sub) + 1:] if not t.startswith("-")]

        if sub == "add":
            touches_index = True
            if has_short_flag(flags, "A") or "--all" in flags or "." in operands:
                problems.append(
                    "使用了 `git add .` 或 `git add -A`。"
                    "請改用明確檔名，避免掃進不相關的變更。"
                )
            if has_short_flag(flags, "f") or "--force" in flags:
                problems.append("使用了 `git add -f`，會強制加入被 .gitignore 排除的檔案。")
            for op in operands:
                reason = is_forbidden(op)
                if reason:
                    problems.append(f"`{op}` 不該進 git（{reason}）。")
        else:  # commit
            touches_index = True
            if has_short_flag(flags, "a") or "--all" in flags:
                problems.append(
                    "使用了 `git commit -a`，會自動 stage 所有已追蹤的修改。"
                    "請先明確 `git add <file>`。"
                )

    # Inspect what is already staged (covers a bare `git commit`)
    if touches_index:
        for name in filter(None, git("diff", "--cached", "--name-only").splitlines()):
            reason = is_forbidden(name)
            if reason:
                problems.append(f"已 stage 的 `{name}` 不該進 git（{reason}）。")
                continue
            f = Path(name)
            if f.is_file() and f.stat().st_size > MAX_BYTES:
                problems.append(
                    f"已 stage 的 `{name}` 為 {f.stat().st_size // 1024}KB，超過 100KB 上限。"
                )

    if not problems:
        return 0

    print("git 安全檢查未通過：", file=sys.stderr)
    for p in dict.fromkeys(problems):
        print(f"  - {p}", file=sys.stderr)
    print(
        "\n只 commit 腳本(*.py)、config(*.yaml/*.json <100KB)、文件(*.md)。"
        "圖產物與資料集請留在本機。",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)  # fail open
