"""
말뭉치 전처리 스크립트 (벡터화 2, 3단계)

파이프라인:
  parse_papers.py → preprocess_corpus.py → embed_papers.py

단계:
  2단계 - 어휘집 구축: 전체 말뭉치를 형태소 분석하여 고유 토큰 집합 생성
  3단계 - 벡터화 사전 정의: TF-IDF 기반으로 토큰별 중요도 가중치 산출,
                              불용어(저중요도 토큰) 제거 후 정제 텍스트 저장

입력: data/parsed/scienceon_parsed.json (또는 keywords_normalized.json)
출력:
  data/parsed/corpus_vocabulary.json  ← 어휘집 (2단계 산출물)
  data/parsed/scienceon_preprocessed.json ← 전처리된 논문 데이터 (embed_papers.py 입력용)
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")
sys.path.insert(0, str(_PROJECT_ROOT))

from konlpy.tag import Okt
from sklearn.feature_extraction.text import TfidfVectorizer

INPUT_PATH = _PROJECT_ROOT / "data" / "parsed" / "scienceon_keywords_normalized.json"
VOCAB_OUTPUT_PATH = _PROJECT_ROOT / "data" / "parsed" / "corpus_vocabulary.json"
PREPROCESSED_OUTPUT_PATH = _PROJECT_ROOT / "data" / "parsed" / "scienceon_preprocessed.json"

# 한국어 불용어 목록
STOPWORDS = {
    "은", "는", "이", "가", "을", "를", "의", "에", "에서", "로", "으로",
    "와", "과", "도", "만", "이다", "있다", "하다", "되다", "것", "수",
    "그", "및", "등", "또", "즉", "위해", "통해", "따라", "대한", "관한",
    "위한", "이후", "이전", "때", "각", "전", "후", "간", "약", "본",
    "하여", "하고", "하는", "한", "된", "될", "있는", "없는", "더", "많은",
    "적인", "적", "성", "화", "적으로", "으로서", "에서의", "에의",
}


def _clean_text(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[^\w\s가-힣a-zA-Z0-9]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _build_corpus_text(paper: dict) -> str:
    parts = []
    if paper.get("Title"):
        parts.append(paper["Title"])
    if paper.get("Abstract"):
        parts.append(paper["Abstract"])
    keywords = paper.get("Keyword")
    if keywords:
        parts.append(" ".join(keywords))
    return " ".join(parts)


def step2_build_vocabulary(papers: list[dict], okt: Okt) -> dict:
    """2단계: 전체 말뭉치 형태소 분석 → 어휘집 구축"""
    print("\n[2단계] 어휘집 구축 중...")
    token_freq: dict[str, int] = {}

    for i, paper in enumerate(papers):
        if (i + 1) % 100 == 0:
            print(f"  형태소 분석: {i + 1}/{len(papers)}건", end="\r")

        raw_text = _clean_text(_build_corpus_text(paper))
        tokens = okt.morphs(raw_text, stem=True)

        for token in tokens:
            if token in STOPWORDS:
                continue
            if len(token) < 2:
                continue
            token_freq[token] = token_freq.get(token, 0) + 1

    sorted_vocab = sorted(token_freq.items(), key=lambda x: x[1], reverse=True)
    vocabulary = {token: {"idx": idx, "freq": freq} for idx, (token, freq) in enumerate(sorted_vocab)}

    print(f"\n  고유 토큰 수: {len(vocabulary):,}개")
    print(f"  상위 10개: {[t for t, _ in sorted_vocab[:10]]}")
    return vocabulary


def step3_build_tfidf_weights(papers: list[dict], vocabulary: dict) -> dict:
    """3단계: TF-IDF 기반 벡터화 사전 정의 (토큰별 중요도 가중치)"""
    print("\n[3단계] TF-IDF 벡터화 사전 정의 중...")

    all_texts = [_clean_text(_build_corpus_text(p)) for p in papers]
    vocab_list = list(vocabulary.keys())

    vectorizer = TfidfVectorizer(vocabulary=vocab_list, min_df=1)
    tfidf_matrix = vectorizer.fit_transform(all_texts)

    feature_names = vectorizer.get_feature_names_out()
    mean_weights = tfidf_matrix.mean(axis=0).A1

    tfidf_dict = {
        token: float(round(mean_weights[idx], 6))
        for idx, token in enumerate(feature_names)
    }

    sorted_by_weight = sorted(tfidf_dict.items(), key=lambda x: x[1], reverse=True)
    print(f"  벡터화 사전 크기: {len(tfidf_dict):,}개 토큰")
    print(f"  가중치 상위 10개: {[(t, round(w, 4)) for t, w in sorted_by_weight[:10]]}")
    return tfidf_dict


def preprocess_text(text: str, okt: Okt, tfidf_weights: dict, top_ratio: float = 0.8) -> str:
    """어휘집 + TF-IDF 사전을 참조해 불용어 및 저중요도 토큰 제거"""
    cleaned = _clean_text(text)
    tokens = okt.morphs(cleaned, stem=True)

    threshold = sorted(tfidf_weights.values(), reverse=True)
    cutoff_idx = int(len(threshold) * top_ratio)
    cutoff_weight = threshold[cutoff_idx] if cutoff_idx < len(threshold) else 0.0

    filtered = [
        t for t in tokens
        if t not in STOPWORDS
        and len(t) >= 2
        and tfidf_weights.get(t, 0) >= cutoff_weight
    ]
    return " ".join(filtered)


def main() -> None:
    if not INPUT_PATH.exists():
        print(f"[오류] 입력 파일 없음: {INPUT_PATH}")
        sys.exit(1)

    data = json.loads(INPUT_PATH.read_text(encoding="utf-8"))
    papers: list[dict] = data.get("papers", data) if isinstance(data, dict) else data
    print(f"[로드] {len(papers):,}건")

    okt = Okt()
    print("형태소 분석기(Okt) 초기화 완료")

    vocabulary = step2_build_vocabulary(papers, okt)

    VOCAB_OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    VOCAB_OUTPUT_PATH.write_text(
        json.dumps({"meta": {"total_tokens": len(vocabulary), "built_at": datetime.now().isoformat()}, "vocabulary": vocabulary}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"  저장: {VOCAB_OUTPUT_PATH.name}")

    tfidf_weights = step3_build_tfidf_weights(papers, vocabulary)

    print("\n[전처리] 논문별 텍스트 정제 중...")
    preprocessed_papers = []
    for i, paper in enumerate(papers):
        if (i + 1) % 100 == 0:
            print(f"  {i + 1}/{len(papers)}건", end="\r")

        raw_abstract = paper.get("Abstract") or ""
        preprocessed_abstract = preprocess_text(raw_abstract, okt, tfidf_weights)

        preprocessed_papers.append({
            **paper,
            "Abstract": preprocessed_abstract if preprocessed_abstract else raw_abstract,
            "Abstract_original": raw_abstract,
        })

    output = {
        "meta": {
            "preprocessed_at": datetime.now().isoformat(),
            "total_count": len(preprocessed_papers),
            "vocabulary_size": len(vocabulary),
            "source": INPUT_PATH.name,
        },
        "papers": preprocessed_papers,
    }

    PREPROCESSED_OUTPUT_PATH.write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n[완료] {len(preprocessed_papers):,}건 저장 → {PREPROCESSED_OUTPUT_PATH.name}")
    print(f"       어휘집: {VOCAB_OUTPUT_PATH.name}")
    print("\n다음 단계: python scripts/embed_papers.py --input data/parsed/scienceon_preprocessed.json --reset")


if __name__ == "__main__":
    main()
