"""게스트 발급·회원 인증 회귀 통합 테스트 — 실제 PostgreSQL/Redis가 필요하다.

운영/개발 DB를 건드리지 않도록 RUN_GUEST_IT=1일 때만 돈다. 일회용 컨테이너로 실행:

  docker run -d --rm --name guest-it-pg -e POSTGRES_USER=it -e POSTGRES_PASSWORD=it \\
      -e POSTGRES_DB=it -p 127.0.0.1:55432:5432 postgres:15
  docker run -d --rm --name guest-it-redis -p 127.0.0.1:56379:6379 redis:7
  export DATABASE_URL=postgresql+asyncpg://it:it@127.0.0.1:55432/it \\
      REDIS_HOST=127.0.0.1 REDIS_PORT=56379 REDIS_PASSWORD= APP_ENV=local
  alembic upgrade head
  RUN_GUEST_IT=1 pytest tests/test_guest_auth_integration.py
"""
import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

if os.environ.get("RUN_GUEST_IT") != "1":
    pytest.skip("RUN_GUEST_IT=1일 때만 실행 (일회용 DB 필요)", allow_module_level=True)

import httpx
from sqlalchemy import text

from app.api.v1.home import save_search_history
from app.core.database import AsyncSessionLocal, engine
from app.core.redis import get_redis
from app.core.settings import settings
from app.main import app

API = "/api/v1"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
PAPER_ID = "IT-GUEST-PAPER-1"


@pytest.fixture(autouse=True)
def _demo_env(monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True)
    monkeypatch.setattr(settings, "guest_issue_limit_per_ip", 1000)
    for db in (0, settings.redis_blacklist_db, settings.redis_rate_limit_db, 7):
        get_redis(db).flushdb()


def _run(scenario):
    async def wrapper():
        try:
            await scenario()
        finally:
            await engine.dispose()  # 테스트마다 이벤트 루프가 달라 풀을 비운다

    asyncio.run(wrapper())


def _client(ip: str = "203.0.113.10") -> httpx.AsyncClient:
    # 신원마다 클라이언트를 따로 둔다 — 쿠키 저장소가 섞이면 access_token 쿠키가 Bearer보다 우선한다
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Forwarded-For": f"9.9.9.9, {ip}"},
    )


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _max_age(resp: httpx.Response, name: str) -> int:
    for header in resp.headers.get_list("set-cookie"):
        if header.startswith(f"{name}="):
            for part in header.split(";"):
                if part.strip().lower().startswith("max-age="):
                    return int(part.split("=")[1])
    raise AssertionError(f"{name} 쿠키 없음")


async def _issue_guest(ip: str = "203.0.113.10") -> dict:
    async with _client(ip) as c:
        resp = await c.post(f"{API}/auth/guest")
    assert resp.status_code == 200, resp.text
    return {"resp": resp, "token": resp.json()["data"]["access_token"]}


async def _ensure_paper() -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            text(
                "INSERT INTO papers (id, source_type, title, citation_count) "
                "VALUES (:id, 'test', '게스트 분리 검증용 논문', 0) ON CONFLICT (id) DO NOTHING"
            ),
            {"id": PAPER_ID},
        )
        await db.commit()


# ── DEMO_MODE ────────────────────────────────────────────────────────────

