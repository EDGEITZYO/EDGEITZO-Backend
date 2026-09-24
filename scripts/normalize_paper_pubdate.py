"""papers.pubdate를 코퍼스 원본에서 채우고 YYYY-MM-DD로 통일한다.

두 가지 문제를 같이 고친다.

**① 적재가 빠뜨린 발행일** — 코퍼스 JSON에는 850편에 Pubdate가 있는데 DB에는 더 적게
들어가 있다(운영 798 / 로컬 848). 구분자가 없는 `20251230` 형식을 적재 코드가 처리하지
못한 것으로 보인다. 원본 JSON이 단일 진실 공급원이므로 거기서 다시 읽어 채운다.

**② 형식이 섞여 있다** — 같은 컬럼에 세 종류가 들어 있다:

    20251230     코퍼스 JSON 648편 (구분자 없음)
    2007.06.01   코퍼스 JSON 200편 (점)
    2015-01-01   적재분 150편 (하이픈)

API는 `format_published_at()`이 숫자만 뽑아 흡수하므로 화면은 지금도 정상이지만,
SQL에서 날짜로 비교·정렬할 때 점 형식이 섞이면 사전순이 시간순과 어긋난다.
저장 단계에서 통일해 둔다.

날짜가 없는 논문은 **비워 둔다.** 연도만 알 때 1월 1일로 채우지 않는다 —
"1월 1일 발행"은 사실이 아니고, 코퍼스의 15%(DIKO 학위논문 150편)가 여기 해당한다.
API가 연도만 있는 경우 "2025"처럼 내보내므로 화면에서는 문제가 없다.

사용법:
  python scripts/normalize_paper_pubdate.py --dry-run
  python scripts/normalize_paper_pubdate.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections import Counter
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

from sqlalchemy import text  # noqa: E402

from app.core.database import AsyncSessionLocal  # noqa: E402

CORPUS_PATH = PROJECT_ROOT / "data" / "parsed" / "scienceon_keywords_normalized.json"
_DIGITS = re.compile(r"\d+")


def normalize(raw: str | None) -> str | None:
    """숫자만 뽑아 YYYY-MM-DD로. 8자리가 안 되면 None(= 채우지 않는다).

    연·월만 있는 값(`201908`)도 None으로 둔다. pubdate는 '발행일' 컬럼이라
    일자가 없으면 담을 것이 없다 — 연도는 pubyear가 이미 갖고 있고,
    API의 format_published_at()이 둘을 합쳐 "2019-08"까지는 못 만들지만
    해당 건이 코퍼스에 1편뿐이라 별도 컬럼을 두지 않는다.
    """
    if not raw:
        return None
    digits = "".join(_DIGITS.findall(str(raw)))
    if len(digits) < 8:
        return None
    y, m, d = digits[:4], digits[4:6], digits[6:8]
    if not ("1900" <= y <= "2100" and "01" <= m <= "12" and "01" <= d <= "31"):
        return None
    return f"{y}-{m}-{d}"


async def main() -> None:
    parser = argparse.ArgumentParser(description="papers.pubdate 보정·정규화")
    parser.add_argument("--dry-run", action="store_true", help="바뀔 내용만 세어보고 쓰지 않는다")
    args = parser.parse_args()

    papers = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))["papers"]
    wanted = {p["CN"]: normalize(p.get("Pubdate")) for p in papers}
    print(f"코퍼스 원본 {len(wanted):,}편 — 정규화 성공 {sum(1 for v in wanted.values() if v):,}편")

    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                text("SELECT id, pubdate FROM papers WHERE id = ANY(:ids)"),
                {"ids": list(wanted)},
            )
        ).all()
    current = {r.id: r.pubdate for r in rows}
    print(f"DB에 있는 코퍼스 논문 {len(current):,}편")

    changes: list[tuple[str, str | None, str]] = []
    stats = Counter()
    for cn, new in wanted.items():
        if cn not in current:
            stats["DB에 없음"] += 1
            continue
        old = current[cn]
        if new is None:
            stats["원본에 날짜 없음"] += 1
            continue
        if old == new:
            stats["이미 정상"] += 1
            continue
        stats["신규 채움" if not old else "형식 교정"] += 1
        changes.append((cn, old, new))

    print(f"\n{dict(sorted(stats.items()))}")
    print(f"바꿀 행: {len(changes):,}")
    for cn, old, new in changes[:8]:
        print(f"  {cn:24} {str(old):14} → {new}")

    if args.dry_run or not changes:
        print("\n(모의실행) 쓰지 않았습니다." if args.dry_run else "\n바꿀 것이 없습니다.")
        return

    async with AsyncSessionLocal() as db:
        for start in range(0, len(changes), 500):
            chunk = changes[start : start + 500]
            await db.execute(
                # unnest에 타입을 명시하지 않으면 Postgres가 후보 함수를 못 고른다
                # (could not choose a best candidate function).
                text(
                    "UPDATE papers SET pubdate = v.pubdate, updated_at = now() "
                    "FROM (SELECT unnest(CAST(:ids AS varchar[])) AS id, "
                    "             unnest(CAST(:dates AS varchar[])) AS pubdate) v "
                    "WHERE papers.id = v.id"
                ),
                {"ids": [c[0] for c in chunk], "dates": [c[2] for c in chunk]},
            )
        await db.commit()
    print(f"\n{len(changes):,}행 갱신 완료")


if __name__ == "__main__":
    asyncio.run(main())
