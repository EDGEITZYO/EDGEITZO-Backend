"""KCI 저자 번호 판정(judge_by_kci_author_ids.py) 결과를 DB에 반영한다.

  1. 판정된 연구자에 researchers.kci_cret_id를 채운다
  2. '다른 사람 논문'을 연구자에게서 떼고 researcher_paper_exclusions에 남긴다(정리 스크립트가 되붙이지 않게).
     단, 본인 논문과 공저자 번호를 2개 이상 공유하는 논문은 KCI가 한 사람을 번호 둘로 쪼갠 것일 수 있어 남긴다
  3. 같은 사람으로 판정된 연구자 ID 쌍을 합친다(merge_duplicate_researchers.merge)
  4. 논문 수·피인용·연도를 남은 행으로 다시 센다
  5. 뗀 논문에서 뽑았던 전공은 비우고, 남은 논문으로 다시 채운다(fill_researcher_departments)

사용법:
  python scripts/apply_kci_author_judgement.py            # 시험 실행 — 바뀔 양만
  python scripts/apply_kci_author_judgement.py --apply
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.load_researchers import CHECKPOINT_DIR, _inst_match  # noqa: E402
from scripts import fill_researcher_departments as dept_fill  # noqa: E402
from scripts.merge_duplicate_researchers import load as load_people, merge, pick_canonical  # noqa: E402
from scripts.reconcile_researcher_papers import _norm  # noqa: E402

from sqlalchemy import text  # noqa: E402

from app.core.database import AsyncSessionLocal  # noqa: E402

JUDGEMENT = CHECKPOINT_DIR / "kci_author_judgement.json"
SPLIT_GUARD_SHARED = 2
REASON = "KCI 저자 번호가 다름 (apply_kci_author_judgement)"


async def build_plan() -> tuple[dict[str, str], dict[str, set], list[tuple[str, str]], int]:
    """(연구자 번호, 뗄 논문, 합칠 쌍, 쪼개짐 의심으로 남긴 수)"""
    judged = json.loads(JUDGEMENT.read_text())
    async with AsyncSessionLocal() as s:
        authors = collections.defaultdict(set)
        for r in (await s.execute(text("SELECT arti_id, cret_id FROM kci_article_authors WHERE cret_id IS NOT NULL"))).all():
            authors[r.arti_id].add(r.cret_id)
        have = collections.defaultdict(set)
        for r in (await s.execute(text("SELECT researcher_id, external_id FROM researcher_external_papers"))).all():
            have[r.researcher_id].add(r.external_id)
    cret: dict[str, str] = {}
    drop: dict[str, set] = {}
    held = 0
    for rid, e in judged["researchers"].items():
        others = set(e.get("others", []))
        if e.get("status") == "ok":
            cret[rid] = e["cret_id"]
            core = {c for a in e["mine"] if a in authors for c in authors[a] - {e["cret_id"]}}
            guard = {a for a in others if a in authors and len(authors[a] & core) >= SPLIT_GUARD_SHARED}
            held += len(guard)
            others -= guard
        others &= have[rid]
        if others:
            drop[rid] = others
    pairs = [(p["a"], p["b"]) for p in judged.get("pairs", []) if p["verdict"] == "같은 사람"]
    return cret, drop, pairs, held


async def stale_departments(drop: dict[str, set]) -> list[str]:
    """지금 전공이 뗄 논문에서만 나오는 연구자 — 전공을 비우고 다시 채운다."""
    async with AsyncSessionLocal() as s:
        people = {r.researcher_id: r for r in (await s.execute(text(
            "SELECT researcher_id, author_name_kor, institution_current, institution_dept FROM researchers "
            "WHERE researcher_id = ANY(:ids) AND institution_dept IS NOT NULL"), {"ids": list(drop)})).all()}
        rows = (await s.execute(text(
            "SELECT researcher_id, external_id, authors, author_institutions FROM researcher_external_papers "
            "WHERE researcher_id = ANY(:ids) AND authors IS NOT NULL"), {"ids": list(people)})).all()
    source = collections.defaultdict(lambda: {"dropped": set(), "kept": set()})
    for r in rows:
        p = people[r.researcher_id]
        for name, inst in zip(r.authors, r.author_institutions or []):
            if _norm(name) == _norm(p.author_name_kor) and inst and _inst_match(p.institution_current, inst):
                d = dept_fill.department_from(inst)
                if d:
                    side = "dropped" if r.external_id in drop[r.researcher_id] else "kept"
                    source[r.researcher_id][side].add(d)
    return [
        rid for rid, p in people.items()
        if p.institution_dept in source[rid]["dropped"] and p.institution_dept not in source[rid]["kept"]
    ]


async def main(apply: bool) -> None:
    cret, drop, pairs, held = await build_plan()
    stale = await stale_departments(drop)
    print(f"[apply] 연구자 번호 채움 {len(cret):,}명 / 뗄 논문 {sum(map(len, drop.values())):,}행(연구자 {len(drop)}명) "
          f"/ 쪼개짐 의심으로 남김 {held} / 합칠 쌍 {len(pairs)} / 전공 다시 채울 연구자 {len(stale)}")
    if not apply:
        return

    async with AsyncSessionLocal() as s:
        for rid, cid in cret.items():
            await s.execute(text("UPDATE researchers SET kci_cret_id = :c WHERE researcher_id = :r"), {"c": cid, "r": rid})
        for rid, arts in drop.items():
            await s.execute(text(
                "DELETE FROM researcher_external_papers WHERE researcher_id = :r AND external_id = ANY(:a)"),
                {"r": rid, "a": list(arts)})
            await s.execute(text(
                "INSERT INTO researcher_paper_exclusions (researcher_id, external_id, reason) "
                "SELECT :r, x, :why FROM unnest(CAST(:a AS text[])) x ON CONFLICT DO NOTHING"),
                {"r": rid, "a": list(arts), "why": REASON})
        if stale:
            await s.execute(text("UPDATE researchers SET institution_dept = NULL WHERE researcher_id = ANY(:ids)"),
                            {"ids": stale})
        await s.commit()

    # 같은 사람 쌍 병합 — 남길 ID는 기존 병합과 같은 규칙
    people, papers, _ = await load_people()
    for a, b in pairs:
        if a in people and b in people:
            keep = pick_canonical([a, b], people, papers)
            await merge(keep, [b if keep == a else a], "KCI 저자 번호가 같음 (apply_kci_author_judgement)")

    # 집계값 다시 세기 (뗀 연구자)
    async with AsyncSessionLocal() as s:
        await s.execute(text(
            "UPDATE researchers r SET total_papers = a.n, total_citations = a.c, first_pubyear = a.y0, "
            "  last_pubyear = a.y1, citation_source = 'kci', updated_at = now() "
            "FROM (SELECT researcher_id, count(*) n, coalesce(sum(citation_count),0) c, min(pubyear) y0, max(pubyear) y1 "
            "      FROM researcher_external_papers WHERE researcher_id = ANY(:ids) GROUP BY researcher_id) a "
            "WHERE r.researcher_id = a.researcher_id"), {"ids": list(drop)})
        await s.execute(text("DELETE FROM researcher_flow_cache WHERE researcher_id = ANY(:ids)"), {"ids": list(drop)})
        await s.commit()

    await dept_fill.main(apply=True)
    print("[apply] 반영 완료")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true")
    asyncio.run(main(p.parse_args().apply))
