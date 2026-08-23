"""연구자 데이터 적재 — KCI 백본 + ScienceON 이메일 오버레이.

설계 근거(실측):
  · ScienceON 연구자 API는 이메일을 주지만 연구자↔논문 색인이 불완전해 매칭률이 46%다.
    반면 KCI는 소속·역할·전체 논문·피인용·연도이력을 전부 커버한다. → KCI를 기준으로 삼고
    ScienceON은 이메일/대표키워드 오버레이로만 쓴다.
  · KCI articleSearch를 이름만으로 치면 흔한 이름이 폭발한다(김민정 3,718건). articleDetail로
    얻은 소속을 affiliation 파라미터에 같이 넣으면 서버에서 걸러진다(김선영 2,153 → 47).
  · ScienceON은 토큰당 동시 요청을 거부한다(concurrency 2에서 30건 중 10건 실패) → 순차 호출.

단계:
  anchor   코퍼스 논문의 KCI articleDetail → 저자 실체(이름·소속·학과·역할·영문명) 확정
  expand   저자별 articleSearch(author+affiliation) → 외부 논문·총 피인용·연도이력
  email    ScienceON 연구자 검색 → 이메일/대표키워드 오버레이 (신뢰도 등급과 함께)

사용법:
  python scripts/load_researchers.py --stage anchor
  python scripts/load_researchers.py --stage expand
  python scripts/load_researchers.py --stage email
  python scripts/load_researchers.py --stage all --limit 20     # 시범 적재
  python scripts/load_researchers.py --report                   # 적재 결과 검증 리포트
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_ENV_PATH = PROJECT_ROOT / ".env"
if _ENV_PATH.exists():
    for _line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        if _k.strip():
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

import httpx
import sqlalchemy as sa
import xmltodict
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.database import AsyncSessionLocal
from app.core.settings import settings
from app.integrations.kci.researcher_client import (
    KCIResearcherClient,
    institution_dept,
    institution_root,
)
from app.integrations.scienceon.researcher_client import (
    ScienceOnResearcherClient,
    parse_researcher_detail_xml,
    parse_researcher_search_xml,
)
from app.models.researcher import Researcher, ResearcherExternalPaper, ResearcherPaper

CHECKPOINT_DIR = PROJECT_ROOT / "data" / "checkpoints"
ANCHOR_CKPT = CHECKPOINT_DIR / "researcher_anchor.json"
EXPAND_CKPT = CHECKPOINT_DIR / "researcher_expand.json"
EMAIL_CKPT = CHECKPOINT_DIR / "researcher_email.json"
KEYWORD_CKPT = CHECKPOINT_DIR / "researcher_keywords.json"
THESIS_CKPT = CHECKPOINT_DIR / "researcher_thesis.json"
ADJUDICATE_CKPT = CHECKPOINT_DIR / "researcher_adjudicate.json"

KCI_PACING = 0.25
SCI_PACING = 0.2
MAX_RETRIES = 4
RATE_LIMIT_BACKOFF = 3.0
# 소속으로 걸러도 이만큼 넘으면 흔한 이름이 섞인 것 — 최신순 앞쪽만 쓰고 표시를 남긴다.
EXPAND_PAGE_CAP = 3
EXPAND_TOTAL_CAP = EXPAND_PAGE_CAP * 100


def _norm(value: str | None) -> str:
    return re.sub(r"[\s·,\.\-]", "", value or "").strip()


def researcher_id_for(name: str, institution: str | None) -> str:
    root = institution_root(institution) or ""
    digest = hashlib.sha1(f"{_norm(name)}|{_norm(root)}".encode()).hexdigest()[:16]
    return f"kci:{digest}"


def _inst_match(a: str | None, b: str | None) -> bool:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return False
    return a == b or a in b or b in a


def _load_ckpt(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return {}
    return {}


def _save_ckpt(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


async def _retry(factory, *, label: str):
    """429는 일반 오류보다 길게 물러난다 — 두 API 모두 지속 호출 시 429를 던진다(실측)."""
    for attempt in range(MAX_RETRIES):
        try:
            return await factory()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                await asyncio.sleep(RATE_LIMIT_BACKOFF * (2 ** attempt))
                continue
            if attempt == MAX_RETRIES - 1:
                print(f"    [오류] {label}: HTTP {exc.response.status_code}")
                return None
            await asyncio.sleep(2 ** attempt)
        except Exception as exc:  # noqa: BLE001 — 네트워크 계열 전반
            if attempt == MAX_RETRIES - 1:
                print(f"    [오류] {label}: {exc}")
                return None
            await asyncio.sleep(2 ** attempt)
    return None


# ─────────────────────────────── anchor ───────────────────────────────

async def stage_anchor(limit: int | None) -> None:
    print("[anchor] 코퍼스 논문의 KCI 저자정보 수집")
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT id, kci_art_id, title, pubyear FROM papers "
                    "WHERE kci_art_id IS NOT NULL ORDER BY id"
                )
            )
        ).all()
    if limit:
        rows = rows[:limit]
    print(f"[anchor] 대상 논문 {len(rows):,}편")

    ckpt = _load_ckpt(ANCHOR_CKPT)
    stats = Counter()
    # (researcher_id) -> 누적 정보. 여러 논문에 걸쳐 최신 소속을 고른다.
    people: dict[str, dict] = {}
    links: list[dict] = []

    async with httpx.AsyncClient(timeout=30.0) as http:
        client = KCIResearcherClient(http)
        for idx, row in enumerate(rows, start=1):
            cached = ckpt.get(row.id)
            if cached is None:
                await asyncio.sleep(KCI_PACING)
                article = await _retry(
                    lambda aid=row.kci_art_id: client.article_detail(aid), label=f"detail {row.kci_art_id}"
                )
                if article is None:
                    stats["detail_fail"] += 1
                    continue
                cached = {
                    "authors": [
                        {"name": a.name, "eng": a.name_eng, "inst": a.institution, "role": a.role, "order": a.order}
                        for a in article.authors
                    ],
                    "keywords": article.keywords,
                    "pubyear": article.pubyear,
                }
                ckpt[row.id] = cached
                if idx % 25 == 0:
                    _save_ckpt(ANCHOR_CKPT, ckpt)

            if not cached["authors"]:
                stats["no_authors"] += 1
                continue

            pubyear = cached.get("pubyear") or row.pubyear
            for author in cached["authors"]:
                rid = researcher_id_for(author["name"], author["inst"])
                person = people.setdefault(
                    rid,
                    {
                        "researcher_id": rid,
                        "name": author["name"],
                        "name_eng": author["eng"],
                        "institutions": {},
                        "corpus_papers": set(),
                        "keywords": Counter(),
                    },
                )
                if author["eng"] and not person["name_eng"]:
                    person["name_eng"] = author["eng"]
                if author["inst"]:
                    # 같은 소속이 여러 해에 걸치면 가장 최근 연도를 남긴다.
                    prev = person["institutions"].get(author["inst"])
                    if prev is None or (pubyear or 0) > prev:
                        person["institutions"][author["inst"]] = pubyear or 0
                person["corpus_papers"].add(row.id)
                person["keywords"].update(cached.get("keywords") or [])
                links.append(
                    {
                        "researcher_id": rid,
                        "paper_id": row.id,
                        "author_order": author["order"],
                        "role": author["role"],
                        "institution_at_time": author["inst"],
                    }
                )
            stats["ok"] += 1
            if idx % 50 == 0:
                print(f"  {idx}/{len(rows)} — 연구자 {len(people):,}명 누적")

    _save_ckpt(ANCHOR_CKPT, ckpt)
    print(f"[anchor] 논문 처리 {stats['ok']:,} / 저자없음 {stats['no_authors']} / 조회실패 {stats['detail_fail']}")
    # 한 논문에 같은 인물이 두 줄로 실리는 경우가 있다(소속을 나눠 적은 경우 등) —
    # (연구자, 논문)이 중복되면 ON CONFLICT가 같은 행을 두 번 건드려 실패한다.
    deduped: dict[tuple[str, str], dict] = {}
    for link in links:
        deduped.setdefault((link["researcher_id"], link["paper_id"]), link)
    dropped = len(links) - len(deduped)
    links = list(deduped.values())
    print(f"[anchor] 고유 연구자 {len(people):,}명, 논문-저자 링크 {len(links):,}건" + (f" (중복 {dropped}건 제거)" if dropped else ""))

    async with AsyncSessionLocal() as session:
        for rid, person in people.items():
            ordered = sorted(person["institutions"].items(), key=lambda kv: -kv[1])
            current = ordered[0][0] if ordered else None
            values = {
                "researcher_id": rid,
                "source": "kci",
                "author_name_kor": person["name"],
                "author_name_eng": person["name_eng"],
                "institution_current": current,
                "institution_dept": institution_dept(current),
                "institution_history": [i for i, _ in ordered] or None,
                "keywords": [k for k, _ in person["keywords"].most_common(10)] or None,
                "corpus_paper_count": len(person["corpus_papers"]),
            }
            stmt = pg_insert(Researcher).values(**values).on_conflict_do_update(
                index_elements=["researcher_id"],
                set_={k: v for k, v in values.items() if k != "researcher_id"} | {"updated_at": sa.func.now()},
            )
            await session.execute(stmt)
        await session.commit()

        for start in range(0, len(links), 500):
            chunk = links[start : start + 500]
            stmt = pg_insert(ResearcherPaper).values(chunk)
            stmt = stmt.on_conflict_do_update(
                index_elements=["researcher_id", "paper_id"],
                set_={
                    "author_order": stmt.excluded.author_order,
                    "role": stmt.excluded.role,
                    "institution_at_time": stmt.excluded.institution_at_time,
                },
            )
            await session.execute(stmt)
        await session.commit()
    print("[anchor] DB 반영 완료")


# ─────────────────────────────── thesis ───────────────────────────────

# 학위논문(DIKO)·학술대회(CFKO)는 kci_art_id도 DOI도 없어서 anchor·OpenAlex 어느 경로에도
# 걸리지 않았다. 그 결과 논문 153편의 저자가 통째로 빠져 있었다.
#
# 이들은 KCI로 확장할 수 없다(실측): 대학원생이라 학술지 논문이 아직 없다.
#   권태광(중부대) 이름 검색 13건 → 전부 안동대·메리놀병원·연세대의 동명이인
#   한유림(한양대) 4건, 류병석(연세대) 3건도 같음
# 이름만으로 긁으면 동명이인 13명이 한 사람으로 뭉치므로, 확장하지 않고
# ScienceON이 주는 학위논문 자체의 저자·소속만으로 연구자를 만든다.
_THESIS_META_CODES = ("Author", "Affiliation", "Publisher", "Degree")


def _split_semicolon(raw: str | None) -> list[str]:
    """'김건우;윤영빈;' → ['김건우', '윤영빈']. 빈 조각과 꼬리 세미콜론을 버린다."""
    if not raw:
        return []
    return [part.strip() for part in raw.split(";") if part.strip()]


def _scienceon_fields(xml: str) -> dict:
    """ScienceON 논문 상세 XML → metaCode 사전. 중첩 item을 재귀로 훑는다."""
    try:
        parsed = xmltodict.parse(xml)
        record = parsed["MetaData"]["recordList"]["record"]
    except (KeyError, TypeError, Exception):  # noqa: B014 — 파싱 실패 전반
        return {}
    if isinstance(record, list):
        record = record[0] if record else {}
    found: dict[str, str] = {}

    def walk(items):
        if isinstance(items, dict):
            items = [items]
        for item in items or []:
            if not isinstance(item, dict):
                continue
            code, value = item.get("@metaCode"), item.get("#text")
            if code and value:
                found.setdefault(code, value)
            if "item" in item:
                walk(item["item"])

    walk(record.get("item"))
    return found


async def stage_thesis(limit: int | None) -> None:
    print("[thesis] 학위논문·학술대회 저자 적재")
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    # 학위논문·학술대회 + anchor(KCI)·OpenAlex 어느 경로에도 안 걸린 논문.
                    # 후자는 국내 학술지라 OpenAlex에 없고, KCI 제목 검색은 특수문자·표기차로
                    # 22편 중 7편만 맞았다(실측). ScienceON은 CN으로 바로 조회하므로 확실하다.
                    "SELECT id, scienceon_cn, authors, keywords_ko, degree, pubyear "
                    "FROM papers p WHERE (db_code IN ('DIKO','CFKO') "
                    "  OR NOT EXISTS (SELECT 1 FROM researcher_papers rp WHERE rp.paper_id = p.id)) "
                    "ORDER BY id"
                )
            )
        ).all()
    if limit:
        rows = rows[:limit]
    print(f"[thesis] 대상 논문 {len(rows):,}편")

    ckpt = _load_ckpt(THESIS_CKPT)
    stats = Counter()
    people: dict[str, dict] = {}
    links: dict[tuple[str, str], dict] = {}

    async with httpx.AsyncClient(timeout=30.0) as http:
        for idx, row in enumerate(rows, start=1):
            cached = ckpt.get(row.id)
            if cached is None:
                await asyncio.sleep(SCI_PACING)
                params = {
                    "client_id": settings.scienceon_client_id,
                    "token": settings.scienceon_token,
                    "version": settings.scienceon_version,
                    "action": "browse",
                    "target": "ARTI",
                    "cn": row.scienceon_cn,
                }
                resp = await _retry(
                    lambda: http.get(settings.scienceon_base_url, params=params),
                    label=f"thesis {row.scienceon_cn}",
                )
                if resp is None:
                    stats["조회 실패"] += 1
                    continue
                fields = _scienceon_fields(resp.text)
                cached = {
                    "author": fields.get("Author"),
                    # 학위논문은 Affiliation이 비어도 Publisher에 학교가 들어온다(실측).
                    "affiliation": fields.get("Affiliation") or fields.get("Publisher"),
                }
                ckpt[row.id] = cached
                if idx % 25 == 0:
                    _save_ckpt(THESIS_CKPT, ckpt)

            # ScienceON은 다중 저자를 "김건우;윤영빈;" / "(주)유스풀제스트;(주)유스풀제스트;"처럼
            # 세미콜론으로 이어 붙여 준다. 순서가 서로 맞으므로 짝지어 쓴다.
            authors = _split_semicolon(cached.get("author"))
            affiliations = _split_semicolon(cached.get("affiliation"))
            if not authors:
                authors = [a.strip() for a in (row.authors or []) if a and a.strip()]
            if not authors:
                stats["저자 없음"] += 1
                continue
            is_thesis = bool(row.degree)

            for order, author in enumerate(authors, start=1):
                affiliation = affiliations[order - 1] if order - 1 < len(affiliations) else None
                if not affiliation:
                    stats["소속 없음"] += 1
                # 영문 소속("Graduate School, Yonsei University")은 기관 조각을 먼저 골라낸다.
                display_institution = polish_institution(affiliation)
                rid = researcher_id_for(author, display_institution)
                person = people.setdefault(
                    rid,
                    {
                        "researcher_id": rid,
                        "name": author,
                        "institution": display_institution,
                        "keywords": Counter(),
                        "papers": set(),
                    },
                )
                person["keywords"].update(row.keywords_ko or [])
                person["papers"].add(row.id)
                links[(rid, row.id)] = {
                    "researcher_id": rid,
                    "paper_id": row.id,
                    "author_order": order,
                    # 학위논문은 단독 저자. 학술지 논문은 ScienceON이 역할을 주지 않는다.
                    "role": "단독" if is_thesis else None,
                    "institution_at_time": affiliation,
                }
            stats["확보"] += 1

    _save_ckpt(THESIS_CKPT, ckpt)
    print(f"[thesis] {dict(stats)}")
    print(f"[thesis] 연구자 {len(people):,}명 / 논문 링크 {len(links):,}건")

    async with AsyncSessionLocal() as session:
        for rid, person in people.items():
            values = {
                "researcher_id": rid,
                "source": "scienceon",
                "author_name_kor": person["name"],
                "institution_current": person["institution"],
                "institution_dept": institution_dept(person["institution"]),
                "institution_history": [person["institution"]] if person["institution"] else None,
                "keywords": [k for k, _ in person["keywords"].most_common(15)] or None,
                "corpus_paper_count": len(person["papers"]),
                # 학위논문 1편이 이 사람에 대해 아는 전부다. 피인용은 데이터 없음으로 둔다
                # (명세서의 "없으면 데이터 없음으로 표시" 폴백에 해당).
                "total_papers": len(person["papers"]),
            }
            stmt = pg_insert(Researcher).values(**values)
            # 같은 인물이 JAKO 저자로도 잡혀 있으면 그쪽 수집 결과가 더 풍부하다 — 덮지 않는다.
            await session.execute(
                stmt.on_conflict_do_update(
                    index_elements=["researcher_id"],
                    set_={
                        "institution_current": sa.func.coalesce(
                            Researcher.__table__.c.institution_current, stmt.excluded.institution_current
                        ),
                        "keywords": sa.func.coalesce(Researcher.__table__.c.keywords, stmt.excluded.keywords),
                        "updated_at": sa.func.now(),
                    },
                )
            )
        await session.commit()

        rows_to_link = list(links.values())
        for start in range(0, len(rows_to_link), 500):
            stmt = pg_insert(ResearcherPaper).values(rows_to_link[start : start + 500])
            await session.execute(
                stmt.on_conflict_do_update(
                    index_elements=["researcher_id", "paper_id"],
                    set_={
                        "author_order": stmt.excluded.author_order,
                        "role": stmt.excluded.role,
                        "institution_at_time": stmt.excluded.institution_at_time,
                    },
                )
            )
        await session.commit()
        # 코퍼스 논문 수는 실제 링크로 다시 센다.
        await session.execute(
            text(
                "UPDATE researchers c SET corpus_paper_count = "
                "(SELECT count(*) FROM researcher_papers WHERE researcher_id = c.researcher_id) "
                "WHERE c.researcher_id = ANY(:ids)"
            ),
            {"ids": list(people)},
        )
        await session.commit()
    print("[thesis] DB 반영 완료")


# ─────────────────────────────── expand ───────────────────────────────

async def stage_expand(limit: int | None, *, refresh: bool = False) -> None:
    print("[expand] 연구자별 KCI 전체 논문·피인용 수집")
    async with AsyncSessionLocal() as session:
        people = (
            await session.execute(
                select(
                    Researcher.researcher_id,
                    Researcher.author_name_kor,
                    Researcher.institution_current,
                ).where(Researcher.source == "kci").order_by(Researcher.researcher_id)
            )
        ).all()
        art_map = dict(
            (
                await session.execute(
                    text("SELECT kci_art_id, id FROM papers WHERE kci_art_id IS NOT NULL")
                )
            ).all()
        )
    if limit:
        people = people[:limit]
    print(f"[expand] 대상 연구자 {len(people):,}명")

    ckpt = _load_ckpt(EXPAND_CKPT)
    stats = Counter()

    async with httpx.AsyncClient(timeout=30.0) as http:
        client = KCIResearcherClient(http)
        for idx, person in enumerate(people, start=1):
            rid = person.researcher_id
            cached = ckpt.get(rid)
            # 저자 없이 저장된 옛 체크포인트는 다시 받아야 한다.
            if cached is not None and (refresh or not cached.get("articles") or "authors" not in cached["articles"][0]):
                cached = None
            if cached is None:
                root = institution_root(person.institution_current)
                await asyncio.sleep(KCI_PACING)
                first = await _retry(
                    lambda: client.search_by_author(person.author_name_kor, affiliation=root, page=1),
                    label=f"search {person.author_name_kor}",
                )
                if first is None:
                    stats["search_fail"] += 1
                    continue
                total, articles = first
                truncated = total > EXPAND_TOTAL_CAP
                pages = min((total + 99) // 100, EXPAND_PAGE_CAP)
                for page in range(2, pages + 1):
                    await asyncio.sleep(KCI_PACING)
                    more = await _retry(
                        lambda p=page: client.search_by_author(person.author_name_kor, affiliation=root, page=p),
                        label=f"search {person.author_name_kor} p{page}",
                    )
                    if more is None:
                        break
                    articles.extend(more[1])

                # 인라인 소속으로 재검증 — affiliation 필터가 학과 단위까지는 못 거른다.
                kept = []
                for article in articles:
                    for author in article.authors:
                        if _norm(author.name) != _norm(person.author_name_kor):
                            continue
                        if root and author.institution and not _inst_match(root, author.institution):
                            continue
                        kept.append(article)
                        break
                cached = {
                    "total": total,
                    "truncated": truncated,
                    "articles": [
                        {
                            "art_id": a.art_id, "title": a.title, "journal": a.journal,
                            "pubyear": a.pubyear, "pubmonth": a.pubmonth, "cites": a.citation_count,
                            "categories": a.categories, "doi": a.doi, "url": a.url,
                            # 저자를 함께 남긴다 — 「함께 연구한 사람들」의 실제 데이터원.
                            "authors": [au.name for au in a.authors if au.name],
                            "author_insts": [au.institution or "" for au in a.authors if au.name],
                        }
                        for a in kept
                    ],
                }
                ckpt[rid] = cached
                if idx % 25 == 0:
                    _save_ckpt(EXPAND_CKPT, ckpt)

            articles = cached["articles"]
            if not articles:
                stats["empty"] += 1
            years = [a["pubyear"] for a in articles if a["pubyear"]]
            total_citations = sum(a["cites"] or 0 for a in articles)

            async with AsyncSessionLocal() as session:
                await session.execute(
                    sa.update(Researcher)
                    .where(Researcher.researcher_id == rid)
                    .values(
                        total_papers=len(articles) or None,
                        total_citations=total_citations,
                        citation_source="kci",
                        first_pubyear=min(years) if years else None,
                        last_pubyear=max(years) if years else None,
                        papers_truncated=bool(cached["truncated"]),
                        expanded_at=datetime.now(timezone.utc),
                        updated_at=sa.func.now(),
                    )
                )
                if articles:
                    payload = [
                        {
                            "researcher_id": rid,
                            "external_source": "kci",
                            "external_id": a["art_id"],
                            "title": a["title"],
                            "journal": a["journal"],
                            "pubyear": a["pubyear"],
                            "pubmonth": a["pubmonth"],
                            "citation_count": a["cites"] or 0,
                            "categories": a["categories"] or None,
                            "doi": a["doi"],
                            "url": a["url"],
                            "authors": a.get("authors") or None,
                            "author_institutions": [i for i in (a.get("author_insts") or [])] or None,
                            "internal_paper_id": art_map.get(a["art_id"]),
                        }
                        for a in articles
                        if a["art_id"]
                    ]
                    for start in range(0, len(payload), 500):
                        stmt = pg_insert(ResearcherExternalPaper).values(payload[start : start + 500])
                        await session.execute(
                            stmt.on_conflict_do_update(
                                index_elements=["researcher_id", "external_id"],
                                set_={
                                    "citation_count": stmt.excluded.citation_count,
                                    "authors": stmt.excluded.authors,
                                    "author_institutions": stmt.excluded.author_institutions,
                                },
                            )
                        )
                await session.commit()

            stats["ok"] += 1
            if total_citations > 0:
                stats["with_citations"] += 1
            if idx % 50 == 0:
                print(f"  {idx}/{len(people)} — 피인용>0 {stats['with_citations']:,}명")

    _save_ckpt(EXPAND_CKPT, ckpt)
    print(f"[expand] 완료 {stats['ok']:,} / 논문0건 {stats['empty']:,} / 조회실패 {stats['search_fail']}")
    print(f"[expand] 피인용>0 연구자 {stats['with_citations']:,}명")


# ─────────────────────────────── merge ───────────────────────────────

# 같은 인물이 소속 표기 차이로 갈라지는 경우가 실제로 많다(실측 118개 이름 / 257행):
#   한국석유관리원 · 한국석유관리원 미래기술연구소 · 한국석유관리원 석유기술연구소  (하위조직)
#   선문대학교 · Sunmoon University · "Department of ..., Sunmoon University, Asan ..."  (국·영문 표기)
# 반대로 '김지현 고려대 / 김지현 이화여대'는 진짜 동명이인이라 합치면 안 된다.
# 공저자 공유가 둘을 가르는 신호다 — 위 세 클러스터는 박태진·이경미·홍혜현을 함께 갖는다.
MERGE_MIN_SHARED_COAUTHORS = 2


def _has_hangul(value: str | None) -> bool:
    return bool(re.search(r"[가-힣]", value or ""))


# 영문 학회지는 소속을 주소째로 싣는다:
#   "Department of Forest Resources, Gyeongsang National University, 33 Dongjin-ro, ... Republic of Korea"
# 검색 결과 카드에 그대로 나가면 못 읽으므로, 콤마로 끊어 기관 단위 조각만 골라낸다.
# 꼬리를 잘라내는 방식은 "Jinju-si"·"Gyeongsangnam-do"처럼 숫자도 국가명도 아닌 조각에서 멈춰버린다.
_ORG_STRONG = re.compile(
    r"\b(?:University|Univ\.|Institute|Hospital|Agency|Administration|Corporation|"
    r"Inc\.?|Ltd\.?|Co\.|LLC|Company|Services)",  # 끝 \b를 빼야 "Angel Co."의 마침표 뒤에서 끊기지 않는다
    re.IGNORECASE,
)
_ORG_WEAK = re.compile(r"\b(?:College|School|Center|Centre|Academy|Laboratory)\b", re.IGNORECASE)
_CORPORATE_SUFFIX = re.compile(r"^(?:Ltd\.?|Inc\.?|LLC|Co\.)$", re.IGNORECASE)


def polish_institution(raw: str | None) -> str | None:
    if not raw:
        return None
    value = raw.strip()
    if _has_hangul(value) or "," not in value:
        return value or None

    segments = [seg.strip() for seg in value.split(",") if seg.strip()]
    if not segments:
        return value

    def pick(pattern) -> int | None:
        for i, seg in enumerate(segments):
            # 우편번호가 붙은 조각은 주소지, 기관명이 아니다.
            if re.search(r"\d{4,}", seg):
                continue
            if pattern.search(seg):
                return i
        return None

    index = pick(_ORG_STRONG)
    if index is None:
        index = pick(_ORG_WEAK)
    if index is None:
        return segments[0]

    chosen = segments[index]
    # "Angel Co., Ltd." 처럼 법인격이 다음 조각으로 넘어간 경우 다시 붙인다.
    if index + 1 < len(segments) and _CORPORATE_SUFFIX.match(segments[index + 1]):
        chosen = f"{chosen}, {segments[index + 1]}"
    return chosen or None


async def polish_institutions() -> None:
    """소속 표시 정리 — 주소 꼬리를 떼고, 한글 표기가 있으면 그쪽을 대표로 올린다."""
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT researcher_id, institution_current, institution_history "
                    "FROM researchers WHERE institution_current IS NOT NULL"
                )
            )
        ).all()
        changed = 0
        for row in rows:
            history = list(row.institution_history or [])
            # 한글 표기가 이력에 있으면 화면에는 그쪽이 낫다.
            korean = [h for h in history if _has_hangul(h)]
            best = min(korean, key=len) if korean else row.institution_current
            polished = polish_institution(best)
            if not polished or polished == row.institution_current:
                continue
            await session.execute(
                text(
                    "UPDATE researchers SET institution_current = :inst, institution_dept = :dept, "
                    "updated_at = now() WHERE researcher_id = :rid"
                ),
                {"inst": polished, "dept": institution_dept(polished), "rid": row.researcher_id},
            )
            changed += 1
        await session.commit()
    print(f"[merge] 소속 표시 정리 {changed:,}건")


async def merge_legacy_scienceon() -> None:
    """예전 ScienceON 배치가 남긴 sci: 행을 KCI 인물로 흡수한다.

    두 행은 같은 사람인데 출처가 달라 갈라져 있다(실측: 같은 논문에 같은 이름이
    두 번 달린 265건이 전부 이 조합). 예전 배치는 논문 CN 교집합을 확인해야만
    매칭을 인정했으므로, 그쪽 이메일은 confirmed 등급으로 옮겨온다.
    """
    async with AsyncSessionLocal() as session:
        pairs = (
            await session.execute(
                text(
                    "SELECT DISTINCT s.researcher_id AS sci_id, k.researcher_id AS kci_id "
                    "FROM researchers s "
                    "JOIN researcher_papers sp ON sp.researcher_id = s.researcher_id "
                    "JOIN researcher_papers kp ON kp.paper_id = sp.paper_id "
                    "JOIN researchers k ON k.researcher_id = kp.researcher_id "
                    "WHERE s.source = 'scienceon' AND k.source = 'kci' "
                    "AND k.author_name_kor = s.author_name_kor"
                )
            )
        ).all()
        if not pairs:
            print("[merge] ScienceON 레거시 병합 대상 없음")
            return

        # 한 sci 행이 여러 kci 인물에 걸리면(동명이인) 근거가 약하니 건너뛴다.
        targets: dict[str, list[str]] = defaultdict(list)
        for row in pairs:
            targets[row.sci_id].append(row.kci_id)
        merged = skipped = with_email = 0
        for sci_id, kci_ids in targets.items():
            if len(set(kci_ids)) != 1:
                skipped += 1
                continue
            kci_id = kci_ids[0]
            params = {"sci": sci_id, "kci": kci_id}
            result = await session.execute(
                text(
                    "UPDATE researchers k SET "
                    "  email = coalesce(k.email, s.email), "
                    "  scienceon_cn = coalesce(k.scienceon_cn, s.scienceon_cn), "
                    "  match_confidence = coalesce(k.match_confidence, "
                    "    CASE WHEN s.email IS NOT NULL THEN 'confirmed' END), "
                    "  patent_cnt = coalesce(k.patent_cnt, s.patent_cnt), "
                    "  report_cnt = coalesce(k.report_cnt, s.report_cnt), "
                    "  author_name_eng = coalesce(k.author_name_eng, s.author_name_eng), "
                    "  keywords = (ARRAY(SELECT DISTINCT unnest("
                    "    coalesce(k.keywords, '{}') || coalesce(s.keyword, '{}'))))[1:15], "
                    "  updated_at = now() "
                    "FROM researchers s WHERE s.researcher_id = :sci AND k.researcher_id = :kci "
                    "AND s.email IS NOT NULL RETURNING k.researcher_id"
                ),
                params,
            )
            if result.first():
                with_email += 1
            await session.execute(
                text(
                    "DELETE FROM researcher_papers WHERE researcher_id = :sci AND paper_id IN "
                    "(SELECT paper_id FROM researcher_papers WHERE researcher_id = :kci)"
                ),
                params,
            )
            await session.execute(
                text("UPDATE researcher_papers SET researcher_id = :kci WHERE researcher_id = :sci"),
                params,
            )
            await session.execute(
                text("DELETE FROM researchers WHERE researcher_id = :sci"), {"sci": sci_id}
            )
            await session.execute(
                text(
                    "UPDATE researchers c SET corpus_paper_count = "
                    "(SELECT count(*) FROM researcher_papers WHERE researcher_id = c.researcher_id) "
                    "WHERE c.researcher_id = :kci"
                ),
                params,
            )
            merged += 1
        await session.commit()
    print(f"[merge] ScienceON 레거시 흡수 {merged}명 (이메일 이관 {with_email}명 / 동명이인 모호 {skipped}명 보류)")


async def stage_merge() -> None:
    print("[merge] 소속 표기 차이로 갈라진 동일인 병합")
    async with AsyncSessionLocal() as session:
        people = (
            await session.execute(
                text(
                    "SELECT researcher_id, author_name_kor, institution_current, "
                    "corpus_paper_count, total_papers FROM researchers "
                    "WHERE source = 'kci' AND author_name_kor IS NOT NULL"
                )
            )
        ).all()
        coauthor_rows = (
            await session.execute(
                text(
                    "SELECT rp.researcher_id, co.author_name_kor AS coauthor "
                    "FROM researcher_papers rp "
                    "JOIN researcher_papers rp2 ON rp2.paper_id = rp.paper_id "
                    "AND rp2.researcher_id <> rp.researcher_id "
                    "JOIN researchers co ON co.researcher_id = rp2.researcher_id "
                    "WHERE co.author_name_kor IS NOT NULL"
                )
            )
        ).all()

    coauthors: dict[str, set[str]] = defaultdict(set)
    for row in coauthor_rows:
        coauthors[row.researcher_id].add(_norm(row.coauthor))

    by_name: dict[str, list] = defaultdict(list)
    for person in people:
        by_name[_norm(person.author_name_kor)].append(person)

    parent: dict[str, str] = {p.researcher_id: p.researcher_id for p in people}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    reasons = Counter()
    for group in by_name.values():
        if len(group) < 2:
            continue
        for i, left in enumerate(group):
            for right in group[i + 1 :]:
                lroot = _norm(institution_root(left.institution_current))
                rroot = _norm(institution_root(right.institution_current))
                if lroot and rroot and (lroot in rroot or rroot in lroot):
                    union(left.researcher_id, right.researcher_id)
                    reasons["소속 포함관계"] += 1
                    continue
                shared = coauthors[left.researcher_id] & coauthors[right.researcher_id]
                # 한쪽만 로마자 표기면 영문 학회지에 실린 같은 사람일 가능성이 높다
                # (인하공업전문대학 ↔ Inha Technical College) — 근거 문턱을 한 명으로 낮춘다.
                cross_script = _has_hangul(left.institution_current) != _has_hangul(right.institution_current)
                threshold = 1 if cross_script else MERGE_MIN_SHARED_COAUTHORS
                if len(shared) >= threshold:
                    union(left.researcher_id, right.researcher_id)
                    reasons["국영문 표기" if cross_script else "공저자 공유"] += 1

    clusters: dict[str, list] = defaultdict(list)
    for person in people:
        clusters[find(person.researcher_id)].append(person)
    clusters = {k: v for k, v in clusters.items() if len(v) > 1}
    if not clusters:
        print("[merge] 소속 표기 병합 대상 없음")
    print(f"[merge] {len(clusters)}개 인물, {sum(len(v) for v in clusters.values())}행 → 근거 {dict(reasons)}")

    async with AsyncSessionLocal() as session:
        for members in clusters.values():
            # 코퍼스 논문이 가장 많은 쪽을 대표로 — 우리 서비스에서 실체가 가장 확실한 행.
            canonical = max(members, key=lambda p: (p.corpus_paper_count or 0, p.total_papers or 0))
            absorbed = [p.researcher_id for p in members if p.researcher_id != canonical.researcher_id]
            if not absorbed:
                continue
            params = {"canon": canonical.researcher_id, "absorbed": absorbed}
            dedupe_params = params | {"all_ids": [canonical.researcher_id, *absorbed]}

            # 링크 이관. 흡수 대상끼리도 같은 논문을 들고 있을 수 있어서, 대표로 옮기기 전에
            # 클러스터 전체에서 논문당 한 행만 남긴다(대표 행 우선). 먼저 옮기고 충돌을
            # 피하는 방식은 UPDATE 중 생기는 행끼리 부딪혀 실패한다.
            for table, key in (("researcher_papers", "paper_id"), ("researcher_external_papers", "external_id")):
                await session.execute(
                    text(
                        f"DELETE FROM {table} WHERE ctid IN ("
                        f"  SELECT ctid FROM ("
                        f"    SELECT ctid, row_number() OVER ("
                        f"      PARTITION BY {key} ORDER BY (researcher_id = :canon) DESC, ctid) AS rn "
                        f"    FROM {table} WHERE researcher_id = ANY(:all_ids)) t "
                        f"  WHERE t.rn > 1)"
                    ),
                    dedupe_params,
                )
                await session.execute(
                    text(f"UPDATE {table} SET researcher_id = :canon WHERE researcher_id = ANY(:absorbed)"),
                    params,
                )
            # 소속 이력·키워드는 합집합으로 남긴다(이직/표기변화가 프로필에서 보여야 한다).
            await session.execute(
                text(
                    "UPDATE researchers c SET "
                    "  institution_history = ARRAY(SELECT DISTINCT unnest("
                    "    coalesce(c.institution_history, '{}') || coalesce(m.hist, '{}'))), "
                    "  keywords = (ARRAY(SELECT DISTINCT unnest("
                    "    coalesce(c.keywords, '{}') || coalesce(m.kw, '{}'))))[1:15], "
                    "  author_name_eng = coalesce(c.author_name_eng, m.eng), "
                    "  updated_at = now() "
                    "FROM (SELECT array_agg(DISTINCT i) FILTER (WHERE i IS NOT NULL) AS hist, "
                    "             array_agg(DISTINCT k) FILTER (WHERE k IS NOT NULL) AS kw, "
                    "             min(author_name_eng) AS eng "
                    "      FROM researchers r "
                    "      LEFT JOIN LATERAL unnest(coalesce(r.institution_history, '{}')) i ON true "
                    "      LEFT JOIN LATERAL unnest(coalesce(r.keywords, '{}')) k ON true "
                    "      WHERE r.researcher_id = ANY(:absorbed)) m "
                    "WHERE c.researcher_id = :canon"
                ),
                params,
            )
            await session.execute(
                text("DELETE FROM researchers WHERE researcher_id = ANY(:absorbed)"), {"absorbed": absorbed}
            )
            # 흡수 후 집계 재계산 — 논문/피인용이 갈라진 채로 남으면 프로필 숫자가 틀린다.
            await session.execute(
                text(
                    "UPDATE researchers c SET "
                    "  corpus_paper_count = (SELECT count(*) FROM researcher_papers WHERE researcher_id = c.researcher_id), "
                    "  total_papers = (SELECT count(*) FROM researcher_external_papers WHERE researcher_id = c.researcher_id), "
                    "  total_citations = (SELECT coalesce(sum(citation_count), 0) FROM researcher_external_papers WHERE researcher_id = c.researcher_id), "
                    "  first_pubyear = (SELECT min(pubyear) FROM researcher_external_papers WHERE researcher_id = c.researcher_id), "
                    "  last_pubyear = (SELECT max(pubyear) FROM researcher_external_papers WHERE researcher_id = c.researcher_id) "
                    "WHERE c.researcher_id = :canon"
                ),
                params,
            )
        await session.commit()
    await merge_legacy_scienceon()
    await polish_institutions()
    print("[merge] 완료")


# ─────────────────────────────── adjudicate ───────────────────────────────

# 규칙으로 못 가르는 쌍이 남는다: 같은 이름인데 한쪽 소속은 한글, 한쪽은 로마자이고
# 공저자가 하나도 안 겹치는 경우다. 사람이 보면 1초 컷인 게 섞여 있다 —
#   조혜준 / 제주대학교  ↔  조혜준 / Jeju National University        같은 곳
#   김지현 / 고려대학교  ↔  김지현 / Pusan National University       다른 곳
# 이건 기관명 대조표가 있어야 푸는 문제라 규칙으로는 한계가 있다. 반대로 LLM은 안다.
# 전수가 아니라 규칙이 포기한 십여 쌍만 넘기므로 호출 비용이 사실상 0이다.
ADJUDICATE_MODEL = "claude-sonnet-5"

_ADJUDICATE_PROMPT = """다음 두 연구자 기록이 같은 사람인지 판정해줘.

