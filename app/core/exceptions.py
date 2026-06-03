import logging

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette import status

from app.schemas.common import ApiErrorResponse, ErrorDetail

logger = logging.getLogger(__name__)


def validation_exception_handler(request: Request, exc: RequestValidationError):
    errors: list[ErrorDetail] = []

    for err in exc.errors():
        loc = " -> ".join(str(item) for item in err.get("loc", []))
        msg = err.get("msg", "invalid value")
        errors.append(ErrorDetail(field=loc, reason=msg))

    response = ApiErrorResponse(
        success=False,
        message="validation error",
        error_code="BAD_REQUEST",
        errors=errors,
    )

    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=response.model_dump(),
    )


def http_exception_handler(request: Request, exc):
    response = ApiErrorResponse(
        success=False,
        message=str(exc.detail),
        error_code="HTTP_EXCEPTION",
        errors=[],
    )

    return JSONResponse(
        status_code=exc.status_code,
        content=response.model_dump(),
    )


def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception(exc)
    response = ApiErrorResponse(
        success=False,
        message="서버 내부 오류가 발생했습니다",
        error_code="INTERNAL_SERVER_ERROR",
        errors=[],
    )

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=response.model_dump(),
    )