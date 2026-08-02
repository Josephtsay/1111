"""Unified Search API — routes to no-graph and graph backends.

啟動:
    cd unified_api
    uvicorn app:app --reload --port 9000

或從 repo 根目錄:
    python -m uvicorn unified_api.app:app --reload --port 9000
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from .backend_graph import GraphBackend
from .backend_no_graph import NoGraphBackend
from .schema import BackendResult, SearchRequest, SearchResponse

# 前端單檔頁面。本機與 Lambda 的相對位置一致（repo 根目錄 / var/task）。
_INDEX_HTML = Path(__file__).resolve().parent.parent / "frontend" / "index.html"


# --------------------------------------------------------------------------
# Global backends (lazy init)
# --------------------------------------------------------------------------

_no_graph: NoGraphBackend | None = None
_graph: GraphBackend | None = None


def get_no_graph() -> NoGraphBackend:
    global _no_graph
    if _no_graph is None:
        _no_graph = NoGraphBackend()
    return _no_graph


def get_graph() -> GraphBackend:
    global _graph
    if _graph is None:
        _graph = GraphBackend()
    return _graph


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Warm up no-graph (always available)
    get_no_graph()
    # Graph backend is lazy — don't fail startup if artifacts missing
    get_graph()
    yield


# --------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------

app = FastAPI(
    title="1111 Unified Search API",
    description="統一接口層 — 同時查詢 no-graph (OpenSearch hybrid) 和 graph (FTS + skill graph rerank) 兩個 backend",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _parse_codes(value: str | list[str] | None) -> list[str] | None:
    """Normalize comma-separated string or list into list."""
    if value is None:
        return None
    if isinstance(value, list):
        return [c.strip() for c in value if c.strip()]
    return [c.strip() for c in value.split(",") if c.strip()]


def _do_search(req: SearchRequest) -> SearchResponse:
    c0 = _parse_codes(req.c0)
    d0 = _parse_codes(req.d0)

    common_kwargs = dict(
        ks=req.ks,
        c0=c0,
        d0=d0,
        top_k=req.top_k,
        mode=req.mode,
        salary_min=req.salary_min,
        job_types=req.job_types,
        work_hours=req.work_hours,
    )

    results: list[BackendResult] = []

    if req.backend in ("no-graph", "both"):
        results.append(get_no_graph().search(**common_kwargs))

    if req.backend in ("graph", "both"):
        results.append(get_graph().search(**common_kwargs))

    return SearchResponse(query=req.ks, results=results)


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index():
    """搜尋比較頁面。跟 API 同源，前端直接打相對路徑。"""
    if not _INDEX_HTML.is_file():
        return HTMLResponse(
            "<h1>1111 Unified Search API</h1>"
            "<p>前端頁面未打包。API 端點：<code>/search</code>、<code>/health</code></p>",
            status_code=200,
        )
    return HTMLResponse(_INDEX_HTML.read_text(encoding="utf-8"))


@app.get("/health")
def health():
    graph = get_graph()
    return {
        "status": "ok",
        "backends": {
            "no-graph": "ready",
            "graph": "ready" if graph.available else f"unavailable: {graph.error}",
        },
    }


@app.post("/search", response_model=SearchResponse)
def search_post(req: SearchRequest):
    """POST /search — 主要查詢端點。"""
    return _do_search(req)


@app.get("/search", response_model=SearchResponse)
def search_get(
    ks: str = Query(..., description="搜尋關鍵字"),
    c0: str | None = Query(default=None, description="地區代碼，逗號分隔"),
    d0: str | None = Query(default=None, description="職務分類代碼，逗號分隔"),
    top_k: int = Query(default=10, ge=1, le=100),
    mode: str = Query(default="hybrid"),
    salary_min: int | None = Query(default=None),
    job_types: str | None = Query(default=None, description="逗號分隔"),
    work_hours: str | None = Query(default=None, description="逗號分隔"),
    backend: str = Query(default="both", description="no-graph / graph / both"),
):
    """GET /search?ks=水電&backend=both — 方便瀏覽器測試。"""
    req = SearchRequest(
        ks=ks,
        c0=c0,
        d0=d0,
        top_k=top_k,
        mode=mode,
        salary_min=salary_min,
        job_types=job_types.split(",") if job_types else None,
        work_hours=work_hours.split(",") if work_hours else None,
        backend=backend,
    )
    return _do_search(req)
