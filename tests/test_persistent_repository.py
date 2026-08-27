import asyncio
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from shopbot.db import (
    CategoryRow,
    DeliveryRow,
    MerchantCardRow,
    OrderRow,
    OutboxRow,
    ProductRow,
    QuoteRow,
    create_engine_and_session,
)
from shopbot.repository import AccessDenied, InvalidState, RedisCoordinator, ShopRepository
from shopbot.runtime import Runtime
from shopbot.security import Vault, mask_pan

DATABASE_URL = os.environ.get("DATABASE_URL")
REDIS_URL = os.environ.get("REDIS_URL")


@pytest.fixture
async def repository():
    assert DATABASE_URL and REDIS_URL, "integration services must be configured"
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
    yield repo
    await redis.aclose()
    await engine.dispose()


async def configure_checkout(repo: ShopRepository):
    terms = await repo.publish_terms(100, "Terms", "Required terms")
    await repo.accept_terms(200, terms.id)
    kyc = await repo.submit_kyc(200, "kyc-file", "kyc-unique", "photo")
    await repo.review_kyc(100, kyc.id, True, "identity document manually reviewed")
    card = await repo.submit_customer_card(200, "Test Bank", "4111111111111111", "card-file")
    await repo.review_card(100, card.id, True, "ownership evidence manually reviewed")
    await repo.set_rate(100, 50_000)
    await repo.set_pricing(
        100,
        {
            "mode": "markup",
            "markup": "10.25",
            "platform_fee": "1.5",
            "payment_fee": "0.5",
            "warranty_reserve": "0.75",
            "fixed_cost_toman": 100,
        },
    )
    async with repo.sessions.begin() as session:
        category = CategoryRow(title="Category", description="Description", position=1)
        session.add(category)
        await session.flush()
        product = ProductRow(
            category_id=category.id,
            title="Product",
            description="Full",
            base_price_usd=Decimal("10.50"),
            duration="1 month",
            plan_type="standard",
            activation_method="link",
            warranty_text="manual warranty",
            warranty_days=7,
            delivery_minutes=60,
            stock=2,
            reserved=0,
            position=1,
        )
        session.add(product)
        merchant_pan = "5555555555554444"
        session.add(
            MerchantCardRow(
                bank_name="Merchant Bank",
                holder_name="Holder",
                encrypted_pan=repo.vault.encrypt(merchant_pan),
                masked_pan=mask_pan(merchant_pan),
                priority=1,
                daily_limit=10_000_000,
            )
        )
        await session.flush()
        return product, card


@pytest.mark.asyncio
async def test_persistent_acceptance_path_survives_new_session(repository):
    product, card = await configure_checkout(repository)
    quote = await repository.create_quote(200, product.id, card.id)
    assert quote.expires_at - quote.created_at == timedelta(minutes=30)
    order = await repository.final_check(200, quote.id)
    duplicate = await repository.final_check(200, quote.id)
    assert duplicate.id == order.id
    pan, holder = await repository.reveal_destination(200, order.id)
    assert pan == "5555555555554444" and holder == "Holder"
    payment = await repository.submit_receipt(200, order.id, "receipt", "receipt-unique", "photo")
    assert payment.status == "AWAITING_RECONCILIATION"
    await repository.manual_reconcile(100, order.id, True, "bank statement manually matched")
    assert await repository.claim(100, order.id)
    await repository.deliver(100, order.id, "Delivery content", "https://example.invalid")
    async with repository.sessions() as restarted_session:
        persisted = await restarted_session.get(OrderRow, order.id)
        delivery = await restarted_session.get(DeliveryRow, order.id)
        assert persisted.status == "DELIVERED" and delivery.text == "Delivery content"


@pytest.mark.asyncio
async def test_gates_requote_reservation_and_duplicate_receipt(repository):
    product, card = await configure_checkout(repository)
    with pytest.raises(AccessDenied):
        await repository.create_quote(300, product.id, card.id)
    quote = await repository.create_quote(200, product.id, card.id)
    order = await repository.final_check(200, quote.id)
    await repository.submit_receipt(200, order.id, "receipt", "duplicate", "photo")
    second_quote = await repository.create_quote(200, product.id, card.id)
    second_order = await repository.final_check(200, second_quote.id)
    with pytest.raises(IntegrityError):
        await repository.submit_receipt(200, second_order.id, "receipt-2", "duplicate", "document")
    async with repository.sessions.begin() as session:
        locked = await session.get(QuoteRow, quote.id, with_for_update=True)
        locked.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await repository.set_rate(100, 60_000)
    fresh = await repository.requote(200, quote.id)
    assert fresh.version == 2 and fresh.rate == 60_000 and fresh.final_toman != quote.final_toman
    with pytest.raises(AccessDenied):
        await repository.reveal_destination(200, order.id)


