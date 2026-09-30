"""KCI 저자 번호(CRT…)로 남은 동명이인 문제를 판정한다 — DB는 바꾸지 않고 판정 결과만 낸다.

입력: kci_article_authors(collect_kci_author_ids.py) + kci_lookup_plan.json(plan_kci_author_lookup.py)

판정 방법:
  1. 연구자 번호 = 그 연구자의 '믿을 만한 논문'에 가장 많이 나오는 저자 번호.
     믿을 만한 논문: 코퍼스 근거 논문(researcher_papers) → 없으면 소속이 맞는 논문 → 없으면 조회한 논문 전부
     공저자는 일부 논문에만 나오고 본인은 모든 본인 논문에 나오므로, 가장 많이 나오는 번호가 본인이다.
     1위 번호가 믿을 만한 논문의 절반 미만에만 나오면 판정 불가(ambiguous)로 둔다.
  2. 논문 판정: 그 논문의 저자 번호에 연구자 번호가 있으면 본인, 없으면 다른 사람(같은 이름의 다른 번호).
  3. 공저자 덩어리 표본: 덩어리에서 조회한 논문의 판정이 모두 같으면 덩어리 전체에 적용, 섞이면 판정 불가.

결과: data/checkpoints/kci_author_judgement.json
"""
from __future__ import annotations

import asyncio
import collections
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.load_researchers import CHECKPOINT_DIR, _inst_match  # noqa: E402
from scripts.plan_kci_author_lookup import components  # noqa: E402
from scripts.reconcile_researcher_papers import _own_institutions, specific_unit  # noqa: E402

from sqlalchemy import text  # noqa: E402

from app.core.database import AsyncSessionLocal  # noqa: E402

OUT = CHECKPOINT_DIR / "kci_author_judgement.json"
MIN_SHARE = 0.5


def pick_researcher_id(
    trusted: list[str], all_papers: list[str], authors_of: dict[str, set]
) -> tuple[str | None, float]:
    """믿을 만한 논문들에 가장 많이 나오는 저자 번호와, 그 번호가 나온 비율.

    동률은 그 연구자의 조회한 논문 전체에서 더 많이 나오는 번호로 가른다. 믿을 만한 논문이 1편이면
    그 논문의 저자 전원이 1회로 동률이라, 동률을 가르지 않으면 공저자 번호가 뽑힌다
    (곽이섭 193편 중 1편만 본인으로 나온 오판정 — 2026-09-30).
    """
    fetched = [a for a in trusted if a in authors_of]
    if not fetched:
        return None, 0.0
    counts = collections.Counter(c for a in fetched for c in authors_of[a])
    if not counts:
        return None, 0.0
    overall = collections.Counter(c for a in all_papers if a in authors_of for c in authors_of[a])
    cid = max(counts, key=lambda c: (counts[c], overall[c], c))
    return cid, counts[cid] / len(fetched)


