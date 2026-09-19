"""AI 검색 인당 이용 한도 + 충전(선불) 예산 가드."""
import pytest

from app.core import ai_usage_limit as lim
from app.core.exceptions import AppHTTPException
from app.core.settings import settings


class FakeRedis:
    def __init__(self):
        self.d = {}

    def get(self, k):
        return self.d.get(k)

    def set(self, k, v, ex=None, nx=False):
        if nx and k in self.d:
            return False
        self.d[k] = v
        return True

    def incr(self, k):
        self.d[k] = int(self.d.get(k) or 0) + 1
        return self.d[k]

    def incrby(self, k, n):
        self.d[k] = int(self.d.get(k) or 0) + n
        return self.d[k]

    def decr(self, k):
        self.d[k] = int(self.d.get(k) or 0) - 1
        return self.d[k]

    def expire(self, k, s):
        return True

    def pipeline(self, transaction=True):
        return FakePipe(self)


class FakePipe:
    def __init__(self, r):
        self.r, self.ops = r, []

    def __getattr__(self, name):
        def op(*a, **kw):
            self.ops.append((name, a, kw))
            return self
        return op

    def execute(self):
        return [getattr(self.r, n)(*a, **kw) for n, a, kw in self.ops]


@pytest.fixture
def redis(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr(lim, "get_redis", lambda db: r)
    monkeypatch.setattr(settings, "ai_usage_limit_enabled", True)
    monkeypatch.setattr(settings, "ai_new_chat_limit", 2)
    monkeypatch.setattr(settings, "ai_turns_per_chat_limit", 3)
    return r


def test_disabled_returns_no_counts(monkeypatch):
    monkeypatch.setattr(settings, "ai_usage_limit_enabled", False)
    usage = lim.consume_ai_usage("user:u", "s1", "new_chat")
    assert usage.remaining_new_chats is None and usage.remaining_turns is None


def test_new_chat_limit(redis):
    first = lim.consume_ai_usage("user:u", "s1", "new_chat")
    assert (first.remaining_new_chats, first.remaining_turns) == (1, 3)
    second = lim.consume_ai_usage("user:u", "s2", "new_chat")
    assert second.remaining_new_chats == 0
    with pytest.raises(AppHTTPException) as exc:
        lim.consume_ai_usage("user:u", "s3", "new_chat")
    assert exc.value.status_code == 429 and exc.value.error_code == lim.AI_CHAT_LIMIT
    # 거절된 요청은 횟수를 올리지 않는다
    assert int(redis.get(lim._new_chat_key("user:u"))) == 2


def test_turn_limit_is_per_chat(redis):
    lim.consume_ai_usage("user:u", "s1", "new_chat")
    for expected in (2, 1, 0):
        assert lim.consume_ai_usage("user:u", "s1", "turn").remaining_turns == expected
    with pytest.raises(AppHTTPException) as exc:
        lim.consume_ai_usage("user:u", "s1", "turn")
    assert exc.value.error_code == lim.AI_TURN_LIMIT
    # 다른 채팅의 턴은 따로 센다
    lim.consume_ai_usage("user:u", "s2", "new_chat")
    assert lim.consume_ai_usage("user:u", "s2", "turn").remaining_turns == 2


def test_free_request_does_not_consume(redis):
    lim.consume_ai_usage("user:u", "s1", "new_chat")
    lim.consume_ai_usage("user:u", "s1", "turn")
    usage = lim.consume_ai_usage("user:u", "s1", "free")  # 정렬만 바꾸는 요청
    assert (usage.remaining_new_chats, usage.remaining_turns) == (1, 2)


def test_release_refunds_failed_request(redis):
    usage = lim.consume_ai_usage("user:u", "s1", "new_chat")
    lim.release_ai_usage(usage)
    assert lim.consume_ai_usage("user:u", "s2", "new_chat").remaining_new_chats == 1


def test_subjects_are_isolated(redis):
    lim.consume_ai_usage("user:a", "s1", "new_chat")
    lim.consume_ai_usage("user:a", "s2", "new_chat")
    assert lim.consume_ai_usage("user:b", "s3", "new_chat").remaining_new_chats == 1


def test_redis_outage_lets_request_through(monkeypatch):
    monkeypatch.setattr(settings, "ai_usage_limit_enabled", True)

    def boom(db):
        raise ConnectionError("redis down")

    monkeypatch.setattr(lim, "get_redis", boom)
    usage = lim.consume_ai_usage("user:u", "s1", "new_chat")
    assert usage.remaining_new_chats is None


# ---------------------------------------------------------------------------
# 충전 예산: 리셋 없이 충전액 기준으로 막는다
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_prepaid_budget_blocks_and_ignores_monthly(monkeypatch):
    from app.services.llm import client

    r = FakeRedis()
    monkeypatch.setattr(client, "get_redis", lambda db: r)
    monkeypatch.setattr(settings, "llm_budget_prepaid_usd", 1.0)
    r.set(client._monthly_key(), 0)                 # 이번 달 카운터는 0이어도(= 1일 리셋 직후)
    r.set(client._COST_KEY_PREPAID, 1_000_000)      # 충전액을 다 썼으면 막힌다
    with pytest.raises(client.LLMBudgetExceededError):
        await client.chat([{"role": "user", "content": "x"}], model="claude-haiku-4-5")
    status = client.get_budget_status()
    assert status["mode"] == "prepaid" and status["exhausted"] is True


@pytest.mark.asyncio
async def test_prepaid_counter_accumulates(monkeypatch):
    from app.services.llm import client

    r = FakeRedis()
    monkeypatch.setattr(client, "get_redis", lambda db: r)
    monkeypatch.setattr(settings, "llm_budget_prepaid_usd", 5.0)

    async def fake_call(*a, **kw):
        return "ok", 1000, 200, 0, 0

    monkeypatch.setattr(client, "_call_claude", fake_call)
    await client.chat([{"role": "user", "content": "x"}], model="claude-sonnet-5", use_cache=False)
    assert int(r.get(client._COST_KEY_PREPAID)) == 4_000  # 1000×$2 + 200×$10 (per 1M)
    assert client.get_budget_status()["remaining_usd"] == pytest.approx(5.0 - 0.004)
