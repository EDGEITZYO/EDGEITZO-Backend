"""
Celery 동작 검증 스크립트.

사용법:
  python scripts/test_celery.py
"""
from __future__ import annotations

import os
import sys
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
        _k = _k.strip()
        _v = _v.strip().strip('"').strip("'")
        if _k:
            os.environ.setdefault(_k, _v)

from app.tasks.health import add, echo, ping  # noqa: E402


def main() -> None:
    print("=" * 50)
    print("Celery Health Check")
    print("=" * 50)

    print("\n[1] ping()")
    result = ping.delay()
    print(f"  task_id: {result.id}")
    print(f"  status before: {result.status}")
    output = result.get(timeout=10)
    print(f"  status after:  {result.status}")
    print(f"  output: {output}")

    print("\n[2] echo('hello celery')")
    result = echo.delay("hello celery")
    output = result.get(timeout=10)
    print(f"  output: {output}")

    print("\n[3] add(3, 5)")
    result = add.delay(3, 5)
    output = result.get(timeout=10)
    print(f"  output: {output}")

    print("\n[OK] All tests passed")


if __name__ == "__main__":
    main()
