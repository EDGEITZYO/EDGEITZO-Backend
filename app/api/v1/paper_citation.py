from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.response import success_response
from app.schemas.common import ApiErrorResponse, ApiResponse
from app.schemas.paper_citation import (
    PaperCitationExpandRequest,
    PaperCitationExpandResponse,
    PaperCitationGraphResponse,
)
from app.services.paper_citation_service import expand_citation_node, get_citation_graph

router = APIRouter()


@router.get(
    "/papers/{paper_id}/citation-graph",
    response_model=ApiResponse[PaperCitationGraphResponse],
    responses={404: {"model": ApiErrorResponse}},
    summary="논문 인용관계 그래프 (참고문헌/피인용)",
    description="""논문 상세페이지 하단 요약 그래프 및 관계 상세보기 화면의 최초 로드 모두 이 엔드포인트를 사용합니다.

- `direction=reference`(기본값): 참고문헌 — 이 논문이 인용한 선행 논문(과거 방향)
- `direction=citing`: 피인용 — 이 논문을 인용한 후속 논문(미래 방향)
- 노드 상한(요약 12개) 안에서 자체 서비스 코퍼스 안팎을 가리지 않고 실제 인용관계를 채웁니다.
  코퍼스 안 논문(`in_service=true`)은 `paper_id`로 상세페이지 이동 가능, 코퍼스 밖 논문(`in_service=false`)은
  제목/저자/저널/연도 등 서지정보만 제공되고 상세페이지 이동은 불가 — 프론트에서 `in_service` 기준으로 분기 처리
- 1단계(직접 인용관계)까지만 반환. 2단계 이상은 각 노드의 `expand`로 조회 (단, `in_service=false` 노드는
  `has_more`가 항상 false라 확장 불가)
- 관계 데이터가 없으면 `nodes`가 빈 배열로 반환됨 (프론트에서 "표시할 참고문헌 관계가 없어요" 안내 처리)
""",
)
async def get_paper_citation_graph(
    paper_id: str = Path(..., description="중심 논문 ID (서비스 상세페이지 URL과 동일한 값, 항상 in-service 논문)"),
    direction: Literal["reference", "citing"] = Query(
        default="reference", description="'reference'=참고문헌(과거 방향) | 'citing'=피인용(미래 방향)"
    ),
    db: AsyncSession = Depends(get_db),
):
    result = await get_citation_graph(paper_id, direction, db)
    return success_response(data=result, message="paper citation graph loaded")


@router.post(
    "/papers/{paper_id}/citation-graph/node/{node_key}/expand",
    response_model=ApiResponse[PaperCitationExpandResponse],
    responses={404: {"model": ApiErrorResponse}},
    summary="인용관계 그래프 노드 확장",
    description="""그래프에 표시된 논문 노드(중앙 논문 자신 포함)를 선택했을 때, 그 논문과 직접적인 인용관계에 있는
다음 단계 논문을 제자리에서 펼칩니다. `in_service=false`(코퍼스 밖) 노드는 상세 데이터가 없어 확장할 수 없으며
(`has_more`가 항상 false), 이 엔드포인트를 그런 노드의 key로 호출하면 404가 반환됩니다.

- `node_key`: 확장할 노드의 key (`PaperCitationNode.key`) — in-service 노드만 유효
- `existing_node_keys`: 현재 화면에 표시 중인 전체 노드의 key (전역 중복 제거 및 100개 캡 계산용, 필수)
- `current_tier`: 확장 대상 노드의 현재 tier. 신규 노드는 이 값+1로 배정됨
- 그래프 전체 노드는 최대 100개로 제한되며, 캡에 걸리면 `capped=true`로 알려줌
- 다른 논문 상세페이지로 이동한 뒤 "관계 시각화"를 다시 선택하는 경우(기존 탐색 그래프 유지)도 이 엔드포인트를
  그 논문의 key로 호출하면 됨 — 별도 recenter 엔드포인트 없음
""",
)
async def expand_paper_citation_node(
    paper_id: str = Path(..., description="중심 논문 ID (URL 경로 표시용, 확장 로직 자체는 node_key로만 동작)"),
    node_key: str = Path(..., description="확장할 노드의 key (PaperCitationNode.key) — in-service 노드만 유효, 아니면 404"),
    request: PaperCitationExpandRequest = ...,
    db: AsyncSession = Depends(get_db),
):
    result = await expand_citation_node(
        node_key,
        direction=request.direction,
        current_tier=request.current_tier,
        existing_node_keys=request.existing_node_keys,
        db=db,
    )
    return success_response(data=result, message="paper citation node expanded")
