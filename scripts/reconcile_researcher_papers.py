"""연구자 논문 목록을 근거 기반으로 다시 판정한다 — 누락 채우기 + 다른 사람 논문 빼기.

왜 필요한가 (2026-09-29 실측):
  1. expand가 소속 문자열 하나로 한 번만 검색한다. 그런데 KCI affiliation 검색은 표기에 민감하다.
     "농촌진흥청 국립농업과학원" 연구자는 institution_root가 "농촌진흥청"을 뽑아, 논문에
     "국립농업과학원 유기농업과"로 적힌 본인 논문을 놓친다(김민정: 농촌진흥청 54건 / 국립농업과학원 83건).
     기관 개칭도 놓친다(이원석: 국립환경연구원 21건 / 국립환경과학원 53건).
  2. expand를 다시 돌려도 결과에서 빠진 행을 지우지 않는다. 8월 첫 적재 때 붙은 761행이 남아
     59명의 논문 수가 total_papers보다 많다. 이 중 저자 정보가 있는 264행은 263행이 본인 소속과
     같아 무조건 지우면 안 되고, 저자 정보가 없는 497행은 소속을 알 수 없다.

판정 규칙 — 논문 속 저자 이름이 연구자와 같고, 아래 둘 중 하나를 만족하면 본인 논문:
  근거 1 (소속)  : 그 저자의 소속이 연구자의 구체 기관과 맞는다. 상위기관만으로는 인정하지 않는다 —
                   '농촌진흥청'은 국립식량과학원·국립축산과학원 등 여러 연구원을 거느려서, 상위기관으로
                   대조하면 산하 다른 연구원의 동명이인이 3,017편 들어왔다(시험 실행 실측).
                   상위기관만 적힌 논문은 근거 2·3으로만 받는다.
  근거 2 (공저자): 근거 1로 확정된 논문들의 공저자와 2명 이상 겹친다.
                   소속 표기가 다른 본인 논문(개칭·이동·하위조직)을 살린다. 2명은 merge 단계의
                   동일인 판정 기준(MERGE_MIN_SHARED_COAUTHORS)과 같은 값이다.
  근거 3 (확인된 소속): 근거 2로 받은 논문 중 2편 이상에 나오는 본인 소속은 그 사람이 실제로
                   있었던 소속으로 인정하고, 그 소속으로 근거 1을 한 번 더 적용한다(구체 기관만).
                   기관 개칭 후 팀이 바뀐 시기의 논문을 살린다 — 이원석(국립환경연구원 → 국립환경과학원)은
                   근거 2까지만 쓰면 2013~2018년 국립환경과학원 논문 13편이 공저자가 달라 빠졌다.
  확장은 여기서 멈춘다(근거 3으로 받은 논문의 공저자·소속으로 다시 넓히지 않는다) — 사슬처럼
  번지면 같은 이름의 다른 연구실까지 끌려온다.

한계: KCI는 저자 고유 ID를 주지 않는다. 같은 기관에 같은 이름의 연구자가 둘이면 근거 1로
  둘 다 받아진다. 그런 의심 논문(근거 1로 받았지만 다른 확정 논문과 공저자가 한 명도 안 겹침)은
  빼지 않고 수만 센다.

대상: total_papers보다 논문 행이 많은 연구자 + 소속이 상위기관(○○청/부/처)으로 시작하는 연구자.

사용법:
  python scripts/reconcile_researcher_papers.py                 # 시험 실행 — KCI 조회 후 판정 결과만 보고
  python scripts/reconcile_researcher_papers.py --apply         # 판정대로 DB 반영 (조회 결과는 캐시에서 재사용)
  python scripts/reconcile_researcher_papers.py --researcher-id kci:b485eb73eece062d
  python scripts/reconcile_researcher_papers.py --all           # 연구자 전원

반영 뒤 할 일: --stage keywords(새 행 키워드), 연구 흐름 캐시는 논문 서명이 바뀌어 자동 재생성.
Neo4j 공저자 그래프(build_researcher_graph.py --load)는 **운영과 공유**하므로 운영 반영 때 한 번만 돌린다.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.load_researchers import (  # noqa: E402 — .env를 먼저 읽는 모듈
    CHECKPOINT_DIR,
    KCI_PACING,
    _inst_match,
    _norm,
    _retry,
    strip_corp,
)

import httpx  # noqa: E402
import sqlalchemy as sa  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: E402

from app.core.database import AsyncSessionLocal  # noqa: E402
from app.integrations.kci.researcher_client import KCIResearcherClient, institution_root  # noqa: E402
from app.models.researcher import Researcher, ResearcherExternalPaper  # noqa: E402

CACHE = CHECKPOINT_DIR / "researcher_reconcile.json"
MIN_SHARED_COAUTHORS = 2
SEARCH_PAGE_CAP = 20  # 소속 단위 하나당 최대 2,000건. 5(500건)로 두었더니 노영희가 정확히 500편에서 잘렸다.

_PARENT_AGENCY_SPACED = re.compile(r"^([가-힣]+(?:청|부|처))\s+(\S.*)$")
# 띄어쓰기 없이 붙은 표기("농촌진흥청농업과학기술원" — 국립농업과학원의 2008년 이전 이름)도 나눈다.
# 붙은 경우는 오분리 위험이 커서("동부대학" 등) 데이터에서 확인된 상위기관 이름에만 적용한다.
_PARENT_AGENCY_JOINED = re.compile(r"^((?:국립)?농촌진흥청|산림청)(\S.*)$")


def _split_parent(raw: str | None) -> tuple[str, str] | None:
    """(상위기관, 나머지). 상위기관으로 시작하지 않으면 None."""
    text_ = (raw or "").strip()
    match = _PARENT_AGENCY_SPACED.match(text_) or _PARENT_AGENCY_JOINED.match(text_)
    return (match.group(1), match.group(2)) if match else None

_TARGET_SQL = """
SELECT r.researcher_id, r.author_name_kor, r.institution_current
FROM researchers r
WHERE r.author_name_kor IS NOT NULL
  AND (
    r.institution_current ~ '^[가-힣]+(청|부|처) [가-힣]'
    OR (SELECT count(*) FROM researcher_external_papers e WHERE e.researcher_id = r.researcher_id)
       > coalesce(r.total_papers, 0)
  )
