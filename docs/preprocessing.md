## 전처리 파이프라인

### 전체 순서
1. 논문 수집 (ScienceON API)
2. 파싱 (원문 텍스트 추출)
3. 키워드 정규화
4. 임베딩 모델 선택 및 적용
5. ChromaDB 적재 + 유사도 검색 테스트
6. Neo4j 노드·엣지 스키마 설계 및 적재

---

### 로컬 세팅

**사전 조건**
- Python 3.11+
- `.env` 파일 세팅 (`.env.example` 활용,  노션 키 파일 참고)


**데이터 파일 위치**
- `scienceon_parsed.json` 파일 다운 받아서 아래 경로에 저장
(!! 직접 스크립트 실행 금지 — 데이터 일관성 유지)
```
data/parsed/scienceon_parsed.json
```

---


### 데이터 파일 구조 (`scienceon_parsed.json`)

```json
{
  "meta": {
    "parsed_at": "...",
    "total_count": 1000,
    "source": "scienceon_raw.json"
  },
  "papers": [
    {
      "CN": "JAKO...",
      "DBCode": "JAKO",
      "Title": "...",
      "Title2": "...",
      "Abstract": "...",
      "Abstract2": "...",
      "Keyword": ["키워드1", "키워드2"],
      "keyword_raw": "키워드1 . 키워드2",
      "Keyword2": ["keyword1", "keyword2"],
      "keyword2_raw": "keyword1 . keyword2",
      "ISSN": ["1234-5678"],
      "DOI": "https://doi.org/...",
      "Pubyear": 2024,
      "JournalName": "...",
      "Author": ["저자1", "저자2"]
    }
  ]
}
```

### 필드 설명

| 필드 | 타입 | null 가능 | 비고 |
|---|---|---|---|
| CN | string | ❌ | 논문 고유 ID (PK) |
| DBCode | string | ❌ | JAKO / JAFO / DIKO 등 |
| Title | string | ❌ | 한국어 제목 |
| Title2 | string | ✅ | 영어 제목, 국내 논문 미입력 多 |
| Abstract | string | ❌ | 한국어 초록 (null 논문 제거됨) |
| Abstract2 | string | ✅ | 영어 초록 |
| Keyword | list | ❌ | 한국어 키워드 리스트 |
| keyword_raw | string | ❌ | Keyword 원본 문자열 |
| Keyword2 | list | ✅ | 영어 키워드 리스트 (OpenAlex 매핑용) |
| keyword2_raw | string | ✅ | Keyword2 원본 문자열 |
| ISSN | list | ✅ | SJR CSV 매핑용, DIKO 미제공 |
| DOI | string | ✅ | 런타임 API 보완 예정, DIKO 미제공 |
| Pubyear | int | ❌ | 발행연도 |
| JournalName | string | ✅ | DIKO 미제공 |
| Author | list | ❌ | 저자 리스트 |