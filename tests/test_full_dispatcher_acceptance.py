import os
from datetime import UTC, datetime

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, DeleteMessage, SendMessage, SendPhoto
from aiogram.types import CallbackQuery, Chat, Message, MessageEntity, PhotoSize, Update, User
from cryptography.fernet import Fernet
from redis.asyncio import Redis
from sqlalchemy import func, select, text

from shopbot.db import (
    AuditRow,
    CategoryRow,
    ConfigRow,
    DeliveryRow,
    MerchantCardRow,
    OrderRow,
    PaymentRow,
    ProductRow,
    QuoteRow,
    RateRow,
    TermsRow,
    UserRow,
    create_engine_and_session,
)
from shopbot.repository import RedisCoordinator, ShopRepository
from shopbot.runtime import Runtime, persistent_router
from shopbot.security import Vault

DATABASE_URL = os.environ.get("DATABASE_URL")
REDIS_URL = os.environ.get("REDIS_URL")


class TelegramSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.sequence = 1000

    async def close(self):
        return None

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, AnswerCallbackQuery | DeleteMessage):
            return True
        if isinstance(method, SendMessage | SendPhoto):
            self.sequence += 1
            return Message(
                message_id=self.sequence,
                date=datetime.now(UTC),
                chat=Chat(id=method.chat_id, type="private"),
                from_user=User(id=bot.id, is_bot=True, first_name="Bot"),
                text=getattr(method, "text", None) or getattr(method, "caption", None),
            ).as_(bot)
        raise AssertionError(type(method).__name__)

    async def stream_content(self, *_args, **_kwargs):
        if False:  # pragma: no cover - abstract async-generator contract
            yield b""


class TelegramDriver:
    def __init__(self, bot, dispatcher, session):
        self.bot, self.dispatcher, self.session = bot, dispatcher, session
        self.update_id = 0

    def message(self, actor, value, *, photo=None):
        entities = None
        if value and value.startswith("/"):
            entities = [MessageEntity(type="bot_command", offset=0, length=len(value))]
        return Message(
            message_id=self.update_id + 10,
            date=datetime.now(UTC),
            chat=Chat(id=actor, type="private"),
            from_user=User(id=actor, is_bot=False, first_name=f"User {actor}"),
            text=value or None,
            entities=entities,
            photo=photo,
        ).as_(self.bot)

    async def send(self, actor, value="", *, photo_unique=None):
        self.update_id += 1
        photo = None
        if photo_unique:
            photo = [
                PhotoSize(
                    file_id=f"file-{photo_unique}",
                    file_unique_id=photo_unique,
                    width=100,
                    height=100,
                )
            ]
        await self.dispatcher.feed_update(
            self.bot,
            Update(
                update_id=self.update_id,
                message=self.message(actor, value, photo=photo),
            ),
        )

    def button(self, label):
        for call in reversed(self.session.calls):
            markup = getattr(call, "reply_markup", None)
            if not markup:
                continue
            for row in markup.inline_keyboard:
                for button in row:
                    if label in button.text:
                        return button
        raise AssertionError(f"button not found: {label}")

    async def click(self, actor, label):
        button = self.button(label)
        assert len(button.callback_data.encode()) <= 64
        await self.click_data(actor, button.callback_data)
        return button.callback_data

    async def click_data(self, actor, callback_data):
        self.update_id += 1
        await self.dispatcher.feed_update(
            self.bot,
            Update(
                update_id=self.update_id,
                callback_query=CallbackQuery(
                    id=f"callback-{self.update_id}",
                    from_user=User(id=actor, is_bot=False, first_name=f"User {actor}"),
                    chat_instance=f"chat-{actor}",
                    data=callback_data,
                    message=self.message(actor, "callback"),
                ),
            ),
        )

    def message_texts(self):
        return [
            getattr(call, "text", None) or getattr(call, "caption", None) or ""
            for call in self.session.calls
            if isinstance(call, SendMessage | SendPhoto)
        ]

    def last_message_text(self):
        return self.message_texts()[-1]


