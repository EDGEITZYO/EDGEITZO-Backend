from __future__ import annotations

import json
import logging
import re

from app.core.settings import settings
from app.services.llm.client import chat

logger = logging.getLogger(__name__)

KEYWORD_MAP_SYSTEM_PROMPT = """당신은 학술 연구 분야의 키워드 구조를 분석하는 전문가입니다.
사용자가 입력한 연구 분야를 4개의 축으로 분해하여 계층형 키워드 트리를 생성합니다.

## 4축 정의

1. 핵심기술 (Core Technology)
   정의: 해당 연구 분야에서 사용되는 핵심 방법론, 알고리즘, 기술 요소
   예시 (LLM 설계): Transformer, Attention Mechanism, RLHF, Fine-tuning

2. 연구대상 (Research Target)
   정의: 연구가 적용되거나 분석하는 대상 데이터, 시스템, 현상
   예시 (LLM 설계): 언어 데이터, 토큰 시퀀스, 언어 모델 파라미터

3. 상위분야 (Parent Domain)
   정의: 해당 연구가 속하는 상위 학문 분야 또는 연구 계보
   예시 (LLM 설계): 자연어처리, 딥러닝, 기계학습, 인공지능

4. 응용분야 (Application Domain)
   정의: 해당 연구 기술이 실제로 적용되는 산업/서비스/도메인
   예시 (LLM 설계): 챗봇, 문서 요약, 코드 생성, 검색 시스템

## 계층 규칙

- 각 축은 2depth 구성: 루트 → 1st 레벨 → 2nd 레벨
- 1st 레벨: 축당 2~4개 노드 (중요도 순)
- 2nd 레벨: 각 1st 레벨 노드 아래 2~3개 노드
- 동일 개념이 여러 축에 겹치면 가장 대표적인 축에만 배치

## 출력 품질 기준

- 실제 학술 논문 검색어로 사용 가능한 용어
- 지나치게 포괄적인 단어 금지 ("기술", "방법", "연구" 단독 사용 금지)
- ko/en 모두 포함
- 전체 노드 수: 15~30개 범위

## 엣지 케이스 처리

- 너무 광범위한 입력 ("AI", "컴퓨터"): 가장 일반적인 맥락으로 해석, parent_domain 노드 풍부하게
- 너무 구체적인 입력: 해당 개념을 root로 두고 상위 개념으로 parent_domain 확장
- 복합 분야 ("바이오인포매틱스 + 딥러닝"): 두 분야를 core_technology와 parent_domain에서 교차 반영
- 영문 입력 ("Federated Learning"): ko/en 모두 채움, root.ko는 한글 번역
- 약어 입력 ("GAN", "NLP"): 풀네임으로 해석

## 출력 형식

JSON만 반환. 설명, 마크다운 코드블록 없이.

{
  "root": {"ko": "연구분야명", "en": "Research Domain Name"},
  "axes": {
    "core_technology": [
      {
        "ko": "1st 레벨 노드명", "en": "Node Name", "depth": 1,
        "children": [
          {"ko": "2nd 레벨 노드명", "en": "Node Name", "depth": 2},
          {"ko": "2nd 레벨 노드명", "en": "Node Name", "depth": 2}
        ]
      }
    ],
    "research_target": [],
    "parent_domain": [],
    "application_domain": []
  }
}"""

KEYWORD_MAP_USER_TEMPLATE = (
    '연구 분야: "{field}"\n\n'
    "위 연구 분야를 4축(핵심기술 / 연구대상 / 상위분야 / 응용분야)으로 분해하여 "
    "계층형 키워드 트리를 JSON 형식으로 생성하라."
)


async def generate_keyword_map(research_field: str) -> dict:
    """
    입력: 연구분야 텍스트
    출력: 4축 키워드 트리 JSON
    """
    user_prompt = KEYWORD_MAP_USER_TEMPLATE.format(field=research_field)
    resp = await chat(
        messages=[
            {"role": "user", "content": f"[System]\n{KEYWORD_MAP_SYSTEM_PROMPT}\n\n[User]\n{user_prompt}"},
        ],
        model="claude-sonnet-4-6",
        temperature=0.7,
        max_tokens=2000,
    )
    raw = resp.text.strip()
    cleaned = re.sub(r"```json|```", "", raw).strip()
    return json.loads(cleaned)


_EXPAND_SYSTEM = """당신은 학술 키워드 구조 전문가입니다.
주어진 부모 키워드 아래에 2~3개의 하위 키워드를 생성합니다.

규칙:
- 실제 학술 검색어로 사용 가능한 구체적 용어
- 부모 키워드보다 더 구체적·세분화된 개념
- ko/en 모두 포함
- JSON만 반환 (코드블록 없이)

출력 형식:
[
  {"ko": "하위 키워드명", "en": "Sub-keyword Name"},
  {"ko": "하위 키워드명", "en": "Sub-keyword Name"}
]"""


_AXIS_LABEL = {
    "core_technology": "핵심기술",
    "research_target": "연구대상",
    "parent_domain": "상위분야",
    "application_domain": "응용분야",
}


def transform_tree(raw: dict) -> dict:
    """LLM 4축 트리 → {root > children(recursive, edge_type 포함)} 변환."""
    root_raw = raw.get("root", {})
    axes = raw.get("axes", {})

    def _convert_node(node: dict, edge_type: str | None, depth: int) -> dict:
        children_raw = node.get("children", [])
        return {
            "id": node.get("ko") or node.get("en", ""),
            "ko": node.get("ko", ""),
            "en": node.get("en", ""),
            "depth": depth,
            "edge_type": edge_type,
            "definition": node.get("definition"),
            "children": [_convert_node(c, edge_type, depth + 1) for c in children_raw],
        }

    children: list[dict] = []
    for axis_key, axis_label in _AXIS_LABEL.items():
        for node in axes.get(axis_key, []):
            children.append(_convert_node(node, axis_label, 1))

    return {
        "id": "root",
        "ko": root_raw.get("ko", ""),
        "en": root_raw.get("en", ""),
        "depth": 0,
        "edge_type": None,
        "definition": None,
        "children": children,
    }


async def expand_keyword_node(
    parent_label: str,
    parent_label_en: str,
    axis: str,
    research_field: str,
    depth: int,
) -> list[dict]:
    """부모 키워드 노드 아래 하위 키워드 2~3개 생성 (LLM 호출)."""
    user_prompt = (
        f"연구분야: {research_field}\n"
        f"축: {axis}\n"
        f"부모 키워드: {parent_label} ({parent_label_en})\n"
        f"현재 depth: {depth} → 하위 노드 depth: {depth + 1}\n\n"
        f"위 부모 키워드의 하위 학술 키워드 2~3개를 JSON 배열로 생성하라."
    )
    resp = await chat(
        messages=[
            {"role": "user", "content": f"[System]\n{_EXPAND_SYSTEM}\n\n[User]\n{user_prompt}"},
        ],
        model="claude-sonnet-4-6",
        temperature=0.7,
        max_tokens=400,
    )
    raw = re.sub(r"```json|```", "", resp.text.strip()).strip()
    children_raw = json.loads(raw)
    return [
        {
            "id": c.get("ko") or c.get("en", ""),
            "ko": c.get("ko", ""),
            "en": c.get("en", ""),
            "depth": depth + 1,
            "edge_type": axis,
        }
        for c in children_raw
    ]