def test_guest_endpoint_404_when_demo_mode_off(monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", False)

    async def scenario():
        async with _client() as c:
            assert (await c.post(f"{API}/auth/guest")).status_code == 404

    _run(scenario)


# ── 회원 플로우 회귀 ──────────────────────────────────────────────────────

def test_member_register_login_refresh_logout_regression():
    email = f"member_{uuid.uuid4().hex[:8]}@example.com"
    password = "Passw0rd!"

    async def scenario():
        get_redis(0).setex(f"verify:{email}", 900, "1")  # 이메일 인증 완료 상태
        async with _client() as c:
            reg = await c.post(
                f"{API}/auth/register",
                json={"email": email, "password": password, "confirm_password": password},
            )
            assert reg.status_code == 200, reg.text
            assert set(reg.json()["data"]) == {"access_token", "refresh_token", "token_type"}
            assert _max_age(reg, "refresh_token") == 86400 * settings.jwt_refresh_expire_days

        async with _client() as c:
            bad = await c.post(f"{API}/auth/login", json={"email": email, "password": "Wrong0rd!"})
            assert bad.status_code == 400
            login = await c.post(f"{API}/auth/login", json={"email": email, "password": password})
            assert login.status_code == 200, login.text
            token = login.json()["data"]["access_token"]

            prof = await c.post(f"{API}/auth/profile", json={"name": "회원"}, headers=_auth(token))
            assert prof.status_code == 200, prof.text
            patch = await c.patch(f"{API}/mypage/profile", json={"research_field": "면역학"}, headers=_auth(token))
            assert patch.status_code == 200, patch.text
            me = await c.get(f"{API}/mypage", headers=_auth(token))
            assert me.status_code == 200

            refreshed = await c.post(f"{API}/auth/refresh")  # 쿠키 사용
            assert refreshed.status_code == 200, refreshed.text
            assert _max_age(refreshed, "refresh_token") == 86400 * settings.jwt_refresh_expire_days

            out = await c.post(f"{API}/auth/logout", headers=_auth(token))
            assert out.status_code == 200
            c.cookies.clear()
            assert (await c.get(f"{API}/auth/me", headers=_auth(token))).status_code == 401

    _run(scenario)


# ── 게스트 발급 ──────────────────────────────────────────────────────────

def test_guest_issue_response_profile_and_refresh():
    async def scenario():
        guest = await _issue_guest()
        resp, token = guest["resp"], guest["token"]
        assert set(resp.json()["data"]) == {"access_token", "refresh_token", "token_type"}
        assert _max_age(resp, "access_token") == 60 * settings.jwt_access_expire_minutes
        assert _max_age(resp, "refresh_token") == 86400 * settings.guest_token_expire_days

        async with _client() as c:
            profile = (await c.get(f"{API}/mypage", headers=_auth(token))).json()["data"]["profile"]
            assert profile["is_profile_set"] is True
            assert profile["name"].startswith("게스트")
            assert profile["research_field"] == "암 분자 생물학"
            assert profile["role"] == "석사과정"
            assert profile["purposes"] == ["논문 작성 참고"]
            assert profile["purpose_custom"] == "랩미팅 준비"
            assert profile["gender"] == "여성"
            assert profile["birth_year"] == 1999

            start = (await c.get(f"{API}/auth/start", headers=_auth(token))).json()
            assert start["authenticated"] is True and start["next_route"] == "/main"

        # refresh 회전 후에도 게스트 만료 유지
        async with _client() as c:
            c.cookies.set("refresh_token", resp.json()["data"]["refresh_token"])
            refreshed = await c.post(f"{API}/auth/refresh")
            assert refreshed.status_code == 200, refreshed.text
            assert _max_age(refreshed, "refresh_token") == 86400 * settings.guest_token_expire_days

    _run(scenario)


def test_guest_cannot_use_account_management_apis():
    async def scenario():
        token = (await _issue_guest())["token"]
        async with _client() as c:
            h = _auth(token)
            assert (await c.post(f"{API}/auth/profile", json={"name": "x"}, headers=h)).status_code == 403
            assert (await c.patch(f"{API}/mypage/profile", json={"name": "x"}, headers=h)).status_code == 403
            assert (await c.delete(f"{API}/mypage/account", headers=h)).status_code == 403
            assert (await c.get(f"{API}/auth/me", headers=h)).status_code == 200
            assert (await c.post(f"{API}/auth/logout", headers=h)).status_code == 200

    _run(scenario)


def test_two_concurrent_guests_have_separate_history():
    async def scenario():
        await _ensure_paper()
        a, b = await asyncio.gather(_issue_guest(), _issue_guest())
        ta, tb = a["token"], b["token"]

        async with _client() as c:
            ha, hb = _auth(ta), _auth(tb)
            me_a = (await c.get(f"{API}/home", headers=ha)).json()["data"]["user"]
            me_b = (await c.get(f"{API}/home", headers=hb)).json()["data"]["user"]
            assert me_a["id"] != me_b["id"]
            assert me_a["name"] != me_b["name"]

            # A만 탐색/열람/북마크
            save_search_history(user_id=me_a["id"], search_type="ai", title="A의 탐색", search_id="sess-a")
            save_search_history(user_id=me_b["id"], search_type="ai", title="B의 탐색", search_id="sess-b")
            assert (await c.post(f"{API}/home/recent-reads", json={"paper_id": PAPER_ID}, headers=ha)).status_code == 200
            assert (await c.post(f"{API}/bookmarks", json={"paper_id": PAPER_ID}, headers=ha)).status_code == 200
            saved = await c.post(
                f"{API}/researchers/recent-searches",
                json={"query": "암 유전체", "search_type": "field"},
                headers=ha,
            )
            assert saved.status_code == 200, saved.text

            home_a = (await c.get(f"{API}/home", headers=ha)).json()["data"]
            home_b = (await c.get(f"{API}/home", headers=hb)).json()["data"]
            assert [s["title"] for s in home_a["recent_searches"]] == ["A의 탐색"]
            assert [s["title"] for s in home_b["recent_searches"]] == ["B의 탐색"]
            assert [p["paper_id"] for p in home_a["recent_papers"]] == [PAPER_ID]
            assert home_b["recent_papers"] == []

            bm_a = (await c.get(f"{API}/bookmarks", headers=ha)).json()["data"]
            bm_b = (await c.get(f"{API}/bookmarks", headers=hb)).json()["data"]
            assert bm_a["total"] == 1
            assert bm_b["total"] == 0

            rs_a = (await c.get(f"{API}/researchers/recent-searches", headers=ha)).json()["data"]
            rs_b = (await c.get(f"{API}/researchers/recent-searches", headers=hb)).json()["data"]
            assert [i["query"] for i in rs_a["items"]] == ["암 유전체"]
            assert rs_b["items"] == []

    _run(scenario)


# ── 남용 방지 ────────────────────────────────────────────────────────────

def test_guest_issue_rate_limited_per_ip(monkeypatch):
    monkeypatch.setattr(settings, "guest_issue_limit_per_ip", 2)

    async def scenario():
        async with _client("198.51.100.1") as c:
            assert (await c.post(f"{API}/auth/guest")).status_code == 200
            assert (await c.post(f"{API}/auth/guest")).status_code == 200
            blocked = await c.post(f"{API}/auth/guest")
            assert blocked.status_code == 429
            assert int(blocked.headers["Retry-After"]) > 0
        async with _client("198.51.100.2") as c:  # 다른 IP는 영향 없음
            assert (await c.post(f"{API}/auth/guest")).status_code == 200

    _run(scenario)


def test_llm_route_limits_guest_and_anonymous_but_not_member(monkeypatch):
    monkeypatch.setattr(settings, "guest_llm_rate_limit", 1)
    monkeypatch.setattr(settings, "anon_llm_rate_limit_per_ip", 1)
    # 없는 연구자 → 핸들러가 LLM 호출 전에 404. rate limit 의존성은 핸들러보다 먼저 돈다.
    url = f"{API}/researchers/kci:does-not-exist/research-flow"

    async def scenario():
        token = (await _issue_guest())["token"]
        async with _client() as c:
            assert (await c.get(url, headers=_auth(token))).status_code == 404
            assert (await c.get(url, headers=_auth(token))).status_code == 429

        async with _client("198.51.100.50") as c:
            assert (await c.get(url)).status_code == 404
            assert (await c.get(url)).status_code == 429
        async with _client("198.51.100.51") as c:
            assert (await c.get(url)).status_code == 404

        email = f"m_{uuid.uuid4().hex[:8]}@example.com"
        get_redis(0).setex(f"verify:{email}", 900, "1")
        async with _client("198.51.100.50") as c:  # 막힌 IP여도 회원은 통과
            reg = await c.post(
                f"{API}/auth/register",
                json={"email": email, "password": "Passw0rd!", "confirm_password": "Passw0rd!"},
            )
            mt = reg.json()["data"]["access_token"]
            for _ in range(3):
                assert (await c.get(url, headers=_auth(mt))).status_code == 404

    _run(scenario)


# ── 정리 스크립트 ────────────────────────────────────────────────────────

def test_cleanup_script_dry_run_then_delete():
    async def seed():
        await _ensure_paper()
        guest = await _issue_guest()
        async with _client() as c:
            h = _auth(guest["token"])
            uid = (await c.get(f"{API}/home", headers=h)).json()["data"]["user"]["id"]
            await c.post(f"{API}/bookmarks", json={"paper_id": PAPER_ID}, headers=h)
            await c.post(f"{API}/home/recent-reads", json={"paper_id": PAPER_ID}, headers=h)
            await c.post(f"{API}/researchers/recent-searches", json={"query": "q", "search_type": "name"}, headers=h)
        save_search_history(user_id=uid, search_type="ai", title="t", search_id="s")
        seed.uid = uid

    _run(seed)
    uid = seed.uid

    async def counts():
        async with AsyncSessionLocal() as db:
            guests = (await db.execute(text("SELECT count(*) FROM users WHERE is_guest"))).scalar_one()
            members = (await db.execute(text("SELECT count(*) FROM users WHERE NOT is_guest"))).scalar_one()
            reads = (
                await db.execute(text("SELECT count(*) FROM recent_reads WHERE user_id = :u"), {"u": uuid.UUID(uid)})
            ).scalar_one()
        counts.value = (guests, members, reads)

    _run(counts)
    guests_before, members_before, reads_before = counts.value
    assert guests_before >= 1 and reads_before == 1

    script = [sys.executable, str(PROJECT_ROOT / "scripts" / "cleanup_guests.py")]
    dry = subprocess.run(script + ["--dry-run"], capture_output=True, text=True, env=os.environ, cwd=PROJECT_ROOT)
    assert dry.returncode == 0, dry.stderr
    assert f"대상 게스트: {guests_before}명" in dry.stdout and "삭제하지 않았습니다" in dry.stdout

    _run(counts)
    assert counts.value == (guests_before, members_before, reads_before)  # dry-run은 아무것도 안 지움

    real = subprocess.run(script, capture_output=True, text=True, env=os.environ, cwd=PROJECT_ROOT)
    assert real.returncode == 0, real.stderr

    _run(counts)
    assert counts.value == (0, members_before, 0)  # 회원은 그대로
    assert not get_redis(7).exists(f"recent_searches:{uid}", f"researcher_searches:{uid}")
