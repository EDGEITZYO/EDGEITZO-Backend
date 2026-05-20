import json
import os
import re

import httpx
from dotenv import load_dotenv


def build_rag_context(retrieved_papers: list[dict]) -> str:
    context = ""

    for i, paper in enumerate(retrieved_papers, start=1):
        keywords = ", ".join(paper.get("keywords", []))
        distance = paper.get("distance")
        distance_line = f"distance: {distance}\n" if distance is not None else ""

        context += f"""
[논문 {i}]
paper_id: {paper.get("paper_id")}
title: {paper.get("title")}
keywords: {keywords}
{distance_line}retrieval_note: distance는 cosine distance 기준이며 0에 가까울수록 사용자 질문과 의미적으로 유사함
abstract:
{paper.get("abstract")}

"""
    return context.strip()


def build_rag_prompt(user_query: str, retrieved_papers: list[dict]) -> str:
    retrieved_context = build_rag_context(retrieved_papers)

    prompt = f"""
너는 Bio-me의 생명공학 논문 탐색 AI 연구 assistant야.

[사용자 질문]
{user_query}

위 [사용자 질문]에 적힌 문장을 실제 사용자 질문으로 간주한다.
사용자 질문이 비어 있다고 판단하지 않는다.

[검색된 논문 목록]
{retrieved_context}

[절대 기준]
1. 답변은 반드시 [검색된 논문 목록]의 title, keywords, abstract, distance만 근거로 작성한다.
2. 외부 지식, 최신 사례, 일반 상식, 모델이 알고 있는 배경지식은 절대 추가하지 않는다.
3. 제공된 논문 초록만으로 답할 수 없으면 아래 문장만 출력한다.
제공된 논문 초록만으로는 확인하기 어렵습니다.
4. 분석 과정, 의도 분석 문장, 영어 문장, "검색 요청을 분석했다" 같은 메타 문장을 출력하지 않는다.
5. 답변 제목, 인사말, 마무리 문장, 권유 문장을 작성하지 않는다.
6. 출력은 반드시 [정답 출력 예시]와 동일한 구조를 따른다.
7. [정답 출력 예시]의 예시 논문 제목과 예시 내용은 형식 참고용이다. 실제 답변에는 [검색된 논문 목록]에 있는 논문만 사용한다.

[관련도 판단 기준]
1. distance는 보조 지표로만 사용한다. 최종 판단은 사용자 질문과 title, keywords, abstract의 실제 연결성으로 한다.
2. distance가 0.70 이상이면 관련도가 낮은 논문으로 보고 근거 논문에 포함하지 않는다.
3. distance가 0.50~0.70이어도 title, keywords, abstract가 사용자 질문과 직접 연결되면 medium 이상으로 사용할 수 있다.
4. 각 논문 관련도는 high, medium, low 중 하나로 판단한다.
5. high 또는 medium 논문만 "2. 근거 논문"에 포함한다.
6. 사용자 질문의 핵심어가 title, keywords, abstract에 직접 나타나거나 같은 의미로 명확히 연결되면 high로 판단한다.
7. 교육과정, 인식 조사, 정책, 거버넌스처럼 질문과 간접적으로 연결되는 논문은 medium으로 판단한다.
8. 생명공학 윤리 질문에서는 초록이나 키워드에 윤리적 문제, 윤리적 측면, 도덕적 혼란, 인간 존엄성, 생명정책 거버넌스, 사회적 위험, 정치적 대응이 확인되는 논문을 관련 논문으로 판단한다.
9. 여러 논문이 관련되면 질문의 핵심 의도와 가장 직접 연결되는 논문을 먼저 제시한다.

[출력 형식 강제]
1. 일반 답변의 첫 줄은 반드시 "1. 요약 답변"이다.
2. 일반 답변은 반드시 아래 3개 섹션만 사용한다.
1. 요약 답변
2. 근거 논문
3. 추가 탐색 키워드
3. 섹션 제목 문구를 바꾸지 않는다.
4. "요약 답변", "근거 논문", "추가 탐색 키워드 제안"처럼 번호가 없거나 다른 제목을 쓰지 않는다.
5. 각 근거 논문은 반드시 아래 4개 필드를 모두 포함한다.
- 논문 제목:
- 관련도:
- 초록에서 확인된 근거:
- 이 논문이 질문과 연결되는 이유:
6. 위 4개 필드 중 하나라도 생략하지 않는다.
7. 추가 탐색 키워드는 정확히 3개만 작성한다.
8. 사용자가 "키워드만" 요청하면 [키워드만 출력 예시] 형식만 따른다.

[정답 출력 예시]
아래 예시는 형식만 보여준다. 예시 내용을 실제 답변에 복사하지 않는다.

1. 요약 답변
- 예시 사용자 질문과 관련된 논문은 2편입니다. 첫 번째 논문은 질문의 핵심 주제를 직접 다룹니다. 두 번째 논문은 질문과 연결되는 사회적 인식이나 정책 맥락을 보조적으로 설명합니다.

2. 근거 논문
- 논문 제목: 예시 논문 제목 A
- 관련도: high
- 초록에서 확인된 근거: 예시 논문 A의 초록에서 사용자 질문과 직접 연결되는 내용이 확인됩니다.
- 이 논문이 질문과 연결되는 이유: 예시 논문 A는 사용자 질문의 핵심 개념을 직접 다루기 때문입니다.

- 논문 제목: 예시 논문 제목 B
- 관련도: medium
- 초록에서 확인된 근거: 예시 논문 B의 초록에서 사용자 질문과 간접적으로 연결되는 내용이 확인됩니다.
- 이 논문이 질문과 연결되는 이유: 예시 논문 B는 사용자 질문의 사회적, 교육적, 정책적 맥락을 보조하기 때문입니다.

3. 추가 탐색 키워드
- 예시 키워드 1
- 예시 키워드 2
- 예시 키워드 3

[키워드만 출력 예시]
- 예시 키워드 1
- 예시 키워드 2
- 예시 키워드 3
"""
    return prompt.strip()


