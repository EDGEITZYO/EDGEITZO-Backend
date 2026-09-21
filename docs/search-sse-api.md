# 자연어 검색 SSE 스트리밍 API 규격

## 엔드포인트

`POST /api/v1/search/chat/stream`

`POST /api/v1/search/chat`(비스트리밍)과 같은 대화를 SSE로 흘린다. `done` 이벤트의 payload가
`/search/chat`의 JSON 응답(`ChatResponse`)과 같은 필드다.

---

> **⚠️ 클라이언트 수신 방식: `fetch` + `ReadableStream` 필수**
>
> 이 엔드포인트는 **POST**이므로 브라우저 내장 `EventSource`를 사용할 수 없다.
> `EventSource`는 GET 전용이며 요청 body를 실을 수 없다.
> 반드시 `fetch` API + `response.body.getReader()`(ReadableStream)로 수신해야 한다.

---

## 요청

```json
{
  "session_id": "string | null",
  "message": "string",
  "chip_id": "string | null",
  "chip_type": "year | paper_type | citation | expand | null",
  "sort_order": "relevance | year_asc | year_desc | citation_desc | null",
  "pub_year_start": null,
  "paper_type": null,
  "kci_only": null,
  "sci_only": null
}
```

- 첫 턴은 `session_id: null`, `message`에 검색 주제. 이후 턴은 응답의 `session_id`를 재사용한다.
- 칩 클릭은 `chip_id` + `chip_type`(직전 응답에서 받은 값 그대로). 이때 `message`는 무시된다.
- `pub_year_start`/`paper_type`/`kci_only`/`sci_only`는 필터 패널의 현재값. **필드를 아예 빼면
  이전 값 유지, 명시적으로 null/false를 보내면 해제**다. 각 필드의 정확한 의미는 Swagger의
  `ChatRequest` 설명을 따른다(이 문서는 스트리밍 규격만 다룬다).

### 이용 한도 (공모전 기간 한정, 켜져 있을 때만)

한도를 넘으면 **스트림을 열기 전에 일반 JSON `429`** 로 응답한다. SSE 프레임이 아니다.
`error_code`가 `AI_CHAT_LIMIT`(새 채팅 소진) / `AI_TURN_LIMIT`(이 채팅의 턴 소진)로 구분되고,
`message`는 그대로 사용자에게 보여줄 수 있는 문장이다.

## 응답 형식

`Content-Type: text/event-stream`

각 프레임:
```
data: {"type": "<event_type>", ...payload}\n\n
```

> **`event:` 라인은 없다.** 이벤트 종류는 JSON 안의 `type` 필드로만 구분한다.

---

## 이벤트 타입

### 1. `search_started` / `searching`
턴 시작 직후 순서대로 1회씩. payload 없음. 로딩 UI 전환용이다.

```json
{"type": "search_started"}
{"type": "searching"}
```

---

### 2. `heartbeat`
검색(그래프 실행)이 길어질 때 유휴 커넥션이 끊기지 않도록 **15초 간격**으로 보낸다.
클라이언트는 무시해도 된다.

```json
{"type": "heartbeat"}
```

그래프 실행이 `graph_timeout_seconds`(기본 300초)를 넘기면 `done` 대신 `error`가 나가고
스트림이 끝난다. 이때 소모된 이용 횟수는 돌려준다.

---

### 3. `papers_found` / `fetching`
검색이 끝나고 결과 건수가 확정된 시점에 1회씩.

```json
{"type": "papers_found", "count": 71}
{"type": "fetching"}
```

`count`는 `done`의 `total_count`와 같은 값이다.

---

### 4. `token`
`ai_summary`를 `sse_chunk_size`자 단위로 chunking해서 흘림 (현재 설정: **2자 / 40ms 간격**).

> **서버 chunking (A방식)**: 실제 LLM 토큰 스트리밍이 아님. 완성된 문자열을 서버에서 잘라 일정 간격으로 전송. 타이핑 효과 구현용.
> Python 문자열 슬라이싱(코드포인트 단위)이므로 한글 깨짐 없음.
>
> **체감 지연에 유의**: 이 구간은 의도된 연출 지연이다. 요약이 120~160자면
> `ceil(len/2) × 40ms` = **2.4~3.2초**가 여기서 소비된다. 검색이 느리다고 느껴질 때
> 가장 먼저 확인할 값이며, `sse_chunk_size` / `sse_chunk_delay_seconds`로 조절한다.

```json
{"type": "token", "text": "입력하신 내용"}
```

`ai_summary`가 없으면(요약 실패·결과 0건) 이 이벤트는 오지 않는다. `done`의 `summary_failed`로
구분한다.