@pytest.mark.asyncio
async def test_claim_is_atomic(repository):
    product, card = await configure_checkout(repository)
    quote = await repository.create_quote(200, product.id, card.id)
    order = await repository.final_check(200, quote.id)
    await repository.submit_receipt(200, order.id, "receipt", "claim-receipt", "photo")
    await repository.manual_reconcile(100, order.id, True, "manual statement match")
    results = await asyncio.gather(
        repository.claim(100, order.id),
        repository.claim(100, order.id),
        return_exceptions=True,
    )
    assert sum(result is True for result in results) == 1
    assert sum(isinstance(result, InvalidState) for result in results) == 1


@pytest.mark.asyncio
async def test_concurrent_requote_is_idempotent_and_allocation_is_fixed(repository):
    product, card = await configure_checkout(repository)
    quote = await repository.create_quote(200, product.id, card.id)
    order = await repository.final_check(200, quote.id)
    first_pan, _ = await repository.reveal_destination(200, order.id)
    async with repository.sessions.begin() as session:
        merchant_id = (await session.get(OrderRow, order.id)).merchant_card_id
        merchant = await session.get(MerchantCardRow, merchant_id, with_for_update=True)
        merchant.active = False
        session.add(
            MerchantCardRow(
                bank_name="Other",
                holder_name="Other",
                encrypted_pan=repository.vault.encrypt("4000000000000002"),
                masked_pan="**** 0002",
                priority=0,
                daily_limit=0,
            )
        )
    second_pan, _ = await repository.reveal_destination(200, order.id)
    assert first_pan == second_pan
    async with repository.sessions.begin() as session:
        locked = await session.get(QuoteRow, quote.id, with_for_update=True)
        locked.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await repository.set_rate(100, 61_000)
    first, second = await asyncio.gather(
        repository.requote(200, quote.id), repository.requote(200, quote.id)
    )
    assert first.id == second.id and first.version == 2
    async with repository.sessions() as session:
        persisted_product = await session.get(ProductRow, product.id)
        successors = list(
            (
                await session.scalars(
                    select(QuoteRow).where(QuoteRow.predecessor_quote_id == quote.id)
                )
            ).all()
        )
        assert len(successors) == 1 and persisted_product.reserved == 1


@pytest.mark.asyncio
async def test_outbox_text_photo_delivery_retry_and_dead_letter(repository):
    runtime = Runtime.__new__(Runtime)
    runtime.repo = repository
    runtime.bot = AsyncMock()
    now = repository.now()
    async with repository.sessions.begin() as session:
        session.add_all(
            [
                OutboxRow(
                    kind="TEXT", chat_id=1, payload={"order_id": "1"}, attempts=0, available_at=now
                ),
                OutboxRow(
                    kind="PAYMENT_REVIEW",
                    chat_id=1,
                    payload={"order_id": "2", "receipt_file_id": "photo"},
                    attempts=0,
                    available_at=now,
                ),
                OutboxRow(
                    kind="ORDER_DELIVERED",
                    chat_id=1,
                    payload={
                        "order_id": "3",
                        "content": "content",
                        "activation_link": "https://example.invalid",
                    },
                    attempts=0,
                    available_at=now,
                ),
            ]
        )
    assert await runtime.process_outbox_once()
    assert await runtime.process_outbox_once()
    assert await runtime.process_outbox_once()
    assert runtime.bot.send_message.await_count == 2
    runtime.bot.send_photo.assert_awaited_once()

    runtime.bot.send_message.side_effect = RuntimeError("sensitive detail must not persist")
    async with repository.sessions.begin() as session:
        failed = OutboxRow(
            kind="FAIL",
            chat_id=1,
            payload={"order_id": "4"},
            attempts=7,
            available_at=repository.now(),
        )
        session.add(failed)
        await session.flush()
        failed_id = failed.id
    assert await runtime.process_outbox_once()
    async with repository.sessions() as session:
        failed = await session.get(OutboxRow, failed_id)
        assert failed.attempts == 8 and failed.dead_at is not None and failed.sent_at is None
        assert failed.last_error == "RuntimeError"


