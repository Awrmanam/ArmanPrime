from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from shopbot import fx_bootstrap


class Secret:
    def __init__(self, value: str):
        self.value = value

    def get_secret_value(self) -> str:
        return self.value


def bootstrap_settings(provider: str = "navasan", key: str = "test-api-key") -> SimpleNamespace:
    return SimpleNamespace(
        fx_provider=provider,
        navasan_api_key=Secret(key),
        database_url="postgresql+asyncpg://test",
        redis_url="redis://test",
        encryption_key=Secret("vault-key"),
        hmac_key=Secret("hmac-key"),
        admin_telegram_user_id=100,
        order_notification_chat_id=-100,
        navasan_base_url="https://api.navasan.test/latest/",
        fx_http_connect_timeout=2,
        fx_http_read_timeout=3,
        fx_retry_limit=2,
        fx_max_age_minutes=720,
    )


@pytest.mark.asyncio
async def test_bootstrap_manual_mode_does_not_open_dependencies(monkeypatch, capsys):
    monkeypatch.setattr(fx_bootstrap, "settings", bootstrap_settings("manual", ""))
    engine_factory = Mock()
    monkeypatch.setattr(fx_bootstrap, "create_engine_and_session", engine_factory)

    await fx_bootstrap.bootstrap()

    assert "manual emergency mode" in capsys.readouterr().out
    engine_factory.assert_not_called()


@pytest.mark.asyncio
async def test_bootstrap_navasan_requires_api_key(monkeypatch):
    monkeypatch.setattr(fx_bootstrap, "settings", bootstrap_settings(key=""))

    with pytest.raises(SystemExit, match="Navasan configuration is incomplete"):
        await fx_bootstrap.bootstrap()


@pytest.mark.asyncio
@pytest.mark.parametrize(("persisted", "fails"), ((10, False), (9, True)))
async def test_bootstrap_persists_all_rates_and_always_closes_resources(
    monkeypatch, capsys, persisted, fails
):
    settings = bootstrap_settings()
    monkeypatch.setattr(fx_bootstrap, "settings", settings)
    engine = SimpleNamespace(dispose=AsyncMock())
    sessions = object()
    monkeypatch.setattr(
        fx_bootstrap, "create_engine_and_session", Mock(return_value=(engine, sessions))
    )
    redis = SimpleNamespace(aclose=AsyncMock())
    redis_factory = Mock(return_value=redis)
    monkeypatch.setattr(fx_bootstrap, "Redis", SimpleNamespace(from_url=redis_factory))
    repository = SimpleNamespace(refresh_currency_rates=AsyncMock(return_value=persisted))
    repository_factory = Mock(return_value=repository)
    monkeypatch.setattr(fx_bootstrap, "ShopRepository", repository_factory)
    monkeypatch.setattr(fx_bootstrap, "RedisCoordinator", Mock(return_value="coordinator"))
    monkeypatch.setattr(fx_bootstrap, "Vault", Mock(return_value="vault"))
    provider = object()
    provider_factory = Mock(return_value=provider)
    monkeypatch.setattr(fx_bootstrap, "NavasanRateProvider", provider_factory)

    if fails:
        with pytest.raises(SystemExit, match="Initial currency refresh was incomplete"):
            await fx_bootstrap.bootstrap()
    else:
        await fx_bootstrap.bootstrap()
        assert "Validated and persisted 10 initial currency rates" in capsys.readouterr().out

    redis_factory.assert_called_once_with(settings.redis_url, decode_responses=True)
    provider_factory.assert_called_once_with(
        "test-api-key",
        settings.navasan_base_url,
        connect_timeout=2,
        read_timeout=3,
        retry_limit=2,
    )
    repository.refresh_currency_rates.assert_awaited_once_with(
        provider, fx_bootstrap.CURRENCIES, 720
    )
    assert fx_bootstrap.CURRENCIES == {
        "USD",
        "RUB",
        "EUR",
        "TRY",
        "AED",
        "GBP",
        "CNY",
        "INR",
        "SGD",
        "EGP",
    }
    redis.aclose.assert_awaited_once()
    engine.dispose.assert_awaited_once()