---

### 5. `done`
턴 종료 시 1회. `POST /search/chat` JSON 응답과 동일한 필드를 포함한다.

```json
{
  "type": "done",
  "session_id": "string",
  "filters": {"pub_year_start": null, "paper_type": null, "citation_min": null, "kci_only": null, "sci_only": null, "keywords": ["치매 조기진단"]},
  "sort_order": "relevance",
  "history": [],
  "result_items": [],
  "total_count": 71,
  "narrow_chips": [],
  "expand_chips": [],
  "keyword_map_anchor": {"key": "ko:치매", "name_ko": "치매", "name_en": null, "paper_count": 2},
  "ai_summary": "string | null",
  "summary_failed": false,
  "fallback": null,
  "is_broad_result": true,
  "remaining_new_chats": 1,
  "remaining_turns": 2
}
```

| 필드 | 설명 |
|------|------|
| `filters` | 지금까지 누적된 검색 조건. `keywords`는 LLM이 뽑은 검색 키워드다 |
| `history` | 탐색 경로(검색→좁히기→확장). 각 스텝이 그 시점의 `result_items`를 들고 있다 |
| `result_items` | 결과 논문 목록. `sort_order` 기준 정렬 |
| `narrow_chips` / `expand_chips` | 좁히기/확장 칩. `chip_id`+`chip_type`을 다음 턴에 그대로 보내면 적용된다. `total_count`가 4 이하면 `narrow_chips`는 빈 배열 |
| `keyword_map_anchor` | **선택 필드.** 이 검색 결과에 대응하는 키워드맵 앵커 노드. 검색 결과에서 키워드맵으로 넘어가는 동선이 있을 때 `GET /api/v1/keyword-map?key={key}`로 넘기면 404 없이 그래프를 받는다. 그런 동선이 없으면 무시해도 된다. 결과 0건이거나 못 찾으면 `null` |
| `ai_summary` / `summary_failed` | 요약 문장과 그 실패 여부. `summary_failed=true`라도 검색 자체는 성공이다 |
| `fallback` | `null`(정상) / `clarify`(검색어가 비어 명확화 필요) / `no_result`(0건) / `off_topic`(검색과 무관한 발화라 재검색 안 함) / `topic_change`(주제 전환으로 판단해 새 주제로 재검색함) |
| `remaining_new_chats` / `remaining_turns` | 남은 이용 횟수. 한도가 꺼져 있으면 `null` |

> **`keyword_map_anchor`는 언제 쓰나**: 키워드 탐색이 AI 검색과 무관한 독립 화면이라면 쓸 일이 없다.
> 기존 `GET /keyword-map?keyword=<사용자가 고른 키워드>` 방식이 그대로 동작하며, 이 필드 때문에
> `/search/chat`을 따로 호출할 이유는 없다. 검색 결과에서 키워드맵으로 **이어지는 동선이 있을 때만**
> 쓰는 값이다. Neo4j 키워드 노드는 논문 원본 키워드로 만들어져 있어
> 사용자 어휘·LLM 키워드와 어휘가 다르다. 실측으로, 검색이 정상 성공한 턴의 `filters.keywords`
> 3개(치매 조기진단 / 인지기능 저하 / 신경영상 바이오마커)가 **하나도** 노드로 존재하지 않았다.
> `keyword_map_anchor`는 검색 결과 논문들의 원본 키워드에서 뽑으므로 노드가 반드시 존재한다.
> `key` 값에는 콜론·한글·슬래시가 들어가므로(`ko:치매`, `en:acetate/butyrate ratio`)
> URL에 직접 이어붙이지 말고 `URLSearchParams`/`encodeURIComponent`로 인코딩할 것.

---

### 6. `selection_reason`

**`done` 이후에** 논문 1건당 1회 emit. 카드에 노출할 「논문 선정 이유」(명세 02-11)를 실어 보낸다.

> **왜 `done` 안에 안 넣나**: 선정 사유는 논문마다 LLM 호출이 필요해 수 초가 걸린다.
> `done`에 함께 실으면 그 시간 동안 카드가 화면에 아예 뜨지 않는다. 그래서 카드는 사유 없이
> 먼저 `done`으로 보내고, 사유는 뒤이어 흘린다. 프런트는 카드를 먼저 그린 뒤 사유 자리에
> 스켈레톤을 띄웠다가 이 이벤트로 채우면 된다.

```json
{
  "type": "selection_reason",
  "paper_id": "JAKO202216466710649",
  "reason": "단일세포 해상도로 개별 세포에만 존재해 조직 단위 분석에서는 검출되지 않던 은닉 변이를 분석한 연구입니다. ...",
  "highlight_start": 0,
  "highlight_end": 11,
  "cached": true
}
```

