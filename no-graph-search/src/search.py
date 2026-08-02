"""Task 9：Hybrid Search（BM25 + kNN）查詢層。

用法：
    .venv/bin/python -m src.search "水電"
    .venv/bin/python -m src.search "會計" --city 100107        # 區層代碼會自動升級
    .venv/bin/python -m src.search "行政" --salary-min 35000
    .venv/bin/python -m src.search --benchmark                 # 跑典型 query 比較三種模式
    .venv/bin/python -m src.search "水電" --explain-filter     # 只印出組好的 query DSL

兩個必須在查詢層處理的資料現實
-----------------------------
1. 區層代碼升級
   實測 200 萬筆搜尋 log：68.68% 的搜尋帶地區條件，其中 74.86% 是區層代碼
   （新莊區、西屯區…），只有 24.64% 是城市層。但職缺資料的「工作城市」100% 是
   城市層，完全沒有區的資訊。直接把區層代碼丟進 terms filter 會讓四分之三的
   地區搜尋回傳 0 筆。這裡一律把區層代碼升級成所屬城市代碼再篩，讓查詢優雅
   降級成城市範圍，而不是空結果。

2. 代碼髒值
   c0 有 '100900/'、'100900//////' 這種帶斜線的值（約 0.338% 查不到），
   d0 同樣問題（約 4.65%）。查表前先只保留數字。

薪資 filter
----------
salary_min_monthly 是「全時等量」換算值（時薪 ×176、日薪 ×22、年薪 ÷12），
對兼職／工讀會高估，所以只用於 filter，顯示一律用 salary_text 原文。
面議或無法換算的職缺 is_negotiable 為 true，必須放行而不是濾掉。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings  # noqa: E402
from src.create_index import (  # noqa: E402
    MIN_HYBRID_VERSION,
    SEARCH_PIPELINE_ID,
    VECTOR_FIELD,
)
from src.lookup import Lookup  # noqa: E402

# BM25 欄位權重。query 極短且多為精確職稱，所以 title 與別名權重最高。
# .cjk 子欄位是 bigram 備援，權重略低於主欄位，避免 bigram 的雜訊蓋過正確斷詞。
BM25_FIELDS = [
    "title^4",
    "title.cjk^2",
    "job_category_aliases^3",
    "job_category_aliases.cjk^1.5",
    "job_category_l3^3",
    "job_category_l2^1.5",
    "job_category_l2_alt^1.5",
    "job_category_l1^1",
    "job_category_en^2",
    "content^1",
    "content.cjk^0.5",
    "content_summary^1",
    "job_category_definition^0.8",
    "skills^1.5",
    "skills.cjk^0.8",
    "industry_l3^0.8",
    "industry_l2^0.5",
    "certificates^1",
    "conditions^0.5",
]

# 只保留數字，處理 '100900/' 這類髒值
_DIGITS_RE = re.compile(r"\D+")

# 回傳給呼叫端的欄位。embedding_text 與向量不回傳（前者 index=False，後者太大）
SOURCE_FIELDS = [
    "job_id",
    "title",
    "city",
    "district",
    "job_category_l1",
    "job_category_l2",
    "job_category_l3",
    "industry_l3",
    "salary_text",
    "salary_type",
    "salary_min",
    "salary_max",
    "salary_min_monthly",
    "is_negotiable",
    "job_type",
    "work_hours",
    "overseas",
    "education",
    "experience",
    "company_id",
    "updated_at",
    "content",
]


def clean_code(raw: str) -> str:
    """去掉非數字字元。'100900/' → '100900'，'100900//////' → '100900'。"""
    return _DIGITS_RE.sub("", str(raw or ""))


def normalize_location_codes(
    codes: list[str] | None, lookup: Lookup
) -> tuple[list[str], dict[str, str]]:
    """把使用者送來的地區代碼正規化成職缺資料實際存在的層級。

    Returns:
        (可用於 terms filter 的代碼清單, {原始代碼: 說明} 的升級紀錄)
    """
    if not codes:
        return [], {}

    resolved: dict[str, None] = {}
    notes: dict[str, str] = {}

    for raw in codes:
        code = clean_code(raw)
        if not code:
            notes[str(raw)] = "空值或非數字，已忽略"
            continue

        city = lookup.get_city_by_code(code)
        if city is None:
            notes[str(raw)] = "對照表查不到，已忽略"
            continue

        if city.level == 3:
            # 區層 → 升級到所屬城市。職缺資料沒有區，不升級就會 0 筆。
            if city.city_code:
                resolved.setdefault(city.city_code, None)
                notes[str(raw)] = f"{city.city_name}{city.name} → 升級為 {city.city_name}（{city.city_code}）"
            else:
                notes[str(raw)] = "區層但查不到所屬城市，已忽略"
        else:
            resolved.setdefault(city.code, None)
            if str(raw) != city.code:
                notes[str(raw)] = f"正規化為 {city.code}（{city.name}）"

    return list(resolved), notes


@dataclass
class Filters:
    """Hard filter 條件。全部為 None / 空表示不篩。"""

    location_codes: list[str] = field(default_factory=list)
    job_types: list[str] = field(default_factory=list)
    work_hours: list[str] = field(default_factory=list)
    salary_min: int | None = None
    exclude_overseas: bool = False
    updated_after: str | None = None

    def build(self, lookup: Lookup) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """組出 bool.filter 子句。"""
        clauses: list[dict[str, Any]] = []

        codes, notes = normalize_location_codes(self.location_codes, lookup)
        if codes:
            # location_codes 存了祖先 code，打城市層即可命中該市所有職缺
            clauses.append({"terms": {"location_codes": codes}})

        if self.job_types:
            clauses.append({"terms": {"job_type": self.job_types}})

        if self.work_hours:
            clauses.append({"terms": {"work_hours": self.work_hours}})

        if self.salary_min is not None:
            # 面議與無法換算月薪的職缺必須放行，否則會誤殺 15.5% 的職缺
            clauses.append(
                {
                    "bool": {
                        "should": [
                            {"range": {"salary_min_monthly": {"gte": self.salary_min}}},
                            {"term": {"is_negotiable": True}},
                        ],
                        "minimum_should_match": 1,
                    }
                }
            )

        if self.exclude_overseas:
            clauses.append({"term": {"overseas": False}})

        if self.updated_after:
            clauses.append({"range": {"updated_at": {"gte": self.updated_after}}})

        return clauses, notes


def bm25_query(text: str) -> dict[str, Any]:
    """短關鍵字為主，用 best_fields；多職稱 query（逗號/頓號分隔）也能拆開比對。"""
    return {
        "multi_match": {
            "query": text,
            "fields": BM25_FIELDS,
            "type": "best_fields",
            "tie_breaker": 0.3,
        }
    }


def knn_query(vector: list[float], k: int, filters: list[dict[str, Any]]) -> dict[str, Any]:
    """kNN 子查詢。filter 放在 knn 內部才會在圖搜尋階段就套用。

    k 是要取回的鄰居數。預設等於 top_k 會讓 hybrid 的候選池嚴重不對稱 ——
    BM25 那半動輒回傳數千筆，kNN 只有 top_k 筆。把 k 放大能讓向量那半在
    分數合併時更有份量，實際最佳值需要用評估指標量測。
    """
    body: dict[str, Any] = {"vector": vector, "k": k}
    if filters:
        body["filter"] = {"bool": {"filter": filters}}
    return {"knn": {VECTOR_FIELD: body}}


def build_hybrid_body(
    text: str,
    vector: list[float],
    filter_clauses: list[dict[str, Any]],
    top_k: int,
    knn_k: int | None = None,
) -> dict[str, Any]:
    """OpenSearch >= 2.10 的原生 hybrid query。

    hybrid 的兩個子查詢分數由 search pipeline 的 normalization-processor 各自
    min-max 正規化後加權合併。BM25 分數無上界、kNN 是 0~1，不正規化就直接相加
    會讓 BM25 完全主導。
    """
    subqueries: list[dict[str, Any]] = []

    bm25 = bm25_query(text)
    if filter_clauses:
        subqueries.append({"bool": {"must": [bm25], "filter": filter_clauses}})
    else:
        subqueries.append(bm25)

    subqueries.append(knn_query(vector, knn_k or top_k, filter_clauses))

    return {
        "size": top_k,
        "_source": SOURCE_FIELDS,
        "query": {"hybrid": {"queries": subqueries}},
    }


def build_bm25_body(
    text: str, filter_clauses: list[dict[str, Any]], top_k: int
) -> dict[str, Any]:
    query: dict[str, Any] = {"bool": {"must": [bm25_query(text)]}}
    if filter_clauses:
        query["bool"]["filter"] = filter_clauses
    return {"size": top_k, "_source": SOURCE_FIELDS, "query": query}


def build_knn_body(
    vector: list[float],
    filter_clauses: list[dict[str, Any]],
    top_k: int,
    knn_k: int | None = None,
) -> dict[str, Any]:
    return {
        "size": top_k,
        "_source": SOURCE_FIELDS,
        "query": knn_query(vector, knn_k or top_k, filter_clauses),
    }


# --------------------------------------------------------------------------
# 查詢執行
# --------------------------------------------------------------------------


class JobSearch:
    """封裝 embedding、filter 正規化與 hybrid 查詢。"""

    def __init__(self, *, index: str | None = None) -> None:
        from src.clients import bedrock_runtime_client, opensearch_client

        self.index = index or settings.opensearch_index
        self.lookup = Lookup.load()
        self.client = opensearch_client()
        self._bedrock = bedrock_runtime_client()
        self._version = self._detect_version()

    def _detect_version(self) -> tuple[int, ...]:
        info = self.client.info()
        return tuple(int(p) for p in info["version"]["number"].split(".")[:2])

    @property
    def supports_hybrid(self) -> bool:
        return self._version >= MIN_HYBRID_VERSION

    def embed_query(self, text: str) -> list[float]:
        """query 必須用 search_query 這個 input_type，與 document 端非對稱。"""
        from src.clients import INPUT_TYPE_QUERY, embed_texts

        return embed_texts([text], input_type=INPUT_TYPE_QUERY, client=self._bedrock)[0]

    def search(
        self,
        text: str,
        *,
        filters: Filters | None = None,
        top_k: int = 10,
        mode: str = "hybrid",
        knn_k: int | None = None,
    ) -> dict[str, Any]:
        """執行搜尋。需要向量時會即時呼叫 Bedrock。

        Args:
            mode: hybrid | bm25 | knn。後兩者用於比較與除錯。
        """
        vector = None
        if mode in ("knn", "hybrid"):
            vector = self.embed_query(text)
        return self.search_with_vector(
            text, vector, filters=filters, top_k=top_k, mode=mode, knn_k=knn_k
        )

    def search_with_vector(
        self,
        text: str,
        vector: list[float] | None,
        *,
        filters: Filters | None = None,
        top_k: int = 10,
        mode: str = "hybrid",
        knn_k: int | None = None,
    ) -> dict[str, Any]:
        """用已備好的 query 向量執行搜尋。

        參數掃描會對同一批 query 重跑數十次，每次重新 embedding 既慢又浪費
        Bedrock 配額。把向量的取得與查詢分開，呼叫端就能自行快取。
        """
        filters = filters or Filters()
        filter_clauses, notes = filters.build(self.lookup)

        params: dict[str, Any] = {}
        if mode == "bm25":
            body = build_bm25_body(text, filter_clauses, top_k)
        elif mode == "knn":
            if vector is None:
                raise ValueError("knn 模式需要 query 向量")
            body = build_knn_body(vector, filter_clauses, top_k, knn_k)
        elif mode == "hybrid":
            if not self.supports_hybrid:
                raise RuntimeError(
                    f"叢集版本 {'.'.join(map(str, self._version))} 不支援 hybrid query，"
                    f"需要 >= {'.'.join(map(str, MIN_HYBRID_VERSION))}。"
                    "請改用 --mode bm25 或 --mode knn。"
                )
            if vector is None:
                raise ValueError("hybrid 模式需要 query 向量")
            body = build_hybrid_body(text, vector, filter_clauses, top_k, knn_k)
            params["search_pipeline"] = SEARCH_PIPELINE_ID
        else:
            raise ValueError(f"mode 必須是 hybrid / bm25 / knn，收到 {mode!r}")

        response = self.client.search(index=self.index, body=body, params=params)
        return {
            "mode": mode,
            "query": text,
            "took_ms": response["took"],
            "total": response["hits"]["total"]["value"],
            "filter_notes": notes,
            "filter_clauses": filter_clauses,
            "hits": [
                {"score": hit["_score"], **hit["_source"]} for hit in response["hits"]["hits"]
            ],
        }


# --------------------------------------------------------------------------
# 輸出
# --------------------------------------------------------------------------


def print_result(result: dict[str, Any], *, show_content: bool = False) -> None:
    print(
        f"\n[{result['mode']}] 「{result['query']}」"
        f" — {result['total']:,} 筆命中，{result['took_ms']} ms"
    )
    for original, note in result["filter_notes"].items():
        print(f"  地區代碼 {original}：{note}")
    if not result["hits"]:
        print("  （無結果）")
        return

    for rank, hit in enumerate(result["hits"], start=1):
        salary = hit.get("salary_text") or "未提供"
        negotiable = "（面議）" if hit.get("is_negotiable") else ""
        print(
            f"  {rank:>2}. {hit['score']:.4f}  {hit.get('title')}"
            f"\n      {hit.get('job_category_l3') or hit.get('job_category_l2')}"
            f" ｜ {hit.get('city')}"
            f" ｜ {hit.get('job_type')}"
            f" ｜ {salary}{negotiable}"
        )
        if show_content:
            content = (hit.get("content") or "")[:100]
            print(f"      {content}…")


def benchmark(searcher: JobSearch, top_k: int) -> int:
    """用真實 query pattern 比較 BM25 / kNN / hybrid 的差異。"""
    # 取自 userSearchLog 的實際 query
    queries = [
        "水電",
        "會計",
        "美容師",
        "遠端",
        "暑期打工",
        "製程工程師",
        "在家 行政",  # 描述性 query，embedding 應該比 BM25 有優勢
        "百萬年薪",
    ]

    modes = ["bm25", "knn", "hybrid"] if searcher.supports_hybrid else ["bm25", "knn"]

    for query in queries:
        print(f"\n{'=' * 72}\nquery：「{query}」\n{'=' * 72}")
        rankings: dict[str, list[str]] = {}
        for mode in modes:
            try:
                result = searcher.search(query, top_k=top_k, mode=mode)
            except Exception as exc:  # noqa: BLE001
                print(f"  [{mode}] 失敗：{type(exc).__name__}: {exc}")
                continue
            rankings[mode] = [h["job_id"] for h in result["hits"]]
            print_result(result)

        if "bm25" in rankings and "knn" in rankings:
            shared = set(rankings["bm25"]) & set(rankings["knn"])
            print(f"\n  BM25 與 kNN 的 top-{top_k} 重疊：{len(shared)}/{top_k}")
        if "hybrid" in rankings and "bm25" in rankings:
            shared = set(rankings["hybrid"]) & set(rankings["bm25"])
            print(f"  hybrid 與 BM25 的 top-{top_k} 重疊：{len(shared)}/{top_k}")

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Task 9：職缺 hybrid search")
    parser.add_argument("query", nargs="?", help="搜尋關鍵字")
    parser.add_argument("--index", default=settings.opensearch_index)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument(
        "--mode", default="hybrid", choices=["hybrid", "bm25", "knn"]
    )
    parser.add_argument(
        "--city",
        action="append",
        default=[],
        metavar="CODE",
        help="地區代碼，可重複。區層代碼會自動升級為城市層",
    )
    parser.add_argument("--job-type", action="append", default=[], help="全職 / 兼職 / 工讀")
    parser.add_argument("--work-hours", action="append", default=[], help="日班 / 晚班 / 輪班")
    parser.add_argument("--salary-min", type=int, default=None, help="月薪等值下限（面議不篩掉）")
    parser.add_argument("--exclude-overseas", action="store_true", help="排除需外派")
    parser.add_argument("--updated-after", default=None, help="例如 2026-06-01")
    parser.add_argument("--show-content", action="store_true", help="顯示職務內容片段")
    parser.add_argument("--benchmark", action="store_true", help="跑典型 query 比較三種模式")
    parser.add_argument(
        "--explain-filter",
        action="store_true",
        help="只印出組好的 filter DSL 與地區代碼升級結果，不連線查詢",
    )
    args = parser.parse_args(argv)

    filters = Filters(
        location_codes=args.city,
        job_types=args.job_type,
        work_hours=args.work_hours,
        salary_min=args.salary_min,
        exclude_overseas=args.exclude_overseas,
        updated_after=args.updated_after,
    )

    if args.explain_filter:
        lookup = Lookup.load()
        clauses, notes = filters.build(lookup)
        print("地區代碼處理：")
        for original, note in (notes or {"(無)": "未指定地區"}).items():
            print(f"  {original}: {note}")
        print("\nfilter DSL：")
        print(json.dumps(clauses, ensure_ascii=False, indent=2))
        if args.query:
            print("\nBM25 子查詢：")
            print(json.dumps(bm25_query(args.query), ensure_ascii=False, indent=2))
        return 0

    if not args.query and not args.benchmark:
        parser.error("請提供 query，或使用 --benchmark / --explain-filter")

    searcher = JobSearch(index=args.index)
    print(f"叢集版本支援 hybrid：{searcher.supports_hybrid}")

    if args.benchmark:
        return benchmark(searcher, args.top_k)

    result = searcher.search(args.query, filters=filters, top_k=args.top_k, mode=args.mode)
    print_result(result, show_content=args.show_content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
