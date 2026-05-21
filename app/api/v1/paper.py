from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.response import success_response
from app.integrations.crossref.client import get_references
from app.repositories.paper_repository import (
    get_doi_by_paper_id,
    get_papers_by_dois_batch,
    normalize_doi,
)
from app.schemas.common import ApiErrorResponse, ApiResponse
from app.schemas.paper import ReferenceResponse

router = APIRouter(prefix="/papers", tags=["Paper"])


@router.get(
    "/{paper_id}/references",
    response_model=ApiResponse[list[ReferenceResponse]],
    responses={
        404: {"model": ApiErrorResponse},
        500: {"model": ApiErrorResponse},
    },
    summary="참고문헌 조회",
    description=(
        "논문의 DOI로 CrossRef API를 조회해 참고문헌 목록을 반환합니다.\n\n"
        "- `in_service: true` — 서비스 papers 테이블에 있는 논문 (상세페이지 이동 가능)\n"
        "- `in_service: false` — 서비스 외 논문 (외부 링크 또는 불가 메시지)\n"
        "- DOI 없는 논문은 빈 리스트 반환"
    ),
)
async def get_paper_references(
    paper_id: str,
    db: AsyncSession = Depends(get_db),
):
    doi = await get_doi_by_paper_id(db, paper_id)
    if doi is None:
        # DOI 없는 논문 — 참고문헌 조회 불가
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="해당 논문을 찾을 수 없거나 DOI 정보가 없습니다",
        )

    refs = await get_references(doi)

    # DOI 리스트 수집 → papers 테이블 IN 쿼리 1회
    bare_dois = [normalize_doi(ref.doi) for ref in refs if ref.doi]
    papers_by_doi = await get_papers_by_dois_batch(db, bare_dois)

    result: list[ReferenceResponse] = []
    for ref in refs:
        bare_doi = normalize_doi(ref.doi) if ref.doi else None
        matched = papers_by_doi.get(bare_doi) if bare_doi else None

        if matched:
            # 1순위: DB 데이터로 채우기
            result.append(ReferenceResponse(
                doi=bare_doi,
                title=matched.title,
                authors=matched.authors,
                year=matched.pubyear,
                journal=None,  # journal_id만 있음 — join 없이는 name 불가
                in_service=True,
                paper_id=matched.id,
                unstructured=ref.unstructured,
            ))
        else:
            # 2순위: CrossRef raw 필드 / 3순위: unstructured fallback
            result.append(ReferenceResponse(
                doi=bare_doi,
                title=ref.title,
                authors=ref.authors or None,
                year=ref.year,
                journal=ref.journal,
                in_service=False,
                paper_id=None,
                unstructured=ref.unstructured,
            ))

    return success_response(data=result, message="ok")