@pytest.mark.asyncio
async def test_full_dispatcher_acceptance_persists_after_reconstruction():
    assert DATABASE_URL and REDIS_URL, "PostgreSQL and Redis integration services are required"
    engine, sessions = create_engine_and_session(DATABASE_URL)
    redis = Redis.from_url(REDIS_URL, decode_responses=True)
    async with engine.begin() as connection:
        tables = await connection.execute(
            text(
                "SELECT tablename FROM pg_tables WHERE schemaname='public' "
                "AND tablename <> 'alembic_version'"
            )
        )
        names = [f'"{row[0]}"' for row in tables]
        if names:
            await connection.execute(text(f"TRUNCATE {', '.join(names)} CASCADE"))
    await redis.flushdb()
    vault = Vault({"v1": Fernet.generate_key()}, "v1")
    repo = ShopRepository(sessions, RedisCoordinator(redis), vault, b"h" * 32, 100, -100)
    transport = TelegramSession()
    bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi", session=transport)
    dispatcher = Dispatcher()
    dispatcher.include_router(persistent_router(repo))
    telegram = TelegramDriver(bot, dispatcher, transport)

    await telegram.send(100, "/admin")
    await telegram.click(100, "قوانین")
    await telegram.send(100, "قوانین فروشگاه")
    await telegram.send(100, "متن لازم‌الاجرای قوانین")
    await telegram.click(100, "رد کردن")
    await telegram.click(100, "انتشار قوانین")
    await telegram.click(100, "نرخ ارزها")
    await telegram.click(100, "USD")
    await telegram.send(100, "50000")
    await telegram.click(100, "رد کردن")
    await telegram.click(100, "تأیید نرخ")
    await telegram.click(100, "قیمت‌گذاری")
    await telegram.send(100, "10")
    await telegram.click(100, "گرد کردن به ۱٬۰۰۰ تومان")
    await telegram.click(100, "تأیید و ثبت")

    await telegram.send(100, "/admin")
    await telegram.click(100, "کارت مقصد")
    await telegram.click(100, "ایجاد مورد جدید")
    await telegram.send(100, "5555555555554444")
    await telegram.send(100, "Test Bank")
    await telegram.send(100, "Test Holder")
    await telegram.click(100, "اولویت ۱")
    await telegram.click(100, "تعیین سقف")
    await telegram.send(100, "10000000")
    await telegram.click(100, "فعال")
    await telegram.click(100, "تأیید و ثبت")

    await telegram.send(100, "/admin")
    await telegram.click(100, "دسته")
    await telegram.click(100, "ایجاد مورد جدید")
    await telegram.send(100, "Category")
    await telegram.send(100, "Description")
    await telegram.click(100, "فعال")
    await telegram.click(100, "انتهای فهرست")
    await telegram.click(100, "بدون آیکون")
    await telegram.click(100, "تأیید و ثبت")

    await telegram.send(100, "/admin")
    await telegram.click(100, "محصول")
    await telegram.click(100, "ایجاد مورد جدید")
    await telegram.click(100, "Category")
    for value in ("Product", "Full description", "10"):
        await telegram.send(100, value)
    await telegram.click(100, "USD")
    await telegram.send(100, "30 days")
    await telegram.click(100, "موجودی محدود")
    await telegram.send(100, "2")
    await telegram.click(100, "لازم است")
    await telegram.click(100, "تأیید و ثبت")

    await telegram.send(200, "/start")
    consent_callback = await telegram.click(200, "تأیید قوانین")
    await telegram.click_data(200, consent_callback)
    assert getattr(transport.calls[-1], "show_alert", False) is True
    await telegram.click(200, "فروشگاه")
    category_callback = await telegram.click(200, "Category")
    await telegram.click_data(201, category_callback)
    assert getattr(transport.calls[-1], "show_alert", False) is True
    await telegram.click(200, "Product")
    await telegram.click(200, "خرید")
    assert "احراز هویت" in telegram.last_message_text()
    assert "5555555555554444" not in "\n".join(telegram.message_texts())

    await telegram.click(200, "شروع احراز هویت")
    await telegram.send(200, photo_unique="kyc-evidence")
    await telegram.send(100, "/admin")
    await telegram.click(100, "احراز هویت")
    await telegram.click(100, "تأیید احراز هویت")
    await telegram.send(100, "مدرک هویتی به‌صورت دستی بررسی شد")

    await telegram.send(200, "/start")
    await telegram.click(200, "حساب کاربری")
    await telegram.click(200, "کارت‌های بانکی من")
    await telegram.click(200, "ثبت کارت جدید")
    await telegram.send(200, "Customer Bank")
    await telegram.send(200, "4111111111111111")
    await telegram.send(200, photo_unique="card-evidence")
    assert "4111111111111111" not in "\n".join(telegram.message_texts())
    await telegram.send(100, "/admin")
    await telegram.click(100, "کارت‌ها")
    await telegram.click(100, "تأیید Customer Bank")
    await telegram.send(100, "مالکیت کارت به‌صورت دستی بررسی شد")

    await telegram.send(200, "/start")
    await telegram.click(200, "فروشگاه")
    await telegram.click(200, "Category")
    await telegram.click(200, "Product")
    await telegram.click(200, "خرید")
    await telegram.click(200, "Customer Bank")
    assert "اعتبار قیمت: ۳۰ دقیقه" in telegram.last_message_text()
    final_callback = await telegram.click(200, "تأیید و ادامه")
    assert "5555555555554444" in telegram.last_message_text()
    await telegram.click_data(200, final_callback)
    async with sessions() as session:
        assert await session.scalar(select(func.count(OrderRow.id))) == 1
    await telegram.send(200, photo_unique="receipt-evidence")

    async with sessions() as session:
        payment = await session.scalar(select(PaymentRow))
        assert payment.status == "AWAITING_RECONCILIATION"

    await telegram.send(100, "/admin")
    await telegram.click(100, "سفارش‌ها")
    await telegram.click(100, "تأیید پرداخت")
    await telegram.send(100, "واریز با بررسی دستی تطبیق داده شد")
    await telegram.send(100, "/admin")
    await telegram.click(100, "سفارش‌ها")
    await telegram.click(100, "دریافت سفارش")
    await telegram.send(100, "/admin")
    await telegram.click(100, "سفارش‌ها")
    await telegram.click(100, "ثبت تحویل")
    await telegram.send(100, "Delivery content")
    await telegram.send(100, "https://example.invalid/activate")
    async with sessions() as session:
        assert await session.scalar(select(func.count(DeliveryRow.order_id))) == 0
    await telegram.click(100, "تأیید و ثبت")

    worker = Runtime.__new__(Runtime)
    worker.repo, worker.bot = repo, bot
    while await worker.process_outbox_once():
        pass
    assert any("Delivery content" in item for item in telegram.message_texts())

    await bot.session.close()
    await redis.aclose()
    await engine.dispose()
    new_engine, new_sessions = create_engine_and_session(DATABASE_URL)
    async with new_sessions() as session:
        customer = await session.scalar(select(UserRow).where(UserRow.telegram_id == 200))
        owner_customer = await session.scalar(select(UserRow).where(UserRow.telegram_id == 100))
        owner_audits = list(
            (await session.scalars(select(AuditRow).where(AuditRow.actor_id == 100))).all()
        )
        terms = await session.scalar(select(TermsRow).where(TermsRow.published.is_(True)))
        rate = await session.scalar(select(RateRow).order_by(RateRow.created_at.desc()))
        pricing = await session.get(ConfigRow, "pricing.global")
        category = await session.scalar(select(CategoryRow))
        product = await session.scalar(select(ProductRow))
        merchant = await session.scalar(select(MerchantCardRow))
        quote = await session.scalar(select(QuoteRow))
        order = await session.scalar(select(OrderRow))
        payment = await session.scalar(select(PaymentRow))
        delivery = await session.scalar(select(DeliveryRow))
        assert customer is not None and customer.kyc_status == "VERIFIED"
        assert owner_customer is None  # Owner identity is configuration/RBAC, not a customer row.
        assert owner_audits and {item.action for item in owner_audits} >= {
            "terms.publish",
            "currency.rate",
            "pricing.update",
            "category.create",
            "product.create",
            "merchant_card.create",
        }
        assert terms.title == "قوانین فروشگاه"
        assert rate.usd_to_toman == 50_000
        assert rate.currency_code == "USD" and rate.buffer_percent == 0
        assert pricing.value["mode"] == "markup"
        assert category.title == "Category" and product.title == "Product"
        assert merchant.masked_pan == "**** 4444"
        assert order.status == "DELIVERED"
        assert payment.status == "VERIFIED"
        assert delivery.text == "Delivery content"
        persisted_non_secret_data = repr(
            [
                [(item.action, item.target, item.detail) for item in owner_audits],
                quote.snapshot,
            ]
        )
        assert "5555555555554444" not in persisted_non_secret_data
        assert "4111111111111111" not in persisted_non_secret_data
    await new_engine.dispose()
