"""연구 흐름(08-05/08-06)을 미리 만들어 researcher_flow_cache에 채운다.

왜 필요한가:
  흐름은 첫 조회 때 계산한다 — 논문 임베딩(BGE) + 묶기 + LLM 문장화로 연구자당 6~17초가
  걸리고, 그 결과를 저장해 두 번째부터는 0.1초에 응답한다. 미리 채워두면 그 '첫 한 번'을
  사용자가 겪지 않는다.

언제 돌리나:
  연구자 논문 목록이 바뀐 뒤. 캐시 서명(paper_signature)에 논문 ID와 편입 여부가 들어가
  있어서, 논문이 늘거나 papers 편입이 진행되면 기존 캐시는 자동으로 무효가 된다.
  promote_researcher_papers.py를 돌린 직후가 적기다.

비용:
  연구자 1명당 Claude 호출 1회(주제명 + 요약 문장). 입력 ~800 / 출력 ~150 토큰 기준
  건당 약 0.0016 USD이고, 이미 만들어진 연구자는 건너뛰므로 재실행은 거의 공짜다.
  월 예산 한도에 걸리면 LLMBudgetExceededError로 중단한다(규칙 기반 폴백이 저장되는 것을
  막기 위해 --skip-on-budget 없이는 멈춘다).

사용법:
  python scripts/warm_researcher_flow_cache.py --dry-run          # 대상만 세어본다
  python scripts/warm_researcher_flow_cache.py --limit 20         # 20명만
  python scripts/warm_researcher_flow_cache.py --min-papers 5     # 논문 5편 이상만 (기본 2)
  python scripts/warm_researcher_flow_cache.py                    # 전체
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
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

from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from app.services.researcher_flow_service import PROMPT_VERSION, get_research_flow

# 논문이 이보다 적으면 흐름이랄 게 없다 — 카드 한 장에 논문 한 편이라 LLM을 부를 값어치가 없다.
# API는 그런 연구자도 정상 응답하므로(계산이 1초 안에 끝난다) 예열만 건너뛴다.
_DEFAULT_MIN_PAPERS = 2

# 규칙 기반 문장이 이만큼 연속으로 저장되면 멈춘다. LLM이 죽은 채로 계속 도는 것을 막는 안전장치다.
# 정상 동작 중에도 rule은 드물게 나오지만(응답 거부·파싱 실패) 연속으로 쌓이지는 않는다.
_RULE_STREAK_LIMIT = 10

# 이미 유효한 캐시가 있는 연구자는 제외한다. 서명 비교는 get_research_flow가 하므로
# 여기서는 '이 프롬프트 버전으로 만든 행이 아예 없는' 연구자만 1차로 거른다.
# 편수는 **외부 논문 + 코퍼스 논문**을 합쳐서 센다. 흐름 계산(fetch_paper_rows)이
# 두 테이블을 합집합으로 읽기 때문이다. 예전에는 researcher_external_papers만
# INNER JOIN해서 셌는데, 그러면 두 가지가 어긋났다:
#   - 외부 논문 0편·코퍼스 논문 3편인 연구자가 JOIN에서 통째로 빠져 영영 예열되지 않는다
#   - --min-papers 5로 걸러도 실제 흐름에 들어가는 논문 수는 그보다 많을 수 있다
# 중복(같은 논문이 두 테이블에 다 있는 경우)은 fetch_paper_rows가 제거하므로
# 여기서도 코퍼스 논문 중 외부 테이블에 없는 것만 더한다.
_TARGET_SQL = """
SELECT r.researcher_id,
       (
           (SELECT count(*) FROM researcher_external_papers e
            WHERE e.researcher_id = r.researcher_id)
         + (SELECT count(*) FROM researcher_papers rp
            WHERE rp.researcher_id = r.researcher_id
              AND NOT EXISTS (
                  SELECT 1 FROM researcher_external_papers e2
                  WHERE e2.researcher_id = r.researcher_id
                    AND e2.internal_paper_id = rp.paper_id
              ))
       ) AS paper_count
FROM researchers r
WHERE NOT EXISTS (
    SELECT 1 FROM researcher_flow_cache c
    WHERE c.researcher_id = r.researcher_id AND c.prompt_version = :ver
)
GROUP BY r.researcher_id
HAVING (
           (SELECT count(*) FROM researcher_external_papers e
            WHERE e.researcher_id = r.researcher_id)
         + (SELECT count(*) FROM researcher_papers rp
            WHERE rp.researcher_id = r.researcher_id
              AND NOT EXISTS (
                  SELECT 1 FROM researcher_external_papers e2
                  WHERE e2.researcher_id = r.researcher_id
                    AND e2.internal_paper_id = rp.paper_id
              ))
       ) >= :min_papers
