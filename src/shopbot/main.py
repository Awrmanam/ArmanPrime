import asyncio

import uvicorn

from .app_v2 import create_app
from .config import settings


async def polling() -> None:
    app = create_app(settings)
    runtime = app.state.runtime
    server = uvicorn.Server(
        uvicorn.Config(app, host="0.0.0.0", port=8080, log_level="info")  # noqa: S104
    )
    health_task = asyncio.create_task(server.serve())
    try:
        await runtime.bot.delete_webhook(drop_pending_updates=False)
        await runtime.dispatcher.start_polling(runtime.bot)
    finally:
        server.should_exit = True
        await health_task


def run() -> None:
    if settings.run_mode == "webhook":
        uvicorn.run("shopbot.api:app", host="0.0.0.0", port=8080)  # noqa: S104
    else:
        asyncio.run(polling())


if __name__ == "__main__":
    run()
