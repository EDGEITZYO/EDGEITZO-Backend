import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.response import success_response
from app.integrations.crossref.client import get_references as crossref_get_references
from app.integrations.scienceon.client import ScienceOnClient
from app.integrations.scienceon.parser import (
    ScienceOnApiError,
    ScienceOnReference,
    parse_cited_references,
)
from app.repositories.paper_repository import (
    get_paper_meta,
    get_paper_with_journal,
    get_papers_by_dois_batch,
    get_similar_papers,
    normalize_doi,
)
from app.schemas.common import ApiErrorResponse, ApiResponse
from app.schemas.paper import PaperDetailResponse, ReferenceResponse, SimilarPaperResponse
from app.services.credibility_service import (
    _journal_to_evidence,
    build_trust_badge,
    calculate_credibility,
    calculate_thesis_credibility,
    paper_type_label,
    resolve_paper_type,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/papers", tags=["Paper"])

_scienceon = ScienceOnClient()


@router.get(
    "/{paper_id}",
    response_model=ApiResponse[PaperDetailResponse],
    responses={
        404: {"model": ApiErrorResponse},
    },
    summary="논문 단건 조회",
    description=(
        "paper_id로 논문 상세 정보를 반환합니다.\n\n"
        "**응답 필드**\n"
        "- `title` / `title_en` — 한국어·영문 제목\n"
        "- `abstract` / `abstract_en` — 한국어·영문 초록\n"
        "- `keywords_ko` / `keywords_en` — 한국어·영문 키워드\n"
        "- `published_at` — pubdate 우선, 없으면 `{pubyear}-01-01`, 둘 다 없으면 null\n"
        "- `paper_type` — `'박사학위 논문'` | `'석사학위 논문'` | `'학위논문'`(학위 구분 미상) | `'학술 저널'` | null\n"
        "- `journal_name` — 학술지명. 학위논문·학회는 null일 수 있음\n"
        "- `degree` / `affiliation` — 학위논문 전용. 저널·학회는 null\n"
        "- `fulltext_flag` — 원문 제공 여부. 없으면 null\n"
        "- `credibility` — 신뢰도 정보 (badge: `high`|`medium`|`low`|`unknown`)\n"
        "- `trust_badge` — 신뢰도 뱃지 (kci, sci, if_value, degree_type 등)\n\n"
        "**404** — 해당 paper_id가 서비스 DB에 없는 경우"
    ),
)
async def get_paper_detail(
    paper_id: str,
    db: AsyncSession = Depends(get_db),
):
    paper = await get_paper_with_journal(db, paper_id)
    if paper is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="해당 논문을 찾을 수 없습니다",
        )

    journal_evidence = _journal_to_evidence(paper.journal)
    paper_type = resolve_paper_type(paper.db_code, paper.degree)

    if paper_type in ("thesis_phd", "thesis_master"):
        credibility = calculate_thesis_credibility(paper.degree, paper.affiliation)
    else:
        credibility = calculate_credibility(
            citation_count=paper.citation_count,
            journal_name=paper.journal.title if paper.journal else paper.journal_name,
            journal=journal_evidence,
            kci_hint=paper.db_code == "JAKO",
        )

    trust_badge = build_trust_badge(
        paper_type,
        journal=journal_evidence,
        citation_count=paper.citation_count,
        degree=paper.degree,
        institution=paper.affiliation,
        full_text_available=paper.fulltext_flag,
        kci_hint=paper.db_code == "JAKO",
    )

    if paper.pubdate:
        published_at = str(paper.pubdate).replace('.', '-')
    elif paper.pubyear:
        published_at = f"{paper.pubyear}-01-01"
    else:
        published_at = None

    data = PaperDetailResponse(
        paper_id=paper.id,
        title=paper.title,
        title_en=paper.title_en,
        authors=paper.authors,
        abstract=paper.abstract,
        abstract_en=paper.abstract_en,
        keywords_ko=paper.keywords_ko,
        keywords_en=paper.keywords_en,
        published_at=published_at,
        paper_type=paper_type_label(resolve_paper_type(paper.db_code, paper.degree)),
        journal_name=paper.journal.title if paper.journal else paper.journal_name,
        doi=paper.doi,
        citation_count=paper.citation_count,
        degree=paper.degree,
        affiliation=paper.affiliation,
        fulltext_flag=paper.fulltext_flag,
        credibility=credibility,
        trust_badge=trust_badge,
    )
    return success_response(data=data, message="ok")


