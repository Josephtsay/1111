"""
A6 — LLM 技能分類（Phase 1 合規主線）
Role: A (Content Graph)
Playbook: LLM 必須實際參與抽取／正規化／分類／關係之一，且可關閉做 ablation

為什麼是分類而不是全量抽取：
  職缺 121.8 萬筆逐筆跑 LLM 在時程與成本上做不到；
  但技能字典只有 ~1,437 筆，批次分類約 36 次呼叫、數分鐘、數美分。
  分類寫回 skill_dictionary.csv，step6_graph_export 已經會把 skill_kind
  帶進 nodes.csv（step6_graph_export.py:113），因此不需要改 B 的任何程式。

關鍵設計：讓分類「真的有下游效果」
  原本 skill_kind 沒有任何消費者（step3 寫死 technical、step6 只是搬運），
  所以「開／關分類」的 ablation 會完全沒有指標差異，LLM 等於裝飾。
  本腳本把分類結果接到既有的唯一消費點 —— soft-skill blacklist：
      skill_kind == soft / non_skill
        → 通過統計守衛（DF + 跨職類分散度 + salience）
        → 併入 fixtures/soft_skill_blacklist_v0.3.csv
        → step5 已會自動吃最新版 v*.csv
        → 影響 CO_OCCURS_WITH / CORE_SKILL / global_job_frequency
        → step8 檢索結果才會真的改變

合規邊界：
  - LLM 只能對「已存在於 registry 的技能」指定 kind；不得新增／改寫／刪除 registry_key
  - 被封鎖的技能仍來自職缺原文抽取（2A／2B evidence），不是模型憑常識造出來的
  - 封鎖需 dual-signal：LLM 語意判斷 + A5 全量統計訊號，單靠模型不足以入黑名單
  - dictionary_version 不動（extractions.jsonl 的 mention 內嵌 v0.1，動了會 join 不一致）；
    分類版本另存 skill_kind_version / skill_kind_source 欄位

用法：
  python step_a6_skill_classification.py --mode dry-run           # 批次與 token 估算
  python step_a6_skill_classification.py --mode live --limit 80   # 小批試跑
  python step_a6_skill_classification.py --mode live             # 全量
  python step_a6_skill_classification.py --make-eval-set         # 產生人工標註樣本
  python step_a6_skill_classification.py --eval                  # 對人工標籤算準確率

輸出：
  graph/skill_dictionary.csv                     （就地更新 skill_kind；先自動備份）
  graph/skill_dictionary_pre_classification.csv  （備份，可回退）
  fixtures/soft_skill_blacklist_v0.3.csv         （dual-signal 後的黑名單）
  graph/skill_classification_audit.csv           （逐筆：新舊 kind、理由、統計、與規則法比較）
  graph/skill_classification_manifest.json
  fixtures/skill_kind_eval_set_v0.1.csv          （--make-eval-set：待人工填答）
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import llm_client
from step_a0b_llm_bakeoff_run import parse_strict_json

ROOT = Path(__file__).resolve().parent.parent  # pipeline/ -> repo root; graph/ dataset/ fixtures/ 都掛在根目錄
GRAPH_DIR = ROOT / "graph"
FIXTURES_DIR = ROOT / "fixtures"

SKILL_DICT = GRAPH_DIR / "skill_dictionary.csv"
DICT_BACKUP = GRAPH_DIR / "skill_dictionary_pre_classification.csv"
A5_AUDIT = GRAPH_DIR / "soft_skill_blacklist_audit_v0.2.csv"
PROMPT_PATH = ROOT / "prompts" / "llm_skill_classification_v0.2.txt"
AUDIT_OUT = GRAPH_DIR / "skill_classification_audit.csv"
MANIFEST_OUT = GRAPH_DIR / "skill_classification_manifest.json"
BLACKLIST_V3 = FIXTURES_DIR / "soft_skill_blacklist_v0.3.csv"
EVAL_SET = FIXTURES_DIR / "skill_kind_eval_set_v0.1.csv"

PROMPT_VERSION = "llm_skill_classification_v0.2"
CLASSIFICATION_VERSION = "skill_kind_v0.2"
BLACKLIST_VERSION = "v0.3"
POLICY_VERSION = "soft_skill_blacklist_policy_v0.3"

# 分類法決策（2026-08-01 人工定案）：不設獨立的 task 類別。
# 理由：「任務 vs 技能」邊界模糊——雇主把「櫃檯收銀服務」填在工作技能欄，
# 就是在要求求職者會做這件事，對他而言那就是技能。多一類的收益不足以
# 抵銷 sign-off 與全量重跑的成本。工作內容型敘述一律歸 technical。
# v0.2 prompt 因此明確規定：低門檻工作內容 → technical，不是 soft。
VALID_KINDS = {"technical", "tool", "soft", "non_skill"}

# 只有這些 kind 會成為黑名單候選（仍須通過統計守衛）
BLACKLISTABLE_KINDS = {"soft", "non_skill"}
BATCH_SIZE = 40

# ── 統計守衛（沿用 A5 v0.2 門檻，維持同一口徑）──────────────────────────────
MIN_JOB_FREQ = 50
MIN_NORM_ENTROPY = 0.55
MIN_DISTINCT_MAJOR = 8
MAX_SALIENCE_TO_BLOCK = 0.50

DEFAULT_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"  # Gate 1 preferred


def _sha256_file(path: Path) -> str | None:
    if not path.exists():
        return None
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_dictionary() -> tuple[list[dict[str, str]], list[str]]:
    with SKILL_DICT.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = [dict(r) for r in reader]
        fields = list(reader.fieldnames or [])
    return rows, fields


def load_a5_stats() -> dict[str, dict[str, Any]]:
    """canonical_id → A5 全量統計訊號（DF、熵、大類數、salience、規則法判定）。"""
    stats: dict[str, dict[str, Any]] = {}
    if not A5_AUDIT.exists():
        return stats
    with A5_AUDIT.open("r", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            cid = (r.get("canonical_id") or "").strip()
            if not cid:
                continue

            def num(key: str, row: dict = r, default: float = 0.0) -> float:
                try:
                    return float(row.get(key) or default)
                except ValueError:
                    return default

            stats[cid] = {
                "job_frequency": int(num("job_frequency")),
                "eligible_job_frequency": int(num("eligible_job_frequency")),
                "entropy": num("occupation_major_entropy_norm"),
                "distinct_major": int(num("distinct_occupation_major")),
                "max_salience": num("max_occupation_salience"),
                "rule_decision": r.get("decision") or "",
                "rule_lexical_code": r.get("lexical_reason_code") or "",
                "top_major": r.get("top_occupation_major") or "",
            }
    return stats


def load_prior_blacklist() -> dict[str, dict[str, str]]:
    """
    讀入 v0.3 之前所有版本已封鎖的項目（沿用 step5 loader 的 contract：
    status != 'rejected' 即視為封鎖），確保 v0.3 是超集、換版不會回退。
    只沿用真的存在於 A5 統計中的 ID，因此 v0.1 無效草案不會被帶進來。
    """
    prior: dict[str, dict[str, str]] = {}
    for path in sorted(FIXTURES_DIR.glob("soft_skill_blacklist_v*.csv")):
        if path.name == BLACKLIST_V3.name:
            continue
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                if (row.get("status") or "").strip() == "rejected":
                    continue
                cid = (row.get("canonical_id") or "").strip()
                if cid and cid not in prior:
                    prior[cid] = row
    return prior


def render_batch(batch: list[dict[str, str]], stats: dict[str, dict[str, Any]]) -> str:
    template = PROMPT_PATH.read_text(encoding="utf-8")
    lines = []
    for row in batch:
        key = row["registry_key"]
        st = stats.get(f"skill:{key}", {})
        lines.append(
            f"- {key} | {row.get('canonical_name', key)} | "
            f"{st.get('job_frequency', 0)} | {st.get('distinct_major', 0)}"
        )
    return template.replace("{{skills}}", "\n".join(lines))


def classify_batch(
    batch: list[dict[str, str]],
    stats: dict[str, dict[str, Any]],
    *,
    model_id: str,
    mode: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """回傳 (registry_key → {skill_kind, confidence, reason}, 呼叫統計)。"""
    prompt = render_batch(batch, stats)
    meta: dict[str, Any] = {"prompt_chars": len(prompt)}

    if mode == "dry-run":
        meta["skipped"] = True
        return {}, meta

    try:
        out = llm_client.invoke(model_id, prompt, max_tokens=8192, temperature=0.0)
    except llm_client.BedrockError as e:
        meta["error"] = f"http_{e.status}: {e.message[:160]}"
        return {}, meta

    meta.update({
        "latency_ms": round(out["latency_ms"], 1),
        "input_tokens": out["input_tokens"],
        "output_tokens": out["output_tokens"],
        "stop_reason": out.get("stop_reason"),
    })
    obj, strict_ok, repair = parse_strict_json(out["text"])
    meta["strict_json"] = strict_ok
    meta["repair_note"] = repair
    if obj is None:
        meta["error"] = "unparseable_json"
        return {}, meta

    requested = {r["registry_key"] for r in batch}
    result: dict[str, dict[str, Any]] = {}
    rejected: list[str] = []
    for item in obj.get("classifications") or []:
        if not isinstance(item, dict):
            continue
        key = (item.get("registry_key") or "").strip()
        kind = (item.get("skill_kind") or "").strip()
        # 守衛：LLM 不得發明 registry_key，也不得用未定義的 kind
        if key not in requested or kind not in VALID_KINDS:
            rejected.append(f"{key}:{kind}")
            continue
        conf = item.get("confidence")
        result[key] = {
            "skill_kind": kind,
            "confidence": float(conf) if isinstance(conf, (int, float)) else None,
            "reason": (item.get("reason") or "")[:40],
        }
    meta["returned"] = len(result)
    meta["rejected_items"] = rejected[:10]
    meta["missing"] = sorted(requested - set(result))[:10]
    return result, meta


def statistical_guard(cid: str, stats: dict[str, dict[str, Any]]) -> tuple[bool, str]:
    """LLM 判 soft/non_skill 後，仍須通過 A5 全量統計訊號才可入黑名單。"""
    st = stats.get(cid)
    if not st:
        return False, "no_statistics（不在 A5 audit 中，無法驗證）"
    df, ent = st["job_frequency"], st["entropy"]
    majors, sal = st["distinct_major"], st["max_salience"]
    if df < MIN_JOB_FREQ:
        return False, f"DF={df} < {MIN_JOB_FREQ}（統計支持不足）"
    if not (ent >= MIN_NORM_ENTROPY or majors >= MIN_DISTINCT_MAJOR):
        return False, f"分散度不足（entropy={ent}, majors={majors}）"
    if sal > MAX_SALIENCE_TO_BLOCK:
        return False, f"salience={sal} > {MAX_SALIENCE_TO_BLOCK}（疑為特定職類核心能力）"
    return True, f"dual-signal 通過（DF={df}, entropy={ent}, majors={majors}, salience={sal}）"


def make_eval_set(rows: list[dict[str, str]], stats: dict[str, dict[str, Any]], n: int) -> None:
    """產生分層人工標註樣本；不含答案，由人填 expected_skill_kind。"""
    rng = random.Random(2026)
    hi, mid, lo = [], [], []
    for r in rows:
        df = stats.get(f"skill:{r['registry_key']}", {}).get("job_frequency", 0)
        (hi if df >= 1000 else mid if df >= 50 else lo).append(r)
    picked: list[dict[str, str]] = []
    for pool, k in [(hi, n // 3), (mid, n // 3), (lo, n - 2 * (n // 3))]:
        rng.shuffle(pool)
        picked.extend(pool[:k])

    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    with EVAL_SET.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "registry_key", "canonical_name", "job_frequency",
            "distinct_occupation_major", "expected_skill_kind", "labeler", "notes",
        ])
        for r in picked:
            st = stats.get(f"skill:{r['registry_key']}", {})
            w.writerow([
                r["registry_key"], r.get("canonical_name", ""),
                st.get("job_frequency", 0), st.get("distinct_major", 0), "", "", "",
            ])
    print(f"  wrote {EVAL_SET} ({len(picked)} rows, stratified by DF, seed=2026)")
    print("  → 人工填 expected_skill_kind（technical/tool/soft/non_skill）後跑 --eval")


def run_eval() -> int:
    """對照人工標籤計算分類準確率，並比較 LLM 與 A5 規則法的 soft 偵測。"""
    if not EVAL_SET.exists():
        print(f"  [FAIL] missing {EVAL_SET}; run --make-eval-set first")
        return 1
    labeled: dict[str, str] = {}
    with EVAL_SET.open("r", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            exp = (r.get("expected_skill_kind") or "").strip()
            if exp:
                labeled[r["registry_key"]] = exp
    if not labeled:
        print(f"  [FAIL] {EVAL_SET} 尚未填任何 expected_skill_kind")
        return 1
    if not AUDIT_OUT.exists():
        print(f"  [FAIL] missing {AUDIT_OUT}; run --mode live first")
        return 1

    audit: dict[str, dict[str, str]] = {}
    with AUDIT_OUT.open("r", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            audit[r["registry_key"]] = r

    total = correct = 0
    confusion: Counter = Counter()
    for key, expected in labeled.items():
        row = audit.get(key)
        if not row:
            continue
        got = row.get("new_skill_kind") or ""
        total += 1
        if got == expected:
            correct += 1
        else:
            confusion[f"{expected}->{got}"] += 1
    print(f"  labeled: {len(labeled)}; scored: {total}")
    print(f"  classification_accuracy: "
          f"{round(correct / total, 4) if total else None} ({correct}/{total})")
    if confusion:
        print(f"  confusions: {dict(confusion)}")

    soft_expected = {k for k, v in labeled.items() if v in ("soft", "non_skill")}
    llm_soft = {k for k in labeled
                if (audit.get(k, {}).get("new_skill_kind") in ("soft", "non_skill"))}
    rule_soft = {k for k in labeled if audit.get(k, {}).get("rule_lexical_code")}

    def prf(pred: set[str]) -> str:
        tp = len(pred & soft_expected)
        p = tp / len(pred) if pred else 0.0
        rc = tp / len(soft_expected) if soft_expected else 0.0
        return f"precision={p:.3f} recall={rc:.3f} (tp={tp}, predicted={len(pred)})"

    print(f"  soft detection — LLM : {prf(llm_soft)}")
    print(f"  soft detection — rule: {prf(rule_soft)}")
    print(f"  (human-labeled soft/non_skill in sample: {len(soft_expected)})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="A6: LLM skill_kind classification")
    ap.add_argument("--mode", choices=["dry-run", "live"], default="dry-run")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--limit", type=int, default=None, help="只分類前 N 筆（試跑）")
    ap.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    ap.add_argument("--make-eval-set", action="store_true")
    ap.add_argument("--eval-size", type=int, default=100)
    ap.add_argument("--eval", action="store_true")
    ap.add_argument("--no-write-dictionary", action="store_true",
                    help="不就地更新 skill_dictionary.csv（只產 audit / blacklist）")
    args = ap.parse_args()

    print("=" * 70)
    print(f"A6 — LLM skill classification   mode={args.mode}")
    print("=" * 70)

    if not SKILL_DICT.exists():
        print(f"  [FAIL] missing {SKILL_DICT}")
        return 1
    rows, fields = load_dictionary()
    stats = load_a5_stats()
    print(f"  dictionary: {len(rows)} skills; A5 stats: {len(stats)} entries")

    if args.eval:
        return run_eval()
    if args.make_eval_set:
        make_eval_set(rows, stats, args.eval_size)
        return 0

    targets = rows[: args.limit] if args.limit else rows
    batches = [targets[i:i + args.batch_size]
               for i in range(0, len(targets), args.batch_size)]
    print(f"  to classify: {len(targets)} in {len(batches)} batches "
          f"(batch_size={args.batch_size})")

    if args.mode == "live":
        llm_client.load_env()
        missing = [k for k, v in llm_client.credentials_present().items() if not v]
        if missing:
            print(f"  [FAIL] missing credentials: {missing}")
            return 1
        print(f"  model={args.model}  region={llm_client.region()}")

    classified: dict[str, dict[str, Any]] = {}
    call_meta: list[dict[str, Any]] = []
    for i, batch in enumerate(batches, 1):
        got, meta = classify_batch(batch, stats, model_id=args.model, mode=args.mode)
        meta["batch"] = i
        meta["batch_len"] = len(batch)
        call_meta.append(meta)
        classified.update(got)
        if args.mode == "live":
            print(f"    batch {i}/{len(batches)}: "
                  f"{meta.get('error') or f'{len(got)}/{len(batch)}'}")

    if args.mode == "dry-run":
        est_in = sum(m["prompt_chars"] for m in call_meta) / 2.5
        print(f"\n  dry-run estimate: {len(batches)} calls, "
              f"~{est_in:,.0f} input tokens total "
              f"(~{est_in / max(len(batches), 1):,.0f} per call)")
        print("  no API calls made, nothing written")
        return 0

    coverage = len(classified) / len(targets) if targets else 0.0
    kind_counts = Counter(v["skill_kind"] for v in classified.values())
    print(f"\n  classified: {len(classified)}/{len(targets)} ({coverage:.1%})")
    print(f"  kind distribution: {dict(kind_counts)}")
    if coverage < 0.9:
        print("  [WARN] coverage < 90%；未回覆的技能保留原 skill_kind（保守不封鎖）")

    # ── audit + dual-signal blacklist ─────────────────────────────────────────
    audit_rows: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for row in targets:
        key = row["registry_key"]
        cid = f"skill:{key}"
        got = classified.get(key)
        st = stats.get(cid, {})
        new_kind = got["skill_kind"] if got else row.get("skill_kind", "technical")
        decision, guard_reason = "not_blocked", ""

        if got and got["skill_kind"] in BLACKLISTABLE_KINDS:
            ok, guard_reason = statistical_guard(cid, stats)
            decision = "blocked" if ok else "candidate_needs_review"
            if ok:
                blocked.append({
                    "canonical_id": cid,
                    "canonical_name": row.get("canonical_name", key),
                    "skill_kind": got["skill_kind"],
                    "llm_confidence": got["confidence"],
                    "llm_reason": got["reason"],
                    "job_frequency": st.get("job_frequency", 0),
                    "eligible_job_frequency": st.get("eligible_job_frequency", 0),
                    "distinct_occupation_major": st.get("distinct_major", 0),
                    "occupation_major_entropy_norm": st.get("entropy", 0.0),
                    "max_occupation_salience": st.get("max_salience", 0.0),
                    "guard": guard_reason,
                })

        rule_code = st.get("rule_lexical_code", "")
        llm_says_soft = new_kind in BLACKLISTABLE_KINDS
        audit_rows.append({
            "registry_key": key,
            "canonical_name": row.get("canonical_name", key),
            "old_skill_kind": row.get("skill_kind", ""),
            "new_skill_kind": new_kind,
            "llm_confidence": got["confidence"] if got else "",
            "llm_reason": got["reason"] if got else "no_response",
            "blacklist_decision": decision,
            "guard_reason": guard_reason,
            "rule_lexical_code": rule_code,
            "rule_decision": st.get("rule_decision", ""),
            "agreement_with_rule": (
                "" if not got
                else "both_soft" if (rule_code and llm_says_soft)
                else "llm_only_soft" if llm_says_soft
                else "rule_only_soft" if rule_code
                else "both_not_soft"
            ),
            "job_frequency": st.get("job_frequency", 0),
            "distinct_occupation_major": st.get("distinct_major", 0),
            "occupation_major_entropy_norm": st.get("entropy", 0.0),
            "max_occupation_salience": st.get("max_salience", 0.0),
            "top_occupation_major": st.get("top_major", ""),
        })

    GRAPH_DIR.mkdir(parents=True, exist_ok=True)
    with AUDIT_OUT.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(audit_rows[0].keys()))
        w.writeheader()
        w.writerows(audit_rows)
    print(f"  wrote {AUDIT_OUT} ({len(audit_rows)} rows)")

    # v0.3 必須是 v0.2 的超集：只採用 LLM 結果會讓規則法已封鎖的項目回退。
    prior_blocked = load_prior_blacklist()
    already = {r["canonical_id"] for r in blocked}
    carried = 0
    for cid, prior in prior_blocked.items():
        if cid in already:
            continue
        if cid not in stats:
            # v0.1 草案的手寫 ID 不存在於 registry，不帶進 v0.3
            continue
        st = stats.get(cid, {})
        blocked.append({
            "canonical_id": cid,
            "canonical_name": prior.get("canonical_name", ""),
            "skill_kind": "soft",
            "llm_confidence": "",
            "llm_reason": "carried_from_v0.2_rule_based",
            "job_frequency": st.get("job_frequency", 0),
            "eligible_job_frequency": st.get("eligible_job_frequency", 0),
            "distinct_occupation_major": st.get("distinct_major", 0),
            "occupation_major_entropy_norm": st.get("entropy", 0.0),
            "max_occupation_salience": st.get("max_salience", 0.0),
            "guard": "沿用 v0.2 規則法 dual-signal 判定（避免換版造成回退）",
        })
        carried += 1
    if carried:
        print(f"  carried {carried} entries forward from v0.2 (superset guarantee)")

    blocked.sort(key=lambda r: -r["job_frequency"])
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)

    # 部分執行不可覆寫正式 v0.3：step5 會挑「最新版」，
    # 一個只含少數項目的 v0.3 等於悄悄關掉黑名單。
    if args.limit:
        blacklist_path = BLACKLIST_V3.with_name(
            f"soft_skill_blacklist_{BLACKLIST_VERSION}_partial_{args.limit}.csv.txt"
        )
        print(f"  [partial run] 不覆寫正式 {BLACKLIST_V3.name}；改寫 {blacklist_path.name}")
    else:
        blacklist_path = BLACKLIST_V3
    with blacklist_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "canonical_id", "canonical_name", "reason", "status", "category",
            "job_frequency", "eligible_job_frequency", "distinct_occupation_major",
            "occupation_major_entropy_norm", "max_occupation_salience",
            "llm_confidence", "llm_reason", "policy_version", "notes",
        ])
        for r in blocked:
            w.writerow([
                r["canonical_id"], r["canonical_name"], "llm_classified_generic",
                "blocked", r["skill_kind"], r["job_frequency"],
                r["eligible_job_frequency"], r["distinct_occupation_major"],
                r["occupation_major_entropy_norm"], r["max_occupation_salience"],
                r["llm_confidence"], r["llm_reason"], POLICY_VERSION, r["guard"],
            ])
    print(f"  wrote {blacklist_path} ({len(blocked)} blocked)"
          + ("" if args.limit else " → step5 自動吃最新版"))

    # ── 就地更新 dictionary（skill_kind 生效點；dictionary_version 保持不動）──
    if not args.no_write_dictionary and classified:
        if not DICT_BACKUP.exists():
            DICT_BACKUP.write_text(SKILL_DICT.read_text(encoding="utf-8"), encoding="utf-8")
            print(f"  backed up original → {DICT_BACKUP}")
        # 只更新 skill_kind 的值，不新增任何欄位：
        #   - 分類版本／模型是整批 run 的屬性 → 記在 manifest，不是每列重複
        #   - 模型自評 confidence 依 playbook §3.4 #4 不得作為通過依據 → 只留在 audit
        #   - 未分類的例外清單 → manifest 的 unclassified_registry_keys
        # 如此字典結構與 step3 產出完全一致，step6 讀取零風險。
        with SKILL_DICT.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            for row in rows:
                got = classified.get(row["registry_key"])
                if got:
                    row["skill_kind"] = got["skill_kind"]
                w.writerow(row)
        print(f"  updated {SKILL_DICT}（只改 skill_kind 值；欄位與 dictionary_version 均未動）")

    agree = Counter(r["agreement_with_rule"] for r in audit_rows if r["agreement_with_rule"])
    manifest = {
        "step": "step_a6_skill_classification",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "role": "A (Content Graph) — Phase 1 LLM classification",
        "mode": args.mode,
        "model_id": args.model,
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": _sha256_file(PROMPT_PATH),
        "classification_version": CLASSIFICATION_VERSION,
        "blacklist_version": BLACKLIST_VERSION,
        "policy_version": POLICY_VERSION,
        "taxonomy": sorted(VALID_KINDS),
        "taxonomy_note": (
            "skill_kind 詞彙表由 A 提出，需 A/B 雙人 sign-off（會出現在 B 的 nodes.csv）；"
            "credential 保留給證照降級實作，不在此批次使用"
        ),
        "inputs": {
            "skill_dictionary_sha256_before": _sha256_file(DICT_BACKUP),
            "a5_audit_sha256": _sha256_file(A5_AUDIT),
        },
        "statistics": {
            "skills_targeted": len(targets),
            "skills_classified": len(classified),
            "coverage": round(coverage, 4),
            "kind_distribution": dict(kind_counts),
            "blocked_into_blacklist": len(blocked),
            "candidates_needing_review": sum(
                1 for r in audit_rows if r["blacklist_decision"] == "candidate_needs_review"
            ),
            "agreement_with_rule_based": dict(agree),
            # 例外清單記在這裡，而不是在字典每列加 skill_kind_source 欄
            "unclassified_registry_keys": sorted(
                r["registry_key"] for r in targets
                if r["registry_key"] not in classified
            ),
            "strict_json_batches": sum(1 for m in call_meta if m.get("strict_json")),
            "total_batches": len(call_meta),
            "batch_errors": [m for m in call_meta if m.get("error")][:10],
            "total_input_tokens": sum(m.get("input_tokens") or 0 for m in call_meta),
            "total_output_tokens": sum(m.get("output_tokens") or 0 for m in call_meta),
        },
        "guard_policy": {
            "note": "LLM 判 soft/non_skill 後仍須通過 A5 全量統計訊號才入黑名單（dual-signal）",
            "min_job_freq": MIN_JOB_FREQ,
            "min_norm_entropy": MIN_NORM_ENTROPY,
            "min_distinct_major": MIN_DISTINCT_MAJOR,
            "max_salience_to_block": MAX_SALIENCE_TO_BLOCK,
        },
        "downstream_effect": {
            "skill_kind": "step6_graph_export → nodes.csv（B 無需改碼）",
            "blacklist": (
                "fixtures/soft_skill_blacklist_v0.3.csv → step5 → "
                "CO_OCCURS / CORE_SKILL / global_job_frequency → step8"
            ),
            "ablation_off": (
                "use_llm_classification=false：改指定 --path v0.2（規則法）或空黑名單，"
                "重跑 step5→6→8 即為對照組"
            ),
        },
        "known_limitations": [
            "分類準確率需人工標註才能量化：--make-eval-set 後 --eval",
            "Gate 1 bake-off 量的是抽取任務，指標不可直接轉用於分類任務",
            "未回覆的技能保留 step3 預設 technical（保守，不封鎖）",
            "dictionary_version 未升版以避免與 extractions.jsonl 內嵌版本不一致；"
            "分類版本記於 skill_kind_version",
            "credential_dictionary 未分類（證照走 2A 專業證照路徑）",
        ],
    }
    MANIFEST_OUT.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"  wrote {MANIFEST_OUT}")

    print(f"\n  agreement with rule-based A5: {dict(agree)}")
    print("\n  top blocked by LLM classification:")
    for r in blocked[:12]:
        print(f"    {r['job_frequency']:>7,}  {r['canonical_id']}  "
              f"[{r['skill_kind']}] {r['llm_reason']}")
    print("\n✓ A6 complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
