import json
import math
import os
import re

import httpx
from dotenv import load_dotenv


MAX_DISTANCE = 0.70
DEFAULT_TOP_K = 5
STOPWORDS = {
    "관련",
    "논문",
    "찾아줘",
    "찾아",
    "다룬",
    "대한",
    "기준",
    "정리",
    "추천",
}


def _to_float(value: object) -> float | None:
    if value is None:
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_keywords(keywords: object) -> str:
    if isinstance(keywords, list):
        return ", ".join(str(keyword) for keyword in keywords)

    return str(keywords or "")


def _query_terms(user_query: str) -> list[str]:
    terms = re.findall(r"[가-힣A-Za-z0-9]+", user_query.lower())
    return [term for term in terms if len(term) >= 2 and term not in STOPWORDS]


def _paper_text(paper: dict) -> str:
    keywords = _format_keywords(paper.get("keywords"))
    return " ".join(
        [
            str(paper.get("title") or ""),
            keywords,
            str(paper.get("abstract") or ""),
        ]
    ).lower()


def _text_overlap_count(user_query: str, paper: dict) -> int:
    text = _paper_text(paper)
    return sum(1 for term in _query_terms(user_query) if term in text)


def _retrieval_relevance(distance: float | None, overlap_count: int) -> str:
    if distance is not None and distance < 0.50:
        return "high"

    if overlap_count >= 2:
        return "high"

    return "medium"


def filter_retrieved_papers(
    user_query: str,
    retrieved_papers: list[dict],
    max_distance: float = MAX_DISTANCE,
    top_k: int = DEFAULT_TOP_K,
) -> list[dict]:
    """Filter and annotate retrieval results before they are passed to the LLM."""
    filtered: list[dict] = []

    for paper in retrieved_papers:
        distance = _to_float(paper.get("distance"))

        if distance is not None and distance >= max_distance:
            continue

        filtered.append(
            {
                **paper,
                "distance": distance,
                "retrieval_relevance": _retrieval_relevance(
                    distance,
                    _text_overlap_count(user_query, paper),
                ),
            }
        )

    filtered.sort(
        key=lambda paper: (
            math.inf if paper.get("distance") is None else paper["distance"],
            paper.get("title", ""),
        )
    )
    return filtered[:top_k]


def build_rag_context(filtered_papers: list[dict]) -> str:
    if not filtered_papers:
        return "검색 결과 없음"

    context = ""

    for i, paper in enumerate(filtered_papers, start=1):
        distance = paper.get("distance")
        distance_line = f"distance: {distance:.4f}\n" if distance is not None else ""

        context += f"""
[논문 {i}]
paper_id: {paper.get("paper_id")}
title: {paper.get("title")}
keywords: {_format_keywords(paper.get("keywords"))}
{distance_line}retrieval_relevance: {paper.get("retrieval_relevance")}
abstract:
{paper.get("abstract")}

"""
    return context.strip()


def build_rag_prompt(
    user_query: str,
    retrieved_papers: list[dict],
    max_distance: float = MAX_DISTANCE,
    top_k: int = DEFAULT_TOP_K,
) -> str:
    filtered_papers = filter_retrieved_papers(
        user_query,
        retrieved_papers,
        max_distance=max_distance,
        top_k=top_k,
    )
    retrieved_context = build_rag_context(filtered_papers)

    prompt = f"""
너는 Bio-me의 생명공학 논문 탐색 AI 연구 assistant야.

[사용자 질문]
{user_query}

[검색된 논문 목록]
{retrieved_context}

[답변 기준]
- [검색된 논문 목록]은 Python retrieval 단계에서 distance < {max_distance:.2f} 기준으로 1차 필터링된 결과다.
- title, keywords, abstract, retrieval_relevance에 있는 정보만 근거로 답변한다.
- 외부 지식이나 추측을 추가하지 않는다.
- 제공된 논문만으로 답할 수 없으면 "제공된 논문 초록만으로는 확인하기 어렵습니다."만 출력한다.
- 사용자 질문과 title, keywords, abstract의 연결성이 확인되는 논문만 근거로 사용한다.
- "관련도"에는 각 논문의 retrieval_relevance 값을 그대로 적는다.
- 분석 과정, 인사말, 답변 제목, 마무리 문장은 작성하지 않는다.

[출력 형식]
첫 줄은 반드시 "1. 요약 답변"으로 시작한다.
아래 skeleton과 같은 구조만 사용한다.

1. 요약 답변
- ...

2. 근거 논문
- 논문 제목:
- 관련도:
- 초록에서 확인된 근거:
- 이 논문이 질문과 연결되는 이유:

3. 추가 탐색 키워드
- ...
- ...
- ...

[키워드만 요청 시]
사용자가 "키워드만" 요청하면 섹션 제목 없이 아래처럼 키워드 3개만 출력한다.
- ...
- ...
- ...
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
            "max_tokens": 900,
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
        {
            "paper_id": "DUMMY001",
            "title": "인공지능 딥러닝 모델 구조 분석",
            "keywords": ["인공지능", "딥러닝"],
            "distance": 0.7201,
            "abstract": "생명공학 윤리와 직접 관련 없는 인공지능 딥러닝 모델 구조를 분석한다.",
        },
    ]

    filtered_sample_papers = filter_retrieved_papers(sample_query, sample_papers)
    generated_prompt = build_rag_prompt(sample_query, sample_papers)

    print("=" * 80)
    print("LLM RAG PROMPT V6 TEST")
    print("=" * 80)
    print(generated_prompt)
    print("=" * 80)
    print("RETRIEVAL FILTER CHECK")
    print(f"before_count: {len(sample_papers)}")
    print(f"after_count: {len(filtered_sample_papers)}")
    print(f"filtered_out_distance_0_7201: {'DUMMY001' not in {paper['paper_id'] for paper in filtered_sample_papers}}")
    print(
        "relevance_labels: "
        + json.dumps(
            {
                paper["paper_id"]: paper["retrieval_relevance"]
                for paper in filtered_sample_papers
            },
            ensure_ascii=False,
        )
    )
    print("=" * 80)
    print("PROMPT CHECK")
    print(f"query_included: {sample_query in generated_prompt}")
    print(f"paper_count: {generated_prompt.count('[논문 ')}")
    print(f"has_python_filter_note: {'Python retrieval 단계에서 distance < 0.70' in generated_prompt}")
    print(f"has_compact_skeleton: {'아래 skeleton과 같은 구조만 사용한다' in generated_prompt}")
    print(f"has_keyword_only_rule: {'키워드 3개만 출력한다' in generated_prompt}")
    print(f"prompt_char_count: {len(generated_prompt)}")
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
