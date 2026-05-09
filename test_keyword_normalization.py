import re

sample_keywords = [
    "생명공학 . 미디어 프레임 . 공론장",
    "Human gene . Media frame . Public sphere",
    "생명공학기술 관련 학습 내용 . 생명공학기술 학습 주제 . 생명공학기술 종류",
    "biotechnology . cognitive and social representation of biotechnology . benefits and costs judgements in biotechnology"
]

def normalize_keywords(keyword_text: str):
    # "." 기준 split
    keywords = keyword_text.split(".")

    normalized = []

    for keyword in keywords:
        # 공백 제거
        keyword = keyword.strip()

        # 영어는 lowercase
        keyword = keyword.lower()

        # 특수문자 제거
        keyword = re.sub(r"[\"'()]", "", keyword)

        # 빈 문자열 제거
        if keyword:
            normalized.append(keyword)

    # 중복 제거
    normalized = list(dict.fromkeys(normalized))

    return normalized

for text in sample_keywords:
    print("=" * 50)
    print("원본:")
    print(text)

    print("\n정규화 결과:")
    print(normalize_keywords(text))