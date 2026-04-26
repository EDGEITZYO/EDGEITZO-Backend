from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError

from app.api.v1.health import router as health_router
from app.api.v1.search import router as search_router
from app.api.v1.test import router as test_router
from app.core.exceptions import (
    http_exception_handler,
    unhandled_exception_handler,
    validation_exception_handler,
)
from app.core.response import success_response
from app.api.v1.scienceon import router as scienceon_router
from app.api.v1.semanticscholar import router as semantic_scholar_router


app = FastAPI(
    title="PaperGraph Agent API",
    description="LLM + RAG 기반 국내 연구자/논문 탐색 백엔드",
    version="0.1.0",
)


@app.get("/")
def root():
    return success_response(
        data={"message": "PaperGraph API running"},
        message="root endpoint",
    )


app.include_router(health_router, prefix="/api/v1", tags=["Health"])
app.include_router(search_router, prefix="/api/v1", tags=["Search"])
app.include_router(test_router, prefix="/api/v1", tags=["Test"])

app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.add_exception_handler(HTTPException, http_exception_handler)
app.add_exception_handler(Exception, unhandled_exception_handler)

app.include_router(scienceon_router, prefix="/api/v1", tags=["ScienceON"])

app.include_router(semantic_scholar_router, prefix="/api/v1", tags=["SemanticScholar"])