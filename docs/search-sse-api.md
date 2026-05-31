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
`ai_message`를 10자 단위로 chunking해서 흘림.

> **서버 chunking (A방식)**: 실제 LLM 토큰 스트리밍이 아님. 완성된 문자열을 서버에서 10자씩 잘라 15ms 간격으로 전송. 타이핑 효과 구현용.
> Python 문자열 슬라이싱(코드포인트 단위)이므로 한글 깨짐 없음.

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

### 6. `error`
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
done         (1회)
```

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
      case 'done':           /* 최종 상태 저장, 옵션 버튼 렌더 */ break;
      case 'error':          /* 오류 표시, 부분 상태 롤백 */ break;
    }
  }
}
```
