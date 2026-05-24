"""전체 데이터 파이프라인 순차 실행.

순서:
  1. recollect_papers_detail.py  — ScienceON 상세 API 수집 → paper_details.json
  2. load_paper_relations.py     — paper_similar / paper_references DB 적재
  3. embed_papers.py --reset     — ChromaDB 재임베딩 (기존 컬렉션 삭제 후 재생성)
  4. load_neo4j_graph.py --reset — Neo4j 그래프 재적재

기존 collect_papers.py, parse_papers.py, normalize_keywords.py, load_papers_postgres.py는
이 파이프라인에서 실행하지 않는다 (데이터 재수집 아님, 관계 데이터만 동기화).
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_PYTHON = sys.executable
_SCRIPTS = _PROJECT_ROOT / "scripts"


def _run(script: str, *args: str) -> None:
    cmd = [_PYTHON, str(_SCRIPTS / script), *args]
    print(f"\n{'='*60}")
    print(f"▶ {' '.join(cmd)}")
    print('='*60)
    t = time.time()
    result = subprocess.run(cmd, cwd=str(_PROJECT_ROOT))
    elapsed = time.time() - t
    if result.returncode != 0:
        print(f"\n[오류] {script} 실패 (exit {result.returncode}) — 파이프라인 중단")
        sys.exit(result.returncode)
    print(f"[완료] {script} ({elapsed:.0f}초)")


def main() -> None:
    total_start = time.time()
    print("=== reload_all 파이프라인 시작 ===")
    print("순서: recollect → load_relations → embed(--reset) → neo4j(--reset)")

    _run("recollect_papers_detail.py")
    _run("load_paper_relations.py")
    _run("embed_papers.py", "--reset")
    _run("load_neo4j_graph.py", "--reset")

    elapsed = time.time() - total_start
    print(f"\n=== 전체 파이프라인 완료 ({elapsed:.0f}초) ===")


if __name__ == "__main__":
    main()
