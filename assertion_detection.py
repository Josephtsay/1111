"""
Assertion / requirement-level detector for unstructured JD text.
Role: A (Content Graph) — shared by Step 2B / 2C

Playbook §2C + requirement_level 判定建議：
- assertion_status: affirmed / negated / uncertain
- requirement_level: required / preferred / unspecified

規則版 MVP：看 mention 左側（與少量右側）窗口的語氣 cue。
無法可靠判斷的語意混淆（蟒蛇 Python、爪哇島 Java）會落在 affirmed／unspecified，
屬已知限制，由 challenge set 的 confusion 類別揭露。
"""

from __future__ import annotations

import re
from typing import Any

# 否定（距離 mention 越近權重越高；窗口內命中即 negated）
NEGATION_CUES: list[re.Pattern[str]] = [
    re.compile(p)
    for p in [
        r"不需要",
        r"不需",
        r"無需",
        r"無須",
        r"不用",
        r"不要",
        r"並非必須",
        r"非必須",
        r"沒有要求",
        r"無要求",
    ]
]

# 不確定 / 弱條件
UNCERTAIN_CUES: list[re.Pattern[str]] = [
    re.compile(p)
    for p in [
        r"可能接觸",
        r"可能",
        r"或許",
        r"也許",
        r"偶爾",
        r"視情況",
    ]
]

# preferred
PREFERRED_CUES: list[re.Pattern[str]] = [
    re.compile(p)
    for p in [
        r"尤佳",
        r"者佳",
        r"為佳",
        r"加分",
        r"優先錄取",
        r"優先面試",
        r"優先考慮",
        r"優先",
        r"歡迎",
        r"若熟悉",
        r"如熟悉",
        r"有.{0,8}經驗佳",
        r"佳",
    ]
]

# required
REQUIRED_CUES: list[re.Pattern[str]] = [
    re.compile(p)
    for p in [
        r"必備",
        r"必須具備",
        r"必須",
        r"需具備",
        r"須具備",
        r"需熟悉",
        r"須熟悉",
        r"具備",
        r"條件[：:]",
    ]
]

# 混淆語境（保守標 uncertain；覆蓋率低，供 challenge 揭露）
CONFUSION_CUES: list[re.Pattern[str]] = [
    re.compile(p)
    for p in [
        r"飼養",
        r"照護",
        r"島嶼",
        r"旅遊",
        r"行程",
    ]
]


def _find_mention_span(context: str, mention: str) -> tuple[int, int] | None:
    if not context or not mention:
        return None
    idx = context.casefold().find(mention.casefold())
    if idx < 0:
        # 容忍空白差異
        compact_ctx = re.sub(r"\s+", "", context.casefold())
        compact_men = re.sub(r"\s+", "", mention.casefold())
        j = compact_ctx.find(compact_men)
        if j < 0:
            return None
        # fallback：用原始 casefold find 的近似；若失敗則整段當窗口
        return 0, len(context)
    return idx, idx + len(mention)


def _window(context: str, start: int, end: int, left: int = 24, right: int = 12) -> str:
    return context[max(0, start - left) : min(len(context), end + right)]


def _any_cue(text: str, cues: list[re.Pattern[str]]) -> re.Pattern[str] | None:
    for cue in cues:
        if cue.search(text):
            return cue
    return None


def detect_assertion_and_requirement(
    context: str,
    mention: str,
    *,
    source_field: str = "",
    default_assertion: str = "affirmed",
    default_requirement: str = "unspecified",
) -> dict[str, Any]:
    """
    Returns:
      assertion_status, requirement_level, cues_fired, detector_version

    窗口策略：
    - 否定：左偏短窗口（避免把後文「但需…」誤套到前文否定技能）
    - 其他語氣：較大窗口／全文（「Windows server…優先錄取」cue 常在句尾）
    """
    span = _find_mention_span(context, mention)
    if span is None:
        left_win = context
        wide_win = context
        used_full = True
    else:
        left_win = _window(context, span[0], span[1], left=28, right=8)
        wide_win = _window(context, span[0], span[1], left=40, right=80)
        # 附加條件常把「優先／尤佳」放句尾，必要時升到全句
        if source_field == "附加條件" and len(context) <= 200:
            wide_win = context
        used_full = False

    cues: list[str] = []
    assertion = default_assertion
    requirement = default_requirement

    neg = _any_cue(left_win, NEGATION_CUES)
    if neg:
        assertion = "negated"
        requirement = "unspecified"
        cues.append(f"negation:{neg.pattern}")
        return {
            "assertion_status": assertion,
            "requirement_level": requirement,
            "cues_fired": cues,
            "detector_version": "rules_v0.1",
            "window": left_win,
            "used_full_context": used_full,
        }

    conf = _any_cue(wide_win, CONFUSION_CUES)
    # 職稱「辦公室*」中的 Office 字樣不應當成 MS Office
    title_office = (
        source_field == "職務名稱"
        and mention.casefold().strip() == "office"
        and ("辦公" in context or "主任" in context or "經理" in context)
    )
    if title_office or (
        conf and source_field in {"職務內容", "附加條件", "職務名稱"}
        and re.fullmatch(r"[A-Za-z.+#\-/ ]{1,20}", mention.strip())
    ):
        assertion = "uncertain"
        requirement = "unspecified"
        cues.append(
            "confusion:title_office" if title_office else f"confusion:{conf.pattern}"
        )
        return {
            "assertion_status": assertion,
            "requirement_level": requirement,
            "cues_fired": cues,
            "detector_version": "rules_v0.1",
            "window": wide_win,
            "used_full_context": used_full,
        }

    unc = _any_cue(wide_win, UNCERTAIN_CUES)
    if unc:
        assertion = "uncertain"
        requirement = "unspecified"
        cues.append(f"uncertain:{unc.pattern}")
        return {
            "assertion_status": assertion,
            "requirement_level": requirement,
            "cues_fired": cues,
            "detector_version": "rules_v0.1",
            "window": wide_win,
            "used_full_context": used_full,
        }

    # requirement cues（只在非 negated/uncertain 時；用寬窗口）
    pref = _any_cue(wide_win, PREFERRED_CUES)
    req = _any_cue(wide_win, REQUIRED_CUES)
    if pref:
        requirement = "preferred"
        cues.append(f"preferred:{pref.pattern}")
    elif req:
        requirement = "required"
        cues.append(f"required:{req.pattern}")

    return {
        "assertion_status": assertion,
        "requirement_level": requirement,
        "cues_fired": cues,
        "detector_version": "rules_v0.1",
        "window": wide_win,
        "used_full_context": used_full,
    }


def annotate_mention(
    mention: dict[str, Any],
    context: str,
    *,
    source_field: str = "",
) -> dict[str, Any]:
    """In-place annotate a §3.4 mention dict; returns the same object."""
    raw = mention.get("raw_mention") or mention.get("evidence") or ""
    result = detect_assertion_and_requirement(
        context,
        raw,
        source_field=source_field or mention.get("source_field", ""),
    )
    mention["assertion_status"] = result["assertion_status"]
    mention["requirement_level"] = result["requirement_level"]
    return mention