| 필드 | 타입 | 설명 |
|------|------|------|
| `paper_id` | `string` | `done`의 `result_items[].paper_id`와 대응 |
| `reason` | `string` | 선정 사유 본문. 공백 포함 150~200자, 3문장. 드물게 220자까지 나올 수 있으니 레이아웃은 220자 기준으로 잡을 것 |
| `highlight_start` | `number \| null` | 강조 구절의 `reason` 내 시작 위치(0-based). null이면 강조 없이 본문만 표시 |
| `highlight_end` | `number \| null` | 끝 위치(exclusive). `reason.slice(start, end)`가 강조 대상 |
| `cached` | `boolean` | true면 기존 생성분 재사용 (LLM 호출 없음) |

**주의**
- **모든 논문에 대해 오지 않는다.** 첫 화면에 보이는 상위 N건(`search_selection_reason_initial_count`, 기본 10)만 emit된다. 스크롤·정렬 변경·필터 적용으로 새로 보이는 논문은 프런트가 **`POST /api/v1/search/selection-reasons`** 로 따로 요청해야 한다.
- 초록이 없거나 생성에 실패한 논문은 **이벤트 자체가 오지 않는다.** 일정 시간 뒤에도 안 오면 사유 영역을 비우거나 숨기면 된다.
- 이 이벤트가 하나도 오지 않아도 검색은 정상이다 (LLM 예산 소진 등).

---

### 7. `error`
서버 예외·타임아웃 시. **`error` 이후 `done`은 오지 않는다. 스트림이 즉시 종료된다.**

```json
{"type": "error", "message": "서버 오류가 발생했어요. 잠시 후 다시 시도해주세요."}
```

`done`을 받기 전에 끊긴 턴은 이용 횟수를 돌려받는다. 클라이언트는 이 턴의 부분 상태
(진행 중이던 로딩 UI, 누적하던 `token`)를 버리고 이전 상태를 유지하면 된다.

---

## 이벤트 순서 (정상 흐름)

```
search_started   (1회)
searching        (1회)
heartbeat        (×0–N, 검색이 15초를 넘길 때만)
[graph.ainvoke — 검색 + LLM 호출, 블로킹 구간]
papers_found     (1회)
fetching         (1회)
token            (×N, 2자 chunk — ai_summary가 있을 때만)
done             (1회)  ← 카드가 여기서 화면에 뜬다 (선정 사유는 아직 없음)
[선정 사유 생성 — 상위 10건, 병렬]
selection_reason (×0–10, 논문당 1회)  ← 스켈레톤을 채운다
```

`done` 이후 `selection_reason`이 뒤따르므로, **클라이언트는 `done`을 받았다고 리더를 닫으면
안 된다.** 스트림이 끝날 때까지(`reader.read()`의 `done: true`) 계속 읽어야 한다.

---

## 클라이언트 구현 예시 (fetch + ReadableStream)

```typescript
const res = await fetch('/api/v1/search/chat/stream', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ session_id, message }),
});

if (res.status === 429) { /* 이용 한도 — 일반 JSON. error_code로 분기 */ }

const reader = res.body!.getReader();
const decoder = new TextDecoder();
let buffer = '';

while (true) {
  const { done, value } = await reader.read();
  if (done) break;

  buffer += decoder.decode(value, { stream: true });
  const frames = buffer.split('\n\n');
  buffer = frames.pop() ?? '';          // 마지막 미완성 프레임 보존

  for (const frame of frames) {
    const line = frame.replace(/^data: /, '').trim();
    if (!line) continue;
    const event = JSON.parse(line);

    switch (event.type) {
      case 'search_started':
      case 'searching':
      case 'fetching':       /* 로딩 단계 표시 */ break;
      case 'heartbeat':      /* 무시 */ break;
      case 'papers_found':   /* event.count 표시 */ break;
      case 'token':          /* ai_summary에 text 누적 */ break;
      case 'done':
        /* 최종 상태 저장, 카드 렌더 (선정 사유 자리는 스켈레톤).
           keyword_map_anchor가 있으면 그 key로 키워드맵 호출, null이면 빈 상태.
           주의: 여기서 break로 루프를 빠져나가지 말 것 — selection_reason이 뒤따른다 */
        break;
      case 'selection_reason':
        /* event.paper_id 카드의 스켈레톤을 event.reason으로 교체.
           highlight_start/end가 null이 아니면 그 구간만 강조 처리 */
        break;
      case 'error':          /* 오류 표시, 이번 턴 부분 상태 폐기 */ break;
    }
  }
}
```
