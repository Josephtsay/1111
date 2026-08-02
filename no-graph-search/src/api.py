"""Search API — 把 JobSearch 包成 HTTP 端點供評審線上測試。

公開端點，無需 API key。

本地測試：
    .venv/bin/uvicorn src.api:app --reload --port 8000

部署：由 src/lambda_handler.py 透過 Mangum 適配 Lambda + API Gateway。

端點：
    GET  /health          → 存活確認
    POST /search          → hybrid search（主要端點）
    GET  /search?ks=水電  → 同上，GET 方便瀏覽器測

Request body / query params 對應 userSearchLog 欄位：
    ks  : 搜尋關鍵字（必填）
    c0  : 地區代碼，逗號分隔或陣列（選填）
    d0  : 職務分類代碼，逗號分隔或陣列（選填，目前保留未使用）
    top_k        : 回傳筆數，預設 10
    mode         : hybrid / bm25 / knn，預設 hybrid
    salary_min   : 月薪等值下限（選填）
    job_types    : 職缺屬性，例如 ["全職"]（選填）
    work_hours   : 工時，例如 ["日班"]（選填）
"""

from __future__ import annotations

import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# --------------------------------------------------------------------------
# Global state (初始化一次，Lambda container 重用)
# --------------------------------------------------------------------------

_searcher = None


def get_searcher():
    global _searcher
    if _searcher is None:
        from src.search import JobSearch
        _searcher = JobSearch()
    return _searcher


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Warm up on startup (載入 lookup、建立 client)
    get_searcher()
    yield


# --------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------

app = FastAPI(
    title="1111 Job Search API",
    description="Hybrid search (BM25 + kNN) 職缺檢索端點",
    version="1.0.0",
    lifespan=lifespan,
)

# 評審從任意來源測試，全開 CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------
# Request / Response schema
# --------------------------------------------------------------------------


class SearchRequest(BaseModel):
    """對應 userSearchLog 的查詢欄位。"""

    ks: str = Field(..., description="搜尋關鍵字", examples=["水電"])
    c0: str | list[str] | None = Field(
        default=None,
        description="地區代碼，逗號分隔字串或陣列。區層代碼會自動升級為城市層",
        examples=["100221,100100"],
    )
    d0: str | list[str] | None = Field(
        default=None,
        description="職務分類代碼（保留欄位，目前未使用於 filter）",
        examples=["110102,110103"],
    )
    top_k: int = Field(default=10, ge=1, le=100, description="回傳筆數")
    mode: str = Field(default="hybrid", description="hybrid / bm25 / knn")
    salary_min: int | None = Field(default=None, description="月薪等值下限")
    job_types: list[str] | None = Field(default=None, description="職缺屬性 filter", examples=[["全職"]])
    work_hours: list[str] | None = Field(default=None, description="工時 filter", examples=[["日班"]])


class HitResponse(BaseModel):
    score: float
    job_id: str
    title: str | None = None
    city: str | None = None
    district: str | None = None
    job_category_l3: str | None = None
    job_category_l2: str | None = None
    job_category_l1: str | None = None
    industry_l3: str | None = None
    salary_text: str | None = None
    salary_type: str | None = None
    salary_min: int | None = None
    salary_max: int | None = None
    is_negotiable: bool | None = None
    job_type: str | None = None
    work_hours: list[str] | None = None
    education: str | None = None
    experience: str | None = None
    company_id: str | None = None
    updated_at: str | None = None
    content: str | None = None


class SearchResponse(BaseModel):
    mode: str
    query: str
    took_ms: int
    total: int
    filter_notes: dict[str, str]
    hits: list[HitResponse]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _parse_codes(value: str | list[str] | None) -> list[str]:
    """把逗號分隔字串或陣列統一成 list。"""
    if value is None:
        return []
    if isinstance(value, list):
        return [c.strip() for c in value if c.strip()]
    return [c.strip() for c in value.split(",") if c.strip()]


def _do_search(req: SearchRequest) -> dict[str, Any]:
    from src.search import Filters

    searcher = get_searcher()
    filters = Filters(
        location_codes=_parse_codes(req.c0),
        job_types=req.job_types or [],
        work_hours=req.work_hours or [],
        salary_min=req.salary_min,
    )
    result = searcher.search(
        req.ks,
        filters=filters,
        top_k=req.top_k,
        mode=req.mode,
    )
    return result


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/search", response_model=SearchResponse)
def search_post(req: SearchRequest):
    """POST /search — 主要查詢端點。"""
    return _do_search(req)


@app.get("/search", response_model=SearchResponse)
def search_get(
    ks: str = Query(..., description="搜尋關鍵字"),
    c0: str | None = Query(default=None, description="地區代碼，逗號分隔"),
    d0: str | None = Query(default=None, description="職務分類代碼（保留）"),
    top_k: int = Query(default=10, ge=1, le=100),
    mode: str = Query(default="hybrid"),
    salary_min: int | None = Query(default=None),
    job_types: str | None = Query(default=None, description="逗號分隔"),
    work_hours: str | None = Query(default=None, description="逗號分隔"),
):
    """GET /search?ks=水電 — 方便瀏覽器 / curl 直接測。"""
    req = SearchRequest(
        ks=ks,
        c0=c0,
        d0=d0,
        top_k=top_k,
        mode=mode,
        salary_min=salary_min,
        job_types=job_types.split(",") if job_types else None,
        work_hours=work_hours.split(",") if work_hours else None,
    )
    return _do_search(req)
