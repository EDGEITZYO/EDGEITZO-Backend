"""연구자 전공(institution_dept)이 비어 있으면 본인 최근 논문의 소속 문자열에서 학과를 뽑아 채운다.

KCI는 학과를 따로 주지 않아 전공 보유율이 21%(3,688명 중 785명)였다. 그런데 논문의 저자 소속 칸에는
"충북대학교 환경공학과"처럼 학과까지 적힌 경우가 많다(2026-09-29 실측: 비어 있는 2,903명 중 1,304명).

규칙:
  - 논문 속 그 저자의 소속이 연구자의 현 소속과 같은 기관일 때만 쓴다(_inst_match) — 다른 기관 시절 학과를 넣지 않는다
  - 그중 가장 최근 논문의 학과를 쓴다
  - 상위기관 뒤의 구체 기관명은 떼어낸다: "농촌진흥청 국립농업과학원 토양비료과" → "토양비료과"
  - 끝에 붙은 신분 표기는 뗀다: "심리학과 석사과정생" → "심리학과"
  - 남는 것이 기관명 자체뿐이면 쓰지 않는다
  - 이미 전공이 있는 연구자는 건드리지 않는다

사용법:
  python scripts/fill_researcher_departments.py            # 시험 실행 — 채울 값만 보고
  python scripts/fill_researcher_departments.py --apply
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.load_researchers import _inst_match  # noqa: E402 — .env를 먼저 읽는 모듈
from scripts.reconcile_researcher_papers import specific_unit  # noqa: E402

from sqlalchemy import text  # noqa: E402

from app.core.database import AsyncSessionLocal  # noqa: E402
from app.integrations.kci.researcher_client import institution_dept  # noqa: E402

_norm = lambda s: re.sub(r"\s+", "", s or "")  # noqa: E731
# 학과 뒤에 붙는 신분 표기. "심리학과 석사과정생" → "심리학과"
_ROLE_SUFFIX = re.compile(
    r"\s*(석박사통합과정생?|석사과정생?|박사과정생?|박사후연구원|대학원생|학부생|연구교수|조교수|부교수|교수|"
    r"책임연구원|선임연구원|연구원|연구위원|교사|학생)\s*$"
)


def department_from(inst: str) -> str | None:
    """논문 속 소속 한 줄에서 학과. 상위기관·구체 기관명을 떼고 남은 것."""
    dept = institution_dept(inst)
    if not dept:
        return None
    unit = specific_unit(inst)
    if unit and dept.startswith(unit):
        dept = dept[len(unit):].strip()
    dept = _ROLE_SUFFIX.sub("", dept).strip()
    if not dept or (unit and _norm(dept) == _norm(unit)):
        return None
    return dept


async def plan() -> dict[str, str]:
    async with AsyncSessionLocal() as s:
        people = {r.researcher_id: r for r in (await s.execute(text(
            "SELECT researcher_id, author_name_kor, institution_current FROM researchers "
            "WHERE author_name_kor IS NOT NULL AND institution_current IS NOT NULL "
            "AND (institution_dept IS NULL OR btrim(institution_dept) = '')"))).all()}
        rows = (await s.execute(text(
            "SELECT researcher_id, pubyear, pubmonth, authors, author_institutions FROM researcher_external_papers "
            "WHERE researcher_id = ANY(:ids) AND authors IS NOT NULL AND author_institutions IS NOT NULL"),
            {"ids": list(people)})).all()
    best: dict[str, tuple] = {}
    for r in rows:
        p = people[r.researcher_id]
        month = int(r.pubmonth) if r.pubmonth and str(r.pubmonth).isdigit() else 0
        key = (r.pubyear or 0, month)
        for name, inst in zip(r.authors, r.author_institutions):
            if _norm(name) != _norm(p.author_name_kor) or not inst or not _inst_match(p.institution_current, inst):
                continue
            dept = department_from(inst)
            if dept and (r.researcher_id not in best or key > best[r.researcher_id][0]):
                best[r.researcher_id] = (key, dept)
    return {rid: v[1] for rid, v in best.items()}


async def main(apply: bool) -> None:
    fills = await plan()
    print(f"[dept] 채울 연구자 {len(fills):,}명")
    print("  자주 나오는 값:", Counter(fills.values()).most_common(12))
    for rid, d in list(fills.items())[:8]:
        print(f"    {rid} → {d}")
    if not apply:
        return
    async with AsyncSessionLocal() as s:
        for rid, dept in fills.items():
            await s.execute(text(
                "UPDATE researchers SET institution_dept = :d, updated_at = now() "
                "WHERE researcher_id = :r AND (institution_dept IS NULL OR btrim(institution_dept) = '')"),
                {"d": dept[:500], "r": rid})
        await s.commit()
        total, with_dept = (await s.execute(text(
            "SELECT count(*), count(*) FILTER (WHERE institution_dept IS NOT NULL AND btrim(institution_dept) <> '') "
            "FROM researchers"))).one()
    print(f"[dept] 반영 완료 — 전공 보유 {with_dept:,}/{total:,} ({with_dept / total:.0%})")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true")
    asyncio.run(main(p.parse_args().apply))
