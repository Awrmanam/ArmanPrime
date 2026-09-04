import asyncio

from redis.asyncio import Redis

from .config import settings
from .db import create_engine_and_session
from .fx import NavasanRateProvider
from .repository import RedisCoordinator, ShopRepository
from .security import Vault

CURRENCIES = {"USD", "RUB", "EUR", "TRY", "AED", "GBP", "CNY", "INR", "SGD", "EGP"}


async def bootstrap() -> None:
    if settings.fx_provider != "navasan":
        print("FX manual emergency mode enabled")
        return
    key = settings.navasan_api_key.get_secret_value()
    if not key:
        raise SystemExit("Navasan configuration is incomplete")
    engine, sessions = create_engine_and_session(settings.database_url)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        repo = ShopRepository(
            sessions,
            RedisCoordinator(redis),
            Vault({"v1": settings.encryption_key.get_secret_value().encode()}, "v1"),
            settings.hmac_key.get_secret_value().encode(),
            settings.admin_telegram_user_id,
            settings.order_notification_chat_id,
        )
        provider = NavasanRateProvider(
            key,
            settings.navasan_base_url,
            connect_timeout=settings.fx_http_connect_timeout,
            read_timeout=settings.fx_http_read_timeout,
            retry_limit=settings.fx_retry_limit,
        )
        count = await repo.refresh_currency_rates(provider, CURRENCIES, settings.fx_max_age_minutes)
        if count != len(CURRENCIES):
            raise SystemExit("Initial currency refresh was incomplete")
        print(f"Validated and persisted {count} initial currency rates")
    finally:
        await redis.aclose()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(bootstrap())