def call_claude(prompt: str) -> str | None:
    load_dotenv()

    api_key = os.getenv("ANTHROPIC_API_KEY") or os.getenv("CLAUDE_API_KEY")
    model = os.getenv("CLAUDE_MODEL")

    if not api_key:
        print("CLAUDE CALL SKIPPED: ANTHROPIC_API_KEY 또는 CLAUDE_API_KEY가 설정되어 있지 않습니다.")
        return None

    if not model:
        print("CLAUDE CALL SKIPPED: CLAUDE_MODEL이 설정되어 있지 않습니다.")
        return None

    response = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": 1200,
            "temperature": 0.1,
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                }
            ],
        },
        timeout=60,
    )
    response.raise_for_status()

    payload = response.json()
    text_blocks = [
        block.get("text", "")
        for block in payload.get("content", [])
        if block.get("type") == "text"
    ]
    return "\n".join(text_blocks).strip()


def _keyword_count(answer: str) -> int:
    if "3. 추가 탐색 키워드" not in answer:
        return 0

    keyword_section = answer.split("3. 추가 탐색 키워드", 1)[1]
    return len(re.findall(r"(?m)^- .+", keyword_section))


def evaluate_llm_answer(answer: str) -> dict[str, bool]:
    stripped = answer.strip()
    required_fields = [
        "- 논문 제목:",
        "- 관련도:",
        "- 초록에서 확인된 근거:",
        "- 이 논문이 질문과 연결되는 이유:",
    ]
    blocked_phrases = [
        "The user prompt is empty",
        "검색 요청을 분석",
        "Bio-me 논문 탐색 결과",
        "추가 탐색 키워드 제안",
    ]

    return {
        "starts_with_summary_section": stripped.startswith("1. 요약 답변"),
        "has_evidence_section": "2. 근거 논문" in stripped,
        "has_keyword_section": "3. 추가 탐색 키워드" in stripped,
        "has_all_evidence_fields": all(field in stripped for field in required_fields),
        "keyword_count_is_three": _keyword_count(stripped) == 3,
        "has_no_blocked_meta_text": not any(phrase in stripped for phrase in blocked_phrases),
        "mentions_core_governance_paper": "생명공학의 발전과 정치" in stripped,
        "uses_korean": any("가" <= char <= "힣" for char in stripped),
    }


