"""Postgres / Neo4j Aura / ChromaDB 세 저장소 백업.

백업 파일 위치: data/backups/
  - postgres_YYYYMMDD_HHMMSS.dump   (pg_dump -Fc 커스텀 포맷)
  - neo4j_YYYYMMDD_HHMMSS.cypher    (APOC stream, cypher-shell 포맷)
  - chroma_YYYYMMDD_HHMMSS.tar.gz   (Docker volume tar)

사용법:
  python scripts/backup_stores.py                    # 전체 3개 저장소
  python scripts/backup_stores.py --stores postgres
  python scripts/backup_stores.py --stores neo4j
  python scripts/backup_stores.py --stores chroma
  python scripts/backup_stores.py --out-dir /path/to/dir

복구 방법: docs/BACKUP.md 참고
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime
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
        os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

DEFAULT_OUT_DIR = PROJECT_ROOT / "data" / "backups"
TIMESTAMP = datetime.now().strftime("%Y%m%d_%H%M%S")

# ---------------------------------------------------------------------------
# Postgres
# ---------------------------------------------------------------------------

def backup_postgres(out_dir: Path) -> Path:
    out_file = out_dir / f"postgres_{TIMESTAMP}.dump"
    print(f"\n[Postgres] pg_dump → {out_file.name}")
    env = {**os.environ, "PGPASSWORD": os.environ.get("POSTGRES_PASSWORD", "password")}
    result = subprocess.run(
        [
            "pg_dump",
            "-h", "localhost",
            "-U", os.environ.get("POSTGRES_USER", "user"),
            "-d", os.environ.get("POSTGRES_DB", "edgeitzo"),
            "-F", "c",           # custom format (압축 포함)
            "-f", str(out_file),
        ],
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(f"pg_dump 실패 (exit {result.returncode})")
    size_mb = out_file.stat().st_size / 1024 / 1024
    print(f"  완료: {out_file.name} ({size_mb:.1f} MB)")
    return out_file


# ---------------------------------------------------------------------------
# Neo4j (APOC stream export)
# ---------------------------------------------------------------------------

def backup_neo4j(out_dir: Path) -> Path:
    out_file = out_dir / f"neo4j_{TIMESTAMP}.cypher"
    print(f"\n[Neo4j] APOC stream export → {out_file.name}")

    from neo4j import GraphDatabase  # type: ignore

    uri  = os.environ["NEO4J_URI"]
    user = os.environ.get("NEO4J_USERNAME", os.environ.get("NEO4J_USER", "neo4j"))
    pwd  = os.environ["NEO4J_PASSWORD"]

    driver = GraphDatabase.driver(uri, auth=(user, pwd))
    try:
        with driver.session() as session:
            # stream:true → cypherStatements를 청크로 반환. 전부 이어붙임.
            result = session.run(
                "CALL apoc.export.cypher.all(null, "
                "{stream:true, format:'cypher-shell'}) "
                "YIELD cypherStatements RETURN cypherStatements"
            )
            parts = [r["cypherStatements"] for r in result if r["cypherStatements"]]

        if not parts:
            raise RuntimeError("APOC export가 빈 결과 반환")

        cypher_text = "".join(parts)
        out_file.write_text(cypher_text, encoding="utf-8")

        size_mb = out_file.stat().st_size / 1024 / 1024
        line_count = cypher_text.count("\n")
        print(f"  완료: {out_file.name} ({size_mb:.1f} MB, ~{line_count:,}줄)")
    finally:
        driver.close()

    return out_file


# ---------------------------------------------------------------------------
# ChromaDB (Docker volume tar)
# ---------------------------------------------------------------------------

def backup_chroma(out_dir: Path) -> Path:
    out_file = out_dir / f"chroma_{TIMESTAMP}.tar.gz"
    print(f"\n[ChromaDB] docker exec tar → {out_file.name}")
    # 실제 데이터 위치: 컨테이너 내부 /data/ (chroma.sqlite3 등)
    # docker-compose의 volume mount(/chroma/chroma)가 실제 데이터 경로와 불일치하므로
    # volume 대신 docker exec로 /data 를 직접 tar한다.

    # 컨테이너 실행 중 확인
    check = subprocess.run(
        ["docker", "ps", "-q", "--filter", "name=papergraph-chromadb"],
        capture_output=True, text=True,
    )
    if check.returncode != 0:
        raise RuntimeError("Docker 데몬이 응답하지 않습니다. Docker를 실행한 뒤 재시도하세요.")
    if not check.stdout.strip():
        raise RuntimeError("papergraph-chromadb 컨테이너가 실행 중이지 않습니다.")

    with open(out_file, "wb") as f:
        result = subprocess.run(
            ["docker", "exec", "papergraph-chromadb", "tar", "czf", "-", "/data"],
            stdout=f,
            stderr=subprocess.PIPE,
        )
    if result.returncode != 0:
        out_file.unlink(missing_ok=True)
        raise RuntimeError(f"docker exec tar 실패: {result.stderr.decode().strip()}")

    size_mb = out_file.stat().st_size / 1024 / 1024
    print(f"  완료: {out_file.name} ({size_mb:.1f} MB)")
    return out_file


# ---------------------------------------------------------------------------
# 메인
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="세 저장소 백업")
    parser.add_argument(
        "--stores",
        default="all",
        choices=["all", "postgres", "neo4j", "chroma"],
        help="백업할 저장소 선택 (기본: all)",
    )
    parser.add_argument(
        "--out-dir",
        default=str(DEFAULT_OUT_DIR),
        help=f"백업 파일 저장 경로 (기본: {DEFAULT_OUT_DIR})",
    )
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    targets = (
        ["postgres", "neo4j", "chroma"] if args.stores == "all" else [args.stores]
    )

    print(f"=== 저장소 백업 시작: {', '.join(targets)} ===")
    print(f"    출력 경로: {out_dir}")
    print(f"    타임스탬프: {TIMESTAMP}")

    results: dict[str, Path | str] = {}
    for store in targets:
        try:
            if store == "postgres":
                results[store] = backup_postgres(out_dir)
            elif store == "neo4j":
                results[store] = backup_neo4j(out_dir)
            elif store == "chroma":
                results[store] = backup_chroma(out_dir)
        except Exception as exc:
            print(f"  [오류] {store} 백업 실패: {exc}")
            results[store] = f"FAILED: {exc}"

    # 결과 요약
    print("\n" + "=" * 60)
    print("백업 결과 요약")
    print("=" * 60)
    all_ok = True
    for store, outcome in results.items():
        if isinstance(outcome, Path):
            print(f"  ✓ {store:10s} → {outcome.name}")
        else:
            print(f"  ✗ {store:10s} → {outcome}")
            all_ok = False

    if all_ok:
        print(f"\n모든 백업 완료. 복구 방법: docs/BACKUP.md")
    else:
        print(f"\n일부 백업 실패. 위 오류를 확인하세요.")
        sys.exit(1)


if __name__ == "__main__":
    main()