@router.get(
    "/{paper_id}/similar",
    response_model=ApiResponse[list[SimilarPaperResponse]],
    responses={
        404: {"model": ApiErrorResponse},
    },
    summary="유사 논문 조회",
    description=(
        "논문의 유사 논문 목록을 반환합니다.\n\n"
        "- `in_service: true` — 서비스 DB에 있는 논문. `paper_id`로 상세 페이지 이동 가능\n"
        "- `in_service: false` — 서비스 외 논문. `paper_id` / `journal_name` / `keywords` / `doi` / `trust_badge`는 null\n"
        "- 유사 논문이 없는 경우 빈 리스트 반환"
    ),
)
async def get_paper_similar(
    paper_id: str,
    db: AsyncSession = Depends(get_db),
):
    paper_meta = await get_paper_meta(db, paper_id)
    if paper_meta[0] is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="해당 논문을 찾을 수 없습니다",
        )

    similars = await get_similar_papers(db, paper_id)
    result = []
    for s, p, j in similars:
        trust = None
        paper_type_str = None
        if p:
            ptype = resolve_paper_type(p.db_code, p.degree)
            paper_type_str = paper_type_label(ptype)
            trust = build_trust_badge(
                ptype,
                journal=_journal_to_evidence(j),
                citation_count=p.citation_count or None,
                institution=p.affiliation or p.publisher,
                full_text_available=p.fulltext_flag,
                kci_hint=p.db_code == "JAKO",
            )
        result.append(SimilarPaperResponse(
            title=s.title,
            author=", ".join(p.authors) if p and p.authors else s.author,
            pubyear=p.pubyear if p else s.pubyear,
            material_type=s.material_type,
            paper_type=paper_type_str,
            in_service=s.internal_paper_id is not None,
            paper_id=s.internal_paper_id,
            journal_name=j.title if j else None,
            keywords=(p.keywords_ko or []) if p else None,
            doi=p.doi if p else None,
            trust_badge=trust,
        ))
    return success_response(data=result, message="ok")


@router.get(
    "/{paper_id}/references",
    response_model=ApiResponse[list[ReferenceResponse]],
    responses={
        404: {"model": ApiErrorResponse},
        500: {"model": ApiErrorResponse},
        502: {"model": ApiErrorResponse},
    },
    summary="참고문헌 조회",
    description=(
        "논문의 참고문헌 목록을 반환합니다.\n\n"
        "**조회 경로** (db_code가 아니라 아래 순서로 분기합니다)\n"
        "- `db_code = JAKO` → ScienceON browse API (CitedDocumentInfo). `CitedDOI`로만 서비스 DB 매칭 "
        "(ScienceON의 `CitedCn`은 논문 CN이 아니라 항목 일련번호라 쓸 수 없음 — DOI 없는 참고문헌은 항상 `in_service=false`)\n"
        "- 그 외 논문 중 **DOI 보유** → CrossRef API. DOI로 서비스 DB 매칭 (JAFO가 대부분이지만 db_code로 거르지 않음)\n"
        "- 그 외 (DOI 없음, DIKO 학위논문 등) → 빈 리스트\n\n"
        "**응답 필드**\n"
        "- `in_service: true` — 서비스 papers 테이블에 있는 논문 (`paper_id`로 상세 이동 가능)\n"
        "- `in_service: false` — 서비스 외 논문\n"
        "- `unstructured` — CrossRef 경로에서만 채워지는 원문 인용 문자열. ScienceON 경로는 항상 null\n\n"
        "**502** — ScienceON(JAKO 경로) 호출 실패에만 해당합니다. 참고문헌이 실제로 없는 경우(빈 리스트)와 구분하기 위한 것입니다.\n"
        "⚠️ CrossRef 경로는 호출이 실패해도 502가 아니라 **빈 리스트**로 반환되므로, 프론트에서 "
        "\'참고문헌 없음\'과 구분할 수 없습니다"
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
        try:
            xml = await _scienceon.browse_article(paper_id)
            refs = parse_cited_references(xml)
        except (httpx.HTTPError, ScienceOnApiError) as exc:
            # 토큰 만료·호출 한도 초과를 "참고문헌 0건"으로 내려보내지 않는다
            logger.warning("ScienceON 참고문헌 조회 실패 (paper_id=%s): %s", paper_id, exc)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="참고문헌 제공처(ScienceON) 호출에 실패했습니다",
            ) from exc
        return await _build_scienceon_response(db, refs)

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


async def _build_scienceon_response(db: AsyncSession, refs: list[ScienceOnReference]) -> dict:
    """ScienceON 참고문헌을 서비스 DB와 매칭해 in_service를 판정한다 (CrossRef 경로와 동일 규칙).

    ScienceON 응답의 CitedCn은 논문 CN이 아니라 참고문헌 항목 일련번호라 쓸 수 없어
    CitedDOI가 유일한 매칭 키다. DOI 없는 참고문헌은 항상 in_service=false가 된다.
    """
    bare_dois = [normalize_doi(ref.doi) for ref in refs if ref.doi]
    papers_by_doi = await get_papers_by_dois_batch(db, bare_dois)

    result: list[ReferenceResponse] = []
    for ref in refs:
        bare_doi = normalize_doi(ref.doi) if ref.doi else None
        matched = papers_by_doi.get(bare_doi) if bare_doi else None

        if matched:
            result.append(ReferenceResponse(
                doi=normalize_doi(matched.doi) if matched.doi else bare_doi,
                title=matched.title,
                authors=list(matched.authors or []) or None,
                year=matched.pubyear,
                journal=matched.journal_name or ref.journal,
                in_service=True,
                paper_id=matched.id,
                unstructured=None,
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
                unstructured=None,
            ))

    return success_response(data=result, message="ok")
