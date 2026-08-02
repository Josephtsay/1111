"""
A7b — 量化 47e543d 的 registry_key 正規化變更對 Step 3 產出的影響
Role: A (Content Graph)

為什麼需要這支腳本：
  47e543d 修了 normalize_key() 的括號處理（str.strip("()") 只清字串兩端，
  導致 'Kubernetes(K8S)' → 'kubernetes(k8s' 這種壞 ID 進了字典）。
  commit message 只舉了 skill 的例子，也只說「既有 graph/ 已是修正後的 key，
  故無需重跑」—— 但那是就 commit 作者的機器而言。任何機器上的 graph/ 都是
  本地產物（.gitignore 忽略 graph/），所以必須能隨時回答三個問題：

    1. 到底有多少 registry ID 會改變？（skill 與 credential 都要算）
    2. 新規則會不會讓兩個原本不同的 key 靜默碰撞成一個？
    3. alias 綁定會不會斷？—— 這一項最危險，因為斷了不會報錯，
       只會讓 node.js / react / k8s 這類查詢默默失去技能入口。

本腳本唯讀：不寫任何 artifact，不改 schema / registry / blacklist。
它比較「現有字典的 key」與「對同一批 key 套用新規則後的結果」。

用法：
  python step_a7b_key_change_impact.py            # 摘要
  python step_a7b_key_change_impact.py --verbose  # 列出每一個變動的 key
"""

from __future__ import annotations

import argparse
import collections
import csv
import sys
from pathlib import Path

import step3_canonicalization as s3

ROOT = Path(__file__).resolve().parent.parent  # pipeline/ -> repo root; graph/ dataset/ fixtures/ 都掛在根目錄
GRAPH_DIR = ROOT / "graph"
FIXTURES_DIR = ROOT / "fixtures"

SKILL_DICT = GRAPH_DIR / "skill_dictionary.csv"
CREDENTIAL_DICT = GRAPH_DIR / "credential_dictionary.csv"
ALIAS_DICT = GRAPH_DIR / "alias_dictionary.csv"
CLASSIFICATION_AUDIT = GRAPH_DIR / "skill_classification_audit.csv"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return [dict(r) for r in csv.DictReader(f)]


def analyse_dictionary(label: str, rows: list[dict[str, str]], *, verbose: bool) -> dict[str, str]:
    """Return {old_key: new_key} for keys whose value changes under the new rule."""
    changed: dict[str, str] = {}
    grouped: dict[str, list[str]] = collections.defaultdict(list)
    for r in rows:
        old = r["registry_key"]
        new = s3.sanitize_registry_key(old)
        grouped[new].append(old)
        if new != old:
            changed[old] = new

    untouched = {r["registry_key"] for r in rows
                 if s3.sanitize_registry_key(r["registry_key"]) == r["registry_key"]}
    collisions = {k: sorted(set(v)) for k, v in grouped.items() if len(set(v)) > 1}
    clash_existing = sorted(n for o, n in changed.items() if n in untouched)
    rejected_new = sorted(n for n in changed.values() if not s3.is_clean_registry_key(n))
    rejected_unchanged = sorted(k for k in untouched if not s3.is_clean_registry_key(k))

    pct = 100 * len(changed) / len(rows) if rows else 0.0
    print(f"  {label:<24} total={len(rows):<6} changed={len(changed):<5} ({pct:.1f}%)")
    print(f"      collisions (distinct old -> same new) : {len(collisions)}")
    print(f"      new key clashing with existing key    : {len(clash_existing)}")
    print(f"      new key rejected by is_clean_key      : {len(rejected_new)}")
    print(f"      unchanged key rejected by new rules   : {len(rejected_unchanged)}")
    for k, v in collisions.items():
        print(f"      !! COLLISION {k} <- {v}")
    for n in clash_existing:
        print(f"      !! CLASH {n}")
    for n in rejected_new:
        print(f"      !! REJECTED-NEW {n}")
    for k in rejected_unchanged:
        print(f"      !! REJECTED-UNCHANGED {k}")

    if verbose and changed:
        print(f"      --- all {len(changed)} changed keys ---")
        for o, n in sorted(changed.items()):
            print(f"        {o}\n          -> {n}")
    return changed