async def main() -> None:
    plan = json.loads((CHECKPOINT_DIR / "kci_lookup_plan.json").read_text())
    async with AsyncSessionLocal() as s:
        authors_of: dict[str, set] = collections.defaultdict(set)
        for r in (await s.execute(text("SELECT arti_id, cret_id FROM kci_article_authors WHERE cret_id IS NOT NULL"))).all():
            authors_of[r.arti_id].add(r.cret_id)
        people = {r.researcher_id: r for r in (await s.execute(text(
            "SELECT researcher_id, author_name_kor, institution_current FROM researchers WHERE author_name_kor IS NOT NULL"
        ))).all()}
        papers = collections.defaultdict(list)
        for r in (await s.execute(text(
            "SELECT researcher_id, external_id, authors, author_institutions FROM researcher_external_papers "
            "WHERE external_id LIKE 'ART%'"))).all():
            papers[r.researcher_id].append(r)
        anchors = collections.defaultdict(set)
        for r in (await s.execute(text(
            "SELECT rp.researcher_id, p.kci_art_id FROM researcher_papers rp JOIN papers p ON p.id = rp.paper_id "
            "WHERE p.kci_art_id IS NOT NULL"))).all():
            anchors[r.researcher_id].add(r.kci_art_id)

    def researcher_identity(rid: str) -> tuple[str | None, float, str]:
        p = people[rid]
        arts = [r.external_id for r in papers[rid]]
        trusted = [a for a in arts if a in anchors[rid]]
        basis = "코퍼스 근거 논문"
        if not [a for a in trusted if a in authors_of]:
            unit = specific_unit(p.institution_current)
            trusted = [
                r.external_id for r in papers[rid]
                if unit and any(_inst_match(unit, i) for i in _own_institutions(r.authors or [], r.author_institutions or [], p.author_name_kor) if i)
            ]
            basis = "소속이 맞는 논문"
        if not [a for a in trusted if a in authors_of]:
            trusted, basis = arts, "조회한 논문 전부"
        cid, share = pick_researcher_id(trusted, arts, authors_of)
        return cid, share, basis

    report: dict = {"researchers": {}, "summary": {}}
    stat = collections.Counter()

    def judge_researcher(rid: str, sampled: bool) -> dict:
        cid, share, basis = researcher_identity(rid)
        entry = {"name": people[rid].author_name_kor, "institution": people[rid].institution_current,
                 "cret_id": cid, "share": round(share, 2), "basis": basis, "mine": [], "others": [], "undecided": []}
        if not cid or share < MIN_SHARE:
            entry["status"] = "ambiguous"
            stat["연구자 번호 판정 불가"] += 1
            return entry
        entry["status"] = "ok"
        arts = [(r.external_id, list(r.authors or [])) for r in papers[rid]]
        groups = components(people[rid].author_name_kor, arts) if sampled else [[a] for a, _ in arts]
        for g in groups:
            looked = [a for a in g if a in authors_of]
            if not looked:
                entry["undecided"] += g
                continue
            verdicts = {cid in authors_of[a] for a in looked}
            if verdicts == {True}:
                entry["mine"] += g
            elif verdicts == {False}:
                entry["others"] += g
            else:
                # 덩어리 안에서 갈리면 조회한 논문만 판정하고 나머지는 판정 불가
                entry["mine"] += [a for a in looked if cid in authors_of[a]]
                entry["others"] += [a for a in looked if cid not in authors_of[a]]
                entry["undecided"] += [a for a in g if a not in authors_of]
        return entry

    # A. 동명이인 의심 / C. 상위기관뿐 — 덩어리 표본
    suspects = [x["researcher_id"] for x in json.loads((CHECKPOINT_DIR / "namesake_suspects.json").read_text())]
    rda = [rid for rid, p in people.items() if not specific_unit(p.institution_current) and papers[rid]]
    for label, ids in (("A", suspects), ("C", rda)):
        for rid in ids:
            if rid not in people:
                continue
            e = judge_researcher(rid, sampled=True)
            e["category"] = label
            report["researchers"][rid] = e
            stat[f"{label} 연구자"] += 1
            stat[f"{label} 다른 사람 논문"] += len(e["others"])
            stat[f"{label} 본인 논문"] += len(e["mine"])
            stat[f"{label} 판정 불가 논문"] += len(e["undecided"])
            if e["others"]:
                stat[f"{label} 다른 사람 논문이 섞인 연구자"] += 1

    # B. 근거 부족으로 유지한 논문 — 논문마다 판정
    b_rows = set(plan["by_reason"].get("B 근거 부족 유지", []))
    for rid, rows in papers.items():
        mine_b = [r.external_id for r in rows if r.external_id in b_rows]
        if not mine_b or rid not in people:
            continue
        cid, share, basis = researcher_identity(rid)
        for a in mine_b:
            if a not in authors_of or not cid or share < MIN_SHARE:
                stat["B 판정 불가"] += 1
            elif cid in authors_of[a]:
                stat["B 본인(유지 맞음)"] += 1
            else:
                stat["B 다른 사람(지워야 함)"] += 1
                report["researchers"].setdefault(rid, {"category": "B", "others": []})["others"].append(a)

    # D. 같은 이름 ID 쌍 — 번호가 같으면 같은 사람
    pairs = collections.Counter()
    by_paper = collections.defaultdict(set)
    for rid, rows in papers.items():
        for r in rows:
            by_paper[r.external_id].add(rid)
    for rids in by_paper.values():
        rids = sorted(x for x in rids if x in people)
        for i in range(len(rids)):
            for j in range(i + 1, len(rids)):
                if people[rids[i]].author_name_kor.replace(" ", "") == people[rids[j]].author_name_kor.replace(" ", ""):
                    pairs[(rids[i], rids[j])] += 1
    report["pairs"] = []
    for (a, b), n in pairs.items():
        if n >= 3:
            continue
        ca, _, _ = researcher_identity(a)
        cb, _, _ = researcher_identity(b)
        verdict = "판정 불가" if not ca or not cb else ("같은 사람" if ca == cb else "다른 사람")
        stat[f"D 쌍 {verdict}"] += 1
        report["pairs"].append({"a": a, "b": b, "shared": n, "verdict": verdict})

    report["summary"] = dict(stat)
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=1))
    for k in sorted(stat):
        print(f"  {k}: {stat[k]:,}")
    print(f"[judge] 결과 → {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
