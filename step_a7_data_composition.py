"""
A7 — 資料組成揭露統計（Step 9 交付文件 A 段落的數據來源）
Role: A (Content Graph)

為什麼需要這支腳本：
  SKILL_GRAPH_NEXT_PLAN.md r2 §2 寫了「1,032 個工作技能節點只有 14.1% 帶能力標記，
  37.4%（DF 佔 52.1%）是純動作職責描述」，但沒有留下產生這兩個數字的程式。
  14.1% 可以重現（見下方 ability_marker 規則），37.4%/52.1% 這一組無法重現。
  交付文件裡的揭露數字必須能被評審重跑，所以把規則寫死在這裡。

本腳本唯讀，不寫任何圖資產，不改 schema／registry／blacklist。

用法：
  python step_a7_data_composition.py            # 印出報表並寫 manifest
  python step_a7_data_composition.py --no-write # 只印，不寫檔

輸出：
  graph/data_composition_manifest.json
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
GRAPH_DIR = ROOT / "graph"

SKILL_DICT = GRAPH_DIR / "skill_dictionary.csv"
CLASSIFICATION_AUDIT = GRAPH_DIR / "skill_classification_audit.csv"
MANIFEST_OUT = GRAPH_DIR / "data_composition_manifest.json"

COMPOSITION_RULE_VERSION = "data_composition_rule_v0.1"

# ── 分組規則（寫死，供重現）────────────────────────────────────────────────
# 依序判定，先命中者為準。純字面規則，不呼叫模型；目的是描述「來源欄位的
# 命名習慣」，不是要取代 skill_kind。
ABILITY_MARKER = r"能力|技巧|知識|技能"

DUTY_VERB = (
    "維護|維持|管理|操作|執行|處理|進行|製作|規劃|安排|協助|負責|使用|從事|辦理|完成|達成|"
    "辨識|檢查|檢驗|檢修|控制|清潔|接聽|撰寫|繪製|設計|組裝|包裝|搬運|銷售|接待|安裝|測試|"
    "分析|記錄|填寫|建立|開發|指導|教導|訓練|監督|巡視|保養|修理|烹調|調製|裁剪|縫製|焊接|"
    "研磨|噴漆|施作|鋪設|舖設|吊掛|駕駛|裝卸|盤點|收銀|排程|催收|催帳|審核|核對|統計|翻譯|"
    "拍攝|剪輯|照顧|護理|諮詢|接洽|拜訪|推廣|宣傳|佈置|陳列|補貨|理貨|驗收|報關|報價|估價|"
    "投標|洽談|溝通|指揮|調度|派送|遞送|配送|量測|測量|判讀|評估|稽核|申報|報稅|記帳|開立|"
    "沖洗|洗滌|烘烤|切割|裝訂|印刷|裱貼|安撫|陪伴|帶領|主持|司儀|導覽|解說|引導|預約|登記"
)

RX_ABILITY = re.compile(ABILITY_MARKER)
RX_DUTY_PREFIX = re.compile("^(" + DUTY_VERB + ")")
RX_LATIN = re.compile(r"[A-Za-z0-9]")

GROUP_DEFINITIONS = {
    "ability_marked": "名稱含「能力／技巧／知識／技能」字樣 → 明確標示為一種可具備的能力",
    "duty_action_phrase": "不含能力標記，且以動詞開頭 → 明顯是工作動作／職責敘述",
    "named_tool_or_standard": "不含能力標記、非動詞開頭，但含拉丁字母或數字 → 具名工具／規格",
    "other_noun_phrase": "以上皆非 → 領域工作內容名詞片語（例：不動產經紀業務）",
}

# registry_key 中可能造成批次回傳 key 無法逐字對回的字元
RX_RISKY_KEY_CHAR = re.compile(r"[/／╱,，、()（）&_\-]")


def _sha256_file(path: Path) -> str | None:
    if not path.exists():
        return None
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return [dict(r) for r in csv.DictReader(f)]


def group_of(name: str) -> str:
    if RX_ABILITY.search(name):
        return "ability_marked"
    if RX_DUTY_PREFIX.match(name):
        return "duty_action_phrase"
    if RX_LATIN.search(name):
        return "named_tool_or_standard"
    return "other_noun_phrase"


def main() -> int:
    ap = argparse.ArgumentParser(description="A7: skill-node data composition disclosure")
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args()

    print("=" * 70)
    print("A7 — 資料組成揭露統計")
    print("=" * 70)

    for path in (SKILL_DICT, CLASSIFICATION_AUDIT):
        if not path.exists():
            print(f"  [FAIL] missing {path}")
            return 1

    skills = read_csv(SKILL_DICT)
    audit = {r["registry_key"]: r for r in read_csv(CLASSIFICATION_AUDIT)}

    def df(row: dict[str, str]) -> int:
        a = audit.get(row["registry_key"])
        if not a:
            return 0
        try:
            return int(a.get("job_frequency") or 0)
        except ValueError:
            return 0

    # ── 依來源欄位 × skill_kind ───────────────────────────────────────────
    by_source: dict[str, list[dict[str, str]]] = defaultdict(list)
    for r in skills:
        by_source[r.get("source", "")].append(r)

    print(f"\n  Skill 節點總數：{len(skills)}")
    source_summary = {}
    for src in sorted(by_source):
        sub = by_source[src]
        kinds = dict(Counter(r.get("skill_kind", "") for r in sub))
        source_summary[src] = {
            "nodes": len(sub),
            "skill_kind": kinds,
            "total_job_frequency": sum(df(r) for r in sub),
        }
        print(f"    {src:<12} n={len(sub):<5} skill_kind={kinds}")

    # ── 工作技能欄位的命名組成（交付文件要揭露的重點）────────────────────
    target_source = "工作技能"
    ws = by_source.get(target_source, [])
    if not ws:
        print(f"  [FAIL] no skills sourced from {target_source}")
        return 1
    n_ws = len(ws)
    df_ws = sum(df(r) for r in ws)

    groups: Counter = Counter()
    group_df: Counter = Counter()
    examples: dict[str, list[str]] = defaultdict(list)
    for r in ws:
        g = group_of(r.get("canonical_name", ""))
        groups[g] += 1
        group_df[g] += df(r)
        if len(examples[g]) < 5:
            examples[g].append(f"{r.get('canonical_name','')}(DF={df(r)})")

    print(f"\n  「{target_source}」衍生節點：{n_ws} 個；job-level 歸屬總量 DF={df_ws:,}")
    composition = {}
    order = ("ability_marked", "duty_action_phrase", "named_tool_or_standard", "other_noun_phrase")
    for g in order:
        n, d = groups.get(g, 0), group_df.get(g, 0)
        composition[g] = {
            "definition": GROUP_DEFINITIONS[g],
            "nodes": n,
            "node_share": round(n / n_ws, 4),
            "job_frequency": d,
            "df_share": round(d / df_ws, 4) if df_ws else 0.0,
            "examples": examples.get(g, []),
        }
        print(f"    {g:<24} n={n:<4} ({100 * n / n_ws:5.1f}%)   DF share={100 * d / df_ws:5.1f}%")
        print(f"        e.g. {' | '.join(examples.get(g, []))}")

    ab = composition["ability_marked"]
    print(f"\n  → 帶能力標記者僅 {ab['nodes']} 個（{100 * ab['node_share']:.1f}%），"
          f"占該欄位 DF {100 * ab['df_share']:.1f}%")
    print(f"  → 其餘 {n_ws - ab['nodes']} 個（{100 * (1 - ab['node_share']):.1f}%）名稱沒有任何能力字樣")

    # ── 未取得模型分類回覆者的特徵 ─────────────────────────────────────────
    unclassified = [r for r in audit.values() if (r.get("llm_reason") or "") == "no_response"]
    risky = [r for r in unclassified if RX_RISKY_KEY_CHAR.search(r["registry_key"])]
    print(f"\n  未取得分類回覆：{len(unclassified)} / {len(audit)}")
    print(f"    其中 registry_key 含分隔符或括號字元：{len(risky)}")
    print("    （假設：批次回傳的 key 無法逐字對回；未經驗證，需保存 rejected_items 才能確認）")
    print("    全部保留 step3 預設 technical，因此不會被列入黑名單")

    manifest = {
        "step": "step_a7_data_composition",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "role": "A (Content Graph) — Step 9 交付文件資料組成揭露",
        "rule_version": COMPOSITION_RULE_VERSION,
        "purpose": "讓交付文件引用的組成比例可被重跑驗證；取代 NEXT_PLAN r2 中無腳本的 37.4%/52.1%",
        "inputs": {
            "skill_dictionary": str(SKILL_DICT.relative_to(ROOT)),
            "skill_dictionary_sha256": _sha256_file(SKILL_DICT),
            "skill_classification_audit": str(CLASSIFICATION_AUDIT.relative_to(ROOT)),
            "skill_classification_audit_sha256": _sha256_file(CLASSIFICATION_AUDIT),
        },
        "rules": {
            "ability_marker_regex": ABILITY_MARKER,
            "duty_verb_prefix_regex": "^(" + DUTY_VERB + ")",
            "named_tool_regex": r"[A-Za-z0-9]",
            "evaluation_order": list(order),
            "df_source": "skill_classification_audit.csv 的 job_frequency（A5 全量統計）",
            "note": "純字面規則，不呼叫模型；描述來源欄位命名習慣，不取代 skill_kind",
        },
        "totals": {
            "skill_nodes": len(skills),
            "by_source": source_summary,
        },
        "work_skill_field_composition": {
            "source_field": target_source,
            "nodes": n_ws,
            "total_job_frequency": df_ws,
            "groups": composition,
        },
        "unclassified_by_llm": {
            "count": len(unclassified),
            "of_total": len(audit),
            "registry_key_contains_delimiter_or_bracket": len(risky),
            "retained_skill_kind": "technical（step3 預設，保守不封鎖）",
            "hypothesis": "批次回傳的 registry_key 無法逐字對回 → 被 A6 的 allowlist 守衛丟棄",
            "hypothesis_status": "未驗證；需在 A6 manifest 持久化 rejected_items 才能確認",
        },
        "known_limitations": [
            "分組規則是字面啟發式，不是人工標註；用於揭露命名習慣，不可當作品質指標",
            "DF 取自 A5 全量統計，與 statistical_eligible 口徑不同（後者另有 eligible_job_frequency）",
            "只涵蓋 skill_dictionary；credential_dictionary 不在此統計",
        ],
    }

    if not args.no_write:
        MANIFEST_OUT.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n  wrote {MANIFEST_OUT}")
    else:
        print("\n  --no-write：未寫出 manifest")

    print("\n✓ A7 complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