기록 A
- 이름: {name}
- 소속: {inst_a}
- 연구 키워드: {kw_a}
- 논문 수: {papers_a} / 활동 시기: {years_a}

기록 B
- 이름: {name}
- 소속: {inst_b}
- 연구 키워드: {kw_b}
- 논문 수: {papers_b} / 활동 시기: {years_b}

판정 기준:
- 두 소속이 **같은 기관을 다른 언어·표기로 적은 것**이면 같은 사람일 가능성이 높다.
- 기관이 개명·통합된 경우도 같은 기관으로 본다.
- 서로 다른 기관이면, 연구 분야가 비슷하다는 것만으로 같은 사람이라고 판정하지 마라.
  흔한 이름의 동명이인이 같은 분야에 있는 일은 흔하다.
- 확신이 없으면 unknown으로 답하라. 틀리게 합치는 쪽이 나누어 두는 쪽보다 나쁘다.

JSON만 출력하라. 다른 말은 붙이지 마라. reason은 40자 이내로 짧게 쓴다.
{{"verdict": "same" | "different" | "unknown", "confidence": 0.0~1.0, "reason": "40자 이내"}}"""


async def stage_adjudicate(dry_run: bool = False) -> None:
    """규칙이 포기한 동명이인 쌍을 LLM에 넘겨 판정한다."""
    from app.services.llm.client import LLMBudgetExceededError, chat

    print("[adjudicate] 규칙으로 못 가른 동명이인 쌍 판정")
    async with AsyncSessionLocal() as session:
        pairs = (
            await session.execute(
                text(
                    "SELECT a.researcher_id AS a_id, b.researcher_id AS b_id, a.author_name_kor AS name, "
                    "       a.institution_current AS a_inst, b.institution_current AS b_inst, "
                    "       a.keywords AS a_kw, b.keywords AS b_kw, "
                    "       a.total_papers AS a_papers, b.total_papers AS b_papers, "
                    "       a.first_pubyear AS a_from, a.last_pubyear AS a_to, "
                    "       b.first_pubyear AS b_from, b.last_pubyear AS b_to "
                    "FROM researchers a JOIN researchers b "
                    "  ON a.author_name_kor = b.author_name_kor AND a.researcher_id < b.researcher_id "
                    "WHERE a.institution_current ~ '[가-힣]' AND b.institution_current !~ '[가-힣]' "
                    "ORDER BY a.author_name_kor"
                )
            )
        ).all()
    print(f"[adjudicate] 대상 {len(pairs)}쌍")
    if not pairs:
        return

    def years(a, b):
        return f"{a}~{b}" if a and b else "정보 없음"

    ckpt = _load_ckpt(ADJUDICATE_CKPT)
    stats = Counter()
    merges: list[tuple[str, str]] = []

    for pair in pairs:
        key = f"{pair.a_id}|{pair.b_id}"
        verdict = ckpt.get(key)
        if verdict is None:
            prompt = _ADJUDICATE_PROMPT.format(
                name=pair.name,
                inst_a=pair.a_inst, inst_b=pair.b_inst,
                kw_a=", ".join((pair.a_kw or [])[:8]) or "없음",
                kw_b=", ".join((pair.b_kw or [])[:8]) or "없음",
                papers_a=pair.a_papers or 0, papers_b=pair.b_papers or 0,
                years_a=years(pair.a_from, pair.a_to), years_b=years(pair.b_from, pair.b_to),
            )
            try:
                response = await chat(
                    [{"role": "user", "content": prompt}],
                    model=ADJUDICATE_MODEL, max_tokens=1000,
                )
            except LLMBudgetExceededError as exc:
                print(f"[adjudicate] 예산 초과로 중단: {exc}")
                break
            except Exception as exc:  # noqa: BLE001
                print(f"    [오류] {pair.name}: {exc}")
                stats["호출 실패"] += 1
                continue
            raw = response.text.strip()
            match = re.search(r"\{.*\}", raw, re.S)
            try:
                verdict = json.loads(match.group(0)) if match else None
            except ValueError:
                verdict = None
            if verdict is None:
                # 응답이 잘리면 JSON이 닫히지 않는다. unknown으로 삼키면 판정을 잃으므로
                # 파싱 실패로 따로 세고 체크포인트에 남기지 않는다(다음 실행에서 재시도).
                stats["파싱 실패"] += 1
                print(f"    [파싱 실패] {pair.name}: {raw[:70]}")
                continue
            ckpt[key] = verdict
            _save_ckpt(ADJUDICATE_CKPT, ckpt)

        decision = verdict.get("verdict", "unknown")
        stats[decision] += 1
        marker = {"same": "합침", "different": "분리 유지", "unknown": "보류"}[decision]
        print(f"  [{marker}] {pair.name} — {str(pair.a_inst)[:22]} ↔ {str(pair.b_inst)[:26]}")
        print(f"           {verdict.get('reason', '')[:88]}")
        # 확신이 낮은 '같음'은 합치지 않는다 — 틀리게 합치면 남의 논문·이메일이 붙는다.
        if decision == "same" and float(verdict.get("confidence") or 0) >= 0.8:
            merges.append((pair.a_id, pair.b_id))

    print(f"\n[adjudicate] {dict(stats)} / 병합 예정 {len(merges)}쌍")
    if dry_run or not merges:
        if dry_run:
            print("[adjudicate] --dry-run: DB 반영 없음")
        return

    async with AsyncSessionLocal() as session:
        for a_id, b_id in merges:
            # 논문이 많은 쪽을 대표로 — 수집 결과가 풍부한 실체를 남긴다.
            canonical, absorbed = (
                await session.execute(
                    text(
                        "SELECT researcher_id FROM researchers WHERE researcher_id IN (:a, :b) "
                        "ORDER BY coalesce(total_papers, 0) DESC, coalesce(corpus_paper_count, 0) DESC"
                    ),
                    {"a": a_id, "b": b_id},
                )
            ).scalars().all()
            params = {"canon": canonical, "absorbed": [absorbed], "all_ids": [canonical, absorbed]}
            for table, key in (("researcher_papers", "paper_id"), ("researcher_external_papers", "external_id")):
                await session.execute(
                    text(
                        f"DELETE FROM {table} WHERE ctid IN ("
                        f"  SELECT ctid FROM (SELECT ctid, row_number() OVER ("
                        f"    PARTITION BY {key} ORDER BY (researcher_id = :canon) DESC, ctid) AS rn "
                        f"  FROM {table} WHERE researcher_id = ANY(:all_ids)) t WHERE t.rn > 1)"
                    ),
                    params,
                )
                await session.execute(
                    text(f"UPDATE {table} SET researcher_id = :canon WHERE researcher_id = ANY(:absorbed)"),
                    params,
                )
            await session.execute(
                text(
                    "UPDATE researchers c SET "
                    "  institution_history = ARRAY(SELECT DISTINCT unnest("
                    "    coalesce(c.institution_history,'{}') || coalesce(m.institution_history,'{}') "
                    "    || CASE WHEN m.institution_current IS NULL THEN '{}' ELSE ARRAY[m.institution_current] END)), "
                    "  email = coalesce(c.email, m.email), "
                    "  author_name_eng = coalesce(c.author_name_eng, m.author_name_eng), "
                    "  updated_at = now() "
                    "FROM researchers m WHERE m.researcher_id = :absorbed_one AND c.researcher_id = :canon"
                ),
                {"canon": canonical, "absorbed_one": absorbed},
            )
            await session.execute(text("DELETE FROM researchers WHERE researcher_id = :i"), {"i": absorbed})
            await session.execute(
                text(
                    "UPDATE researchers c SET "
                    "  corpus_paper_count = (SELECT count(*) FROM researcher_papers WHERE researcher_id = c.researcher_id), "
                    "  total_papers = nullif((SELECT count(*) FROM researcher_external_papers WHERE researcher_id = c.researcher_id), 0), "
                    "  total_citations = (SELECT coalesce(sum(citation_count),0) FROM researcher_external_papers WHERE researcher_id = c.researcher_id) "
                    "WHERE c.researcher_id = :canon"
                ),
                {"canon": canonical},
            )
        await session.commit()
    print(f"[adjudicate] {len(merges)}쌍 병합 완료")


# ─────────────────────────────── email ───────────────────────────────

async def stage_email(limit: int | None) -> None:
    """ScienceON 연구자 검색으로 이메일·대표키워드를 덧입힌다.

    남의 이메일을 프로필에 띄우는 값이라 근거 등급을 같이 저장한다:
      confirmed  이름+소속이 맞고, 그 연구자의 논문 CN이 우리 논문과 겹침
      inferred   이름+소속이 맞는 후보가 유일 (CN 교집합은 확인 못 함)
    """
    print("[email] ScienceON 이메일 오버레이")
    async with AsyncSessionLocal() as session:
        people = (
            await session.execute(
                select(
                    Researcher.researcher_id,
                    Researcher.author_name_kor,
                    Researcher.institution_current,
                ).where(Researcher.source == "kci", Researcher.email.is_(None))
                .order_by(Researcher.researcher_id)
            )
        ).all()
        corpus_cns = set()
        for row in (await session.execute(text("SELECT id, scienceon_cn FROM papers"))).all():
            corpus_cns.add(row.id)
            if row.scienceon_cn:
                corpus_cns.add(row.scienceon_cn)
        links = defaultdict(set)
        for row in (
            await session.execute(
                text(
                    "SELECT rp.researcher_id, p.id, p.scienceon_cn FROM researcher_papers rp "
                    "JOIN papers p ON p.id = rp.paper_id"
                )
            )
        ).all():
            links[row.researcher_id].add(row.id)
            if row.scienceon_cn:
                links[row.researcher_id].add(row.scienceon_cn)
    if limit:
        people = people[:limit]
    print(f"[email] 대상 연구자 {len(people):,}명")

    ckpt = _load_ckpt(EMAIL_CKPT)
    stats = Counter()
    sci = ScienceOnResearcherClient()

    for idx, person in enumerate(people, start=1):
        rid = person.researcher_id
        cached = ckpt.get(rid)
        if cached is None:
            root = institution_root(person.institution_current)
            await asyncio.sleep(SCI_PACING)
            xml = await _retry(
                lambda: sci.search_researchers(person.author_name_kor, search_field="TI", size=100),
                label=f"sci search {person.author_name_kor}",
            )
            if xml is None:
                stats["search_fail"] += 1
                continue
            candidates = [
                c for c in parse_researcher_search_xml(xml)
                if c.cn and _norm(c.author_name_kor) == _norm(person.author_name_kor)
            ]
            hits = [c for c in candidates if _inst_match(root, institution_root(c.author_inst_kor))]
            cached = {"status": "no_inst", "email": None}
            if hits:
                hits.sort(key=lambda c: -(c.article_cnt or 0))
                mine = links.get(rid, set())
                chosen, confidence = None, None
                for candidate in hits[:3]:
                    await asyncio.sleep(SCI_PACING)
                    detail = await _retry(
                        lambda c=candidate: sci.browse_researcher(c.cn), label=f"sci browse {candidate.cn}"
                    )
                    if detail is None:
                        continue
                    record, infos = parse_researcher_detail_xml(detail)
                    if record is None or not record.cn:
                        continue
                    if _scienceon_paper_cns(infos) & mine:
                        chosen, confidence = record, "confirmed"
                        break
                    if chosen is None:
                        chosen = record
                if chosen is not None:
                    if confidence is None:
                        confidence = "inferred" if len(hits) == 1 else None
                    cached = {
                        "status": "matched" if confidence else "ambiguous",
                        "cn": chosen.cn,
                        "email": chosen.email if confidence else None,
                        "confidence": confidence,
                        "keyword": chosen.keyword,
                        "article_cnt": chosen.article_cnt,
                        "patent_cnt": chosen.patent_cnt,
                        "report_cnt": chosen.report_cnt,
                    }
            ckpt[rid] = cached
            if idx % 25 == 0:
                _save_ckpt(EMAIL_CKPT, ckpt)

        stats[cached["status"]] += 1
        if cached.get("confidence"):
            stats[f"conf_{cached['confidence']}"] += 1
            async with AsyncSessionLocal() as session:
                await session.execute(
                    sa.update(Researcher).where(Researcher.researcher_id == rid).values(
                        scienceon_cn=cached.get("cn"),
                        email=cached.get("email"),
                        match_confidence=cached["confidence"],
                        patent_cnt=cached.get("patent_cnt"),
                        report_cnt=cached.get("report_cnt"),
                        updated_at=sa.func.now(),
                    )
                )
                await session.commit()
            if cached.get("email"):
                stats["email"] += 1
        if idx % 50 == 0:
            print(f"  {idx}/{len(people)} — 이메일 {stats['email']:,}건")

    _save_ckpt(EMAIL_CKPT, ckpt)
    print(f"[email] 매칭 {stats['matched']:,} / 소속불일치 {stats['no_inst']:,} / 동명이인모호 {stats['ambiguous']:,}")
    print(f"[email] 이메일 확보 {stats['email']:,}건 (confirmed {stats['conf_confirmed']:,} / inferred {stats['conf_inferred']:,})")


def _scienceon_paper_cns(infos) -> set[str]:
    for info in infos:
        if info.provider_api_id != "API-001-01":
            continue
        raw = info.parameter_values.get("searchQuery")
        if not raw:
            return set()
        try:
            return {c.strip() for c in json.loads(raw).get("CN", "").split("|") if c.strip()}
        except (ValueError, TypeError):
            return set()
    return set()


# ─────────────────────────────── keywords ───────────────────────────────

KEYWORD_CONCURRENCY = 4  # articleDetail 실측 0.075초/건(동시성 4). 8은 429 위험이 있어 4로 둔다.
KEYWORD_MAX_PER_PAPER = 20


async def stage_keywords(limit: int | None, *, refresh: bool = False) -> None:
    """외부 논문의 키워드를 채운다 — 「연구 흐름 시각화」의 실제 입력.

    articleSearch는 키워드를 주지 않고 articleDetail만 준다. 같은 논문이 여러 연구자에게
    중복 저장돼 있으므로(31,599행 / 고유 25,286편) 논문 단위로 한 번만 호출한다.
    """
    print("[keywords] 외부 논문 키워드 수집")
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT DISTINCT external_id FROM researcher_external_papers "
                    "WHERE external_source = 'kci' AND external_id LIKE 'ART%' "
                    + ("" if refresh else "AND (keywords IS NULL OR fwci IS NULL) ")
                    + "ORDER BY external_id"
                )
            )
        ).scalars().all()
    if limit:
        rows = rows[:limit]
    print(f"[keywords] 대상 논문 {len(rows):,}편 (고유 기준)")
    if not rows:
        return

    ckpt = _load_ckpt(KEYWORD_CKPT)
    stats = Counter()
    semaphore = asyncio.Semaphore(KEYWORD_CONCURRENCY)
    pending: dict[str, list[str]] = {}
    lock = asyncio.Lock()

    async def flush() -> None:
        """모아둔 결과를 논문 단위로 반영 — 같은 art_id를 가진 모든 연구자 행이 함께 채워진다."""
        if not pending:
            return
        async with AsyncSessionLocal() as session:
            for art_id, detail in pending.items():
                await session.execute(
                    text(
                        "UPDATE researcher_external_papers SET keywords = :kw, fwci = :fwci, "
                        "  language = :lang, regularity = :reg, kci_registration = :kci_reg "
                        "WHERE external_source = 'kci' AND external_id = :aid"
                    ),
                    {
                        "kw": detail.get("kw") or None,
                        "fwci": detail.get("fwci"),
                        "lang": detail.get("lang"),
                        "reg": detail.get("reg"),
                        "kci_reg": detail.get("kci_reg"),
                        "aid": art_id,
                    },
                )
            await session.commit()
        pending.clear()

    async with httpx.AsyncClient(timeout=30.0) as http:
        client = KCIResearcherClient(http)

        async def one(art_id: str, idx: int) -> None:
            cached = ckpt.get(art_id)
            # 키워드만 담던 옛 형식(list)은 서지 지표가 없다 — 다시 받아야 한다.
            if isinstance(cached, list):
                cached = None
            if cached is None:
                async with semaphore:
                    await asyncio.sleep(0.05)
                    article = await _retry(
                        lambda: client.article_detail(art_id), label=f"keywords {art_id}"
                    )
                if article is None:
                    stats["fail"] += 1
                    return
                # 파서가 걸러도 남는 예외값이 있을 수 있다 — 컬럼 길이(200)를 넘겨
                # 배치 전체가 죽는 일이 없도록 여기서 한 번 더 자른다.
                cached = {
                    "kw": [k[:200] for k in article.keywords[:KEYWORD_MAX_PER_PAPER]],
                    "fwci": article.fwci,
                    "lang": article.language,
                    "reg": article.regularity,
                    "kci_reg": article.kci_registration,
                }
                ckpt[art_id] = cached
            if cached.get("kw"):
                stats["with_keywords"] += 1
            else:
                stats["empty"] += 1
            if cached.get("fwci") is not None:
                stats["with_fwci"] += 1
            async with lock:
                pending[art_id] = cached
                if len(pending) >= 300:
                    await flush()
                    _save_ckpt(KEYWORD_CKPT, ckpt)
                    print(f"  {idx}/{len(rows)} — 키워드 보유 {stats['with_keywords']:,}편")

        for start in range(0, len(rows), 300):
            chunk = rows[start : start + 300]
            await asyncio.gather(*(one(a, start + i + 1) for i, a in enumerate(chunk)))
        await flush()

    _save_ckpt(KEYWORD_CKPT, ckpt)
    print(f"[keywords] 키워드 보유 {stats['with_keywords']:,}편 / 없음 {stats['empty']:,}편 / 실패 {stats['fail']:,}편")
    print(f"[keywords] fwci 보유 {stats['with_fwci']:,}편")


# ─────────────────────────────── verify_email ───────────────────────────────

# 개인 메일은 소속 증거가 되지 못한다.
_PERSONAL_MAIL = re.compile(
    r"(gmail|naver|hanmail|daum|hotmail|nate|yahoo|outlook|kakao|empal|chol\.com|korea\.com|"
    r"dreamwiz|paran|freechal|lycos|hanmir|netian)", re.IGNORECASE
)
# 여러 기관이 함께 쓰는 도메인(korea.kr 등)은 어느 기관인지 특정하지 못한다.
GENERIC_DOMAIN_MIN_INSTITUTIONS = 3


def _email_domain(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    return email.rsplit("@", 1)[-1].strip().lower() or None


def _institution_verdict(a: str | None, b: str | None) -> str:
    """두 소속이 같은 기관인가. 'match' | 'conflict' | 'unknown'."""
    ra, rb = institution_root(a), institution_root(b)
    if not ra or not rb:
        return "unknown"
    na, nb = _norm(ra), _norm(rb)
    if na == nb or na in nb or nb in na:
        return "match"
    # 한쪽만 로마자면(경북대학교 vs Kyungpook National University) 대조표만으로는 판정할 수 없다.
    # 여기서 conflict로 단정하면 멀쩡한 이메일을 지우게 된다.
    if _has_hangul(ra) != _has_hangul(rb):
        return "unknown"
    return "conflict"


async def stage_verify_email() -> None:
    """이메일 도메인으로 소속을 교차검증한다.

    `inferred`는 이름+소속이 유일하게 맞았을 뿐 논문 대조를 못 한 추정이다. 그런데 이메일
    도메인 자체가 독립적인 소속 증거다(jmleekr@kpetro.or.kr ↔ 한국석유관리원). 논문 대조까지
    끝난 `confirmed` 건에서 도메인→기관 대조표를 만들어 추정 건을 검증한다.

    확인된 건을 절반씩 나눠 교차검증한 결과 커버리지 49% / 정확도 96.3%였고,
    틀린 3건은 전부 같은 대학의 다른 학과·국영문 표기여서 실질 정확도는 그보다 높다.
    """
    print("[verify] 이메일 도메인으로 소속 교차검증")
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT researcher_id, author_name_kor, institution_current, email, match_confidence "
                    "FROM researchers WHERE email IS NOT NULL AND institution_current IS NOT NULL"
                )
            )
        ).all()

    # 1) 논문 대조가 끝난 건으로 도메인 → 기관 대조표를 만든다.
    domain_to_institutions: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        if row.match_confidence != "confirmed":
            continue
        domain = _email_domain(row.email)
        if domain and not _PERSONAL_MAIL.search(domain):
            domain_to_institutions[domain].add(row.institution_current)

    generic = {
        d for d, insts in domain_to_institutions.items()
        if len({institution_root(i) for i in insts}) >= GENERIC_DOMAIN_MIN_INSTITUTIONS
    }
    usable = {d: v for d, v in domain_to_institutions.items() if d not in generic}
    print(f"[verify] 대조표 {len(usable):,}개 도메인 (범용 제외 {len(generic)}개: {sorted(generic)[:5]})")

    # 2) 추정 건을 검증한다.
    stats = Counter()
    upgrades: list[str] = []
    disputes: list[tuple] = []
    for row in rows:
        if row.match_confidence != "inferred":
            continue
        domain = _email_domain(row.email)
        if not domain or _PERSONAL_MAIL.search(domain):
            stats["개인메일"] += 1
            continue
        candidates = usable.get(domain)
        if not candidates:
            stats["대조표에 없음"] += 1
            continue
        verdicts = {_institution_verdict(row.institution_current, c) for c in candidates}
        if "match" in verdicts:
            stats["도메인 일치 → 승격"] += 1
            upgrades.append(row.researcher_id)
        elif verdicts == {"conflict"}:
            stats["도메인 불일치 → 이메일 회수"] += 1
            disputes.append((row.researcher_id, row.author_name_kor, row.institution_current, row.email, sorted(candidates)[:1]))
        else:
            stats["판정 보류"] += 1

    # 3) 확인된 건도 훑어 모순을 찾는다(논문 대조가 더 강한 증거라 회수하진 않고 보고만 한다).
    confirmed_conflicts = []
    for row in rows:
        if row.match_confidence != "confirmed":
            continue
        domain = _email_domain(row.email)
        candidates = usable.get(domain) if domain and not _PERSONAL_MAIL.search(domain) else None
        if not candidates:
            continue
        if {_institution_verdict(row.institution_current, c) for c in candidates} == {"conflict"}:
            confirmed_conflicts.append((row.author_name_kor, row.institution_current, row.email))

    async with AsyncSessionLocal() as session:
        if upgrades:
            await session.execute(
                text(
                    "UPDATE researchers SET match_confidence = 'domain_verified', updated_at = now() "
                    "WHERE researcher_id = ANY(:ids)"
                ),
                {"ids": upgrades},
            )
        if disputes:
            # 소속과 모순되는 이메일은 남의 것일 수 있다 — 화면에 띄우느니 지운다.
            await session.execute(
                text(
                    "UPDATE researchers SET email = NULL, match_confidence = 'disputed', updated_at = now() "
                    "WHERE researcher_id = ANY(:ids)"
                ),
                {"ids": [d[0] for d in disputes]},
            )
        await session.commit()

    for key, value in stats.items():
        print(f"  {key:24}: {value:,}")
    if disputes:
        print("\n  [회수] 소속과 모순된 이메일:")
        for _rid, name, inst, email, seen in disputes:
            print(f"    {name} / {inst} / {email} — 그 도메인은 {seen[0]}의 것")
    if confirmed_conflicts:
        print(f"\n  [주의] 논문 대조는 됐지만 도메인이 어긋나는 건 {len(confirmed_conflicts)}건 (회수하지 않음):")
        for name, inst, email in confirmed_conflicts[:5]:
            print(f"    {name} / {inst} / {email}")
    print(f"\n[verify] 승격 {len(upgrades):,}건 / 회수 {len(disputes):,}건")


# ─────────────────────────────── report ───────────────────────────────

async def report() -> None:
    async with AsyncSessionLocal() as session:
        async def scalar(sql: str):
            return (await session.execute(text(sql))).scalar()

        print("\n=== 연구자 적재 검증 리포트 ===")
        total = await scalar("SELECT count(*) FROM researchers")
        print(f"연구자 총원                : {total:,}")
        for src in ("kci", "openalex", "scienceon"):
            n = await scalar(f"SELECT count(*) FROM researchers WHERE source = '{src}'")
            print(f"  - {src:10}            : {n:,}")
        print(f"소속 보유                  : {await scalar('SELECT count(*) FROM researchers WHERE institution_current IS NOT NULL'):,}")
        print(f"대표 키워드 보유           : {await scalar('SELECT count(*) FROM researchers WHERE keywords IS NOT NULL'):,}")
        print(f"이메일 보유                : {await scalar('SELECT count(*) FROM researchers WHERE email IS NOT NULL'):,}")
        # 이메일 근거 등급 — 남의 이메일을 프로필에 띄우는 값이라 근거별로 나눠 본다.
        grades = (await session.execute(text(
            "SELECT match_confidence AS g, count(*) AS n, count(email) AS e "
            "FROM researchers WHERE match_confidence IS NOT NULL GROUP BY 1 ORDER BY 2 DESC"
        ))).all()
        labels = {
            "confirmed": "논문 대조 확인",
            "domain_verified": "이메일 도메인 교차검증",
            "inferred": "이름+소속 유일 일치(추정)",
            "disputed": "소속과 모순 → 이메일 회수",
        }
        for row in grades:
            print(f"  {row.g:16}: {row.n:,}명 (이메일 {row.e:,}) — {labels.get(row.g, '')}")
        print(f"총 피인용 > 0              : {await scalar('SELECT count(*) FROM researchers WHERE total_citations > 0'):,}")
        print(f"논문수 집계 완료           : {await scalar('SELECT count(*) FROM researchers WHERE total_papers IS NOT NULL'):,}")
        print(f"논문 목록 절단(흔한 이름)  : {await scalar('SELECT count(*) FROM researchers WHERE papers_truncated'):,}")
        print(f"코퍼스 논문-저자 링크      : {await scalar('SELECT count(*) FROM researcher_papers'):,}")
        print(f"  역할 채워짐              : {await scalar('SELECT count(*) FROM researcher_papers WHERE role IS NOT NULL'):,}")
        print(f"외부 논문                  : {await scalar('SELECT count(*) FROM researcher_external_papers'):,}")
        print(f"  저자 채워짐              : {await scalar('SELECT count(*) FROM researcher_external_papers WHERE authors IS NOT NULL'):,}")
        print(f"  키워드 채워짐            : {await scalar('SELECT count(*) FROM researcher_external_papers WHERE keywords IS NOT NULL'):,}")
        # KCI(국내 등재지)와 OpenAlex(국제) 피인용은 스케일이 30배 이상 달라 섞어 보면 안 된다.
        rows = (await session.execute(text(
            "SELECT citation_source, count(*) n, "
            "  percentile_cont(0.5) WITHIN GROUP (ORDER BY total_citations) med, max(total_citations) mx "
            "FROM researchers WHERE total_citations > 0 GROUP BY 1 ORDER BY 1"
        ))).all()
        for row in rows:
            print(f"피인용 분포 [{row.citation_source:9}] : {row.n:,}명 · 중앙값 {row.med:.0f} · 최대 {row.mx:,}")
        print(f"논문당 평균 연결 저자      : {await scalar('SELECT round(count(*)::numeric / nullif(count(distinct paper_id),0), 2) FROM researcher_papers')}")


async def run(stage: str, limit: int | None, *, refresh: bool = False, dry_run: bool = False) -> None:
    if stage in ("anchor", "all"):
        await stage_anchor(limit)
    if stage in ("thesis", "all"):
        await stage_thesis(limit)
    if stage in ("expand", "all"):
        await stage_expand(limit, refresh=refresh)
    if stage in ("merge", "all"):
        await stage_merge()
    if stage in ("keywords", "all"):
        await stage_keywords(limit, refresh=refresh)
    if stage in ("email", "all"):
        await stage_email(limit)
    if stage in ("adjudicate", "all"):
        await stage_adjudicate(dry_run=dry_run)
    if stage in ("verify-email", "all"):
        await stage_verify_email()
    await report()


def main() -> None:
    parser = argparse.ArgumentParser(description="연구자 데이터 적재 (KCI 백본 + ScienceON 이메일)")
    parser.add_argument("--stage", choices=["anchor", "thesis", "expand", "merge", "keywords", "adjudicate", "email", "verify-email", "all"], default="all")
    parser.add_argument("--limit", type=int, default=None, help="처리 건수 제한 (시범 적재용)")
    parser.add_argument("--report", action="store_true", help="적재하지 않고 검증 리포트만 출력")
    parser.add_argument("--refresh", action="store_true", help="체크포인트를 무시하고 다시 수집")
    parser.add_argument("--dry-run", action="store_true", help="adjudicate: 판정만 하고 DB는 건드리지 않음")
    args = parser.parse_args()

    if args.report:
        asyncio.run(report())
        return
    asyncio.run(run(args.stage, args.limit, refresh=args.refresh, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
