"""남은 동명이인 문제만 풀기 위해 저자 번호를 조회할 논문을 고른다 (collect_kci_author_ids.py --plan 입력).

전수(연구자 논문 7만 편)가 아니라 판정이 막힌 곳만 본다:
  A. 같은 기관 동명이인 의심 연구자 (data/checkpoints/namesake_suspects.json)
  B. 소속 근거가 없어 '근거 부족하지만 유지'된 논문 — 그 논문 + 그 연구자의 확정 논문 3편
  C. 소속이 상위기관('농촌진흥청')뿐이라 판정을 건너뛴 연구자
  D. 같은 이름으로 논문 1~2편만 공유하는 연구자 ID 쌍

A·C는 논문을 공저자로 이어 덩어리로 나누고, 3편 이상 덩어리는 3편만, 작은 덩어리는 전부 본다.
공저자로 이어진 덩어리는 한 사람의 것이라, 덩어리마다 몇 편만 보면 그 덩어리의 저자 번호가 정해진다.
이름별 번호 조회(저자 정보 조회)는 쓰지 않는다 — 한 연구자의 여러 논문에 공통으로 나오는 번호가 그 사람이다.

출력: data/checkpoints/kci_lookup_plan.json  {"articles": [...], "by_reason": {...}}
"""
from __future__ import annotations

import collections
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.load_researchers import CHECKPOINT_DIR  # noqa: E402
from scripts.reconcile_researcher_papers import (  # noqa: E402
    _own_institutions,
    judge,
    other_person_evidence,
    specific_unit,
)

import asyncio  # noqa: E402

from sqlalchemy import text  # noqa: E402

from app.core.database import AsyncSessionLocal  # noqa: E402

SAMPLE_PER_COMPONENT = 3
PLAN_FILE = CHECKPOINT_DIR / "kci_lookup_plan.json"
_norm = lambda s: re.sub(r"\s+", "", s or "")  # noqa: E731


def components(name: str, papers: list[tuple[str, list[str]]]) -> list[list[str]]:
    """공저자를 하나라도 공유하는 논문끼리 잇는다. papers: [(art_id, authors)]"""
    parent = list(range(len(papers)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    by_co = collections.defaultdict(list)
    for i, (_, authors) in enumerate(papers):
        for a in {_norm(p) for au in authors for p in re.split(r"[,;]", au or "") if p.strip()} - {_norm(name)}:
            by_co[a].append(i)
    for idx in by_co.values():
        for j in idx[1:]:
            parent[find(j)] = find(idx[0])
    groups = collections.defaultdict(list)
    for i, (art, _) in enumerate(papers):
        groups[find(i)].append(art)
    return list(groups.values())


def sample(groups: list[list[str]]) -> list[str]:
    out = []
    for g in groups:
        out += sorted(g)[:SAMPLE_PER_COMPONENT] if len(g) >= SAMPLE_PER_COMPONENT else g
    return out


async def main() -> None:
    async with AsyncSessionLocal() as s:
        people = {r.researcher_id: r for r in (await s.execute(text(
            "SELECT researcher_id, author_name_kor, institution_current FROM researchers WHERE author_name_kor IS NOT NULL"
        ))).all()}
        rows = (await s.execute(text(
            "SELECT researcher_id, external_id, authors FROM researcher_external_papers WHERE external_id LIKE 'ART%'"
        ))).all()
    papers = collections.defaultdict(list)
    for r in rows:
        papers[r.researcher_id].append((r.external_id, list(r.authors or [])))

    plan: dict[str, set] = collections.defaultdict(set)

    # A. 같은 기관 동명이인 의심
    suspects = [x["researcher_id"] for x in json.loads((CHECKPOINT_DIR / "namesake_suspects.json").read_text())]
    for rid in suspects:
        if rid in people:
            plan["A 동명이인 의심"] |= set(sample(components(people[rid].author_name_kor, papers[rid])))

    # C. 소속이 상위기관뿐
    for rid, p in people.items():
        if not specific_unit(p.institution_current) and papers[rid]:
            plan["C 상위기관뿐"] |= set(sample(components(p.author_name_kor, papers[rid])))

    # B. 근거 부족하지만 유지된 논문 (reconcile 판정 재현)
    cache = json.loads((CHECKPOINT_DIR / "researcher_reconcile.json").read_text())
    have = {rid: {a for a, _ in ps} for rid, ps in papers.items()}
    for rid, p in people.items():
        unit = specific_unit(p.institution_current)
        if not unit or rid not in cache:
            continue
        cands = cache[rid]["candidates"]
        verdict = judge(p.author_name_kor, [unit], cands)
        confirmed = sorted(a for a, v in verdict.items() if v == "inst" and a in have.get(rid, ()))
        unverified = [
            a for a in have.get(rid, ())
            if verdict.get(a) == "reject" and a in cands and not other_person_evidence(p.author_name_kor, [unit], cands[a])
        ]
        if unverified:
            plan["B 근거 부족 유지"] |= set(unverified)
            plan["B 기준 논문(연구자 번호 확정용)"] |= set(confirmed[:SAMPLE_PER_COMPONENT])

    # D. 같은 이름, 논문 1~2편만 공유
    by_paper = collections.defaultdict(set)
    for rid, ps in papers.items():
        for a, _ in ps:
            by_paper[a].add(rid)
    shared = collections.Counter()
    for a, rids in by_paper.items():
        rids = sorted(r for r in rids if r in people)
        for i in range(len(rids)):
            for j in range(i + 1, len(rids)):
                if _norm(people[rids[i]].author_name_kor) == _norm(people[rids[j]].author_name_kor):
                    shared[(rids[i], rids[j])] += 1
    for (a, b), n in shared.items():
        if n < 3:
            for rid in (a, b):
                plan["D 1~2편 공유 쌍"] |= set(sorted(x for x, _ in papers[rid])[:SAMPLE_PER_COMPONENT])

    articles = sorted(set().union(*plan.values()))
    PLAN_FILE.write_text(json.dumps(
        {"articles": articles, "by_reason": {k: sorted(v) for k, v in plan.items()}}, ensure_ascii=False))
    for k, v in plan.items():
        print(f"  {k}: {len(v):,}편")
    print(f"[plan] 조회할 논문 합계(중복 제거) {len(articles):,}편 → {PLAN_FILE}")


if __name__ == "__main__":
    asyncio.run(main())
