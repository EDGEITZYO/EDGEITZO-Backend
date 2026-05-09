import re
from itertools import combinations


sample_papers = [
    {
        "paper_id": "JAKO200405812524620",
        "title": "한국신문의 유전자 연구 프레임 비교 분석",
        "keywords": "생명공학 . 미디어 프레임 . 공론장",
        "keyword2": "Human gene . Media frame . Public sphere",
    },
    {
        "paper_id": "JAKO202414433385011",
        "title": "생명공학기술 관련 학습 내용 분석",
        "keywords": "생명공학기술 관련 학습 내용 . 생명공학기술 학습 주제 . 생명공학기술 종류 . 생명과학II . 공학일반",
        "keyword2": "learning contents related to biotechnology . biotechnology learning topics . biotechnology types . life science II . general engineering",
    },
    {
        "paper_id": "JAKO200230758883726",
        "title": "생명공학에 대한 한국인들의 표상",
        "keywords": "생명공학 . 생명공학에 대한 표상 . 생명공학에 대한 이득/위험 판단",
        "keyword2": "biotechnology . cognitive and social representation of biotechnology . benefits and costs judgements in biotechnology",
    },
]


def normalize_keywords(keyword_text: str):
    keywords = keyword_text.split(".")
    normalized = []

    for keyword in keywords:
        keyword = keyword.strip().lower()
        keyword = re.sub(r"[\"'()]", "", keyword)

        if keyword:
            normalized.append(keyword)

    return list(dict.fromkeys(normalized))


def create_keyword_relations(keywords):
    return list(combinations(keywords, 2))


for paper in sample_papers:
    print("=" * 70)
    print("논문:", paper["title"])

    kor_keywords = normalize_keywords(paper["keywords"])
    eng_keywords = normalize_keywords(paper["keyword2"])

    print("\n국문 키워드:")
    print(kor_keywords)

    print("\n영문 키워드:")
    print(eng_keywords)

    print("\n국문 키워드 관계:")
    for source, target in create_keyword_relations(kor_keywords):
        print(f"{source} --RELATED_TO-- {target}")

    print("\n영문 키워드 관계:")
    for source, target in create_keyword_relations(eng_keywords):
        print(f"{source} --RELATED_TO-- {target}")