"""신규 50건(KCI 소스, papers.id가 KCI art_id라 ScienceON CN을 모름) 논문에 대해
ScienceON article search(target=ARTI, 제목검색)로 ScienceON CN을 역으로 찾는다.

배경: scripts/ingest_researchers.py의 연구자 매칭은 "이 연구자가 쓴 논문 CN 목록"을
ScienceON 자체 CN 형식(JAKO/NART/DIKO...)으로만 돌려받고, 그걸 papers.id와 교집합 검사한다.
신규 50건의 papers.id는 KCI art_id라서 ScienceON이 절대 알 수 없는 값 — 그래서 지금까지 매칭이
전혀 안 됐다. 제목으로 ScienceON을 검색해서 실제로 같은 논문이 ScienceON 코퍼스에도 있으면 그
CN을 알아내고, 이후 연구자 매칭 시 papers.id 대신 이 CN으로 교집합을 검사하도록 우회한다.

한계: ScienceON 코퍼스에 애초에 없는 논문(KCI 전용으로만 등재된 논문)은 제목 검색으로도 못 찾는다
— 이 경우 그 논문의 저자는 이번 방식으로도 연구자 매칭이 안 된다(자연 탈락).

매칭 기준: 제목 완전일치(정규화 후)만 채택 — 오매칭 방지를 위해 부분일치/유사도 매칭은 하지 않음.

체크포인트: data/checkpoints/scienceon_cn_for_new_papers.json ({kci_id: scienceon_cn})

사용법:
  python scripts/resolve_scienceon_cn_for_new_papers.py --dry-run
  python scripts/resolve_scienceon_cn_for_new_papers.py
"""
from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import re
import sys
from pathlib import Path

import xmltodict

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
        os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from app.integrations.scienceon.client import ScienceOnClient

CHECKPOINT_PATH = PROJECT_ROOT / "data" / "checkpoints" / "scienceon_cn_for_new_papers.json"
RATE_LIMIT_DELAY = 0.3


def _normalize_title(title: str | None) -> str:
    if not title:
        return ""
    t = html.unescape(title)
    t = re.sub(r"<[^>]+>", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t.lower()


def _as_list(node):
    if node is None:
        return []
    return node if isinstance(node, list) else [node]


def _item_text(record: dict, code: str) -> str | None:
    items = _as_list(record.get("item"))
    for it in items:
        if isinstance(it, dict) and it.get("@metaCode") == code:
            return it.get("#text")
    return None


async def _load_target_papers() -> list[tuple[str, str]]:
    # kci_art_id = id : KCI art_id가 곧 papers.id인 논문(=KCI에서만 채워진, ScienceON CN을
    # 모르는 신규 50건)을 source 값과 무관하게 전부 잡는다. source는 적재 스크립트마다
    # 값이 달라(kci_reference_expansion/knowledge_base) 신뢰할 수 있는 판별 기준이 아니었음.
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("SELECT id, title FROM papers WHERE kci_art_id = id")
        )
        return [(r.id, r.title) for r in result.fetchall() if r.title]


async def resolve_one(client: ScienceOnClient, kci_id: str, title: str) -> str | None:
    xml = await client.search_articles(title, search_field="TI", size=5)
    try:
        parsed = xmltodict.parse(xml)
        records = _as_list(parsed.get("MetaData", {}).get("recordList", {}).get("record"))
    except Exception:
        return None

    our_norm = _normalize_title(title)
    exact_matches = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        cand_title = _item_text(rec, "Title")
        if _normalize_title(cand_title) == our_norm:
            cn = _item_text(rec, "CN")
            if cn:
                exact_matches.append(cn)

    # 완전일치가 정확히 1건일 때만 채택 (동명이인 방지 원칙과 동일한 보수적 기준)
    return exact_matches[0] if len(exact_matches) == 1 else None


def _load_checkpoint() -> dict[str, str]:
    if CHECKPOINT_PATH.exists():
        return json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
    return {}


def _save_checkpoint(data: dict[str, str]) -> None:
    CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


async def main() -> None:
    parser = argparse.ArgumentParser(description="신규 50건 논문의 ScienceON CN 역탐색 (제목 완전일치 기준)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    targets = await _load_target_papers()
    print(f"=== 대상 {len(targets)}건 ===")

    checkpoint = _load_checkpoint()
    client = ScienceOnClient()
    matched = 0
    unmatched = 0

    for i, (kci_id, title) in enumerate(targets):
        if kci_id in checkpoint:
            if checkpoint[kci_id]:
                matched += 1
            else:
                unmatched += 1
            continue

        await asyncio.sleep(RATE_LIMIT_DELAY)
        scienceon_cn = await resolve_one(client, kci_id, title)
        checkpoint[kci_id] = scienceon_cn or ""
        if scienceon_cn:
            matched += 1
            print(f"  [{i + 1}/{len(targets)}] {kci_id} -> {scienceon_cn}")
        else:
            unmatched += 1
            print(f"  [{i + 1}/{len(targets)}] {kci_id} -> 매칭 없음")
        _save_checkpoint(checkpoint)

    print(f"\n[완료] 매칭 {matched}건 / 미매칭 {unmatched}건")

    if args.dry_run:
        print("[dry-run] papers.scienceon_cn 업데이트 생략")
        return

    async with AsyncSessionLocal() as session:
        updated = 0
        for kci_id, scienceon_cn in checkpoint.items():
            if not scienceon_cn:
                continue
            result = await session.execute(
                text("UPDATE papers SET scienceon_cn = :cn WHERE id = :id"),
                {"cn": scienceon_cn, "id": kci_id},
            )
            updated += result.rowcount or 0
        await session.commit()
    print(f"[Postgres] papers.scienceon_cn 업데이트 {updated}건 (기존 KCI ID 임시값 → 실제 ScienceON CN으로 교체)")


if __name__ == "__main__":
    asyncio.run(main())