ORDER BY r.researcher_id
"""


def institution_units(raw: str | None) -> list[str]:
    """검색·대조에 쓸 소속 단위. 연구자 ID용 institution_root는 바꾸지 않는다(ID가 바뀐다).

    "농촌진흥청 국립농업과학원 유기농업과" → ["농촌진흥청", "국립농업과학원"]
    "연세대학교 의과대학"                 → ["연세대학교"]   (대학 하위조직은 단위로 쓰지 않는다 —
                                            "의과대학"으로 검색하면 전국 의대가 걸린다)
    """
    root = institution_root(raw)
    units = [root] if root else []
    split = _split_parent(raw)
    if split:
        sub = institution_root(split[1])
        if sub and sub not in units:
            units.append(sub)
    return units


def specific_unit(raw: str | None) -> str | None:
    """소속 근거에 쓸 구체 기관. 상위기관만 적힌 소속이면 None.

    "농촌진흥청 국립식량과학원 중부작물부" → "국립식량과학원"
    "농촌진흥청"                        → None   (산하 기관 중 어디인지 모른다)
    "연세대학교 의과대학"               → "연세대학교"
    """
    raw = strip_corp(raw)
    units = institution_units(raw)
    if not units:
        return None
    if _split_parent(raw):
        return units[-1] if len(units) > 1 else None
    if re.fullmatch(r"[가-힣]+(?:청|부|처)", units[0]):
        return None
    return units[0]


def _split_names(raw: str) -> list[str]:
    """KCI가 저자 여러 명을 한 칸에 붙여 주는 경우가 있다("박윤수,문영완임승재,임지순").
    쉼표·세미콜론으로 나눈다. 한글 저자 칸 450편(0.4%)이 이렇게 들어와 본인 이름을 못 찾았다.
    쉼표 없이 붙은 부분("문영완임승재")은 경계를 알 수 없어 그대로 둔다."""
    return [p.strip() for p in re.split(r"[,;]", raw or "") if p.strip()]


def _own_institutions(authors: list[str], insts: list[str], name: str) -> list[str]:
    return [i or "" for a, i in zip(authors, insts) for part in _split_names(a) if _norm(part) == _norm(name)]


def _coauthors(authors: list[str], name: str, insts: list[str] | None = None) -> set[str]:
    """공저자 식별자 = 이름 + 소속 기관. 소속이 비어 있는 공저자는 근거로 세지 않는다.

    이름만 쓰면 '김민수'처럼 흔한 이름이 우연히 겹쳐도 근거가 됐다 — 공저자 근거 2,962편 중
    22.7%가 흔한 이름(상위 300)을 빼면 겹침이 2명 미만이었다(2026-09-29 실측).
    소속은 institution_root 수준으로 뭉개 표기 차이('농촌진흥청 국립식량과학원'/'국립식량과학원')는 흡수한다.
    """
    insts = insts if insts is not None else [""] * len(authors)
    out = set()
    for a, i in zip(authors, insts):
        unit = specific_unit(i) or institution_root(i) if i else None
        if not unit:
            continue
        for p in _split_names(a):
            if _norm(p) != _norm(name):
                out.add(f"{_norm(p)}@{_norm(unit)}")
    return out


def _article_dict(article) -> dict:
    return {
        "art_id": article.art_id, "title": article.title, "journal": article.journal,
        "pubyear": article.pubyear, "pubmonth": article.pubmonth, "cites": article.citation_count,
        "categories": article.categories, "doi": article.doi, "url": article.url,
        "authors": [a.name for a in article.authors if a.name],
        "author_insts": [a.institution or "" for a in article.authors if a.name],
    }


def judge(name: str, units: list[str], candidates: dict[str, dict]) -> dict[str, str]:
    """art_id → 'inst' | 'coauthor' | 'learned_inst' | 'reject' | 'unknown'(저자 정보를 못 얻음).

    units는 구체 기관(specific_unit)이다. 검색어 목록(institution_units)과 다르다.
    """
    verdict: dict[str, str] = {}
    own_of = {
        art_id: _own_institutions(c.get("authors") or [], c.get("author_insts") or [], name)
        for art_id, c in candidates.items()
    }
    matches = lambda own, us: any(_inst_match(u, i) for u in us for i in own if i)

    # 근거 1 — 소속
    core_coauthors: set[str] = set()
    for art_id, c in candidates.items():
        if not c.get("authors"):
            verdict[art_id] = "unknown"
        elif own_of[art_id] and matches(own_of[art_id], units):
            verdict[art_id] = "inst"
            core_coauthors |= _coauthors(c["authors"], name, c.get("author_insts"))

    # 근거 2 — 확정 논문과 공저자 2명 이상
    for art_id, c in candidates.items():
        if art_id in verdict or not own_of[art_id]:
            continue
        if len(_coauthors(c["authors"], name, c.get("author_insts")) & core_coauthors) >= MIN_SHARED_COAUTHORS:
            verdict[art_id] = "coauthor"

    # 근거 3 — 근거 2 논문 2편 이상에 나온 소속을 인정해 소속 근거를 한 번 더
    learned = Counter(
        unit
        for art_id, v in verdict.items() if v == "coauthor"
        for unit in {specific_unit(i) for i in own_of[art_id] if i}
        if unit and not any(_inst_match(u, unit) for u in units)
    )
    learned_units = [root for root, n in learned.items() if n >= MIN_SHARED_COAUTHORS]
    for art_id in candidates:
        if art_id in verdict:
            continue
        if own_of[art_id] and matches(own_of[art_id], learned_units):
            verdict[art_id] = "learned_inst"
        else:
            verdict[art_id] = "reject"
    return verdict


def other_person_evidence(name: str, units: list[str], candidate: dict) -> bool:
    """기존 행을 지울 근거 — 그 저자가 **비교 가능한 다른 구체 기관** 소속으로 적혀 있다.

    기존 행은 원래 적재 때 이름+소속으로 한 번 걸러진 것이라, '본인 근거가 없다'만으로 지우면 안 된다.
    전체 시험 실행에서 지울 예정이던 1,364행 중 848행은 근거가 없는 쪽이었다(2026-09-29 실측):
      상위기관만 적힘 523 / 소속 빈칸 240 / 한글↔영문이라 비교 불가 85
    ("선문대학교"와 "Sunmoon University"는 같은 기관이지만 문자로는 비교할 수 없다.)
    그래서 추가는 본인 근거가 있어야 하고, 삭제는 다른 사람 근거가 있어야 한다 — 비대칭이다.
    """
    hangul = lambda s: bool(re.search(r"[가-힣]", s or ""))  # noqa: E731
    own = _own_institutions(candidate.get("authors") or [], candidate.get("author_insts") or [], name)
    return any(
        i and hangul(i) == hangul(units[0]) and specific_unit(i) is not None
        and not any(_inst_match(u, i) for u in units)
        for i in own
    )


def namesake_suspects(name: str, candidates: dict[str, dict], verdict: dict[str, str]) -> int:
    """소속 근거로 받았지만 다른 본인 논문과 공저자가 하나도 안 겹치는 논문 수(단독 저자 제외)."""
    core = [a for a, v in verdict.items() if v == "inst"]
    count = 0
    for art_id in core:
        c = candidates[art_id]
        mine = _coauthors(c["authors"], name, c.get("author_insts"))
        if not mine:
            continue
        others = set().union(*(
            _coauthors(candidates[o]["authors"], name, candidates[o].get("author_insts")) for o in core if o != art_id
        ))
        count += not (mine & others)
    return count


# 전체 연구자에서 공저자 근거로 확인된 소속 쌍 중, 개칭·통폐합으로 볼 수 있는 것만 같은 기관으로 취급한다.
# 옛 이름이 새 이름 등장 뒤로는 거의 안 쓰이면(10% 이하) 개칭이고, 둘이 계속 함께 쓰이면
# 사람이 옮겨 다닌 것(서울대학교 ↔ 국립산림과학원 83%)이다. 3명 이상이 확인한 쌍만 쓴다.
# 한계: 원예연구소·작물과학원·국립환경연구원처럼 개칭 뒤에도 옛 이름이 33~72% 쓰이는 곳은
# 이동과 구분되지 않아 여기서 빠진다 — 연구자별 근거 2·3이 처리한다.
EQUIV_MIN_RESEARCHERS = 3
EQUIV_MAX_AFTER_RATIO = 0.10


def mine_equivalences(collected: list, years_by_unit: dict[str, list[int]]) -> dict[str, set[str]]:
    pairs: dict[tuple, set] = {}
    for person, _have, data in collected:
        unit = specific_unit(person.institution_current)
        if not unit:
            continue
        verdict = judge(person.author_name_kor, [unit], data["candidates"])
        for art_id, v in verdict.items():
            if v not in ("coauthor", "learned_inst"):
                continue
            c = data["candidates"][art_id]
            for inst in _own_institutions(c["authors"], c["author_insts"], person.author_name_kor):
                other = specific_unit(inst) if inst else None
                if other and not _inst_match(unit, other):
                    pairs.setdefault(tuple(sorted((unit, other))), set()).add(person.researcher_id)

    def quantile(xs, q):
        xs = sorted(xs)
        return xs[min(len(xs) - 1, int(q * (len(xs) - 1)))]

    equiv: dict[str, set[str]] = {}
    for (a, b), who in pairs.items():
        ya, yb = years_by_unit.get(a), years_by_unit.get(b)
        if len(who) < EQUIV_MIN_RESEARCHERS or not ya or not yb:
            continue
        old, new = (a, b) if quantile(ya, 0.5) <= quantile(yb, 0.5) else (b, a)
        y_old, start_new = years_by_unit[old], quantile(years_by_unit[new], 0.05)
        after = sum(1 for y in y_old if y > start_new + 1) / len(y_old)
        if after <= EQUIV_MAX_AFTER_RATIO:
            equiv.setdefault(a, set()).add(b)
            equiv.setdefault(b, set()).add(a)
    return equiv


async def institution_years() -> dict[str, list[int]]:
    """기관(구체 단위)별로 논문에 쓰인 연도들. 개칭 판별용."""
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(text(
            "SELECT DISTINCT external_id, pubyear, i FROM researcher_external_papers, unnest(author_institutions) i "
            "WHERE pubyear IS NOT NULL AND i <> ''"))).all()
    out: dict[str, list[int]] = {}
    for r in rows:
        unit = specific_unit(r.i)
        if unit:
            out.setdefault(unit, []).append(r.pubyear)
    return out


async def collect(client: KCIResearcherClient, person, existing: list, cache: dict) -> dict:
    """후보 = 소속 단위별 검색 결과 ∪ 지금 붙어 있는 행. 저자 정보 없는 행은 articleDetail로 채운다.

    캐시는 KCI 검색 결과만 재사용하고, 지금 붙어 있는 행은 **매번 다시 후보에 넣는다**.
    캐시를 통째로 돌려주면 그 뒤에 붙은 행(ID 병합으로 옮겨온 논문 등)이 후보에서 빠지고,
    후보에 없는 기존 행은 '유지' 판정을 못 받아 지워진다.
    """
    rid = person.researcher_id
    if rid in cache:
        result = cache[rid]
        await _add_existing(client, existing, result["candidates"])
        return result
    units = institution_units(person.institution_current)
    # 기존 행에 적힌 본인 소속 중 연구자 소속과 다른 것(개칭 후 이름 등)도 검색어로 쓴다.
    # 여기서 찾은 논문도 판정 규칙을 똑같이 통과해야 남는다.
    extra = Counter()
    for row in existing:
        for inst in _own_institutions(row.authors or [], row.author_institutions or [], person.author_name_kor):
            r = institution_root(inst)
            if r and r not in units:
                extra[r] += 1
    search_terms = units + [r for r, n in extra.most_common(3) if n >= 2]

    candidates: dict[str, dict] = {}
    for term in search_terms:
        for page in range(1, SEARCH_PAGE_CAP + 1):
            await asyncio.sleep(KCI_PACING)
            got = await _retry(
                lambda p=page, t=term: client.search_by_author(person.author_name_kor, affiliation=t, page=p),
                label=f"search {person.author_name_kor}/{term} p{page}",
            )
            if got is None:
                break
            total, articles = got
            for a in articles:
                if a.art_id:
                    candidates[a.art_id] = _article_dict(a)
            if page * 100 >= total:
                break

    await _add_existing(client, existing, candidates)
    result = {"units": units, "search_terms": search_terms, "candidates": candidates}
    cache[rid] = result
    return result


async def _add_existing(client: KCIResearcherClient, existing: list, candidates: dict) -> None:
    for row in existing:
        if row.external_id in candidates:
            continue
        if row.authors:
            candidates[row.external_id] = {
                "art_id": row.external_id, "authors": list(row.authors),
                "author_insts": list(row.author_institutions or [""] * len(row.authors)),
            }
            continue
        await asyncio.sleep(KCI_PACING)
        detail = await _retry(lambda a=row.external_id: client.article_detail(a), label=f"detail {row.external_id}")
        candidates[row.external_id] = _article_dict(detail) if detail else {"art_id": row.external_id, "authors": []}


async def run(*, apply: bool, researcher_id: str | None, everyone: bool = False) -> None:
    async with AsyncSessionLocal() as session:
        if researcher_id:
            people = (await session.execute(
                text("SELECT researcher_id, author_name_kor, institution_current FROM researchers WHERE researcher_id = :r"),
                {"r": researcher_id},
            )).all()
        elif everyone:
            people = (await session.execute(text(
                "SELECT researcher_id, author_name_kor, institution_current FROM researchers "
                "WHERE author_name_kor IS NOT NULL ORDER BY researcher_id"))).all()
        else:
            people = (await session.execute(text(_TARGET_SQL))).all()
        art_map = dict((await session.execute(
            text("SELECT kci_art_id, id FROM papers WHERE kci_art_id IS NOT NULL"))).all())
        # KCI 저자 번호로 다른 사람 논문이라 판정해 뺀 쌍(039) — 소속이 맞아도 다시 붙이지 않는다
        excluded = {(e.researcher_id, e.external_id) for e in (await session.execute(
            text("SELECT researcher_id, external_id FROM researcher_paper_exclusions"))).all()}
    print(f"[reconcile] 대상 {len(people):,}명 ({'반영' if apply else '시험 실행'})")

    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    totals = Counter()
    per_person = []

    # 1단계 — 수집. 개칭 쌍은 전원의 판정을 봐야 나오므로 판정 전에 모두 모은다.
    collected = []
    async with httpx.AsyncClient(timeout=30.0) as http:
        client = KCIResearcherClient(http)
        for idx, person in enumerate(people, start=1):
            async with AsyncSessionLocal() as session:
                existing = (await session.execute(
                    sa.select(ResearcherExternalPaper).where(ResearcherExternalPaper.researcher_id == person.researcher_id)
                )).scalars().all()
            data = await collect(client, person, existing, cache)
            collected.append((person, {r.external_id for r in existing}, data))
            if idx % 20 == 0:
                CACHE.write_text(json.dumps(cache, ensure_ascii=False))
                print(f"  수집 {idx}/{len(people)}", flush=True)
    CACHE.write_text(json.dumps(cache, ensure_ascii=False))

    # 2단계 — 개칭·통폐합으로 확인된 기관 쌍
    equiv = mine_equivalences(collected, await institution_years())
    print(f"[reconcile] 개칭·통폐합으로 본 기관 쌍 {sum(len(v) for v in equiv.values()) // 2}개")
    for a in sorted(equiv):
        print(f"    {a} ≡ {sorted(equiv[a])}")

    # 3단계 — 판정·반영
    for person, have, data in collected:
        candidates = data["candidates"]
        match_unit = specific_unit(person.institution_current)
        if not match_unit:
            # 소속이 상위기관뿐이거나 없으면 소속 근거를 세울 수 없다 — 지우지도 더하지도 않는다.
            totals["구체 기관 없어 건너뜀(명)"] += 1
            continue
        units = [match_unit] + sorted(equiv.get(match_unit, ()))
        verdict = judge(person.author_name_kor, units, candidates)
        keep = {a for a, v in verdict.items() if v in ("inst", "coauthor", "learned_inst")}
        # 저자 정보를 끝내 못 얻은 기존 행은 판정 불가 — 지우지 않고 남긴다.
        keep |= {a for a in have if verdict.get(a) == "unknown"}
        # 본인 근거가 없어도 다른 사람 근거가 없으면 남긴다(other_person_evidence 참고).
        unverified = {
            a for a in have - keep
            if not other_person_evidence(person.author_name_kor, units, candidates[a])
        }
        keep |= unverified
        keep -= {a for a in keep if (person.researcher_id, a) in excluded}
        drop = have - keep
        add = keep - have
        suspects = namesake_suspects(person.author_name_kor, candidates, verdict)
        stat = Counter(verdict.values())
        totals.update({
            "기존 행": len(have), "유지": len(have & keep), "삭제": len(drop), "추가": len(add),
            "근거 부족하지만 유지": len(unverified), "근거1 소속": stat["inst"], "근거2 공저자": stat["coauthor"], "근거3 확인된 소속": stat["learned_inst"],
            "판정불가 유지": stat["unknown"],
            "동명이인 의심(세기만)": suspects,
        })
        per_person.append({
            "researcher_id": person.researcher_id, "name": person.author_name_kor,
            "institution": person.institution_current, "search_terms": data["search_terms"],
            "before": len(have), "after": len(keep), "drop": len(drop), "add": len(add),
            "by_inst": stat["inst"], "by_coauthor": stat["coauthor"], "by_learned_inst": stat["learned_inst"],
            "suspects": suspects,
        })

        if not apply:
            continue
        async with AsyncSessionLocal() as session:
            if drop:
                await session.execute(sa.delete(ResearcherExternalPaper).where(
                    ResearcherExternalPaper.researcher_id == person.researcher_id,
                    ResearcherExternalPaper.external_id.in_(drop),
                ))
            payload = []
            for art_id in add:
                c = candidates[art_id]
                payload.append({
                    "researcher_id": person.researcher_id, "external_source": "kci", "external_id": art_id,
                    "title": c.get("title"), "journal": c.get("journal"), "pubyear": c.get("pubyear"),
                    "pubmonth": c.get("pubmonth"), "citation_count": c.get("cites") or 0,
                    "categories": c.get("categories") or None, "doi": c.get("doi"), "url": c.get("url"),
                    "authors": c.get("authors") or None, "author_institutions": c.get("author_insts") or None,
                    "internal_paper_id": art_map.get(art_id),
                })
            for start in range(0, len(payload), 500):
                await session.execute(pg_insert(ResearcherExternalPaper).values(payload[start:start + 500])
                                      .on_conflict_do_nothing(index_elements=["researcher_id", "external_id"]))
            # 집계값을 남은 행 기준으로 다시 맞춘다 — total_papers와 목록이 어긋나던 원인이 이것이다.
            agg = (await session.execute(text(
                "SELECT count(*) n, coalesce(sum(citation_count),0) c, min(pubyear) y0, max(pubyear) y1 "
                "FROM researcher_external_papers WHERE researcher_id = :r"), {"r": person.researcher_id})).one()
            await session.execute(sa.update(Researcher).where(Researcher.researcher_id == person.researcher_id).values(
                total_papers=agg.n or None, total_citations=agg.c, first_pubyear=agg.y0, last_pubyear=agg.y1,
                citation_source="kci",  # 합계를 KCI 논문 행으로 다시 셌으므로 출처도 KCI다
                updated_at=sa.func.now(),
            ))
            await session.commit()

    CACHE.write_text(json.dumps(cache, ensure_ascii=False))
    report = CHECKPOINT_DIR / "researcher_reconcile_report.json"
    report.write_text(json.dumps(per_person, ensure_ascii=False, indent=1))
    print("[reconcile] 합계:", dict(totals))
    print(f"[reconcile] 연구자별 결과: {report}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="판정대로 DB 반영. 없으면 시험 실행")
    parser.add_argument("--researcher-id")
    parser.add_argument("--all", action="store_true", help="이름이 있는 연구자 전원을 대상으로 한다")
    args = parser.parse_args()
    asyncio.run(run(apply=args.apply, researcher_id=args.researcher_id, everyone=args.all))


if __name__ == "__main__":
    main()
