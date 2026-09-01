from types import SimpleNamespace
from uuid import uuid4

import pytest
from aiogram import Router

import shopbot.enhanced as enhanced
import shopbot.variant_router as variant_router_module


class FakeRedis:
    def __init__(self):
        self.values = {}

    async def set(self, key, value, ex=None):
        self.values[key] = str(value)
        return True

    async def get(self, key):
        return self.values.get(key)

    async def delete(self, *keys):
        removed = 0
        for key in keys:
            if key in self.values:
                removed += 1
                del self.values[key]
        return removed


class FakeCoordinator:
    def __init__(self, redis):
        self.redis = redis

    async def issue_callback(
        self,
        action,
        actor_id,
        object_id="",
        version=1,
        *,
        one_time=False,
        ttl=1800,
    ):
        return f"legacy:{action}:{actor_id}:{object_id}"


class FakeRepo:
    def __init__(self):
        self.coordinator = FakeCoordinator(FakeRedis())


class FakeStore:
    def __init__(self, actor_id, checkout_id):
        self.actor_id = actor_id
        self.checkout_id = checkout_id
        self.waiting_calls = []
        self.quote_calls = []

    async def mark_waiting_gate(self, checkout_id):
        self.waiting_calls.append(checkout_id)

    async def checkout(self, checkout_id, telegram_id=None):
        if checkout_id != self.checkout_id:
            return None
        if telegram_id is not None and telegram_id != self.actor_id:
            return None
        return {"id": checkout_id, "telegram_id": self.actor_id, "status": "WAITING_GATE"}

    async def create_quote(self, checkout_id, telegram_id, card_id):
        self.quote_calls.append((checkout_id, telegram_id, card_id))
        return SimpleNamespace(id=uuid4())

    async def pending_for_legacy_product(self, telegram_id, legacy_product_id):
        return None

    async def issue_callback(
        self,
        action,
        actor_id,
        object_id="",
        *,
        one_time=False,
        ttl=1800,
    ):
        return f"variant:{action}:{actor_id}:{object_id}"


@pytest.mark.asyncio
async def test_verification_gate_keeps_exact_variant_checkout(monkeypatch):
    actor_id = 200
    checkout_id = uuid4()
    legacy_product_id = uuid4()
    repo = FakeRepo()
    store = FakeStore(actor_id, checkout_id)
    runtime = SimpleNamespace(repo=repo, bot=object(), dispatcher=None)
    app = SimpleNamespace(state=SimpleNamespace(runtime=runtime))

    monkeypatch.setattr(enhanced.runtime_module, "create_app", lambda settings: app)
    monkeypatch.setattr(
        enhanced.runtime_module,
        "answer_keyboard",
        enhanced.runtime_module.answer_keyboard,
    )
    monkeypatch.setattr(enhanced, "VariantStore", lambda value: store)
    monkeypatch.setattr(enhanced, "_install_transport_patch", lambda: None)
    monkeypatch.setattr(
        enhanced.runtime_module,
        "persistent_router",
        lambda value: Router(name="legacy-resume-test"),
    )
    monkeypatch.setattr(
        variant_router_module,
        "build_variant_router",
        lambda value, variant_store: Router(name="variant-resume-test"),
    )

    enhanced.create_app(SimpleNamespace())

    await store.mark_waiting_gate(checkout_id)
    pending_key = f"pending-variant-checkout:{actor_id}"
    assert await repo.coordinator.redis.get(pending_key) == str(checkout_id)

    resume = await repo.coordinator.issue_callback(
        "resume_checkout", actor_id, str(legacy_product_id), one_time=True
    )
    assert resume == f"variant:resume:{actor_id}:{checkout_id}"

    cards = await repo._legacy_issue_callback("begin_card", actor_id)
    assert cards == f"legacy:customer.cards:{actor_id}:"

    legacy_key = f"pending-checkout:{actor_id}"
    await repo.coordinator.redis.set(legacy_key, str(legacy_product_id), ex=86400)
    await store.create_quote(checkout_id, actor_id, None)
    assert await repo.coordinator.redis.get(pending_key) is None
    assert await repo.coordinator.redis.get(legacy_key) is None
