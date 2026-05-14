# Neo4j Graph Schema

작성일: 2026-05-14

## 목적

정규화된 ScienceON 논문 데이터(`scienceon_keywords_normalized.json`)를 Neo4j에 적재해 논문, 키워드, 저자, 저널, 발행연도 간 관계를 탐색할 수 있게 한다.

ChromaDB는 사용자 질문과 논문 초록 간 의미 기반 검색을 담당하고, Neo4j는 검색된 논문에서 출발해 키워드 관계와 연구 흐름을 확장하는 역할을 담당한다.

## 입력 데이터

```text
data/parsed/scienceon_keywords_normalized.json
```

입력 데이터 기준:

- 전체 논문: 1,000건
- 한국어 키워드 고유값: 1,873개
- 영어 키워드 고유값: 1,619개
- 저자 고유값: 1,308개
- 저널 고유값: 95개

## 노드

### Paper

논문 단위 노드다. `CN`을 고유 ID로 사용한다.

```text
(:Paper {
  cn,
  db_code,
  title,
  title_en,
  abstract,
  abstract_en,
  doi,
  pubyear,
  journal_name,
  issn,
  keyword_raw,
  keyword2_raw,
  loaded_at
})
```

### Keyword

한국어 키워드와 영어 키워드를 같은 라벨로 저장하되, `lang`으로 구분한다. 동일한 문자열이라도 언어가 다르면 다른 노드로 본다.

```text
(:Keyword {
  key,              // 예: ko:생명공학, en:biotechnology
  name,
  normalized_name,
  lang,             // ko | en
  source_field,     // Keyword | Keyword2
  loaded_at
})
```

### Author

저자명 기준 노드다.

```text
(:Author {
  name,
  loaded_at
})
```

### Journal

저널명 기준 노드다. DIKO처럼 저널명이 없는 데이터는 저널 노드를 만들지 않는다.

```text
(:Journal {
  name,
  loaded_at
})
```

### Year

발행연도 기준 노드다.

```text
(:Year {
  value,
  loaded_at
})
```

## 엣지

### Paper -> Keyword

논문이 가진 한국어/영어 키워드 관계다.

```text
(:Paper)-[:HAS_KEYWORD {
  lang,
  source_field,
  loaded_at
}]->(:Keyword)
```

### Keyword -> Keyword

같은 논문 안에서 함께 등장한 키워드 관계다. 방향은 중복 방지를 위해 key 정렬 순서로 고정한다.

```text
(:Keyword)-[:RELATED_TO {
  paper_count,
  lang_pair,
  loaded_at
}]->(:Keyword)
```

`paper_count`는 두 키워드가 함께 등장한 논문 수다. 서비스에서는 이 값을 기준으로 관련 키워드 확장, 그래프 시각화 가중치, 추천 키워드 정렬에 사용할 수 있다.

### Author -> Paper

저자와 논문 관계다.

```text
(:Author)-[:AUTHORED {
  order,
  loaded_at
}]->(:Paper)
```

### Paper -> Journal

논문과 저널 관계다.

```text
(:Paper)-[:PUBLISHED_IN {
  loaded_at
}]->(:Journal)
```

### Paper -> Year

논문과 발행연도 관계다.

```text
(:Paper)-[:PUBLISHED_IN_YEAR {
  loaded_at
}]->(:Year)
```

## 제약 조건 및 인덱스

적재 스크립트는 다음 제약 조건과 인덱스를 생성한다.

```cypher
CREATE CONSTRAINT paper_cn_unique IF NOT EXISTS
FOR (p:Paper) REQUIRE p.cn IS UNIQUE;

CREATE CONSTRAINT keyword_key_unique IF NOT EXISTS
FOR (k:Keyword) REQUIRE k.key IS UNIQUE;

CREATE CONSTRAINT author_name_unique IF NOT EXISTS
FOR (a:Author) REQUIRE a.name IS UNIQUE;

CREATE CONSTRAINT journal_name_unique IF NOT EXISTS
FOR (j:Journal) REQUIRE j.name IS UNIQUE;

CREATE CONSTRAINT year_value_unique IF NOT EXISTS
FOR (y:Year) REQUIRE y.value IS UNIQUE;

CREATE INDEX paper_pubyear_index IF NOT EXISTS
FOR (p:Paper) ON (p.pubyear);

CREATE INDEX keyword_lang_index IF NOT EXISTS
FOR (k:Keyword) ON (k.lang);

CREATE FULLTEXT INDEX paper_text_fulltext IF NOT EXISTS
FOR (p:Paper) ON EACH [p.title, p.title_en, p.abstract, p.abstract_en];

CREATE FULLTEXT INDEX keyword_name_fulltext IF NOT EXISTS
FOR (k:Keyword) ON EACH [k.name, k.normalized_name];
```

## 적재 명령

Neo4j가 실행 중이고 `.env`에 접속 정보가 설정되어 있어야 한다.

```bash
python scripts/load_neo4j_graph.py
```

로컬 Docker Compose 기본값을 사용하면 다음 값으로 접속한다.

```text
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=password1234
```

AuraDB에 적재할 때는 Aura 콘솔에서 받은 connection URI와 credential을 `.env`에 넣는다.

```text
NEO4J_URI=neo4j+s://xxxxx.databases.neo4j.io
NEO4J_USER=neo4j
NEO4J_PASSWORD=발급받은비밀번호
```

`NEO4J_USER` 대신 `NEO4J_USERNAME`을 사용해도 적재 스크립트는 인식한다.

그래프를 완전히 다시 만들 때:

```bash
python scripts/load_neo4j_graph.py --reset
```

실제 적재 없이 생성될 노드/엣지 수만 확인할 때:

```bash
python scripts/load_neo4j_graph.py --dry-run
```

일부 논문만 테스트 적재할 때:

```bash
python scripts/load_neo4j_graph.py --limit 50
```

## 서비스 조회 예시

특정 키워드에서 관련 키워드 확장:

```cypher
MATCH (:Keyword {key: "ko:생명공학"})-[r:RELATED_TO]-(k:Keyword)
RETURN k.name, k.lang, r.paper_count
ORDER BY r.paper_count DESC
LIMIT 20;
```

특정 논문 주변 그래프:

```cypher
MATCH (p:Paper {cn: $cn})-[:HAS_KEYWORD]->(k:Keyword)
OPTIONAL MATCH (k)-[r:RELATED_TO]-(rk:Keyword)
RETURN p, k, r, rk
LIMIT 100;
```

ChromaDB 검색 결과 논문에서 그래프 확장:

```cypher
MATCH (p:Paper)-[:HAS_KEYWORD]->(k:Keyword)
WHERE p.cn IN $paper_cns
OPTIONAL MATCH (k)-[r:RELATED_TO]-(related:Keyword)
RETURN p.cn, p.title, k.name, related.name, r.paper_count
ORDER BY r.paper_count DESC;
```
