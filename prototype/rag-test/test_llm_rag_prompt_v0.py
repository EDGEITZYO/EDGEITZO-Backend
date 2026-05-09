def build_rag_context(retrieved_papers: list[dict]) -> str:
    context = ""

    for i, paper in enumerate(retrieved_papers, start=1):
        keywords = ", ".join(paper.get("keywords", []))

        context += f"""
[논문 {i}]
paper_id: {paper.get("paper_id")}
title: {paper.get("title")}
keywords: {keywords}
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

[검색된 논문 목록]
{retrieved_context}

[답변 규칙]
1. 제공된 논문 초록 내용만 사용한다.
2. 논문에 없는 사실은 생성하지 않는다.
3. 각 근거에는 논문 제목을 함께 표시한다.
4. 사용자가 다음 탐색을 이어갈 수 있도록 관련 키워드를 제안한다.
5. 답변은 한국어로 작성한다.

[답변 형식]

1. 요약 답변
- 사용자 질문에 대해 3~5문장으로 요약한다.

2. 근거 논문
- 논문 제목:
- 관련 근거:
- 이 논문이 질문과 연결되는 이유:

3. 추가 탐색 키워드
- 키워드 1
- 키워드 2
- 키워드 3
"""
    return prompt.strip()