ORDER BY paper_count DESC
"""


async def main() -> None:
    parser = argparse.ArgumentParser(description="연구 흐름 캐시 예열")
    parser.add_argument("--limit", type=int, default=None, help="처리할 최대 연구자 수")
    parser.add_argument("--min-papers", type=int, default=_DEFAULT_MIN_PAPERS,
                        help=f"이 편수 미만인 연구자는 건너뛴다 (기본 {_DEFAULT_MIN_PAPERS})")
    parser.add_argument("--dry-run", action="store_true", help="대상만 세고 끝낸다")
    args = parser.parse_args()

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(_TARGET_SQL), {"ver": PROMPT_VERSION, "min_papers": args.min_papers}
            )
        ).all()
    targets = [(r.researcher_id, r.paper_count) for r in rows]
    if args.limit:
        targets = targets[: args.limit]

    if not targets:
        print("예열할 연구자가 없습니다 (이미 전부 캐시됨).")
        return

    total_papers = sum(c for _, c in targets)
    print(f"대상 {len(targets):,}명 / 논문 {total_papers:,}편 / 프롬프트 {PROMPT_VERSION}")
    print(f"예상 비용 약 ${len(targets) * 0.0016:.2f} (연구자당 Claude 1회)")
    if args.dry_run:
        print("\n(모의실행) 상위 5명:")
        for rid, cnt in targets[:5]:
            print(f"  {rid}  논문 {cnt}편")
        return

    stats = {"llm": 0, "rule": 0, "none": 0, "실패": 0}
    rule_streak = 0
    started = time.time()

    # LLM 호출과 임베딩이 섞여 있어 동시 실행해도 이득이 적고, 예산 초과를 빨리 감지하려면
    # 순차가 낫다. 한 명이 6~17초라 전체는 길지만 무인 실행을 전제로 한다.
    for idx, (rid, paper_count) in enumerate(targets, start=1):
        async with AsyncSessionLocal() as session:
            try:
                flow = await get_research_flow(session, rid)
                stats[flow.summary_source] = stats.get(flow.summary_source, 0) + 1
                rule_streak = rule_streak + 1 if flow.summary_source == "rule" else 0
            except Exception as exc:
                await session.rollback()
                stats["실패"] += 1
                print(f"  [{idx}] {rid} 실패: {type(exc).__name__}: {str(exc)[:80]}")
                # 예산 소진은 계속 돌아봐야 규칙 기반 문장만 쌓인다 — 멈춘다.
                if type(exc).__name__ == "LLMBudgetExceededError":
                    print("  월 LLM 예산 소진 — 중단합니다. 다음 달 또는 한도 상향 후 재실행하세요.")
                    break

        # LLM이 연속으로 실패하면 남은 시간을 규칙 기반 문장을 쌓는 데 쓰게 된다.
        # 그렇게 저장된 행은 paper_signature가 같아 다음 실행에서 캐시로 반환되므로,
        # 나중에 문장만 다시 만들 수가 없다 — 행을 지우고 임베딩·클러스터링부터 재계산해야 한다.
        # (2026-09-23 실측: Anthropic 크레딧 소진으로 962건이 rule로 쌓였다. 그때 이 가드가
        #  없어서 LLMBudgetExceededError 분기에 안 걸렸다 — 계정 잔액 부족은 우리 예산
        #  카운터가 아니라 API가 BadRequestError로 던진다.)
        if rule_streak >= _RULE_STREAK_LIMIT:
            print(
                f"\n  규칙 기반 문장이 {_RULE_STREAK_LIMIT}건 연속 저장됐습니다 — 중단합니다.\n"
                "  LLM 호출이 계속 실패하고 있다는 뜻입니다 (API 키·크레딧 잔액·모델명 확인).\n"
                "  지금까지 저장된 rule 행을 지우고 재실행해야 LLM 문장이 들어갑니다:\n"
                f"    DELETE FROM researcher_flow_cache WHERE prompt_version = '{PROMPT_VERSION}' "
                "AND summary_source = 'rule';"
            )
            break

        if idx % 20 == 0 or idx == len(targets):
            elapsed = time.time() - started
            speed = idx / max(elapsed, 1)
            remain = (len(targets) - idx) / max(speed, 0.001) / 60
            print(
                f"  {idx:,}/{len(targets):,} ({idx/len(targets)*100:.1f}%) "
                f"{dict(sorted(stats.items()))} | {speed*60:.1f}명/분 | 남은 시간 약 {remain:.0f}분"
            )

    print(f"\n완료: {dict(sorted(stats.items()))} / {(time.time()-started)/60:.1f}분")
    if stats.get("rule"):
        print(
            f"※ {stats['rule']}명은 규칙 기반 문장으로 저장됐습니다(LLM 거부·파싱 실패 등). "
            "researcher_flow_cache에서 summary_source='rule'로 조회해 재생성할 수 있습니다."
        )


if __name__ == "__main__":
    asyncio.run(main())
