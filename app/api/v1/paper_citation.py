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
    PaperCitationExternalDetail,
    PaperCitationGraphResponse,
)
from app.services.paper_citation_external_service import get_external_paper_detail
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
  **모든 노드가 클릭 가능하며**, `in_service`로 이동할 상세만 갈라집니다 —
  `true`면 `paper_id`로 일반 상세페이지, `false`면 `key`로
  `GET /papers/citation-graph/external/{external_id}`. 후자는 적재 없이 클릭 시점에 KCI/OpenAlex에서
  받아오므로 초록/키워드가 없을 수 있습니다(`enriched=false`)
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


@router.get(
    "/papers/citation-graph/external/{external_id}",
    response_model=ApiResponse[PaperCitationExternalDetail],
    responses={404: {"model": ApiErrorResponse}},
    summary="코퍼스 밖 논문 상세 (그래프의 in_service=false 노드용)",
    description="""인용관계/참고문헌 그래프에서 `in_service=false` 노드를 클릭했을 때 사용합니다.

이 논문들은 papers 테이블에 적재돼 있지 않아 일반 상세페이지(`GET /papers/{paper_id}`)로는 404가
납니다. 대신 저장해 둔 서지정보를 바탕으로, 클릭 시점에 외부에서 상세를 받아와 채워 돌려줍니다.

- `external_id`: `PaperCitationNode.key` (ART…/REF…/W… 등)
- 응답은 **항상 성립합니다.** 외부 조회가 실패하거나 그쪽에 초록이 없으면 `enriched=false`로,
  저장된 서지정보(제목/저자/저널/연도/DOI)만 채워져 돌아옵니다 — 404가 아닙니다
- 404는 해당 `external_id`가 그래프 데이터 자체에 없을 때만 반환됩니다

**초록 제공률** (2026-08-30 실측) — 프론트에서 빈 화면 대비가 필요합니다.

| 노드 종류 | 비중 | 초록 |
|---|---|---|
| `ART…` (KCI 원문 연결) | 약 11% | 약 97% |
| DOI 보유 `REF…` | 약 20% | 약 63% |
| `W…` (OpenAlex) | 피인용 노드 | 약 59% |
| DOI 없는 `REF…` | 약 69% | 없음 |

응답은 24시간 캐시됩니다(서지정보는 거의 바뀌지 않음).
""",
)
async def get_external_paper(
    external_id: str = Path(..., description="그래프 노드의 key (PaperCitationNode.key) — in_service=false 노드만 유효"),
    db: AsyncSession = Depends(get_db),
):
    result = await get_external_paper_detail(external_id, db)
    return success_response(data=result, message="external paper detail loaded")
