import pytest

from shopbot.repository import AccessDenied, RedisCoordinator


class FakeRedis:
    def __init__(self):
        self.values = {}

    async def set(self, key, value, **kwargs):
        if kwargs.get("nx") and key in self.values:
            return False
        self.values[key] = value
        return True

    async def get(self, key):
        return self.values.get(key)

    async def delete(self, key):
        return int(self.values.pop(key, None) is not None)


@pytest.mark.asyncio
async def test_callback_is_compact_owned_and_replay_protected():
    coordinator = RedisCoordinator(FakeRedis())
    token = await coordinator.issue_callback(
        "checkout.final", 42, "d34db33f-0000-4000-8000-000000000000", 17, one_time=True
    )
    assert len(token.encode("utf-8")) <= 64
    with pytest.raises(AccessDenied, match="OWNER"):
        await coordinator.resolve_callback(token, 7)
    state = await coordinator.resolve_callback(token, 42)
    assert state == {
        "a": "checkout.final",
        "u": 42,
        "o": "d34db33f-0000-4000-8000-000000000000",
        "v": 17,
        "once": True,
    }
    with pytest.raises(AccessDenied, match="EXPIRED"):
        await coordinator.resolve_callback(token, 42)


@pytest.mark.asyncio
async def test_navigation_callback_can_be_reused_and_tampering_fails():
    coordinator = RedisCoordinator(FakeRedis())
    token = await coordinator.issue_callback("catalog", 42)
    assert await coordinator.resolve_callback(token, 42)
    assert await coordinator.resolve_callback(token, 42)
    with pytest.raises(AccessDenied):
        await coordinator.resolve_callback(token + "x", 42)