if __name__ == "__main__":
    sample_query = "생명공학 윤리 관련 논문을 찾아줘"
    sample_papers = [
        {
            "paper_id": "JAKO202414433385011",
            "title": "2015 개정 교육과정 생명과학II와 공학일반에 제시된 생명공학기술 관련 학습 내용 분석",
            "keywords": ["생명공학기술", "교육과정", "윤리", "유전자 치료"],
            "distance": 0.5139,
            "abstract": (
                "이 연구는 2015 개정 교육과정 생명과학II와 공학일반에 제시된 생명공학기술 관련 "
                "학습 내용을 분석하였다. 생명과학II는 생명공학기술의 윤리적 측면에 초점을 두지만, "
                "공학일반 교과서는 윤리적 접근을 등한시하고 한국에서 금지된 생식세포 유전자 치료를 다룬다."
            ),
        },
        {
            "paper_id": "ATN0035022745",
            "title": "생명공학의 발전과 정치: 생명정책 거버넌스의 모색",
            "keywords": ["생명공학", "사회적 영향", "윤리적 문제", "정치적 대응", "거버넌스"],
            "distance": 0.5481,
            "abstract": (
                "현대 생명공학은 윤리적 문제뿐만 아니라 개인의 자유 및 자율성의 제한, "
                "생명자본의 문제, 정치적 불평등, 그리고 통제와 감시를 위한 도구화 등 "
                "정치적 문제들도 야기할 수 있다. 이에 대한 대응은 정치적 영역으로 귀결되며, "
                "본 논문은 생명정책 거버넌스를 제안한다."
            ),
        },
        {
            "paper_id": "JAKO200230758883726",
            "title": "생명공학에 대한 한국인들의 표상: 대학생들과 일반 성인들을 중심으로",
            "keywords": ["생명공학", "사회적 인식", "위험 판단", "기술 수용"],
            "distance": 0.5924,
            "abstract": (
                "본 연구는 생명공학과 관련 기술의 활용에 대한 일반 국민들의 인식과 태도를 분석하였다. "
                "한국 성인들의 인식 내용은 인간의 존엄성 손상, 인체에 유해한 부작용, 도덕적 혼란, "
                "상업적 악용 등의 불확실성이나 부작용에 대한 염려를 중심으로 표상되어 있었다."
            ),
        },
    ]

    generated_prompt = build_rag_prompt(sample_query, sample_papers)

    print("=" * 80)
    print("LLM RAG PROMPT V5 TEST")
    print("=" * 80)
    print(generated_prompt)
    print("=" * 80)
    print("PROMPT CHECK")
    print(f"query_included: {sample_query in generated_prompt}")
    print(f"paper_count: {generated_prompt.count('[논문 ')}")
    print(f"has_output_example: {'[정답 출력 예시]' in generated_prompt}")
    print(f"has_keyword_only_example: {'[키워드만 출력 예시]' in generated_prompt}")
    print(f"has_no_meta_rule: {'분석 과정, 의도 분석 문장, 영어 문장' in generated_prompt}")
    print(f"has_exact_first_line_rule: {'첫 줄은 반드시 \"1. 요약 답변\"' in generated_prompt}")
    print(f"has_required_fields_rule: {'위 4개 필드 중 하나라도 생략하지 않는다' in generated_prompt}")
    print(f"has_three_keyword_rule: {'추가 탐색 키워드는 정확히 3개만 작성한다' in generated_prompt}")
    print("=" * 80)
    print("CLAUDE RESPONSE TEST")
    claude_answer = call_claude(generated_prompt)

    if claude_answer is None:
        print("result: prompt construction only")
    else:
        print(claude_answer)
        print("=" * 80)
        print("LLM ANSWER CHECK")
        print(json.dumps(evaluate_llm_answer(claude_answer), ensure_ascii=False, indent=2))
