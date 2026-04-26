from typing import Any, Optional

from app.schemas.common import ApiResponse


def success_response(
    data: Optional[Any] = None,
    message: str = "ok",
    meta: Optional[dict[str, Any]] = None,
) -> ApiResponse:
    return ApiResponse(
        success=True,
        message=message,
        data=data,
        meta=meta or {},
    )