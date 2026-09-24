from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user_optional
from app.models.user import User
from app.core.response import success_response
from app.schemas.common import ApiErrorResponse, ApiResponse
from app.schemas.paper_citation import (
    RelatedCorpusPapersResponse,
    PaperCitationExpandRequest,
    PaperCitationExpandResponse,
    PaperCitationExternalDetail,
    PaperCitationGraphResponse,
)
from app.services.paper_citation_external_service import (
    get_external_paper_detail,
    get_related_corpus_papers,
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
- 노드 상한(요약 12개) 안에서 국내·해외 논문을 가리지 않고 실제 인용관계를 채웁니다(국내 논문 우선).
- `in_service`는 **국내 논문 여부**입니다.
  - `true` 국내 논문 — 코퍼스 논문이거나 KCI 논문 ID(`ART…`)가 있는 논문. `paper_id`로
    `GET /papers/{paper_id}` 상세 조회가 됩니다. 서비스 DB에 아직 없는 국내 논문도 첫 조회 때
    KCI에서 받아 적재합니다(첫 조회만 약 0.5초 추가)
  - `false` 해외 논문 — KCI ID가 없는 참고문헌. 서지정보만 있으며 상세는
    `GET /papers/citation-graph/external/{external_id}` (초록 제공률이 낮아 `enriched=false`일 수 있음)
  - KCI ID가 없는데 제목이 한글인 참고문헌(단행본·법령·백서 등)은 노드로 내려가지 않습니다
- 1단계(직접 인용관계)까지만 반환. 2단계 이상은 각 노드의 `expand`로 조회 (해외 논문은
  `has_more`가 항상 false라 확장 불가)
- `cluster_id`: 1단계 자식끼리 키워드를 2개 이상 공유하면 같은 정수값이 부여됩니다.
  center·해외 논문·expand로 추가된 노드는 항상 null
- 관계 데이터가 없으면 `nodes`가 빈 배열로 반환됨
""",
)
async def get_paper_citation_graph(
    paper_id: str = Path(..., description="중심 논문 ID (서비스 상세페이지 URL과 동일한 값, 항상 국내 논문)"),
    direction: Literal["reference", "citing"] = Query(
        default="reference", description="'reference'=참고문헌(과거 방향) | 'citing'=피인용(미래 방향)"
    ),
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    result = await get_citation_graph(paper_id, direction, db, user_id=current_user.id if current_user else None)
    return success_response(data=result, message="paper citation graph loaded")


@router.post(
    "/papers/{paper_id}/citation-graph/node/{node_key}/expand",
    response_model=ApiResponse[PaperCitationExpandResponse],
    responses={404: {"model": ApiErrorResponse}},
    summary="인용관계 그래프 노드 확장",
    description="""그래프에 표시된 논문 노드(중앙 논문 자신 포함)를 선택했을 때, 그 논문과 직접적인 인용관계에 있는
다음 단계 논문을 제자리에서 펼칩니다. 해외 논문(`in_service=false`) 노드는 인용관계 데이터가 없어 확장할 수
없으며(`has_more`가 항상 false), 이 엔드포인트를 그런 노드의 key로 호출하면 404가 반환됩니다.
KCI 참고문헌을 아직 받지 않은 국내 논문은 이 호출에서 KCI로부터 받아 적재한 뒤 펼칩니다.

- `node_key`: 확장할 노드의 key (`PaperCitationNode.key`) — 국내 논문(in_service=true) 노드만 유효
- `existing_node_keys`: 현재 화면에 표시 중인 전체 노드의 key (전역 중복 제거 및 100개 캡 계산용, 필수)
- `current_tier`: 확장 대상 노드의 현재 tier. 신규 노드는 이 값+1로 배정됨
- 그래프 전체 노드는 최대 100개로 제한되며, 캡에 걸리면 `capped=true`로 알려줌
- 다른 논문 상세페이지로 이동한 뒤 "관계 시각화"를 다시 선택하는 경우(기존 탐색 그래프 유지)도 이 엔드포인트를
  그 논문의 key로 호출하면 됨 — 별도 recenter 엔드포인트 없음
""",
)
async def expand_paper_citation_node(
    paper_id: str = Path(..., description="중심 논문 ID (URL 경로 표시용, 확장 로직 자체는 node_key로만 동작)"),
    node_key: str = Path(..., description="확장할 노드의 key (PaperCitationNode.key) — 국내 논문 노드만 유효, 아니면 404"),
    request: PaperCitationExpandRequest = ...,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    result = await expand_citation_node(
        node_key,
        direction=request.direction,
        current_tier=request.current_tier,
        existing_node_keys=request.existing_node_keys,
        db=db,
        user_id=current_user.id if current_user else None,
    )
    return success_response(data=result, message="paper citation node expanded")


@router.get(
    "/papers/citation-graph/external/{external_id}",
    response_model=ApiResponse[PaperCitationExternalDetail],
    responses={404: {"model": ApiErrorResponse}},
    summary="해외 논문 상세 (그래프의 in_service=false 노드용)",
    description="""인용관계/참고문헌 그래프의 해외 논문(`in_service=false`) 노드 상세입니다.

해외 논문은 papers 테이블에 적재되지 않아 일반 상세(`GET /papers/{paper_id}`)로는 404가
납니다. 대신 미리 적재해 둔 서지정보·초록·링크를 돌려줍니다(외부 호출 없이 1ms 미만).
국내 논문(`in_service=true`, `ART…`)은 `GET /papers/{paper_id}`를 쓰며, 이 엔드포인트도
하위 호환을 위해 ART… key에 응답은 합니다.
아직 적재되지 않은 항목만 클릭 시점에 외부에서 받아옵니다.

- `external_id`: `PaperCitationNode.key` (ART…/REF…/W… 등)
- 응답은 **항상 성립합니다.** 초록을 못 구했으면 `enriched=false`로, 서지정보
  (제목/저자/저널/연도/DOI)만 채워져 돌아옵니다 — 404가 아닙니다
- 404는 해당 `external_id`가 그래프 데이터 자체에 없을 때만 반환됩니다

**초록 제공률** (2026-08-31 전수 실측) — 프론트에서 빈 화면 대비가 필요합니다.

| 노드 종류 | 비중 | 초록/요약 | 원문 링크 |
|---|---|---|---|
| `ART…` (KCI 원문 연결) | 14.6% | 96.7% | 100% |
| DOI 보유 `REF…`/`W…` | 18.4% | 75.4% | 100% |
| DOI 없는 `REF…` | 66.9% | 56.4% | 69.1% |
| **전체** | | **65.8%** | **79.3%** |

제공률을 가르는 건 언어가 아니라 **KCI arti-id(`ART…`) 연결 여부**입니다. 한글 논문은 86%가
`ART…`로 연결돼 있어 결과적으로 83.4%가 채워지고, 영문은 63.6%입니다.

⚠️ **`enrich_source`가 `s2_tldr`인 경우 `abstract` 값은 초록이 아닙니다.** AllenAI 모델이
논문 본문에서 생성한 한 줄 요약이며, 전체 초록의 12.8%p(DOI 보유 경로에서는 약 3분의 1)를
차지합니다. 초록과 같은 모양으로 보여주면 안 되고, "요약 (Semantic Scholar 자동 생성)"처럼
출처를 밝혀 구분 표기해야 합니다.

응답은 24시간 캐시됩니다(서지정보는 거의 바뀌지 않음).
""",
)
async def get_external_paper(
    external_id: str = Path(..., description="그래프 노드의 key (PaperCitationNode.key) — 해외 논문(in_service=false) 노드용"),
    db: AsyncSession = Depends(get_db),
):
    result = await get_external_paper_detail(external_id, db)
    return success_response(data=result, message="external paper detail loaded")


@router.get(
    "/papers/citation-graph/external/{external_id}/related",
    response_model=ApiResponse[RelatedCorpusPapersResponse],
    responses={404: {"model": ApiErrorResponse}},
    summary="해외 논문과 연관된 코퍼스 논문",
    description="""해외 논문(`in_service=false`)과 주제가 가까운 **우리 코퍼스 논문**을 돌려줍니다.
`paper_id`로 `GET /papers/{paper_id}` 상세로 바로 이동할 수 있습니다.

적재하지 않습니다 — 해외 논문의 제목(초록이 있으면 초록까지)을 요청 시점에 임베딩해
검색과 같은 코퍼스에서 가까운 것을 고릅니다. 비용은 검색 한 번과 같습니다.

### ⚠️ 빈 배열이 정상 응답입니다

코퍼스가 1,000편뿐이라 **관련 논문이 아예 없는 해외 문헌이 절반가량**입니다(실측 52.5%).
그중 상당수는 정부 연차보고서·교육과정 문서·법령 조항이라 애초에 논문이 아닙니다.
빈 배열이면 "연관 논문 없음"으로 표시하면 됩니다.

### 몇 건이 오는지는 질의마다 다릅니다

고정 개수가 아닙니다. 거리로 자르기 때문에 0건부터 최대 10건까지 나옵니다
(통과한 질의 기준 평균 2.5건). 화면은 가변 길이를 전제로 만들어야 합니다.

선별 규칙은 두 단계입니다.

1. 코사인 거리 **0.55 이하**만 후보로 둡니다
2. 그중 **1위와의 거리 차가 0.05 이내**인 것만 남기고, 최대 10건으로 자릅니다

2단계가 필요한 이유는 같은 거리라도 뜻이 다르기 때문입니다. 1위가 0.40인 질의는 진짜
관련 논문이 있는 경우라 2·3위도 쓸 만하지만, 1위가 0.54인 질의는 간신히 걸린 것이라
2위부터는 대체로 무관합니다.

### 정확도 (표본 120건 실측, 2026-09-24)

| 규칙 | 반환 | 정밀도 |
|---|---|---|
| 임계값 없이 상위 5건 | 233건 | 59.2% |
| 절대 0.52 + 고정 5건 | 157건 | 46.5% |
| **현재 규칙** | **145건** | **44.1%** |

`distance`를 함께 내려주니 확신도 표기에 쓸 수 있습니다. 0.45 이하는 대체로 정확하고,
0.52에 가까울수록 주제가 스치는 정도입니다.

초록이 없어도 정확도 차이는 크지 않습니다(Recall@10 53.3% vs 48.3%) — `used_abstract`로
구분은 되지만 이것만으로 결과를 감출 필요는 없습니다.

응답은 24시간 캐시됩니다.
""",
)
async def get_external_paper_related(
    external_id: str = Path(..., description="그래프 노드의 key (PaperCitationNode.key)"),
    db: AsyncSession = Depends(get_db),
):
    result = await get_related_corpus_papers(external_id, db)
    return success_response(data=result, message="related corpus papers loaded")
