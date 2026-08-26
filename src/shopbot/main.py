import asyncio

import uvicorn
from aiogram import Bot, Dispatcher

from .bot import build_router
from .config import settings
from .security import CallbackSigner, Vault
from .store import ApplicationStore


async def polling() -> None:
    token = settings.bot_token.get_secret_value()
    if not token:
        raise RuntimeError("BOT_TOKEN is required")
    bot = Bot(token)
    encryption_key = settings.encryption_key.get_secret_value().encode()
    callback_key = settings.callback_key.get_secret_value().encode()
    if not encryption_key or not callback_key:
        raise RuntimeError("ENCRYPTION_KEY and CALLBACK_KEY are required")
    store = ApplicationStore(settings.admin_telegram_user_id, Vault({"v1": encryption_key}, "v1"))
    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(store, CallbackSigner(callback_key)))
    try:
        await dispatcher.start_polling(bot)
    finally:
        await bot.session.close()


def run() -> None:
    if settings.run_mode == "webhook":
        uvicorn.run("shopbot.api:app", host="0.0.0.0", port=8080)  # noqa: S104
    else:
        asyncio.run(polling())


if __name__ == "__main__":
    run()
