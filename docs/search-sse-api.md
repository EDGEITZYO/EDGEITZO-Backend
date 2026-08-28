# 슬롯 대화 SSE 스트리밍 API 규격

## 엔드포인트

`POST /api/v1/search/chat/stream`

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
  "selected_options": ["string"] | null,
  "force_start": false
}
```

## 응답 형식

`Content-Type: text/event-stream`

각 프레임:
```
data: {"type": "<event_type>", ...payload}\n\n
```

---

## 이벤트 타입

### 1. `slot_update`
슬롯 값이 바뀔 때마다 슬롯별로 1회 emit. 턴당 최대 4회(슬롯 개수 기준).

```json
{"type": "slot_update", "slot": "keywords", "value": ["딥러닝", "자연어처리"]}
```

| 필드 | 타입 | 설명 |
|------|------|------|
| `slot` | `string` | `"research_purpose"` \| `"paper_scope"` \| `"pub_year_range"` \| `"keywords"` |
| `value` | `any` | 새 슬롯 값 |

---

### 2. `completeness`
구체화도 업데이트. options 처리 후 1회, 그래프 실행 후 1회 — 턴당 2회.

```json
{"type": "completeness", "pct": 70, "stage": "none"}
```

| 필드 | 타입 | 설명 |
|------|------|------|
| `pct` | `number` | 0–100 정수 |
| `stage` | `string` | `"none"` (<80) \| `"ready"` (80–89) \| `"emphasized"` (90–99) \| `"complete"` (100) |

---

### 3. `keyword_progress`
키워드 추출이 예상되는 턴에만 emit. 추출이 없는 턴(이미 키워드 확정)에는 오지 않는다.

키워드 추출은 그래프 내부에서 단일 LLM 호출로 완료되므로 **`started` / `completed` 두 단계만** 있다. 중간 진행률 이벤트는 없다.

```json
{"type": "keyword_progress", "stage": "started"}
{"type": "keyword_progress", "stage": "completed", "keywords": ["딥러닝", "자연어처리"]}
```

---

### 4. `token`
`ai_message`를 `sse_chunk_size`자 단위로 chunking해서 흘림 (현재 설정: **2자 / 40ms 간격**).

> **서버 chunking (A방식)**: 실제 LLM 토큰 스트리밍이 아님. 완성된 문자열을 서버에서 잘라 일정 간격으로 전송. 타이핑 효과 구현용.
> Python 문자열 슬라이싱(코드포인트 단위)이므로 한글 깨짐 없음.
>
> **체감 지연에 유의**: 이 구간은 의도된 연출 지연이다. 요약이 120~160자면
> `ceil(len/2) × 40ms` = **2.4~3.2초**가 여기서 소비된다. 검색이 느리다고 느껴질 때
> 가장 먼저 확인할 값이며, `sse_chunk_size` / `sse_chunk_delay_seconds`로 조절한다.

```json
{"type": "token", "text": "입력하신 내용"}
```

---

### 5. `done`
턴 종료 시 1회. `POST /search/chat` JSON 응답과 동일한 필드를 포함.

```json
{
  "type": "done",
  "session_id": "string",
  "ai_message": "string",
  "options": [{"label": "string", "value": "string"}],
  "allow_multiple": false,
  "search_ready": false,
  "completeness_pct": 70,
  "search_stage": "none",
  "search_preview": {
    "topic": "string | null",
    "purpose": "string | null",
    "scope": "string | null",
    "pub_year": "string | null",
    "keywords": [],
    "completeness_pct": 70
  },
  "final_search_params": null
}
```

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
서버 예외 발생 시. **`error` 이후 `done`은 오지 않는다. 스트림이 즉시 종료된다.**

그래프 실행 중 예외가 나면 이미 전송된 `slot_update` / `completeness` 이벤트는 부분 상태다.
클라이언트는 `error` 수신 시 해당 턴의 부분 상태 업데이트를 롤백하거나 무시해야 한다.

```json
{"type": "error", "message": "서버 오류가 발생했어요. 잠시 후 다시 시도해주세요."}
```

---

## 이벤트 순서 (정상 흐름)

```
slot_update  (×0–4, selected_options 처리분)
completeness (1회)
keyword_progress {stage:"started"}  ← 키워드 추출 예상 턴만
[graph.ainvoke — LLM 호출, 블로킹 구간]
slot_update  (×0–4, 그래프 실행 후 변경분)
completeness (1회)
keyword_progress {stage:"completed"} ← started 보낸 턴만
token        (×N, 10자 chunk)
done         (1회)  ← 카드가 여기서 화면에 뜬다 (선정 사유는 아직 없음)
[선정 사유 생성 — 상위 N건, 병렬]
selection_reason (×0–N, 논문당 1회)  ← 스켈레톤을 채운다
```

`done` 이후 `selection_reason`이 뒤따르므로, **클라이언트는 `done`을 받았다고 리더를 닫으면
안 된다.** 스트림이 끝날 때까지(`reader.read()`의 `done: true`) 계속 읽어야 한다.

---

## 클라이언트 구현 예시 (fetch + ReadableStream)

```typescript
const res = await fetch('/api/v1/search/chat/stream', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ session_id, message, selected_options }),
});

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
      case 'slot_update':    /* 슬롯 UI 업데이트 */ break;
      case 'completeness':   /* 진행률 바 업데이트 */ break;
      case 'keyword_progress': /* 키워드 로딩 인디케이터 */ break;
      case 'token':          /* ai_message에 text 누적 */ break;
      case 'done':
        /* 최종 상태 저장, 카드 렌더 (선정 사유 자리는 스켈레톤).
           주의: 여기서 break로 루프를 빠져나가지 말 것 — selection_reason이 뒤따른다 */
        break;
      case 'selection_reason':
        /* event.paper_id 카드의 스켈레톤을 event.reason으로 교체.
           highlight_start/end가 null이 아니면 그 구간만 강조 처리 */
        break;
      case 'error':          /* 오류 표시, 부분 상태 롤백 */ break;
    }
  }
}
```