@pytest.mark.asyncio
async def test_owner_crud_validation_pages_buttons_emojis_and_audit(repository):
    with pytest.raises(AccessDenied):
        await repository.set_rate(999, 50_000)
    with pytest.raises(InvalidState):
        await repository.set_rate(100, 0)
    await repository.set_rate(100, 50_000)
    await repository.set_pricing(
        100,
        {
            "mode": "target_margin",
            "target_margin": "20",
            "markup": "0",
            "platform_fee": "1",
            "payment_fee": "2",
            "warranty_reserve": "3",
            "fixed_cost_toman": 100,
        },
    )
    category = await repository.create_category(100, "Category", "Description", 2)
    category = await repository.update_category(
        100, category.id, active=False, position=1, title="Updated"
    )
    assert not category.active and category.position == 1
    with pytest.raises(InvalidState):
        await repository.update_category(100, category.id, forbidden=True)
    product = await repository.create_product(
        100,
        category.id,
        {
            "title": "Product",
            "description": "Full",
            "base_price_usd": "10.25",
            "fixed_price_toman": "12345",
            "duration": "30d",
            "plan_type": "type",
            "activation_method": "link",
            "warranty_text": "warranty",
            "warranty_days": 7,
            "delivery_minutes": 60,
            "stock": 0,
            "unlimited_stock": True,
            "requires_kyc": True,
            "position": 4,
            "pricing_override": {"markup": "5"},
        },
    )
    product = await repository.update_product(
        100, product.id, {"active": False, "stock": 5, "base_price_usd": "11.5"}
    )
    assert not product.active and product.stock == 5
    with pytest.raises(InvalidState):
        await repository.create_product(100, uuid4(), {"title": "x", "base_price_usd": "1"})
    merchant = await repository.create_merchant_card(
        100, "Bank", "Holder", "5555555555554444", 2, 100_000
    )
    merchant = await repository.update_merchant_card(
        100, merchant.id, active=False, priority=1, daily_limit=200_000
    )
    assert not merchant.active and merchant.daily_limit == 200_000
    page = await repository.upsert_page(100, "home", "Welcome")
    page = await repository.upsert_page(100, "home", "Updated")
    button = await repository.create_page_button(
        100, page.id, "Buy", "catalog", 0, 0, "primary", "123"
    )
    assert page.text == "Updated" and button.style == "primary"
    with pytest.raises(InvalidState):
        await repository.create_page_button(100, page.id, "X", "x", 0, 0, "purple")
    emoji = await repository.register_emoji(100, "premium", "123456")
    assert emoji.custom_emoji_id == "123456"
    with pytest.raises(InvalidState):
        await repository.register_emoji(100, "invalid", "not-numeric")
    events = await repository.audit_events(100)
    assert events and "5555555555554444" not in repr(events)


@pytest.mark.asyncio
async def test_rejection_missing_gates_expiration_and_delivery_guards(repository):
    with pytest.raises(AccessDenied):
        await repository.create_quote(200, uuid4(), uuid4())
    terms = await repository.publish_terms(100, "Terms", "Body")
    await repository.accept_terms(200, terms.id)
    kyc = await repository.submit_kyc(200, "kyc", "reject-kyc", "document")
    await repository.review_kyc(100, kyc.id, False, "invalid identity evidence")
    card = await repository.submit_customer_card(200, "Bank", "4000000000000002", "evidence")
    await repository.review_card(100, card.id, False, "ownership evidence rejected")
    with pytest.raises(InvalidState):
        await repository.deliver(100, uuid4(), "content")
    with pytest.raises(InvalidState):
        await repository.deliver(100, uuid4(), "")

    product, card = await configure_checkout(repository)
    quote = await repository.create_quote(200, product.id, card.id)
    async with repository.sessions.begin() as session:
        locked = await session.get(QuoteRow, quote.id, with_for_update=True)
        locked.expires_at = repository.now() - timedelta(seconds=1)
    assert await repository.expire_quotes() == 1
    async with repository.sessions() as session:
        expired = await session.get(QuoteRow, quote.id)
        persisted_product = await session.get(ProductRow, product.id)
        assert expired.status == "EXPIRED" and persisted_product.reserved == 0
