# Neo4j Graph API

작성일: 2026-05-14

## 목적

Neo4j AuraDB에 적재된 키워드 관계 그래프를 프론트엔드에서 조회할 수 있도록 API로 노출한다.

1차 범위는 초기 키워드맵 생성과 키워드 노드 확장이다.

- 02-15 초기 키워드맵 자동생성
- 02-16 키워드 그래프 화면 표시
- 02-17 키워드 그래프 노드 확장

## 환경 변수

`.env`에 Neo4j AuraDB 연결 정보를 설정한다.

```env
NEO4J_URI=neo4j+s://xxxxx.databases.neo4j.io
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=...
```

`NEO4J_USER`도 인식하지만, AuraDB 콘솔 표기와 맞춰 `NEO4J_USERNAME` 사용을 권장한다.

## 초기 키워드 그래프 조회

```http
GET /api/v1/graph/keywords?keyword=생명공학&limit=20
```

쿼리 파라미터:

| 이름 | 타입 | 필수 | 기본값 | 설명 |
|---|---:|---:|---:|---|
| keyword | string | O | - | 키워드 이름 또는 key |
| lang | string | X | null | `ko` 또는 `en` |
| limit | int | X | 20 | 관련 키워드 최대 개수, 1~50 |
| min_paper_count | int | X | 1 | 최소 동시 등장 논문 수 |

예시 응답:

```json
{
  "success": true,
  "message": "keyword graph fetched",
  "data": {
    "center": {
      "key": "ko:생명공학",
      "name": "생명공학",
      "normalized_name": "생명공학",
      "lang": "ko",
      "source_field": "Keyword",
      "paper_count": 20,
      "is_center": true
    },
    "nodes": [
      {
        "key": "ko:생명공학",
        "name": "생명공학",
        "normalized_name": "생명공학",
        "lang": "ko",
        "source_field": "Keyword",
        "paper_count": 20,
        "is_center": true
      }
    ],
    "edges": [
      {
        "source": "ko:생명공학",
        "target": "en:biotechnology",
        "paper_count": 2,
        "lang_pair": "en-ko"
      }
    ]
  },
  "meta": {
    "keyword": "생명공학",
    "lang": null,
    "node_count": 4,
    "edge_count": 3
  }
}
```

## 키워드 노드 확장

```http
GET /api/v1/graph/keywords/ko:생명공학/expand?limit=20
```

경로 파라미터:

| 이름 | 타입 | 설명 |
|---|---:|---|
| keyword_key | string | `ko:생명공학`, `en:biotechnology` 형태의 Keyword key |

쿼리 파라미터:

| 이름 | 타입 | 필수 | 기본값 | 설명 |
|---|---:|---:|---:|---|
| limit | int | X | 20 | 확장할 관련 키워드 최대 개수, 1~50 |
| min_paper_count | int | X | 1 | 최소 동시 등장 논문 수 |

응답 구조는 초기 키워드 그래프 조회와 동일하다.

## 프론트엔드 사용 기준

- `nodes[].key`를 그래프 노드의 고유 ID로 사용한다.
- `nodes[].name`을 화면 표시 이름으로 사용한다.
- `nodes[].is_center`가 `true`인 노드를 중심 노드로 표시한다.
- `edges[].source`, `edges[].target`은 `nodes[].key`를 참조한다.
- `edges[].paper_count`는 연결 강도 또는 edge 두께 가중치로 사용할 수 있다.