def check_alias_rebinding(skill_rows: list[dict[str, str]]) -> None:
    """
    The load-bearing check: after keys change, does the alias seed still bind?

    fixtures/skill_alias_seed_v0.1.csv stores targets in the OLD (pre-fix) form,
    so 47e543d sanitizes targets before matching. If that line were missing the
    bindings would silently drop and the highest-value smoke queries
    (node.js / react / k8s) would lose their skill entry point with no error.
    """
    old_keys = {r["registry_key"] for r in skill_rows}
    new_keys = {s3.sanitize_registry_key(k) for k in old_keys}
    specs = s3.load_alias_seed_specs()

    bound_old = bound_new = bound_without_fix = 0
    lost: list[tuple[str, str]] = []
    for raw_alias, _language, targets in specs:
        alias_key = s3.normalize_key(raw_alias)
        t_old = next((t for t in targets if t in old_keys), None)
        t_new = next((t for t in (s3.sanitize_registry_key(x) for x in targets)
                      if t in new_keys), None)
        t_naive = next((t for t in targets if t in new_keys), None)  # fix omitted

        ok_old = bool(t_old and alias_key != t_old)
        ok_new = bool(t_new and alias_key != t_new)
        bound_old += ok_old
        bound_new += ok_new
        bound_without_fix += bool(t_naive and alias_key != t_naive)
        if ok_old and not ok_new:
            lost.append((raw_alias, t_old or ""))

    print(f"  alias seed specs in fixture : {len(specs)}")
    print(f"  bindings under OLD code     : {bound_old}")
    print(f"  bindings under NEW code     : {bound_new}")
    print(f"  LOST bindings (regression)  : {len(lost)}")
    for a, t in lost:
        print(f"      !! {a} (was -> {t})")
    print(f"  counterfactual: if target-sanitize were omitted -> {bound_without_fix} bindings")
    if bound_without_fix < bound_new:
        print(f"      => that one line prevents "
              f"{bound_new - bound_without_fix} silent alias unbindings")


def report_downstream(changed_skills: dict[str, str]) -> None:
    if ALIAS_DICT.exists():
        alias = read_csv(ALIAS_DICT)
        hits = [r for r in alias
                if r.get("canonical_id", "").removeprefix("skill:") in changed_skills]
        print(f"  alias_dictionary.csv canonical_id affected : {len(hits)} / {len(alias)}")
        for r in sorted(hits, key=lambda x: x["alias_key"]):
            old = r["canonical_id"].removeprefix("skill:")
            print(f"      {r['alias_key']:<22} skill:{old}  =>  skill:{changed_skills[old]}")

    if CLASSIFICATION_AUDIT.exists():
        aud = read_csv(CLASSIFICATION_AUDIT)
        hits = [r for r in aud if r["registry_key"] in changed_skills]
        kinds = dict(collections.Counter(r["new_skill_kind"] for r in hits))
        blocked = sum(1 for r in hits if r["blacklist_decision"] == "blocked")
        print(f"  skill_classification_audit.csv affected   : "
              f"{len(hits)} / {len(aud)}  kinds={kinds}")
        print(f"      of which blacklisted (needs care)     : {blocked}")

    for name in ("soft_skill_blacklist_v0.2.csv", "soft_skill_blacklist_v0.3.csv"):
        path = FIXTURES_DIR / name
        if not path.exists():
            continue
        rows = read_csv(path)
        hits = [r for r in rows
                if r.get("canonical_id", "").removeprefix("skill:") in changed_skills]
        print(f"  {name:<30} affected: {len(hits)} / {len(rows)}")


def main() -> int:
    ap = argparse.ArgumentParser(description="A7b: registry_key change impact")
    ap.add_argument("--verbose", action="store_true", help="list every changed key")
    args = ap.parse_args()

    print("=" * 72)
    print("A7b — registry_key 正規化變更影響分析（47e543d）")
    print("=" * 72)

    for path in (SKILL_DICT, CREDENTIAL_DICT):
        if not path.exists():
            print(f"  [FAIL] missing {path}")
            return 1

    skill_rows = read_csv(SKILL_DICT)
    cred_rows = read_csv(CREDENTIAL_DICT)

    print("\n[1] 有多少 registry ID 會改變")
    changed_skills = analyse_dictionary("skill_dictionary", skill_rows, verbose=args.verbose)
    changed_creds = analyse_dictionary("credential_dictionary", cred_rows, verbose=args.verbose)
    total = len(changed_skills) + len(changed_creds)
    print(f"  => 合計 {total} 個 registry ID 會改變"
          f"（skill {len(changed_skills)} + credential {len(changed_creds)}）")

    print("\n[2] alias 重新綁定是否仍成立（最危險的一項）")
    check_alias_rebinding(skill_rows)

    print("\n[3] 下游 A 端 artifact 受影響範圍")
    report_downstream(changed_skills)

    print("\n[4] DOT_PREFIX_ALLOWLIST 在本資料集是否有作用")
    dot_skill = [r["registry_key"] for r in skill_rows if r["registry_key"].startswith(".")]
    dot_cred = [r["registry_key"] for r in cred_rows if r["registry_key"].startswith(".")]
    print(f"  allowlist = {sorted(s3.DOT_PREFIX_ALLOWLIST)}")
    print(f"  以 '.' 開頭的 skill key      : {len(dot_skill)} {dot_skill}")
    print(f"  以 '.' 開頭的 credential key : {len(dot_cred)} {dot_cred}")
    if not dot_skill and not dot_cred:
        print("  => 防禦性程式碼；在目前資料上不改變任何輸出")

    print("\n" + "=" * 72)
    print(f"結論：新規則不造成靜默誤併，alias 綁定不變；但有 {total} 個 registry ID 會變。")
    print("若重跑 Step 3，必須連帶重跑 step_a6 與 Step 4→8，")
    print("否則舊 edges 會指向已不存在的 key（Step 7 dangling edge = FAIL）。")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
