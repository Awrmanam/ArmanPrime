from __future__ import annotations

import asyncio
import contextlib
import logging
from hashlib import sha256
from uuid import UUID

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, Update
from fastapi import FastAPI, Header, HTTPException, Request, status
from redis.asyncio import Redis
from sqlalchemy import text

from .config import Settings
from .db import create_engine_and_session
from .repository import AccessDenied, RedisCoordinator, ShopRepository
from .security import CallbackSigner, Vault
from .telegram_adapter import Button

log = logging.getLogger(__name__)


def markup(rows: list[list[Button]]) -> InlineKeyboardMarkup:
    # aiogram currently drops new Bot API button fields, so validate centrally and preserve payload.
    return InlineKeyboardMarkup.model_validate(
        {"inline_keyboard": [[button.payload() for button in row] for row in rows]}
    )


def persistent_router(repo: ShopRepository, signer: CallbackSigner) -> Router:
    router = Router(name="persistent-commerce")

    @router.message(Command("start"))
    async def start(message: Message) -> None:
        if not await repo.coordinator.rate_limit("start", message.from_user.id, 10, 60):
            await message.answer("درخواست‌های شما بیش از حد مجاز است.")
            return
        async with repo.sessions.begin() as session:
            await repo.user(message.from_user.id, session)
            terms = await repo.current_terms(session)
        if not terms:
            await message.answer("فروشگاه هنوز راه‌اندازی نشده است.")
            return
        token = signer.sign(
            __import__("shopbot.security", fromlist=["Callback"]).Callback(
                "consent", str(terms.id), terms.version
            )
        )
        await message.answer(
            f"{terms.title}\n\n{terms.pages[0]}",
            reply_markup=markup([[Button("تأیید قوانین", token, "success")]]),
        )

    @router.callback_query(F.data.startswith("1."))
    async def callback(query: CallbackQuery) -> None:
        try:
            parsed = signer.verify(query.data)
            digest = sha256(query.data.encode()).hexdigest()
            if not await repo.coordinator.consume_callback(digest):
                await query.answer("این درخواست قبلاً استفاده شده است.", show_alert=True)
                return
            if parsed.action == "consent":
                await repo.accept_terms(query.from_user.id, UUID(parsed.object_id))
                await query.message.answer("پذیرش قوانین ثبت شد.")
            await query.answer()
        except Exception:
            log.exception("callback rejected", extra={"telegram_id": query.from_user.id})
            await query.answer("درخواست معتبر نیست.", show_alert=True)

    @router.message(Command("admin"))
    async def admin(message: Message) -> None:
        try:
            repo.owner(message.from_user.id)
        except AccessDenied:
            await message.answer("دسترسی مجاز نیست.")
            return
        await message.answer(
            "پنل مدیریت",
            reply_markup=markup(
                [
                    [Button("قوانین", "admin:terms", "primary")],
                    [Button("احراز هویت", "admin:kyc", "default")],
                    [Button("کارت‌ها", "admin:cards", "default")],
                    [Button("سفارش‌ها", "admin:orders", "success")],
                    [Button("بازگشت", "admin:close", "danger")],
                ]
            ),
        )

    @router.message(Command("receipt"), F.photo | F.document)
    async def receipt(message: Message) -> None:
        if not await repo.coordinator.rate_limit("receipt", message.from_user.id, 5, 300):
            await message.answer("تعداد ارسال رسید بیش از حد مجاز است.")
            return
        try:
            order_id = UUID((message.caption or "").strip())
            obj = message.photo[-1] if message.photo else message.document
            await repo.submit_receipt(
                message.from_user.id,
                order_id,
                obj.file_id,
                obj.file_unique_id,
                "photo" if message.photo else "document",
            )
            await message.answer("رسید برای بررسی ثبت شد؛ رسید به‌تنهایی اثبات پرداخت نیست.")
        except (ValueError, AccessDenied):
            await message.answer("رسید یا شناسه سفارش معتبر نیست.")
        except Exception:
            log.exception("receipt submission failed", extra={"telegram_id": message.from_user.id})
            await message.answer("ثبت رسید انجام نشد؛ بعداً دوباره تلاش کنید.")

    return router


class Runtime:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.engine, sessions = create_engine_and_session(settings.database_url)
        self.redis = Redis.from_url(settings.redis_url, decode_responses=True)
        self.bot = Bot(settings.bot_token.get_secret_value())
        self.dispatcher = Dispatcher()
        vault = Vault({"v1": settings.encryption_key.get_secret_value().encode()}, "v1")
        self.repo = ShopRepository(
            sessions,
            RedisCoordinator(self.redis),
            vault,
            settings.hmac_key.get_secret_value().encode(),
            settings.admin_telegram_user_id,
            settings.order_notification_chat_id,
        )
        self.dispatcher.include_router(
            persistent_router(
                self.repo, CallbackSigner(settings.callback_key.get_secret_value().encode())
            )
        )
        self.worker: asyncio.Task | None = None

    async def outbox_worker(self) -> None:
        from sqlalchemy import select

        from .db import OutboxRow

        while True:
            try:
                async with self.repo.sessions.begin() as session:
                    row = await session.scalar(
                        select(OutboxRow)
                        .where(
                            OutboxRow.sent_at.is_(None), OutboxRow.available_at <= self.repo.now()
                        )
                        .order_by(OutboxRow.available_at)
                        .with_for_update(skip_locked=True)
                        .limit(1)
                    )
                    if row:
                        await self.bot.send_message(
                            row.chat_id, f"{row.kind}\nOrder ID: {row.payload['order_id']}"
                        )
                        row.sent_at = self.repo.now()
                await self.repo.expire_quotes()
            except Exception:
                log.exception("background worker iteration failed")
            await asyncio.sleep(2)

    async def close(self) -> None:
        if self.worker:
            self.worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.worker
        await self.bot.session.close()
        await self.redis.aclose()
        await self.engine.dispose()


def create_app(settings: Settings) -> FastAPI:
    runtime = Runtime(settings)
    app = FastAPI(title="Telegram commerce core", docs_url=None, redoc_url=None)
    app.state.runtime = runtime

    @app.on_event("startup")
    async def startup() -> None:
        runtime.worker = asyncio.create_task(runtime.outbox_worker())
        if settings.run_mode == "webhook":
            await runtime.bot.set_webhook(
                settings.webhook_url, secret_token=settings.webhook_secret.get_secret_value()
            )

    @app.on_event("shutdown")
    async def shutdown() -> None:
        if settings.run_mode == "webhook":
            await runtime.bot.delete_webhook()
        await runtime.close()

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready() -> dict[str, str]:
        try:
            async with runtime.repo.sessions() as session:
                await session.execute(text("SELECT 1"))
            if not await runtime.redis.ping():
                raise RuntimeError("redis unavailable")
            if not settings.bot_token.get_secret_value():
                raise RuntimeError("bot not configured")
        except Exception as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "dependencies unavailable"
            ) from exc
        return {"status": "ready"}

    @app.post("/telegram/webhook")
    async def webhook(
        request: Request, x_telegram_bot_api_secret_token: str | None = Header(default=None)
    ) -> dict[str, bool]:
        expected = settings.webhook_secret.get_secret_value()
        if not expected or x_telegram_bot_api_secret_token != expected:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
        update = Update.model_validate(await request.json(), context={"bot": runtime.bot})
        await runtime.dispatcher.feed_update(runtime.bot, update)
        return {"ok": True}

    return app
