"""職缺文本正規化 — Task 3（enrich）與 Task 4（summarize）的共用契約。

兩個 task 必須用完全相同的邏輯，否則 `needs_summary` 的集合會對不上：
Task 3 標記為需要摘要的 job_id，和 Task 4 實際產出摘要的 job_id 必須一致，
Task 5 才能正確組出 embedding_text。

**這裡是唯一來源，不要在各自的模組裡再實作一份。**
"""

from __future__ import annotations

import html
import re

# <br> / <br/> / <BR /> 一律轉空格
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
# 其餘 HTML tag（少數職缺有 <p> <div> <span> 等）
_TAG_RE = re.compile(r"<[^>]{1,80}>")
# 所有空白（含全角空白、換行、tab）壓縮為單一半角空格
_WS_RE = re.compile(r"[\s\u3000\u00a0]+")


def normalize_content(raw: object) -> str:
    """把 CSV 的 `職務內容` 原文正規化成 pipeline 使用的 content 形態。

    步驟固定為：<br> → 空格 → 移除其餘 HTML tag → HTML entity unescape → 壓縮空白 → strip。

    非字串（NaN / None）一律回傳空字串。
    """
    if not isinstance(raw, str):
        return ""
    text = _BR_RE.sub(" ", raw)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def needs_summary(content: str, threshold: int) -> bool:
    """判定是否需要 LLM 摘要。

    Args:
        content: **已經過 normalize_content 的** content。
        threshold: 字數門檻，用 settings.summary_threshold_chars（1000）。

    長度一律以正規化後的字數計算。README 裡「>1000 字有 2 萬筆」是以原文統計的，
    正規化壓縮空白後筆數會略少，這是預期行為。
    """
    return len(content) > threshold
