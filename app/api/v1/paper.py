from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.response import success_response
from app.integrations.crossref.client import get_references as crossref_get_references
from app.integrations.scienceon.client import ScienceOnClient
from app.integrations.scienceon.parser import ScienceOnReference, parse_cited_references
from app.repositories.paper_repository import (
    get_paper_meta,
    get_papers_by_dois_batch,
    normalize_doi,
)
from app.schemas.common import ApiErrorResponse, ApiResponse
from app.schemas.paper import ReferenceResponse

router = APIRouter(prefix="/papers", tags=["Paper"])

_scienceon = ScienceOnClient()


@router.get(
    "/{paper_id}/references",
    response_model=ApiResponse[list[ReferenceResponse]],
    responses={
        404: {"model": ApiErrorResponse},
        500: {"model": ApiErrorResponse},
    },
    summary="참고문헌 조회",
    description=(
        "논문의 참고문헌 목록을 반환합니다.\n\n"
        "- JAKO 논문 → ScienceON browse API (CitedDocumentInfo)\n"
        "- JAFO 논문(DOI 보유) → CrossRef API\n"
        "- DIKO 논문(학위논문) → 참고문헌 미제공, 빈 리스트\n"
        "- `in_service: true` — 서비스 papers 테이블에 있는 논문\n"
        "- `in_service: false` — 서비스 외 논문"
    ),
)
async def get_paper_references(
    paper_id: str,
    db: AsyncSession = Depends(get_db),
):
    db_code, _, doi = await get_paper_meta(db, paper_id)

    if db_code is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="해당 논문을 찾을 수 없습니다",
        )

    # ── JAKO: ScienceON browse CitedDocumentInfo ──────────────────────────
    if db_code == "JAKO":
        xml = await _scienceon.browse_article(paper_id)
        refs = parse_cited_references(xml)
        return _build_scienceon_response(refs)

    # ── JAFO / 기타: CrossRef (DOI 보유 시) ──────────────────────────────
    if doi:
        crossref_refs = await crossref_get_references(doi)
        bare_dois = [normalize_doi(ref.doi) for ref in crossref_refs if ref.doi]
        papers_by_doi = await get_papers_by_dois_batch(db, bare_dois)

        result: list[ReferenceResponse] = []
        for ref in crossref_refs:
            bare_doi = normalize_doi(ref.doi) if ref.doi else None
            matched = papers_by_doi.get(bare_doi) if bare_doi else None

            if matched:
                result.append(ReferenceResponse(
                    doi=bare_doi,
                    title=matched.title,
                    authors=matched.authors,
                    year=matched.pubyear,
                    journal=None,
                    in_service=True,
                    paper_id=matched.id,
                    unstructured=ref.unstructured,
                ))
            else:
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

    # ── DIKO 등 참고문헌 없음 ────────────────────────────────────────────
    return success_response(data=[], message="ok")


def _build_scienceon_response(refs: list[ScienceOnReference]) -> dict:
    result = [
        ReferenceResponse(
            doi=ref.doi,
            title=ref.title,
            authors=ref.authors or None,
            year=ref.year,
            journal=ref.journal,
            in_service=False,
            paper_id=None,
            unstructured=None,
        )
        for ref in refs
    ]
    return success_response(data=result, message="ok")
