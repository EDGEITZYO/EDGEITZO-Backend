from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.response import success_response

router = APIRouter()


class TestRequest(BaseModel):
    name: str = Field(..., min_length=2)
    age: int = Field(..., ge=1)


@router.post("/test/echo")
def test_echo(request: TestRequest):
    return success_response(
        data={
            "name": request.name,
            "age": request.age,
        },
        message="echo success",
    )


@router.get("/test/http-error")
def test_http_error():
    raise HTTPException(status_code=404, detail="test not found")


@router.get("/test/server-error")
def test_server_error():
    raise ValueError("unexpected test error")