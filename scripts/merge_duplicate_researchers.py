"""한 사람이 연구자 ID 여러 개로 중복 등록된 것을 합친다.

왜 생겼나 (2026-09-29 실측, 같은 이름·같은 논문 공유 216쌍 / 그중 212쌍은 원래부터):
  - ID가 '이름 + 소속 root'의 해시라 소속 표기가 적재 회차마다 다르면 ID가 둘이 된다
    (김현주: "농촌진흥청 국립식량과학원" / "국립식량과학원")
  - ScienceON 출처(sci:)와 KCI 출처(kci:)가 병합되지 않았다 (전석원)
  - 8월·9월 적재의 ID 규칙이 달라 소속 문자열까지 같은 사람이 다시 만들어졌다 (홍의철)

같은 사람으로 보는 근거:
  한글 이름이 같고 논문을 MIN_SHARED_PAPERS편 이상 공유한다.
  반증: 두 ID가 서로 다른 사람이라면 공유 논문마다 저자 목록에 그 이름이 두 번 나와야 한다
  (두 사람이 다 저자라서 두 ID에 붙은 것이므로). 공유 논문의 절반 이상에서 이름이 두 번 나오면
  같은 연구실의 동명이인으로 보고 합치지 않는다. 91편 중 1편처럼 드문 경우는 저자 중복 표기다
  (우관식·장윤아·김명숙 실측).

남길 ID(대표) 고르는 순서:
  1. 코퍼스 근거 논문(researcher_papers)이 있는 ID — 연구자가 등록된 근거가 가장 확실하다
  2. KCI 출처 — 논문 이력을 저자별로 전량 수집한 경로다
  3. 논문이 많은 쪽  4. 먼저 만들어진 쪽  5. ID 사전순(결정적으로 만들기 위해)

합칠 때:
  - 논문 행은 대표 ID로 모은다(같은 논문은 하나만)
  - 대표 ID에 비어 있는 값(이메일·전공·영문명·키워드)은 나머지 ID에서 채운다.
    이메일은 신뢰도(match_confidence)와 한 벌로 옮긴다
  - 없어지는 ID는 researcher_id_aliases에 남긴다 — 옛 링크로 들어와도 상세 API가 이어준다
  - 흐름 캐시는 지운다(논문 목록이 바뀌어 어차피 재생성된다)

사용법:
  python scripts/merge_duplicate_researchers.py            # 시험 실행 — 합칠 묶음만 보고
  python scripts/merge_duplicate_researchers.py --apply

반영 뒤: sync_researchers_to_chroma.py(없어진 ID 제거), 운영 반영 때 build_researcher_graph.py --load.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.load_researchers import CHECKPOINT_DIR  # noqa: E402 — .env를 먼저 읽는 모듈

from sqlalchemy import text  # noqa: E402

from app.core.database import AsyncSessionLocal  # noqa: E402

MIN_SHARED_PAPERS = 3
_FILL_COLUMNS = ("institution_dept", "author_name_eng", "keywords", "keyword")

_norm = lambda s: re.sub(r"\s+", "", s or "")  # noqa: E731


def _name_parts(authors: list[str]) -> list[str]:
    return [_norm(p) for a in authors or [] for p in re.split(r"[,;]", a or "") if p.strip()]


def find_groups(people: dict, papers: dict, authors_of: dict) -> tuple[list[list[str]], list[tuple]]:
    """(합칠 묶음들, 반증으로 막은 쌍들). people: rid → dict(name, ...), papers: rid → set(paper_key)."""
    by_paper = collections.defaultdict(list)
    for rid, keys in papers.items():
        for k in keys:
            by_paper[k].append(rid)
    shared = collections.defaultdict(set)
    for key, rids in by_paper.items():
        rids = sorted(set(rids))
        for i in range(len(rids)):
            for j in range(i + 1, len(rids)):
                a, b = rids[i], rids[j]
                if _norm(people[a]["name"]) == _norm(people[b]["name"]):
                    shared[(a, b)].add(key)

    parent: dict[str, str] = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    blocked = []
    for (a, b), keys in shared.items():
        if len(keys) < MIN_SHARED_PAPERS:
            continue
        name = _norm(people[a]["name"])
        twice = [k for k in keys if _name_parts(authors_of.get(k, [])).count(name) >= 2]
        if len(twice) * 2 >= len(keys):
            blocked.append((a, b, len(keys), len(twice)))
            continue
        parent[find(a)] = find(b)

    groups = collections.defaultdict(list)
    for x in list(parent):
        groups[find(x)].append(x)
    return [sorted(g) for g in groups.values() if len(g) > 1], blocked


def pick_canonical(group: list[str], people: dict, papers: dict) -> str:
    return min(
        group,
        key=lambda r: (
            not people[r]["anchored"],
            people[r]["source"] != "kci",
            -len(papers.get(r, ())),
            people[r]["created_at"],
            r,
        ),
    )


async def load() -> tuple[dict, dict, dict]:
    async with AsyncSessionLocal() as s:
        people = {
            r.researcher_id: {
                "name": r.author_name_kor, "source": r.source, "created_at": str(r.created_at),
                "institution": r.institution_current, "anchored": r.anchored,
            }
            for r in (await s.execute(text(
                "SELECT researcher_id, author_name_kor, source, created_at, institution_current, "
                "EXISTS (SELECT 1 FROM researcher_papers rp WHERE rp.researcher_id = r.researcher_id) AS anchored "
                "FROM researchers r WHERE author_name_kor IS NOT NULL"
            ))).all()
        }
        papers = collections.defaultdict(set)
        authors_of: dict[str, list[str]] = {}
        for r in (await s.execute(text(
            "SELECT researcher_id, coalesce(internal_paper_id, external_id) AS k, authors FROM researcher_external_papers"
        ))).all():
            papers[r.researcher_id].add(r.k)
            if r.authors:
                authors_of[r.k] = list(r.authors)
        for r in (await s.execute(text(
            "SELECT rp.researcher_id, rp.paper_id AS k, p.authors FROM researcher_papers rp JOIN papers p ON p.id = rp.paper_id"
        ))).all():
            papers[r.researcher_id].add(r.k)
            if r.authors and r.k not in authors_of:
                authors_of[r.k] = list(r.authors)
    return people, {k: v for k, v in papers.items() if k in people}, authors_of


async def merge(canonical: str, others: list[str], reason: str) -> None:
    async with AsyncSessionLocal() as s:
        for other in others:
            p = {"c": canonical, "o": other}
            await s.execute(text(
                "INSERT INTO researcher_external_papers (researcher_id, external_source, external_id, title, journal, "
                "  pubyear, pubmonth, citation_count, categories, keywords, authors, author_institutions, doi, url, "
                "  internal_paper_id, created_at, fwci, language) "
                "SELECT :c, external_source, external_id, title, journal, pubyear, pubmonth, citation_count, categories, "
                "  keywords, authors, author_institutions, doi, url, internal_paper_id, created_at, fwci, language "
                "FROM researcher_external_papers WHERE researcher_id = :o "
                "ON CONFLICT (researcher_id, external_id) DO NOTHING"), p)
            await s.execute(text(
                "INSERT INTO researcher_papers (researcher_id, paper_id, author_order, role, institution_at_time) "
                "SELECT :c, paper_id, author_order, role, institution_at_time FROM researcher_papers WHERE researcher_id = :o "
                "ON CONFLICT DO NOTHING"), p)
            for col in _FILL_COLUMNS:
                await s.execute(text(
                    f"UPDATE researchers c SET {col} = o.{col} FROM researchers o "
                    f"WHERE c.researcher_id = :c AND o.researcher_id = :o AND c.{col} IS NULL AND o.{col} IS NOT NULL"), p)
            await s.execute(text(
                "UPDATE researchers c SET email = o.email, match_confidence = o.match_confidence FROM researchers o "
                "WHERE c.researcher_id = :c AND o.researcher_id = :o AND c.email IS NULL AND o.email IS NOT NULL"), p)
            # 없어지는 ID를 가리키던 옛 alias도 대표로 옮긴다(사슬이 생기지 않게)
            await s.execute(text("UPDATE researcher_id_aliases SET researcher_id = :c WHERE researcher_id = :o"), p)
            await s.execute(text(
                "INSERT INTO researcher_id_aliases (alias_id, researcher_id, reason) VALUES (:o, :c, :reason) "
                "ON CONFLICT (alias_id) DO UPDATE SET researcher_id = EXCLUDED.researcher_id"), {**p, "reason": reason})
            await s.execute(text("DELETE FROM researchers WHERE researcher_id = :o"), p)  # 논문 행·캐시는 CASCADE
        await s.execute(text("DELETE FROM researcher_flow_cache WHERE researcher_id = :c"), {"c": canonical})
        await s.execute(text(
            "UPDATE researchers r SET total_papers = a.n, total_citations = a.c, first_pubyear = a.y0, "
            "  last_pubyear = a.y1, citation_source = 'kci', corpus_paper_count = (SELECT count(*) FROM researcher_papers WHERE researcher_id = :c), "
            "  updated_at = now() "
            "FROM (SELECT count(*) n, coalesce(sum(citation_count),0) c, min(pubyear) y0, max(pubyear) y1 "
            "      FROM researcher_external_papers WHERE researcher_id = :c) a "
            "WHERE r.researcher_id = :c"), {"c": canonical})
        await s.commit()


async def run(apply: bool) -> None:
    people, papers, authors_of = await load()
    groups, blocked = find_groups(people, papers, authors_of)
    plan = []
    for g in groups:
        c = pick_canonical(g, people, papers)
        others = [r for r in g if r != c]
        plan.append({
            "canonical": c, "others": others, "name": people[c]["name"],
            "institutions": [people[r]["institution"] for r in g],
            "papers_before": {r: len(papers.get(r, ())) for r in g},
            "papers_after": len(set().union(*(papers.get(r, set()) for r in g))),
        })
    print(f"[merge] 합칠 묶음 {len(groups)}개 · 없어질 ID {sum(len(p['others']) for p in plan)}개")
    print(f"[merge] 반증(공유 논문에 같은 이름 2번)으로 막은 쌍 {len(blocked)}개")
    for a, b, n, t in blocked[:5]:
        print(f"    {people[a]['name']} {people[a]['institution']} ↔ {people[b]['institution']} 공유 {n}편 중 {t}편에 이름 2번")
    for p in sorted(plan, key=lambda x: -len(x["others"]))[:5]:
        print(f"    {p['name']} 남김 {p['canonical']} ← {p['others']} | {p['papers_before']} → {p['papers_after']}")
    (CHECKPOINT_DIR / "researcher_merge_plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=1))
    if not apply:
        return
    for p in plan:
        await merge(p["canonical"], p["others"], f"같은 이름·논문 {MIN_SHARED_PAPERS}편+ 공유 (merge_duplicate_researchers)")
    print(f"[merge] 반영 완료 — {len(plan)}개 묶음")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    asyncio.run(run(parser.parse_args().apply))


if __name__ == "__main__":
    main()
