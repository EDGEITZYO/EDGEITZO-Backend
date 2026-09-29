"""KCI 저자 번호(CRT…) 수집 — 공공데이터포털 KCI 논문정보서비스 (마이그레이션 038).

하루 호출 한도(개발 계정 5,000건)가 있어 여러 날에 나눠 받는다. 받은 것은
kci_author_fetch_log에 남기므로 다시 돌려도 이어서 받는다.

수집 순서 (범위 안에서):
  1. 저자 정보 조회(이름) — 대상 연구자 이름마다. 그 이름의 저자 번호 후보가 된다
  2. KCI논문저자 조회(논문) — 대상 연구자 논문마다. 논문별 저자 번호가 된다
  한 논문의 저자 번호 중 1번 후보에 든 것이 그 연구자다.

범위:
  --scope plan      남은 동명이인 문제만 푸는 논문 목록(plan_kci_author_lookup.py가 만든다) — 기본값
  --scope suspects  같은 기관 동명이인 의심 연구자(data/checkpoints/namesake_suspects.json) — 1단계
  --scope all       연구자 전원

사용법:
  python scripts/plan_kci_author_lookup.py                                    # 조회할 논문 고르기
  python scripts/collect_kci_author_ids.py                                    # 오늘 한도까지
  python scripts/collect_kci_author_ids.py --scope suspects --until-done      # 한도에 걸리면 자정까지 기다렸다 계속
  python scripts/collect_kci_author_ids.py --status                           # 진행 상황만
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.load_researchers import CHECKPOINT_DIR  # noqa: E402 — .env를 먼저 읽는 모듈

import httpx  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.core.database import AsyncSessionLocal  # noqa: E402
from app.integrations.kci.data_go_kr_client import (  # noqa: E402
    DataGoKrError,
    KciDataGoKrClient,
    QuotaExceeded,
)

KST = timezone(timedelta(hours=9))
DAILY_LIMIT = 5000
# 스스로 멈추는 선. 한도를 딱 채우면 다른 용도로 쓸 여유가 없다.
DAILY_BUDGET = 4800
# 응답이 20~40초라 동시에 보낸다. 16개·1,000건씩 보냈더니 게이트웨이가 504를 냈다(2026-09-29) —
# 실패한 재시도도 하루 한도를 깎으므로 줄였다.
CONCURRENCY = 6
NAME_PAGE_SIZE = 300
MAX_RETRIES = 3
USAGE_FILE = CHECKPOINT_DIR / "kci_data_go_kr_usage.json"
SUSPECTS_FILE = CHECKPOINT_DIR / "namesake_suspects.json"
PLAN_FILE = CHECKPOINT_DIR / "kci_lookup_plan.json"


def _today() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


class Budget:
    """오늘 쓴 호출 수. 파일에 남겨 스크립트를 다시 켜도 이어서 센다."""

    def __init__(self) -> None:
        self.usage = json.loads(USAGE_FILE.read_text()) if USAGE_FILE.exists() else {}

    @property
    def used(self) -> int:
        return self.usage.get(_today(), 0)

    def take(self) -> bool:
        if self.used >= DAILY_BUDGET:
            return False
        self.usage[_today()] = self.used + 1
        return True

    def exhaust(self) -> None:
        """API가 한도 초과라고 답하면 오늘은 끝이다."""
        self.usage[_today()] = max(self.used, DAILY_BUDGET)

    def save(self) -> None:
        USAGE_FILE.write_text(json.dumps(self.usage, ensure_ascii=False, indent=1))


async def targets(scope: str) -> tuple[list[str], list[str]]:
    """(조회할 이름들, 조회할 논문 ID들) — 이미 받은 것은 뺀다."""
    if scope == "plan":
        # 남은 동명이인 문제만 푸는 목록(plan_kci_author_lookup.py). 이름 조회는 하지 않는다 —
        # 한 연구자의 여러 논문에 공통으로 나오는 번호가 그 사람이라 이름별 후보가 필요 없다.
        planned = json.loads(PLAN_FILE.read_text())["articles"]
        async with AsyncSessionLocal() as s:
            done = set((await s.execute(text(
                "SELECT key FROM kci_author_fetch_log WHERE kind = 'article'"))).scalars().all())
        return [], [a for a in planned if a not in done]
    async with AsyncSessionLocal() as s:
        if scope == "suspects":
            ids = [x["researcher_id"] for x in json.loads(SUSPECTS_FILE.read_text())]
            where, params = "r.researcher_id = ANY(:ids)", {"ids": ids}
        else:
            where, params = "TRUE", {}
        names = (await s.execute(text(
            f"SELECT DISTINCT r.author_name_kor FROM researchers r WHERE {where} AND r.author_name_kor IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM kci_author_fetch_log l WHERE l.kind = 'name' AND l.key = r.author_name_kor) "
            "ORDER BY 1"), params)).scalars().all()
        arts = (await s.execute(text(
            f"SELECT DISTINCT e.external_id FROM researcher_external_papers e JOIN researchers r USING (researcher_id) "
            f"WHERE {where} AND e.external_id LIKE 'ART%' "
            "AND NOT EXISTS (SELECT 1 FROM kci_author_fetch_log l WHERE l.kind = 'article' AND l.key = e.external_id) "
            "ORDER BY 1"), params)).scalars().all()
    return list(names), list(arts)


async def _call(budget: Budget, factory):
    """한도 안에서 한 번 부른다. 한도에 걸리면 QuotaExceeded, 끝내 실패하면 None."""
    for attempt in range(MAX_RETRIES):
        if not budget.take():
            raise QuotaExceeded("스스로 정한 하루 예산 소진")
        try:
            return await factory()
        except QuotaExceeded:
            budget.exhaust()
            raise
        except DataGoKrError as exc:
            if attempt == MAX_RETRIES - 1:
                print(f"    [실패] {exc}", flush=True)
                return None
            await asyncio.sleep(2 ** attempt)
    return None


async def fetch_name(client, budget: Budget, name: str) -> None:
    first = await _call(budget, lambda: client.authors_by_name(name, size=NAME_PAGE_SIZE))
    if first is None:
        return
    items = list(first.items)
    for page in range(2, math.ceil(first.total / NAME_PAGE_SIZE) + 1):
        more = await _call(budget, lambda p=page: client.authors_by_name(name, page=p, size=NAME_PAGE_SIZE))
        if more is None:
            return  # 일부만 받은 이름은 기록하지 않아 다음에 처음부터 다시 받는다
        items += more.items
    async with AsyncSessionLocal() as s:
        for it in items:
            if not it.get("CRET_ID"):
                continue
            await s.execute(text(
                "INSERT INTO kci_authors (cret_id, kri_id, kor_nm, eng_nm, belo_insi_id, belo_insi_nm) "
                "VALUES (:c, :k, :n, :e, :bi, :bn) ON CONFLICT (cret_id) DO UPDATE SET "
                "kri_id = EXCLUDED.kri_id, kor_nm = EXCLUDED.kor_nm, eng_nm = EXCLUDED.eng_nm, "
                "belo_insi_id = EXCLUDED.belo_insi_id, belo_insi_nm = EXCLUDED.belo_insi_nm, fetched_at = now()"),
                {"c": it["CRET_ID"], "k": it.get("KRI_ID") or None, "n": it.get("CRET_KOR_NM") or None,
                 "e": it.get("CRET_ENG_NM") or None, "bi": it.get("BELO_INSI_ID") or None,
                 "bn": it.get("BELO_INSI_NM") or None})
        await s.execute(text(
            "INSERT INTO kci_author_fetch_log (kind, key, result_count) VALUES ('name', :k, :n) "
            "ON CONFLICT (kind, key) DO UPDATE SET result_count = EXCLUDED.result_count, fetched_at = now()"),
            {"k": name, "n": len(items)})
        await s.commit()


async def fetch_article(client, budget: Budget, arti_id: str) -> None:
    first = await _call(budget, lambda: client.article_authors(arti_id))
    if first is None:
        return
    items = list(first.items)
    for page in range(2, math.ceil(first.total / 100) + 1):
        more = await _call(budget, lambda p=page: client.article_authors(arti_id, page=p))
        if more is None:
            return
        items += more.items
    async with AsyncSessionLocal() as s:
        for it in items:
            if not it.get("ARTICRETID"):
                continue
            await s.execute(text(
                "INSERT INTO kci_article_authors (arti_cret_id, arti_id, cret_id, cret_div_cd, belo_insi_id, "
                "  belo_insi_nm, kri_part_div_cd, orcid) VALUES (:ac, :a, :c, :d, :bi, :bn, :k, :o) "
                "ON CONFLICT (arti_cret_id) DO UPDATE SET cret_id = EXCLUDED.cret_id, belo_insi_id = EXCLUDED.belo_insi_id, "
                "belo_insi_nm = EXCLUDED.belo_insi_nm, orcid = EXCLUDED.orcid, fetched_at = now()"),
                {"ac": it["ARTICRETID"], "a": it.get("ARTIID") or arti_id, "c": it.get("CRETID") or None,
                 "d": it.get("CRETDIVCD") or None, "bi": it.get("BELOINSIID") or None,
                 "bn": it.get("BELOINSINM") or None, "k": it.get("KRIPARTDIVCD") or None,
                 "o": it.get("ORCID") or None})
        await s.execute(text(
            "INSERT INTO kci_author_fetch_log (kind, key, result_count) VALUES ('article', :k, :n) "
            "ON CONFLICT (kind, key) DO UPDATE SET result_count = EXCLUDED.result_count, fetched_at = now()"),
            {"k": arti_id, "n": len(items)})
        await s.commit()


async def run_once(scope: str) -> bool:
    """오늘 한도까지 받는다. 전부 받았으면 True."""
    names, arts = await targets(scope)
    budget = Budget()
    print(f"[{datetime.now(KST):%m-%d %H:%M}] 남은 이름 {len(names):,} / 논문 {len(arts):,} "
          f"· 오늘 사용 {budget.used:,}/{DAILY_BUDGET:,}", flush=True)
    if not names and not arts:
        return True
    sem = asyncio.Semaphore(CONCURRENCY)
    stop = asyncio.Event()
    done = 0

    async def worker(coro_factory):
        nonlocal done
        if stop.is_set():
            return
        async with sem:
            if stop.is_set():
                return
            try:
                await coro_factory()
            except QuotaExceeded as exc:
                if not stop.is_set():
                    print(f"  한도 도달 — {exc}", flush=True)
                stop.set()
            done += 1
            if done % 200 == 0:
                budget.save()
                print(f"  {done:,}건 처리 · 오늘 사용 {budget.used:,}", flush=True)

    async with httpx.AsyncClient(timeout=120.0) as http:
        client = KciDataGoKrClient(http)
        # 이름을 먼저 — 논문별 저자 번호를 누구 것인지 가리려면 이름별 후보가 있어야 한다
        await asyncio.gather(*(worker(lambda n=n: fetch_name(client, budget, n)) for n in names))
        if not stop.is_set():
            await asyncio.gather(*(worker(lambda a=a: fetch_article(client, budget, a)) for a in arts))
    budget.save()
    names, arts = await targets(scope)
    print(f"[{datetime.now(KST):%m-%d %H:%M}] 오늘 끝 — 남은 이름 {len(names):,} / 논문 {len(arts):,}", flush=True)
    return not names and not arts


async def status(scope: str) -> None:
    names, arts = await targets(scope)
    async with AsyncSessionLocal() as s:
        row = (await s.execute(text(
            "SELECT (SELECT count(*) FROM kci_author_fetch_log WHERE kind='name') n, "
            "(SELECT count(*) FROM kci_author_fetch_log WHERE kind='article') a, "
            "(SELECT count(*) FROM kci_article_authors) aa, (SELECT count(*) FROM kci_authors) au"))).one()
    print(f"받은 이름 {row.n:,} / 받은 논문 {row.a:,} / 논문저자 행 {row.aa:,} / 저자 {row.au:,}")
    print(f"[{scope}] 남은 이름 {len(names):,} / 논문 {len(arts):,} · 오늘 사용 {Budget().used:,}/{DAILY_BUDGET:,}")


async def main(scope: str, until_done: bool) -> None:
    while True:
        finished = await run_once(scope)
        if finished or not until_done:
            return
        now = datetime.now(KST)
        wake = (now + timedelta(days=1)).replace(hour=0, minute=10, second=0, microsecond=0)
        print(f"  다음 수집 {wake:%m-%d %H:%M} (KST)", flush=True)
        await asyncio.sleep((wake - now).total_seconds())


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--scope", choices=["plan", "suspects", "all"], default="plan")
    p.add_argument("--until-done", action="store_true")
    p.add_argument("--status", action="store_true")
    a = p.parse_args()
    asyncio.run(status(a.scope) if a.status else main(a.scope, a.until_done))
