import asyncio
import logging

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware

logger = logging.getLogger(__name__)

from app.api.v1.auth import router as auth_router
from app.api.v1.bookmark import router as bookmark_router
from app.api.v1.bookmark_folder import router as bookmark_folder_router
from app.api.v1.graph import router as graph_router
from app.api.v1.health import router as health_router
from app.api.v1.home import router as home_router
from app.api.v1.keyword_map import router as keyword_map_router
from app.api.v1.keyword_search import router as keyword_search_router
from app.api.v1.mypage import router as mypage_router
from app.api.v1.paper import router as paper_router
from app.api.v1.saved import router as saved_router
from app.api.v1.scienceon import router as scienceon_router
from app.api.v1.search import router as search_router
from app.api.v1.semanticscholar import router as semantic_scholar_router
from app.api.v1.test import router as test_router
from app.core.exceptions import (
    http_exception_handler,
    unhandled_exception_handler,
    validation_exception_handler,
)
from app.core.response import success_response
from app.core.settings import settings


app = FastAPI(
    title="Bio-me API",
    description="LLM + RAG 기반 국내 연구자/논문 탐색 백엔드",
    version="0.1.0",
    swagger_ui_parameters={"persistAuthorization": True},
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.add_exception_handler(HTTPException, http_exception_handler)
app.add_exception_handler(Exception, unhandled_exception_handler)


@app.on_event("startup")
async def startup_check_chromadb():
    import chromadb
    from app.core.settings import settings
    for attempt in range(1, 6):
        try:
            client = chromadb.HttpClient(host=settings.chroma_host, port=settings.chroma_port)
            client.heartbeat()
            logger.info("ChromaDB 연결 성공")
            return
        except Exception as e:
            logger.warning("ChromaDB 연결 실패 (%d/5): %s", attempt, e)
            if attempt < 5:
                await asyncio.sleep(2)
    logger.warning("ChromaDB 연결 불가 — 앱은 계속 시작합니다")


@app.get("/")
def root():
    return success_response(
        data={"message": "PaperGraph API running"},
        message="root endpoint",
    )


app.include_router(auth_router, prefix="/api/v1", tags=["Auth"])
app.include_router(bookmark_router, prefix="/api/v1", tags=["Bookmark"])
app.include_router(bookmark_folder_router, prefix="/api/v1", tags=["Bookmark"])
app.include_router(graph_router, prefix="/api/v1", tags=["Graph"])
app.include_router(health_router, prefix="/api/v1", tags=["Health"])
app.include_router(home_router, prefix="/api/v1", tags=["Home"])
app.include_router(keyword_map_router, prefix="/api/v1", tags=["KeywordMap"])
app.include_router(keyword_search_router, prefix="/api/v1", tags=["KeywordSearch"])
app.include_router(mypage_router, prefix="/api/v1", tags=["Mypage"])
app.include_router(paper_router, prefix="/api/v1", tags=["Paper"])
app.include_router(saved_router, prefix="/api/v1", tags=["Saved"])
app.include_router(scienceon_router, prefix="/api/v1", tags=["ScienceON"])
app.include_router(search_router, prefix="/api/v1", tags=["Search"])
app.include_router(semantic_scholar_router, prefix="/api/v1", tags=["SemanticScholar"])
app.include_router(test_router, prefix="/api/v1", tags=["Test"])
