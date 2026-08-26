from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import timedelta
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
from .security import Vault
from .telegram_adapter import Button

log = logging.getLogger(__name__)


def markup(rows: list[list[Button]]) -> InlineKeyboardMarkup:
    # aiogram currently drops new Bot API button fields, so validate centrally and preserve payload.
    return InlineKeyboardMarkup.model_validate(
        {"inline_keyboard": [[button.payload() for button in row] for row in rows]}
    )


def persistent_router(repo: ShopRepository) -> Router:
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
        token = await repo.coordinator.issue_callback(
            "consent",
            message.from_user.id,
            str(terms.id),
            terms.version,
            one_time=True,
        )
        await message.answer(
            f"{terms.title}\n\n{terms.pages[0]}",
            reply_markup=markup([[Button("تأیید قوانین", token, "success")]]),
        )

    @router.callback_query(F.data.startswith("c1."))
    async def callback(query: CallbackQuery) -> None:
        try:
            state = await repo.coordinator.resolve_callback(query.data, query.from_user.id)
            if state["a"] == "consent":
                await repo.accept_terms(query.from_user.id, UUID(state["o"]))
                home_token = await repo.coordinator.issue_callback("catalog", query.from_user.id)
                await query.message.answer(
                    "پذیرش قوانین ثبت شد.",
                    reply_markup=markup([[Button("مشاهده محصولات", home_token, "primary")]]),
                )
            elif state["a"] == "catalog":
                rows = []
                for category in await repo.categories():
                    token = await repo.coordinator.issue_callback(
                        "category", query.from_user.id, str(category.id)
                    )
                    rows.append(
                        [Button(category.title, token, "default", category.custom_emoji_id)]
                    )
                await query.message.answer(
                    "دسته‌بندی‌ها" if rows else "دسته فعالی وجود ندارد.",
                    reply_markup=markup(rows) if rows else None,
                )
            elif state["a"] == "category":
                rows = []
                for product in await repo.products(UUID(state["o"])):
                    token = await repo.coordinator.issue_callback(
                        "product", query.from_user.id, str(product.id)
                    )
                    rows.append([Button(product.title, token, "default", product.custom_emoji_id)])
                await query.message.answer(
                    "محصولات" if rows else "محصول فعالی وجود ندارد.",
                    reply_markup=markup(rows) if rows else None,
                )
            elif state["a"] == "product":
                product = await repo.product(UUID(state["o"]))
                if not product:
                    raise AccessDenied("PRODUCT_NOT_FOUND")
                buy = await repo.coordinator.issue_callback(
                    "buy", query.from_user.id, str(product.id)
                )
                await query.message.answer(
                    f"{product.title}\n\n{product.description}\nمدت: {product.duration or '-'}\n"
                    f"پلن: {product.plan_type or '-'}\n"
                    f"فعال‌سازی: {product.activation_method or '-'}\n"
                    f"گارانتی: {product.warranty_text or '-'}\n"
                    f"زمان تحویل: {product.delivery_minutes} دقیقه",
                    reply_markup=markup([[Button("خرید", buy, "success")]]),
                )
            elif state["a"] == "buy":
                rows = []
                for card in await repo.verified_cards(query.from_user.id):
                    token = await repo.coordinator.issue_callback(
                        "quote", query.from_user.id, f"{state['o']}:{card.id}", one_time=True
                    )
                    rows.append([Button(f"{card.bank_name} — {card.masked_pan}", token)])
                await query.message.answer(
                    "کارت مبدأ تأییدشده را انتخاب کنید."
                    if rows
                    else "برای خرید، KYC و کارت بانکی تأییدشده لازم است.",
                    reply_markup=markup(rows) if rows else None,
                )
            elif state["a"] == "quote":
                product_id, card_id = map(UUID, state["o"].split(":"))
                quote = await repo.create_quote(query.from_user.id, product_id, card_id)
                final = await repo.coordinator.issue_callback(
                    "final", query.from_user.id, str(quote.id), quote.version, one_time=True
                )
                await query.message.answer(
                    f"چک نهایی\n{quote.snapshot['title']}\nمبلغ: {quote.final_toman} تومان\n"
                    "اعتبار قیمت: ۳۰ دقیقه",
                    reply_markup=markup([[Button("تأیید و ادامه", final, "success")]]),
                )
            elif state["a"] == "final":
                order = await repo.final_check(query.from_user.id, UUID(state["o"]))
                pan, holder = await repo.reveal_destination(query.from_user.id, order.id)
                await repo.coordinator.redis.set(
                    f"receipt-order:{query.from_user.id}", str(order.id), ex=1800
                )
                await query.message.answer(
                    f"کارت مقصد: {pan}\nصاحب کارت: {holder}\nمبلغ: {order.amount_toman} تومان\n"
                    "اکنون تصویر یا فایل رسید را ارسال کنید. رسید به‌تنهایی اثبات پرداخت نیست."
                )
            elif state["a"] in {"admin.kyc", "admin.cards", "admin.orders", "admin.audit"}:
                repo.owner(query.from_user.id)
                rows = []
                if state["a"] == "admin.kyc":
                    for item in await repo.kyc_queue(query.from_user.id):
                        approve = await repo.coordinator.issue_callback(
                            "admin.kyc.approve", query.from_user.id, str(item.id), one_time=True
                        )
                        reject = await repo.coordinator.issue_callback(
                            "admin.kyc.reject", query.from_user.id, str(item.id), one_time=True
                        )
                        rows.extend(
                            [
                                [Button(f"تأیید KYC {item.id}", approve, "success")],
                                [Button(f"رد KYC {item.id}", reject, "danger")],
                            ]
                        )
                elif state["a"] == "admin.cards":
                    for item in await repo.card_queue(query.from_user.id):
                        approve = await repo.coordinator.issue_callback(
                            "admin.card.approve", query.from_user.id, str(item.id), one_time=True
                        )
                        reject = await repo.coordinator.issue_callback(
                            "admin.card.reject", query.from_user.id, str(item.id), one_time=True
                        )
                        rows.extend(
                            [
                                [
                                    Button(
                                        f"تأیید {item.bank_name} — {item.masked_pan}",
                                        approve,
                                        "success",
                                    )
                                ],
                                [Button("رد کارت", reject, "danger")],
                            ]
                        )
                elif state["a"] == "admin.orders":
                    for item in await repo.order_queue(query.from_user.id):
                        payment = await repo.payment_for_order(query.from_user.id, item.id)
                        if payment and payment.receipt_file_id:
                            await query.message.answer_photo(
                                payment.receipt_file_id,
                                caption=f"Order ID: {item.id}\nوضعیت: {item.status}\n"
                                "رسید به‌تنهایی اثبات پرداخت نیست.",
                            )
                        approve = await repo.coordinator.issue_callback(
                            "admin.payment.approve", query.from_user.id, str(item.id), one_time=True
                        )
                        reject = await repo.coordinator.issue_callback(
                            "admin.payment.reject", query.from_user.id, str(item.id), one_time=True
                        )
                        rows.extend(
                            [
                                [Button(f"تأیید پرداخت {item.id}", approve, "success")],
                                [Button("رد پرداخت", reject, "danger")],
                            ]
                        )
                else:
                    events = await repo.audit_events(query.from_user.id)
                    text = (
                        "\n".join(
                            f"{item.at.isoformat()} | {item.action} | {item.target}"
                            for item in events
                        )
                        or "رویدادی وجود ندارد."
                    )
                    await query.message.answer(text)
                if rows:
                    await query.message.answer("صف بررسی", reply_markup=markup(rows))
                elif state["a"] != "admin.audit":
                    await query.message.answer("صف بررسی خالی است.")
            elif state["a"] == "admin.terms":
                repo.owner(query.from_user.id)
                await repo.coordinator.redis.set(
                    f"fsm:{query.from_user.id}", "admin.terms.title", ex=900
                )
                await query.message.answer("عنوان نسخه جدید قوانین را ارسال کنید.")
            elif state["a"].startswith(("admin.kyc.", "admin.card.", "admin.payment.")):
                repo.owner(query.from_user.id)
                await repo.coordinator.redis.set(
                    f"fsm:{query.from_user.id}",
                    f"{state['a']}:{state['o']}",
                    ex=900,
                )
                await query.message.answer("دلیل تصمیم دستی را ارسال کنید.")
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
        actions = []
        for action in ("terms", "kyc", "cards", "orders", "audit", "close"):
            actions.append(
                await repo.coordinator.issue_callback(
                    f"admin.{action}", message.from_user.id, one_time=False
                )
            )
        await message.answer(
            "پنل مدیریت",
            reply_markup=markup(
                [
                    [Button("قوانین", actions[0], "primary")],
                    [Button("احراز هویت", actions[1], "default")],
                    [Button("کارت‌ها", actions[2], "default")],
                    [Button("سفارش‌ها", actions[3], "success")],
                    [Button("Audit", actions[4], "default")],
                    [Button("بازگشت", actions[5], "danger")],
                ]
            ),
        )

    @router.message(Command("kyc"))
    async def begin_kyc(message: Message) -> None:
        if not await repo.coordinator.rate_limit("kyc", message.from_user.id, 3, 3600):
            await message.answer("تعداد درخواست‌های KYC بیش از حد مجاز است.")
            return
        await repo.coordinator.redis.set(f"fsm:{message.from_user.id}", "kyc.document", ex=900)
        await message.answer("تصویر یا فایل مدرک هویتی را ارسال کنید.")

    @router.message(Command("card"))
    async def begin_card(message: Message) -> None:
        if not await repo.coordinator.rate_limit("card", message.from_user.id, 3, 3600):
            await message.answer("تعداد درخواست‌های کارت بیش از حد مجاز است.")
            return
        await repo.coordinator.redis.set(f"fsm:{message.from_user.id}", "card.bank", ex=900)
        await message.answer("نام بانک را بدون اطلاعات محرمانه ارسال کنید.")

    @router.message(F.text)
    async def form_text(message: Message) -> None:
        state = await repo.coordinator.redis.get(f"fsm:{message.from_user.id}")
        if state == "admin.terms.title":
            try:
                repo.owner(message.from_user.id)
                await repo.coordinator.redis.set(
                    f"terms-title:{message.from_user.id}", message.text, ex=900
                )
                await repo.coordinator.redis.set(
                    f"fsm:{message.from_user.id}", "admin.terms.body", ex=900
                )
                await message.answer("متن قوانین را ارسال کنید.")
            except AccessDenied:
                await message.answer("دسترسی مجاز نیست.")
        elif state == "admin.terms.body":
            try:
                repo.owner(message.from_user.id)
                title = await repo.coordinator.redis.get(f"terms-title:{message.from_user.id}")
                if not title:
                    raise ValueError("FORM_EXPIRED")
                terms = await repo.publish_terms(message.from_user.id, title, message.text)
                await repo.coordinator.redis.delete(
                    f"fsm:{message.from_user.id}", f"terms-title:{message.from_user.id}"
                )
                await message.answer(f"نسخه {terms.version} قوانین منتشر شد.")
            except Exception:
                log.exception("terms publication failed")
                await message.answer("انتشار قوانین انجام نشد.")
        elif state and state.startswith("admin."):
            try:
                repo.owner(message.from_user.id)
                action, object_id = state.rsplit(":", 1)
                approved = action.endswith(".approve")
                if action.startswith("admin.kyc."):
                    await repo.review_kyc(
                        message.from_user.id, UUID(object_id), approved, message.text
                    )
                elif action.startswith("admin.card."):
                    await repo.review_card(
                        message.from_user.id, UUID(object_id), approved, message.text
                    )
                elif action.startswith("admin.payment."):
                    await repo.manual_reconcile(
                        message.from_user.id, UUID(object_id), approved, message.text
                    )
                await repo.coordinator.redis.delete(f"fsm:{message.from_user.id}")
                await message.answer("تصمیم دستی ثبت و Audit شد.")
            except Exception:
                log.exception("admin decision failed", extra={"telegram_id": message.from_user.id})
                await message.answer("ثبت تصمیم انجام نشد.")
        elif state == "card.bank":
            await repo.coordinator.redis.set(
                f"card-bank:{message.from_user.id}", message.text.strip(), ex=900
            )
            await repo.coordinator.redis.set(f"fsm:{message.from_user.id}", "card.pan", ex=900)
            await message.answer(
                "شماره ۱۶ رقمی کارت را ارسال کنید. CVV2، PIN، OTP یا رمز ارسال نکنید. "
                "پیام شماره کارت پس از پردازش حذف می‌شود."
            )
        elif state == "card.pan":
            digits = "".join(item for item in message.text if item.isdigit())
            if len(digits) != 16:
                await message.answer("شماره کارت باید دقیقاً ۱۶ رقم باشد.")
                return
            encrypted = repo.vault.encrypt(digits)
            await repo.coordinator.redis.set(f"card-pan:{message.from_user.id}", encrypted, ex=900)
            await repo.coordinator.redis.set(f"fsm:{message.from_user.id}", "card.evidence", ex=900)
            with contextlib.suppress(Exception):
                await message.delete()
            await message.answer("تصویر یا فایل مدرک مالکیت کارت را ارسال کنید.")

    @router.message(F.photo | F.document)
    async def uploaded_file(message: Message) -> None:
        if not await repo.coordinator.rate_limit("receipt", message.from_user.id, 5, 300):
            await message.answer("تعداد ارسال فایل بیش از حد مجاز است.")
            return
        try:
            stored_order = await repo.coordinator.redis.get(f"receipt-order:{message.from_user.id}")
            obj = message.photo[-1] if message.photo else message.document
            file_type = "photo" if message.photo else "document"
            state = await repo.coordinator.redis.get(f"fsm:{message.from_user.id}")
            if stored_order:
                await repo.submit_receipt(
                    message.from_user.id,
                    UUID(stored_order),
                    obj.file_id,
                    obj.file_unique_id,
                    file_type,
                )
                await repo.coordinator.redis.delete(f"receipt-order:{message.from_user.id}")
                await message.answer("رسید ثبت شد؛ رسید به‌تنهایی اثبات پرداخت نیست.")
            elif state == "kyc.document":
                await repo.submit_kyc(
                    message.from_user.id, obj.file_id, obj.file_unique_id, file_type
                )
                await repo.coordinator.redis.delete(f"fsm:{message.from_user.id}")
                await message.answer("مدرک KYC برای بررسی دستی ثبت شد.")
            elif state == "card.evidence":
                bank = await repo.coordinator.redis.get(f"card-bank:{message.from_user.id}")
                envelope = await repo.coordinator.redis.get(f"card-pan:{message.from_user.id}")
                if not bank or not envelope:
                    raise ValueError("CARD_FORM_EXPIRED")
                card = await repo.submit_customer_card(
                    message.from_user.id, bank, repo.vault.decrypt(envelope), obj.file_id
                )
                await repo.coordinator.redis.delete(
                    f"fsm:{message.from_user.id}",
                    f"card-bank:{message.from_user.id}",
                    f"card-pan:{message.from_user.id}",
                )
                await message.answer(
                    f"کارت {card.bank_name} — {card.masked_pan} برای بررسی ثبت شد."
                )
            else:
                await message.answer("برای این فایل درخواست فعالی وجود ندارد.")
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
        self.dispatcher.include_router(persistent_router(self.repo))
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
                            OutboxRow.sent_at.is_(None),
                            OutboxRow.dead_at.is_(None),
                            OutboxRow.available_at <= self.repo.now(),
                        )
                        .order_by(OutboxRow.available_at)
                        .with_for_update(skip_locked=True)
                        .limit(1)
                    )
                    if row:
                        try:
                            if row.payload.get("receipt_file_id"):
                                await self.bot.send_photo(
                                    row.chat_id,
                                    row.payload["receipt_file_id"],
                                    caption=f"{row.kind}\nOrder ID: {row.payload['order_id']}\n"
                                    "رسید به‌تنهایی اثبات پرداخت نیست.",
                                )
                            else:
                                body = f"{row.kind}\nOrder ID: {row.payload['order_id']}"
                                if row.kind == "ORDER_DELIVERED":
                                    body += f"\n\n{row.payload['content']}"
                                    if row.payload.get("activation_link"):
                                        body += f"\n{row.payload['activation_link']}"
                                await self.bot.send_message(
                                    row.chat_id,
                                    body,
                                )
                            row.sent_at = self.repo.now()
                        except Exception as exc:
                            row.attempts += 1
                            row.last_error = type(exc).__name__
                            if row.attempts >= 8:
                                row.dead_at = self.repo.now()
                            else:
                                delay = min(300, 2 ** min(row.attempts, 8))
                                row.available_at = self.repo.now() + timedelta(seconds=delay)
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
