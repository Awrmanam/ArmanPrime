import asyncio
import importlib
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import shopbot.runtime as runtime_module
from shopbot.config import Settings


class Session:
    def __init__(self, fail=False):
        self.fail = fail

    async def execute(self, _):
        if self.fail:
            raise RuntimeError("database unavailable")


class Sessions:
    def __init__(self, fail=False):
        self.fail = fail

    @asynccontextmanager
    async def __call__(self):
        yield Session(self.fail)


class RuntimeFake:
    def __init__(self, settings, *, db_fail=False, redis_ok=True):
        self.settings = settings
        self.repo = SimpleNamespace(
            sessions=Sessions(db_fail),
            now=lambda: __import__("datetime").datetime.now(__import__("datetime").UTC),
            expire_quotes=AsyncMock(return_value=0),
        )
        self.redis = AsyncMock()
        self.redis.ping.return_value = redis_ok
        self.bot = AsyncMock()
        self.dispatcher = AsyncMock()
        self.worker = None
        self.closed = False

    async def outbox_worker(self):
        await asyncio.Event().wait()

    async def close(self):
        if self.worker:
            self.worker.cancel()
            try:
                await self.worker
            except asyncio.CancelledError:
                pass
        self.closed = True


def settings(mode="polling"):
    return Settings(
        bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",  # noqa: S106
        encryption_key="unused",
        hmac_key="unused",
        callback_key="unused",
        run_mode=mode,
        webhook_url="https://example.invalid/telegram/webhook",
        webhook_secret="secret",  # noqa: S106
    )


def app_with(monkeypatch, fake):
    monkeypatch.setattr(runtime_module, "Runtime", lambda _: fake)
    return runtime_module.create_app(fake.settings)


def test_health_live_ready_and_dependency_failures(monkeypatch):
    fake = RuntimeFake(settings())
    with TestClient(app_with(monkeypatch, fake)) as client:
        assert client.get("/health/live").json() == {"status": "ok"}
        assert client.get("/health/ready").status_code == 200
        fake.redis.ping.return_value = False
        assert client.get("/health/ready").status_code == 503
        fake.redis.ping.return_value = True
        fake.repo.sessions.fail = True
        assert client.get("/health/ready").status_code == 503
    assert fake.closed


def test_webhook_secret_dispatch_and_lifecycle(monkeypatch):
    fake = RuntimeFake(settings("webhook"))
    with TestClient(app_with(monkeypatch, fake)) as client:
        assert client.post("/telegram/webhook", json={"update_id": 1}).status_code == 403
        assert (
            client.post(
                "/telegram/webhook",
                json={"update_id": 1},
                headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
            ).status_code
            == 403
        )
        response = client.post(
            "/telegram/webhook",
            json={"update_id": 1},
            headers={"X-Telegram-Bot-Api-Secret-Token": "secret"},
        )
        assert response.status_code == 200
        fake.dispatcher.feed_update.assert_awaited_once()
        fake.bot.set_webhook.assert_awaited_once()
    fake.bot.delete_webhook.assert_awaited_once()


def test_api_module_builds_application(monkeypatch):
    marker = object()
    monkeypatch.setattr(runtime_module, "create_app", lambda _: marker)
    sys.modules.pop("shopbot.api", None)
    module = importlib.import_module("shopbot.api")
    assert module.app is marker


@pytest.mark.asyncio
async def test_polling_starts_dispatcher_and_health_lifecycle(monkeypatch):
    import shopbot.main as main

    fake = RuntimeFake(settings())
    app = SimpleNamespace(state=SimpleNamespace(runtime=fake))

    class Server:
        def __init__(self, _):
            self.should_exit = False

        async def serve(self):
            while not self.should_exit:
                await asyncio.sleep(0)

    monkeypatch.setattr(main, "create_app", lambda _: app)
    monkeypatch.setattr(main.uvicorn, "Config", lambda *args, **kwargs: object())
    monkeypatch.setattr(main.uvicorn, "Server", Server)
    await main.polling()
    fake.bot.delete_webhook.assert_awaited_once_with(drop_pending_updates=False)
    fake.dispatcher.start_polling.assert_awaited_once_with(fake.bot)
