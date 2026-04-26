from typing import Any, Generic, Optional, TypeVar

from pydantic import BaseModel, Field


T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    success: bool = True
    message: str = "ok"
    data: Optional[T] = None
    meta: dict[str, Any] = Field(default_factory=dict)


class ErrorDetail(BaseModel):
    field: str
    reason: str


class ApiErrorResponse(BaseModel):
    success: bool = False
    message: str = "error"
    error_code: str = "INTERNAL_SERVER_ERROR"
    errors: list[ErrorDetail] = Field(default_factory=list)