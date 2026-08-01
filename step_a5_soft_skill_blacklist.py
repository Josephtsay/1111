"""
A5 — 泛用軟技能黑名單（Step 5 statistical_eligible 的排除集）
Role: A (Content Graph)
Playbook: §Step 3 決策「泛用技能（溝通、認真負責）不進圖」
          §Step 5 statistical_eligible「一律排除：已決的泛用軟技能黑名單」
          §Step 5 決策 #14「Excel / Office 保留 query 能力，依 DF/IDF 降權，不一律刪除」

本腳本不改動 Schema、不改 statistical_eligible 判斷式、不改抽取契約；
只是把 frozen policy 已經引用的「泛用軟技能黑名單」這份輸入用全量資料填出來。

為什麼要重跑：
  fixtures/soft_skill_blacklist_v0.1.csv 是憑印象手寫的草案（skill:溝通 等），
  但實際 canonical_id 來自 `工作技能` / `電腦技能資料` 的欄位值，
  形如 `skill:溝通協調能力`、`skill:行政事務處理能力`。
  草案 ID 在全量 extractions 中命中 0 筆 → 等於沒有黑名單。
  本腳本改用全量 structured + phrase 抽取結果統計，產出真的會生效的黑名單。

判定原則（dual-signal，precision-first）：
  1. 詞彙訊號：canonical_name 命中版本化的泛用特質 / 非技能樣式（含技術詞負向守衛）
  2. 統計訊號：全量 job DF 足夠 + 職類分散度高（跨大類熵、distinct 大類數）
     且沒有集中在少數職類（max occupation salience 守衛）
  只有兩個訊號同時成立才 `blocked`；僅統計泛用但屬真實工具（Excel / Word）
  依 playbook #14 不封鎖，只在 audit 標 `retained_downweight` 供 B 做 IDF 降權。

輸出：
  fixtures/soft_skill_blacklist_v0.2.csv        # 只含 blocked（step5 loader 直接吃）
  graph/soft_skill_blacklist_audit_v0.2.csv     # 全候選 + 訊號 + 決策理由
  graph/soft_skill_blacklist_manifest.json      # 版本、hash、門檻、統計、限制

用法：
  python step_a5_soft_skill_blacklist.py
  python step_a5_soft_skill_blacklist.py --limit-jobs 50000   # smoke（不覆寫正式 fixtures）
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent
GRAPH_DIR = ROOT / "graph"
FIXTURES_DIR = ROOT / "fixtures"

EXTRACTIONS = GRAPH_DIR / "extractions.jsonl"
TRAIN_JOBS = GRAPH_DIR / "train_jobs.parquet"
SKILL_DICT = GRAPH_DIR / "skill_dictionary.csv"
EXTRACTION_CONFIG = GRAPH_DIR / "extraction_config.yaml"
PREV_DRAFT = FIXTURES_DIR / "soft_skill_blacklist_v0.1.csv"

BLACKLIST_VERSION = "v0.2"
POLICY_VERSION = "soft_skill_blacklist_policy_v0.2"
OUT_BLACKLIST = FIXTURES_DIR / f"soft_skill_blacklist_{BLACKLIST_VERSION}.csv"
OUT_AUDIT = GRAPH_DIR / f"soft_skill_blacklist_audit_{BLACKLIST_VERSION}.csv"
OUT_MANIFEST = GRAPH_DIR / "soft_skill_blacklist_manifest.json"

# ─────────────────────────────────────────────────────────────────────────────
# Statistical eligibility mirror（唯讀複製 step5 的判斷式，不得在此改政策）
# ─────────────────────────────────────────────────────────────────────────────
STRUCTURED_FIELDS_ALLOWING_UNSPECIFIED = {"電腦技能資料", "工作技能"}
# step5 StatisticalConfig 現行 method thresholds（extraction_config 仍為 null 時的實際生效值）
FALLBACK_THRESHOLDS = {"structured": 0.8, "phrase": 0.7, "llm": 0.7}

# ─────────────────────────────────────────────────────────────────────────────
# 決策門檻（版本化；改動需升 POLICY_VERSION 並通知 B）
# ─────────────────────────────────────────────────────────────────────────────
MIN_JOB_FREQ = 50             # 低於此 DF 只列 candidate，不封鎖（統計支持不足）
MIN_NORM_ENTROPY = 0.55       # 跨職務大類分散度（normalized Shannon entropy）
MIN_DISTINCT_MAJOR = 8        # 或：出現在 >= 8 個職務大類
MAX_SALIENCE_TO_BLOCK = 0.50  # 在某職類佔比極高 → 可能是該職類真實核心能力，不自動封鎖
MIN_OCC_JOBS_FOR_SALIENCE = 200   # salience 分母下限，避免小樣本 1.0
# 詞彙主導度：泛用特質必須「構成」該技能名稱，而不只是出現在其中
# 例：「具備溝通協調能力」→ core=溝通協調，ratio=1.0 → 封鎖
#     「專案整合技術與溝通管理」→ core 中溝通只佔 0.2 → 不封鎖（是真實專案管理技能）
SOFT_DOMINANCE_MIN_RATIO = 0.40
# 統計泛用但保留（supernode watch，交給 B 做 IDF 降權，不封鎖）
# 注意：只看全域 DF + 跨職類分散度。
# 不加 salience 上限——Excel 在「會計」類 salience 0.61 不代表它沒有全域 supernode 風險，
# 加了上限反而會把 Excel / Word 這些 playbook 明確點名的對象漏掉。
RETAIN_MIN_DF_RATE = 0.02
RETAIN_MIN_NORM_ENTROPY = 0.75

# ─────────────────────────────────────────────────────────────────────────────
# 詞彙政策：泛用特質 / 非技能（reason_code, pattern, 說明）
# 命中後仍須通過統計訊號與 TECHNICAL_GUARDS 才會 blocked
# ─────────────────────────────────────────────────────────────────────────────
SOFT_TRAIT_PATTERNS: list[tuple[str, str, str]] = [
    ("communication",
     r"溝通|協調能力|表達能力|傾聽|人際關係|人際互動|親和力|口條",
     "溝通表達類泛用特質"),
    ("teamwork",
     r"團隊合作|團隊精神|團隊意識|協同合作|合作精神|配合度|團體合作|團隊協作",
     "團隊合作類泛用特質"),
    ("attitude",
     r"認真|負責態度|責任感|敬業|積極|主動性|主動積極|積極主動|工作態度|服務態度|"
     r"正直|誠實|誠信|品德|樂觀|熱情|熱忱|熱誠|穩定性|忠誠|守時|勤勞|吃苦|耐勞|"
     r"細心|謹慎|耐心|謙虛|上進",
     "工作態度／人格特質"),
    ("stress",
     r"抗壓|承壓|壓力承受|情緒管理|情緒穩定|EQ",
     "抗壓／情緒類泛用特質"),
    ("learning",
     r"學習能力|學習意願|樂於學習|願意學習|自我學習|自主學習|快速學習|學習力|學習態度",
     "學習意願類泛用特質"),
    ("adaptability",
     r"適應能力|應變能力|彈性配合|靈活度|靈活性|多工處理|時間管理|自我管理",
     "適應／自我管理類泛用特質"),
    ("independence",
     r"獨立作業|獨立思考|自動自發|自律",
     "獨立作業類泛用特質"),
    ("generic_ability",
     r"^(工作)?態度$|^人格特質$|^軟實力$|^基本能力$|^一般能力$|^其他能力$|^其他$",
     "無資訊量的泛用能力標籤"),
    ("non_skill",
     r"^(無|無需|不限|不拘|皆可|免經驗|經驗不拘|無特殊要求|依公司規定|面議|意者|"
     r"N/?A|NULL|None|nan|-{1,3})$|^具?相關(工作)?經驗$",
     "非技能值／填表雜訊"),
]

# 填充詞：計算主導度前先移除，避免「具備…能力」稀釋比例
FILLER_TOKENS = re.compile(
    r"具備|具有|良好|優良|優秀|傑出|高度|極佳|超強|很強|佳|強|"
    r"能力|技巧|素養|意識|精神|態度|之|的|與|及|和|且|等|"
    r"、|，|,|。|；|;|／|/|\||\(|\)|（|）|\[|\]|:|：|\s"
)

# 負向守衛：命中這些技術語彙就不視為泛用特質（避免誤殺真技能）
TECHNICAL_GUARDS = re.compile(
    r"機器學習|深度學習|強化學習|機械學習|學習演算法|e-?learning|數位學習|教學設計|課程設計|"
    r"壓力測試|水壓|油壓|液壓|氣壓|風壓|血壓|壓力容器|壓力校驗|胎壓|壓力錶|壓力表|"
    r"表達式|正規表達|溝通協議|通訊協定|協調控制|"
    r"護理|照護|急救|藥物|檢驗|會計|稽核|報表|程式|軟體|系統|設備|機台|模具|"
    r"焊接|配線|電路|圖面|製圖|CAD|Excel|Word|PowerPoint|SQL|Python|Java",
    re.IGNORECASE,
)


def _sha256_file(path: Path) -> str | None:
    if not path.exists():
        return None
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _norm_name(name: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", name))


def _soft_dominance_ratio(name: str) -> float:
    """
    泛用特質樣式覆蓋了名稱中多少「有實義字元」。

    分子 = 被 SOFT_TRAIT_PATTERNS 命中且非填充詞的字元數
    分母 = 名稱中非填充詞的字元數
    在原字串上比對（不先刪填充詞），否則「學習能力」會因 能力 被刪而失配。
    """
    if not name:
        return 0.0
    covered: set[int] = set()
    for _, pattern, _ in SOFT_TRAIT_PATTERNS:
        for m in re.finditer(pattern, name, re.IGNORECASE):
            covered.update(range(m.start(), m.end()))
    filler: set[int] = set()
    for m in FILLER_TOKENS.finditer(name):
        filler.update(range(m.start(), m.end()))
    denom = len(name) - len(filler)
    if denom <= 0:
        return 1.0 if covered else 0.0
    return len(covered - filler) / denom


def classify_lexical(canonical_name: str) -> tuple[str, str, str, float]:
    """
    回傳 (reason_code, 說明, guard_note, soft_dominance_ratio)。
    reason_code == "" 表示不視為泛用特質；被守衛或主導度不足時 guard_note 會說明原因。
    """
    name = _norm_name(canonical_name)
    if not name:
        return "", "", "", 0.0

    hit_code = hit_desc = ""
    for code, pattern, desc in SOFT_TRAIT_PATTERNS:
        if re.search(pattern, name, re.IGNORECASE):
            hit_code, hit_desc = code, desc
            break
    if not hit_code:
        return "", "", "", 0.0

    if TECHNICAL_GUARDS.search(name) and hit_code != "non_skill":
        return "", "", f"lexical_hit_{hit_code}_but_technical_guard", 0.0

    # non_skill 樣式本身是整串錨定（^...$），不需主導度判斷
    if hit_code == "non_skill":
        return hit_code, hit_desc, "", 1.0

    ratio = round(_soft_dominance_ratio(name), 4)
    if ratio < SOFT_DOMINANCE_MIN_RATIO:
        return (
            "",
            "",
            f"lexical_hit_{hit_code}_but_soft_dominance_{ratio}<"
            f"{SOFT_DOMINANCE_MIN_RATIO}（複合領域技能，僅包含軟技能字詞）",
            ratio,
        )
    return hit_code, hit_desc, "", ratio


def load_method_thresholds(path: Path = EXTRACTION_CONFIG) -> tuple[dict[str, float], str]:
    """讀 extraction_config.yaml 的 method thresholds；null 時退回 step5 現行預設。"""
    thresholds = dict(FALLBACK_THRESHOLDS)
    source = "fallback_step5_defaults"
    try:
        import yaml  # type: ignore

        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        raw = cfg.get("thresholds") or {}
        used_config = False
        for method in ("structured", "phrase", "llm"):
            val = (raw.get(method) or {}).get("accept_threshold")
            if isinstance(val, (int, float)):
                thresholds[method] = float(val)
                used_config = True
        if used_config:
            source = (
                f"extraction_config.yaml:{cfg.get('version')} "
                "(null 欄位沿用 step5 預設)"
            )
    except Exception as exc:  # pragma: no cover - config optional
        source = f"fallback_step5_defaults (config read failed: {exc})"
    return thresholds, source


def is_statistically_eligible(mention: dict, thresholds: dict[str, float]) -> bool:
    """唯讀鏡像 step5_statistical_edges.is_statistically_eligible（不含黑名單本身）。"""
    if mention.get("assertion_status") != "affirmed":
        return False
    if mention.get("canonicalization_status", "accepted") != "accepted":
        return False
    method = mention.get("method", "phrase")
    if (mention.get("confidence") or 0.0) < thresholds.get(method, thresholds["phrase"]):
        return False
    req = mention.get("requirement_level", "unspecified")
    if req in ("required", "preferred"):
        pass
    elif req == "unspecified":
        if mention.get("source_field", "") not in STRUCTURED_FIELDS_ALLOWING_UNSPECIFIED:
            return False
    else:
        return False
    cid = mention.get("canonical_id") or mention.get("canonical_candidate", "")
    return not cid.startswith("credential:")


# ─────────────────────────────────────────────────────────────────────────────
# Occupation substrate
# ─────────────────────────────────────────────────────────────────────────────


def load_occupation_map() -> dict[str, Any]:
    """job_id → row index，並回傳 occ_code / duty_major 的 category 表與計數。"""
    import numpy as np
    import pandas as pd

    df = pd.read_parquet(
        TRAIN_JOBS, columns=["job_id", "occupation_code", "duty_major"]
    )
    job_ids = df["job_id"].astype(str).tolist()
    occ_cat = pd.Categorical(df["occupation_code"].fillna("").astype(str))
    major_cat = pd.Categorical(df["duty_major"].fillna("").astype(str))

    occ_codes = np.asarray(occ_cat.codes, dtype="int32")
    major_codes = np.asarray(major_cat.codes, dtype="int32")

    occ_job_counts: Counter = Counter()
    codes, counts = np.unique(occ_codes, return_counts=True)
    for c, n in zip(codes.tolist(), counts.tolist()):
        occ_job_counts[int(c)] = int(n)

    major_job_counts: Counter = Counter()
    codes, counts = np.unique(major_codes, return_counts=True)
    for c, n in zip(codes.tolist(), counts.tolist()):
        major_job_counts[int(c)] = int(n)

    return {
        "job_index": {jid: i for i, jid in enumerate(job_ids)},
        "occ_categories": [str(x) for x in occ_cat.categories],
        "major_categories": [str(x) for x in major_cat.categories],
        "occ_codes": occ_codes,
        "major_codes": major_codes,
        "occ_job_counts": occ_job_counts,
        "major_job_counts": major_job_counts,
    }


def load_skill_names() -> dict[str, str]:
    """canonical_id → canonical_name（來自 step3 registry）。"""
    names: dict[str, str] = {}
    if not SKILL_DICT.exists():
        return names
    with SKILL_DICT.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            key = (row.get("registry_key") or "").strip()
            if key:
                names[f"skill:{key}"] = (row.get("canonical_name") or key).strip()
    return names


# ─────────────────────────────────────────────────────────────────────────────
# Aggregation
# ─────────────────────────────────────────────────────────────────────────────


class SkillStats:
    __slots__ = (
        "job_freq", "mention_count", "eligible_job_freq", "source_fields", "methods",
        "requirements", "assertions", "occ_counts", "major_counts", "samples",
        "raw_names",
    )

    def __init__(self) -> None:
        self.job_freq = 0
        self.mention_count = 0
        self.eligible_job_freq = 0
        self.source_fields: Counter = Counter()
        self.methods: Counter = Counter()
        self.requirements: Counter = Counter()
        self.assertions: Counter = Counter()
        self.occ_counts: Counter = Counter()
        self.major_counts: Counter = Counter()
        self.samples: list[str] = []
        self.raw_names: Counter = Counter()


def aggregate(
    *,
    limit_jobs: int | None,
    thresholds: dict[str, float],
    occ: dict[str, Any],
) -> tuple[dict[str, SkillStats], dict[str, int]]:
    stats: dict[str, SkillStats] = defaultdict(SkillStats)
    counters = {
        "jobs_scanned": 0,
        "jobs_with_skill_mentions": 0,
        "skill_mentions": 0,
        "eligible_skill_mentions": 0,
        "credential_mentions_skipped": 0,
        "eligible_jobs_missing_occupation": 0,
        "jobs_not_in_parquet": 0,
    }
    job_index = occ["job_index"]
    occ_codes = occ["occ_codes"]
    major_codes = occ["major_codes"]
    empty_marker = '"skills": [], "credentials": []'

    with EXTRACTIONS.open("r", encoding="utf-8") as f:
        for line in f:
            if limit_jobs is not None and counters["jobs_scanned"] >= limit_jobs:
                break
            counters["jobs_scanned"] += 1
            if empty_marker in line:
                continue
            rec = json.loads(line)
            skills = rec.get("skills") or []
            if not skills:
                continue
            job_id = str(rec.get("job_id"))
            counters["jobs_with_skill_mentions"] += 1

            idx = job_index.get(job_id)
            if idx is None:
                counters["jobs_not_in_parquet"] += 1
                occ_c = major_c = -1
            else:
                occ_c = int(occ_codes[idx])
                major_c = int(major_codes[idx])

            seen: set[str] = set()
            seen_eligible: set[str] = set()
            for m in skills:
                cid = m.get("canonical_id") or m.get("canonical_candidate") or ""
                if not cid:
                    continue
                if cid.startswith("credential:"):
                    counters["credential_mentions_skipped"] += 1
                    continue
                st = stats[cid]
                st.mention_count += 1
                counters["skill_mentions"] += 1
                st.source_fields[m.get("source_field", "")] += 1
                st.methods[m.get("method", "")] += 1
                st.requirements[m.get("requirement_level", "")] += 1
                st.assertions[m.get("assertion_status", "")] += 1
                if cid not in seen:
                    seen.add(cid)
                    st.job_freq += 1
                    raw = (m.get("raw_mention") or "").strip()
                    if raw:
                        st.raw_names[raw] += 1
                    if len(st.samples) < 3:
                        ev = (m.get("evidence") or "").replace("\n", " ")[:60]
                        st.samples.append(f"{job_id}|{m.get('source_field', '')}|{ev}")
                if is_statistically_eligible(m, thresholds):
                    counters["eligible_skill_mentions"] += 1
                    if cid not in seen_eligible:
                        seen_eligible.add(cid)
                        st.eligible_job_freq += 1
                        if occ_c >= 0:
                            st.occ_counts[occ_c] += 1
                            st.major_counts[major_c] += 1
                        else:
                            counters["eligible_jobs_missing_occupation"] += 1
    return stats, counters


def normalized_entropy(counts: Counter, universe_size: int) -> float:
    total = sum(counts.values())
    if total <= 0 or universe_size <= 1:
        return 0.0
    h = -sum((c / total) * math.log(c / total) for c in counts.values() if c > 0)
    return min(h / math.log(universe_size), 1.0)


def build_rows(
    stats: dict[str, SkillStats],
    *,
    total_jobs: int,
    skill_names: dict[str, str],
    occ: dict[str, Any],
    major_universe: int,
) -> list[dict[str, Any]]:
    occ_categories = occ["occ_categories"]
    major_categories = occ["major_categories"]
    occ_job_counts = occ["occ_job_counts"]

    rows: list[dict[str, Any]] = []
    for cid, st in stats.items():
        name = skill_names.get(cid)
        if not name:
            name = (
                st.raw_names.most_common(1)[0][0]
                if st.raw_names
                else cid.split(":", 1)[-1].replace("_", " ")
            )

        max_sal = 0.0
        max_sal_occ = ""
        for occ_c, n in st.occ_counts.items():
            denom = occ_job_counts.get(occ_c, 0)
            if denom < MIN_OCC_JOBS_FOR_SALIENCE:
                continue
            rate = n / denom
            if rate > max_sal:
                max_sal = rate
                max_sal_occ = (
                    occ_categories[occ_c] if 0 <= occ_c < len(occ_categories) else ""
                )

        top_major = ""
        if st.major_counts:
            c = st.major_counts.most_common(1)[0][0]
            top_major = major_categories[c] if 0 <= c < len(major_categories) else ""

        reason_code, reason_desc, guard_note, dominance = classify_lexical(name)

        rows.append(
            {
                "canonical_id": cid,
                "canonical_name": name,
                "job_frequency": st.job_freq,
                "eligible_job_frequency": st.eligible_job_freq,
                "mention_count": st.mention_count,
                "df_rate": st.job_freq / total_jobs if total_jobs else 0.0,
                "distinct_occupation_minor": len(st.occ_counts),
                "distinct_occupation_major": len(st.major_counts),
                "occupation_major_entropy_norm": round(
                    normalized_entropy(st.major_counts, major_universe), 4
                ),
                "max_occupation_salience": round(max_sal, 4),
                "max_salience_occupation": max_sal_occ,
                "top_occupation_major": top_major,
                "source_fields": ";".join(
                    f"{k}:{v}" for k, v in st.source_fields.most_common()
                ),
                "methods": ";".join(f"{k}:{v}" for k, v in st.methods.most_common()),
                "requirement_levels": ";".join(
                    f"{k}:{v}" for k, v in st.requirements.most_common()
                ),
                "assertion_statuses": ";".join(
                    f"{k}:{v}" for k, v in st.assertions.most_common()
                ),
                "lexical_reason_code": reason_code,
                "lexical_reason": reason_desc,
                "soft_dominance_ratio": dominance,
                "technical_guard_note": guard_note,
                "evidence_samples": " || ".join(st.samples),
            }
        )
    return rows


def decide(row: dict[str, Any]) -> tuple[str, str]:
    """回傳 (decision, decision_reason)。dual-signal + salience 守衛。"""
    lex = row["lexical_reason_code"]
    df = row["job_frequency"]
    ent = row["occupation_major_entropy_norm"]
    majors = row["distinct_occupation_major"]
    sal = row["max_occupation_salience"]

    if lex:
        if df < MIN_JOB_FREQ:
            return (
                "candidate_low_support",
                f"lexical={lex} 但 job_frequency={df} < {MIN_JOB_FREQ}；"
                "統計支持不足，留 audit 不進黑名單（precision-first）",
            )
        if not (ent >= MIN_NORM_ENTROPY or majors >= MIN_DISTINCT_MAJOR):
            return (
                "candidate_concentrated",
                f"lexical={lex} 但職類分散度不足（entropy={ent} < {MIN_NORM_ENTROPY} "
                f"且 majors={majors} < {MIN_DISTINCT_MAJOR}）；可能是特定職類語彙，需人工判斷",
            )
        if sal > MAX_SALIENCE_TO_BLOCK:
            return (
                "review_high_salience",
                f"lexical={lex} 且分散，但在 {row['max_salience_occupation']} 佔比 "
                f"{sal} > {MAX_SALIENCE_TO_BLOCK}，可能是該職類真實核心能力；需雙人審核",
            )
        return (
            "blocked",
            f"dual-signal: lexical={lex}; DF={df}>={MIN_JOB_FREQ}; "
            f"major_entropy={ent}; distinct_major={majors}; max_salience={sal}",
        )

    if row["df_rate"] >= RETAIN_MIN_DF_RATE and ent >= RETAIN_MIN_NORM_ENTROPY:
        return (
            "retained_downweight",
            f"統計泛用 supernode 風險（df_rate={row['df_rate']:.4f}, entropy={ent}, "
            f"distinct_major={majors}, max_salience={sal}@{row['max_salience_occupation']}）"
            "但屬真實工具／能力；依 playbook 決策 #14 保留 query 能力，"
            "由 B 做 IDF／職類 salience 降權，不封鎖",
        )

    if row["technical_guard_note"]:
        return (
            "candidate_guarded_review",
            f"{row['technical_guard_note']}；audit-only，未進黑名單，"
            "供 Gate 2 雙人複核是否為真實領域技能",
        )
    return "not_flagged", ""


def main() -> int:
    ap = argparse.ArgumentParser(
        description="A5: build data-derived soft-skill blacklist from full extractions"
    )
    ap.add_argument("--limit-jobs", type=int, default=None, help="只掃前 N 筆（smoke）")
    ap.add_argument(
        "--audit-top",
        type=int,
        default=1500,
        help="audit CSV 額外保留的 not_flagged 高 DF 筆數（供人工複核）",
    )
    args = ap.parse_args()
    smoke = args.limit_jobs is not None

    print("=" * 70)
    print("A5 — soft skill blacklist from FULL structured/phrase extractions")
    print("=" * 70)
    for p in (EXTRACTIONS, TRAIN_JOBS):
        if not p.exists():
            print(f"  [FAIL] missing input: {p}")
            return 1

    thresholds, thr_source = load_method_thresholds()
    print(f"  method thresholds: {thresholds}")
    print(f"  threshold source:  {thr_source}")

    print("  loading occupation substrate ...")
    occ = load_occupation_map()
    major_categories = occ["major_categories"]
    major_universe = sum(
        1
        for c in occ["major_job_counts"]
        if 0 <= c < len(major_categories) and major_categories[c] != ""
    )
    print(
        f"    jobs={len(occ['job_index']):,} "
        f"occupation_codes={len(occ['occ_categories']):,} "
        f"duty_major universe={major_universe}"
    )

    skill_names = load_skill_names()
    print(f"    registry names: {len(skill_names):,}")

    print("  streaming extractions ...")
    stats, counters = aggregate(limit_jobs=args.limit_jobs, thresholds=thresholds, occ=occ)
    print(f"    jobs scanned: {counters['jobs_scanned']:,}")
    print(f"    distinct skill canonical_ids: {len(stats):,}")
    print(
        f"    skill mentions: {counters['skill_mentions']:,} "
        f"(statistically eligible {counters['eligible_skill_mentions']:,})"
    )

    rows = build_rows(
        stats,
        total_jobs=counters["jobs_scanned"],
        skill_names=skill_names,
        occ=occ,
        major_universe=max(major_universe, 2),
    )
    for r in rows:
        r["decision"], r["decision_reason"] = decide(r)

    by_decision = Counter(r["decision"] for r in rows)
    print("\n  decisions:")
    for k, v in by_decision.most_common():
        print(f"    {k}: {v:,}")

    blocked = sorted(
        (r for r in rows if r["decision"] == "blocked"),
        key=lambda r: -r["job_frequency"],
    )

    # ── fixtures blacklist（step5 loader：status != "rejected" 即視為排除）──────
    if smoke:
        print("\n  [smoke] --limit-jobs 指定；不覆寫正式 fixtures 黑名單")
    else:
        FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
        with OUT_BLACKLIST.open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow([
                "canonical_id", "canonical_name", "reason", "status", "category",
                "job_frequency", "eligible_job_frequency", "distinct_occupation_major",
                "occupation_major_entropy_norm", "max_occupation_salience",
                "soft_dominance_ratio", "source_fields", "policy_version", "notes",
            ])
            for r in blocked:
                w.writerow([
                    r["canonical_id"], r["canonical_name"], "generic_soft_skill",
                    "blocked", r["lexical_reason_code"], r["job_frequency"],
                    r["eligible_job_frequency"], r["distinct_occupation_major"],
                    r["occupation_major_entropy_norm"], r["max_occupation_salience"],
                    r["soft_dominance_ratio"], r["source_fields"], POLICY_VERSION,
                    r["decision_reason"],
                ])
        print(f"  wrote {OUT_BLACKLIST} ({len(blocked)} blocked)")

    # ── audit（所有 flagged + 高 DF not_flagged 供複核）─────────────────────────
    audit_rows = [r for r in rows if r["decision"] != "not_flagged"]
    audit_rows.extend(
        sorted(
            (r for r in rows if r["decision"] == "not_flagged"),
            key=lambda r: -r["job_frequency"],
        )[: args.audit_top]
    )
    audit_rows.sort(key=lambda r: (r["decision"] != "blocked", -r["job_frequency"]))

    GRAPH_DIR.mkdir(parents=True, exist_ok=True)
    audit_path = (
        OUT_AUDIT if not smoke else OUT_AUDIT.with_name(OUT_AUDIT.stem + "_smoke.csv")
    )
    fields = [
        "decision", "decision_reason", "canonical_id", "canonical_name",
        "lexical_reason_code", "lexical_reason", "soft_dominance_ratio",
        "technical_guard_note",
        "job_frequency", "eligible_job_frequency", "mention_count", "df_rate",
        "distinct_occupation_minor", "distinct_occupation_major",
        "occupation_major_entropy_norm", "max_occupation_salience",
        "max_salience_occupation", "top_occupation_major", "source_fields",
        "methods", "requirement_levels", "assertion_statuses", "evidence_samples",
    ]
    with audit_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in audit_rows:
            w.writerow(r)
    print(f"  wrote {audit_path} ({len(audit_rows)} rows)")

    # ── 舊草案有效性檢查（證明 v0.1 是 no-op）─────────────────────────────────
    prev_ids: list[str] = []
    if PREV_DRAFT.exists():
        with PREV_DRAFT.open("r", encoding="utf-8") as f:
            prev_ids = [
                (row.get("canonical_id") or "").strip()
                for row in csv.DictReader(f)
                if (row.get("canonical_id") or "").strip()
            ]
    prev_hits = {cid: stats[cid].job_freq for cid in prev_ids if cid in stats}

    retained = sorted(
        (r for r in rows if r["decision"] == "retained_downweight"),
        key=lambda r: -r["job_frequency"],
    )

    manifest = {
        "step": "step_a5_soft_skill_blacklist",
        "blacklist_version": BLACKLIST_VERSION,
        "policy_version": POLICY_VERSION,
        "schema_version": "v0.1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "smoke_run": smoke,
        "role": "A (Content Graph) — Step 5 statistical_eligible 排除集",
        "scope_note": (
            "只填充 frozen policy 已引用的黑名單輸入；未改 Schema、"
            "未改 statistical_eligible 判斷式、未改抽取契約"
        ),
        "inputs": {
            "extractions": str(EXTRACTIONS),
            "extractions_sha256": _sha256_file(EXTRACTIONS),
            "train_jobs": str(TRAIN_JOBS),
            "train_jobs_sha256": _sha256_file(TRAIN_JOBS),
            "skill_dictionary_sha256": _sha256_file(SKILL_DICT),
            "extraction_config_sha256": _sha256_file(EXTRACTION_CONFIG),
        },
        "eligibility_mirror": {
            "policy": "step5_statistical_edges.is_statistically_eligible (read-only mirror)",
            "method_thresholds": thresholds,
            "threshold_source": thr_source,
            "structured_fields_allowing_unspecified": sorted(
                STRUCTURED_FIELDS_ALLOWING_UNSPECIFIED
            ),
        },
        "decision_thresholds": {
            "min_job_freq": MIN_JOB_FREQ,
            "min_norm_entropy": MIN_NORM_ENTROPY,
            "min_distinct_major": MIN_DISTINCT_MAJOR,
            "max_salience_to_block": MAX_SALIENCE_TO_BLOCK,
            "min_occ_jobs_for_salience": MIN_OCC_JOBS_FOR_SALIENCE,
            "soft_dominance_min_ratio": SOFT_DOMINANCE_MIN_RATIO,
            "retain_min_df_rate": RETAIN_MIN_DF_RATE,
            "retain_min_norm_entropy": RETAIN_MIN_NORM_ENTROPY,
            "retain_rule_note": (
                "supernode watch 只用 DF + 跨職類分散度；不設 salience 上限，"
                "否則 Excel（會計類 salience 0.61）會被漏掉"
            ),
        },
        "lexical_policy": {
            "categories": [c for c, _, _ in SOFT_TRAIT_PATTERNS],
            "technical_guard_pattern_sha256": hashlib.sha256(
                TECHNICAL_GUARDS.pattern.encode("utf-8")
            ).hexdigest(),
        },
        "statistics": {
            **counters,
            "distinct_skill_canonical_ids": len(stats),
            "decisions": dict(by_decision),
            "blocked_job_frequency_sum": sum(r["job_frequency"] for r in blocked),
            "blocked_eligible_job_frequency_sum": sum(
                r["eligible_job_frequency"] for r in blocked
            ),
        },
        "outputs": {
            "blacklist_csv": str(OUT_BLACKLIST) if not smoke else None,
            "blacklist_sha256": _sha256_file(OUT_BLACKLIST) if not smoke else None,
            "audit_csv": str(audit_path),
        },
        "supersedes": {
            "path": str(PREV_DRAFT),
            "sha256": _sha256_file(PREV_DRAFT),
            "entries": len(prev_ids),
            "entries_matching_real_canonical_ids": len(prev_hits),
            "matched_detail": prev_hits,
            "finding": (
                "v0.1 草案為手寫詞（skill:溝通 等），與實際 canonical_id"
                "（來自 工作技能／電腦技能資料 欄位值，例：skill:溝通協調能力）不同，"
                "在全量 extractions 中命中 0 筆 → 對 Step 5 統計完全無效果（no-op）。"
            ),
        },
        "blocked_top20": [
            {
                "canonical_id": r["canonical_id"],
                "canonical_name": r["canonical_name"],
                "job_frequency": r["job_frequency"],
                "eligible_job_frequency": r["eligible_job_frequency"],
                "distinct_occupation_major": r["distinct_occupation_major"],
                "occupation_major_entropy_norm": r["occupation_major_entropy_norm"],
                "max_occupation_salience": r["max_occupation_salience"],
                "lexical_reason_code": r["lexical_reason_code"],
            }
            for r in blocked[:20]
        ],
        "retained_downweight_top20": [
            {
                "canonical_id": r["canonical_id"],
                "canonical_name": r["canonical_name"],
                "job_frequency": r["job_frequency"],
                "df_rate": round(r["df_rate"], 5),
                "occupation_major_entropy_norm": r["occupation_major_entropy_norm"],
                "max_occupation_salience": r["max_occupation_salience"],
            }
            for r in retained[:20]
        ],
        "known_limitations": [
            "詞彙政策為人工策劃的中文泛用特質樣式；英文或 OOV 軟技能可能漏抓（列於 audit 供補）",
            "統計訊號只涵蓋 structured + phrase 抽取；LLM 抽取尚未納入（use_llm_extraction=false），"
            "LLM 全量後需重跑本步驟並升版",
            f"max_occupation_salience 僅在 occupation job_count >= {MIN_OCC_JOBS_FOR_SALIENCE} "
            "的職類上計算，小樣本職類不列入守衛",
            "candidate_low_support / candidate_concentrated / review_high_salience 皆未進黑名單，"
            "需 A/B 雙人審核後才可升入 v0.3",
            "本檔只影響 Step 5 統計邊（CO_OCCURS_WITH / CORE_SKILL / global_job_frequency）；"
            "HAS_SKILL 仍保留 mention 與 evidence，不刪任何資料",
            "職缺語料統計，未使用任何 query／點擊／應徵資料（無 test leakage）",
        ],
        "handoff_to_b": {
            "loader_contract": "canonical_id + status（僅 status=rejected 會被忽略）",
            "default_path_note": (
                "step5_statistical_edges.load_soft_skill_blacklist 預設路徑需指向 "
                f"fixtures/soft_skill_blacklist_{BLACKLIST_VERSION}.csv；"
                "v0.1 草案為 no-op，不可繼續當預設值"
            ),
            "expected_effect": (
                "blocked skills 會從 CO_OCCURS_WITH / CORE_SKILL / global_job_frequency "
                "的 eligible set 中移除；HAS_SKILL 不受影響"
            ),
        },
    }
    OUT_MANIFEST.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"  wrote {OUT_MANIFEST}")

    print("\n  top blocked (job_frequency, id, category, entropy, salience):")
    for r in blocked[:15]:
        print(
            f"    {r['job_frequency']:>7,}  {r['canonical_id']}  "
            f"[{r['lexical_reason_code']}] ent={r['occupation_major_entropy_norm']} "
            f"sal={r['max_occupation_salience']}"
        )
    print("\n  retained_downweight (supernode watch, NOT blocked):")
    for r in retained[:10]:
        print(
            f"    {r['job_frequency']:>7,}  {r['canonical_id']}  "
            f"ent={r['occupation_major_entropy_norm']} sal={r['max_occupation_salience']}"
        )
    print(
        f"\n  v0.1 draft effectiveness: {len(prev_hits)}/{len(prev_ids)} "
        "IDs matched real extractions"
    )
    print("\n✓ A5 complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
