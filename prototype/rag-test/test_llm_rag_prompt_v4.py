import json
import os

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
너는 생명공학 논문 탐색을 도와주는 AI 연구 assistant야.

반드시 아래 제공된 논문 초록과 메타데이터만 근거로 답변해.
제공된 논문에 없는 내용은 추측하지 말고
"제공된 논문 초록만으로는 확인하기 어렵습니다."라고 답변해.

[사용자 질문]
{user_query}

위 [사용자 질문]에 적힌 문장을 실제 사용자 질문으로 간주한다.
사용자 질문이 비어 있다고 판단하지 않는다.

[검색된 논문 목록]
{retrieved_context}

[답변 규칙]
1. 제공된 논문 초록 내용만 사용한다.
2. 논문에 없는 사실은 생성하지 않는다.
3. 각 근거에는 논문 제목을 함께 표시한다.
4. 사용자가 다음 탐색을 이어갈 수 있도록 관련 키워드를 제안한다.
5. 답변은 한국어로 작성한다.
6. 불필요한 제목, 인사말, 마무리 문장은 작성하지 않는다.
7. 답변 제목은 작성하지 않는다.
8. 반드시 [답변 형식]의 1, 2, 3번 항목부터 시작한다.
9. 답변 형식 외의 추가 문장은 작성하지 않는다.
10. 사용자가 "키워드만" 요청하면 설명 없이 키워드 목록만 출력한다.
11. 검색 순위와 distance는 보조 지표로만 사용하고, 사용자 질문과 논문 초록의 실제 연결성을 다시 판단한다.
12. distance가 0.70 이상이면 관련도가 낮은 논문으로 보고 근거 논문에 포함하지 않는다.
13. distance가 0.50~0.70이면 보통 관련도로 보고, 제목 또는 초록이 질문 의도와 직접 연결될 때만 근거로 사용한다.
14. 각 논문의 관련도를 high, medium, low 중 하나로 내부 판단하고, high 또는 medium 논문만 근거 논문에 포함한다.
15. 모든 논문이 low라면 "제공된 논문 초록만으로는 확인하기 어렵습니다."라고 답변한다.
16. 근거는 초록에서 확인되는 내용만 요약하고, 일반 상식이나 외부 지식으로 보완하지 않는다.
17. 추가 탐색 키워드는 제공된 title, keywords, abstract, 사용자 질문에서 확인 가능한 표현을 중심으로 만든다.
18. 첫 번째 줄은 반드시 "1. 요약 답변"이어야 한다.
19. "근거 논문", "추가 탐색 키워드" 같은 제목으로 답변을 시작하지 않는다.
20. 요약 답변 섹션은 절대 생략하지 않는다.
21. 추가 탐색 키워드는 정확히 3개만 작성한다.
22. 관련도는 high, medium, low 중 하나로 판단하되, 사용자 질문과 가장 직접 연결되는 논문은 high로 분류한다.
23. 교육과정, 인식 조사, 정책 논문처럼 질문과 간접적으로 연결되는 논문은 medium으로 분류한다.
24. 여러 논문이 모두 관련되어도 질문의 핵심 의도와 가장 직접적으로 맞는 논문을 먼저 제시한다.
25. 섹션 제목은 반드시 번호를 포함해 작성한다.
26. "요약 답변"이라고만 쓰지 말고 반드시 "1. 요약 답변"으로 작성한다.
27. "근거 논문"이라고만 쓰지 말고 반드시 "2. 근거 논문"으로 작성한다.
28. "추가 탐색 키워드"라고만 쓰지 말고 반드시 "3. 추가 탐색 키워드"로 작성한다.
29. 각 근거 논문은 반드시 아래 4개 필드를 모두 포함한다: 논문 제목, 관련도, 초록에서 확인된 근거, 이 논문이 질문과 연결되는 이유.
30. 근거 논문의 4개 필드 중 하나라도 생략하지 않는다.
31. 각 필드명은 [답변 형식]에 적힌 문구와 동일하게 작성한다.

[답변 형식]

1. 요약 답변
- 사용자 질문에 대해 3~5문장으로 요약한다.

2. 근거 논문
각 논문마다 아래 4개 필드를 모두 작성한다.

- 논문 제목:
- 관련도: high 또는 medium
- 초록에서 확인된 근거:
- 이 논문이 질문과 연결되는 이유:

3. 추가 탐색 키워드
- 키워드 1
- 키워드 2
- 키워드 3
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
            "temperature": 0.2,
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


def evaluate_llm_answer(answer: str) -> dict[str, bool]:
    return {
        "has_summary_section": "1. 요약 답변" in answer,
        "has_evidence_section": "2. 근거 논문" in answer,
        "has_keyword_section": "3. 추가 탐색 키워드" in answer,
        "mentions_first_paper": "생명공학의 발전과 정치" in answer,
        "mentions_second_paper": "생명공학에 대한 한국인들의 표상" in answer,
        "uses_korean": any("가" <= char <= "힣" for char in answer),
    }


if __name__ == "__main__":
    sample_query = "생명공학기술의 사회적 영향과 정치적 대응을 다룬 논문을 찾아줘"
    sample_papers = [
        {
            "paper_id": "ATN0035022745",
            "title": "생명공학의 발전과 정치: 생명정책 거버넌스의 모색",
            "keywords": ["생명공학", "사회적 영향", "정치적 대응", "거버넌스"],
            "distance": 0.3984,
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
            "distance": 0.4412,
            "abstract": (
                "본 연구는 생명공학과 관련 기술의 활용에 대한 일반 국민들의 인식과 태도, "
                "그리고 그 인식과 태도에 영향을 미치는 심리적 요인들을 탐색하였다. "
                "생명공학 연구 및 기술 활용에 대한 긍정적 기대와 부작용에 대한 염려가 함께 나타났다."
            ),
        },
    ]

    generated_prompt = build_rag_prompt(sample_query, sample_papers)

    print("=" * 80)
    print("LLM RAG PROMPT TEST")
    print("=" * 80)
    print(generated_prompt)
    print("=" * 80)
    print("PROMPT CHECK")
    print(f"query_included: {sample_query in generated_prompt}")
    print(f"paper_count: {generated_prompt.count('[논문 ')}")
    print(f"has_grounding_rule: {'제공된 논문 초록 내용만 사용한다' in generated_prompt}")
    print(f"has_no_hallucination_rule: {'논문에 없는 사실은 생성하지 않는다' in generated_prompt}")
    print(f"has_fallback_message: {'제공된 논문 초록만으로는 확인하기 어렵습니다.' in generated_prompt}")
    print(f"has_answer_format: {'1. 요약 답변' in generated_prompt and '2. 근거 논문' in generated_prompt and '3. 추가 탐색 키워드' in generated_prompt}")
    print(f"has_distance_guidance: {'distance는 cosine distance 기준' in generated_prompt}")
    print(f"has_relevance_rerank_rule: {'관련도를 high, medium, low' in generated_prompt}")
    print(f"has_first_line_rule: {'첫 번째 줄은 반드시 \"1. 요약 답변\"' in generated_prompt}")
    print(f"has_query_not_empty_rule: {'사용자 질문이 비어 있다고 판단하지 않는다' in generated_prompt}")
    print(f"has_exactly_three_keywords_rule: {'추가 탐색 키워드는 정확히 3개만 작성한다' in generated_prompt}")
    print(f"has_numbered_section_rule: {'섹션 제목은 반드시 번호를 포함해 작성한다' in generated_prompt}")
    print(f"has_evidence_field_rule: {'근거 논문의 4개 필드 중 하나라도 생략하지 않는다' in generated_prompt}")
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
