import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from cryptography.fernet import Fernet
from redis.asyncio import Redis
from sqlalchemy import select, text

from shopbot.db import CategoryRow, MerchantCardRow, ProductRow, QuoteRow, create_engine_and_session
from shopbot.repository import AccessDenied, InvalidState, RedisCoordinator, ShopRepository
from shopbot.security import Vault, mask_pan
from shopbot.variant_store import VariantStore

DATABASE_URL = os.environ.get("DATABASE_URL")
REDIS_URL = os.environ.get("REDIS_URL")


@pytest.fixture
async def variant_env():
    assert DATABASE_URL and REDIS_URL
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
    store = VariantStore(repo)
    yield repo, store
    await redis.aclose()
    await engine.dispose()


async def configure_base(repo):
    terms = await repo.publish_terms(100, "Terms", "Required terms")
    await repo.accept_terms(200, terms.id)
    await repo.set_rate(100, 50_000)
    await repo.set_pricing(
        100,
        {
            "mode": "markup",
            "markup": "10",
            "platform_fee": "0",
            "payment_fee": "0",
            "warranty_reserve": "0",
            "fixed_cost_toman": 0,
            "rounding_increment_toman": 1000,
        },
    )
    async with repo.sessions.begin() as session:
        category = CategoryRow(
            title="AI",
            description="Artificial intelligence",
            position=1,
            active=True,
        )
        session.add(category)
        merchant_pan = "5555555555554444"
        session.add(
            MerchantCardRow(
                bank_name="Merchant Bank",
                holder_name="Holder",
                encrypted_pan=repo.vault.encrypt(merchant_pan),
                masked_pan=mask_pan(merchant_pan),
                priority=1,
                daily_limit=50_000_000,
                active=True,
            )
        )
        await session.flush()
        return category


def variant_data(**overrides):
    data = {
        "title": "Plus — 1 Month",
        "description": "{emoji:gift} activation",
        "duration": "1 Month",
        "fulfillment_type": "activation_code",
        "payment_method": "card_to_card",
        "supplier_name": "Seller A",
        "marketplace": "plati",
        "supplier_url": "https://example.invalid/offer",
        "cost_amount": "10",
        "cost_currency": "USD",
        "supplier_delivery_mode": "manual",
        "fixed_price_toman": None,
        "delivery_type": "range",
        "delivery_min": 5,
        "delivery_max": 30,
        "delivery_unit": "minute",
        "delivery_text": None,
        "warranty_type": "days",
        "warranty_days": 30,
        "warranty_text": "30 روز",
        "requires_kyc": False,
        "requires_verified_source_card": False,
        "unlimited_stock": True,
        "stock": 0,
        "position": 0,
        "button_emoji_key": None,
    }
    data.update(overrides)
    return data


@pytest.mark.asyncio
async def test_variant_no_card_checkout_sensitive_purge_and_order_context(variant_env):
    repo, store = variant_env
    category = await configure_base(repo)
    family_id = await store.create_family(
        100,
        category.id,
        "{emoji:chatgpt} ChatGPT",
        "اشتراک‌های ChatGPT",
    )
    fields = [
        {
            "field_key": "account_email",
            "label": "ایمیل حساب",
            "field_type": "EMAIL",
            "required": True,
            "sensitive": False,
        },
        {
            "field_key": "account_password",
            "label": "رمز موقت",
            "field_type": "PASSWORD",
            "required": True,
            "sensitive": True,
            "delete_after_fulfillment": True,
        },
    ]
    variant_id = await store.create_variant_bundle(
        100,
        family_id,
        variant_data(fulfillment_type="account_login"),
        fields,
    )

    family = await store.family(family_id)
    assert family["title"].endswith("ChatGPT")
    assert len(await store.owner_families()) == 1
    assert len(await store.storefront_families(category.id)) == 1
    item = await store.variant_with_family(variant_id)
    assert item["family_title"].endswith("ChatGPT")
    assert store.delivery_label(item) == "5 تا 30 دقیقه"
    assert store.warranty_label(item) == "30 روز"
    assert len(await store.offers(variant_id)) == 1
    assert len(await store.legacy_products_for_category(category.id)) == 0
    assert await store.estimate_price(variant_id) == 550_000

    checkout_id = await store.start_checkout(200, variant_id)
    form = await store.variant_fields(variant_id)
    await store.save_field_value(checkout_id, 200, form[0]["id"], "user@example.com")
    with pytest.raises(InvalidState, match="INVALID_EMAIL"):
        await store.save_field_value(checkout_id, 200, form[0]["id"], "not-an-email")
    await store.save_field_value(checkout_id, 200, form[1]["id"], "temporary-pass")
    await store.mark_input_ready(checkout_id, 200)

    masked = await store.checkout_values_summary(checkout_id)
    assert masked[0]["value"] == "user@example.com"
    assert masked[1]["value"] == "••••••••"
    revealed = await store.checkout_values_summary(checkout_id, reveal_sensitive=True)
    assert revealed[1]["value"] == "temporary-pass"

    quote = await store.create_quote(checkout_id, 200, None)
    assert quote.selected_card_id is None
    assert quote.snapshot["variant_id"] == str(variant_id)
    assert quote.snapshot["selected_card_masked"] is None
    order = await store.final_check(checkout_id, 200, quote.id)
    pan, holder = await repo.reveal_destination(200, order.id)
    assert pan == "5555555555554444" and holder == "Holder"

    await repo.submit_receipt(200, order.id, "receipt", "variant-receipt", "photo")
    await repo.manual_reconcile(100, order.id, True, "matched")
    await repo.claim(100, order.id)
    await repo.deliver(100, order.id, "{emoji:gift} done")
    assert await store.purge_sensitive(order.id) == 1

    context = await store.order_context(order.id)
    assert context["variant_title"] == "Plus — 1 Month"
    after = await store.reveal_order_values(100, order.id)
    password = next(item for item in after if item["field_key"] == "account_password")
    assert password["value"] == "پاک شده"

    customer_orders = await store.customer_order_contexts(200)
    assert customer_orders[0]["variant"]["family_title"].endswith("ChatGPT")


@pytest.mark.asyncio
async def test_variant_kyc_card_gate_resume_requote_and_management(variant_env):
    repo, store = variant_env
    category = await configure_base(repo)
    family_id = await store.create_family(100, category.id, "Spotify", "Premium")
    variant_id = await store.create_variant_bundle(
        100,
        family_id,
        variant_data(
            title="Individual — 3 Months",
            fulfillment_type="account_login",
            requires_kyc=True,
            requires_verified_source_card=True,
            warranty_type="subscription",
            warranty_days=0,
            warranty_text="تا پایان مدت اشتراک",
            delivery_type="custom",
            delivery_text="حداکثر 12 ساعت",
        ),
        [],
    )
    checkout_id = await store.start_checkout(200, variant_id)
    await store.mark_input_ready(checkout_id, 200)

    with pytest.raises(AccessDenied, match="KYC_REQUIRED"):
        await store.create_quote(checkout_id, 200, None)

    await store.mark_waiting_gate(checkout_id)
    item = await store.variant(variant_id)
    pending = await store.pending_for_legacy_product(200, item["legacy_product_id"])
    assert pending["id"] == checkout_id

    kyc = await repo.submit_kyc(200, "kyc", "variant-kyc", "photo")
    await repo.review_kyc(100, kyc.id, True, "reviewed")
    with pytest.raises(AccessDenied, match="VERIFIED_OWN_CARD_REQUIRED"):
        await store.create_quote(checkout_id, 200, None)

    card = await repo.submit_customer_card(
        200,
        "Test Bank",
        "4111111111111111",
        "card",
        "variant-card",
        "photo",
    )
    await repo.review_card(100, card.id, True, "ownership checked")
    quote = await store.create_quote(checkout_id, 200, card.id)
    assert quote.snapshot["selected_card_masked"] == card.masked_pan

    async with repo.sessions.begin() as session:
        locked = await session.get(QuoteRow, quote.id, with_for_update=True)
        locked.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await repo.set_rate(100, 60_000)
    fresh = await store.requote(checkout_id, 200, quote.id)
    assert fresh.version == 2
    assert fresh.final_toman != quote.final_toman

    order = await store.final_check(checkout_id, 200, fresh.id)
    assert order.status == "AWAITING_PAYMENT"

    await store.add_field(
        100,
        variant_id,
        {
            "label": "Region",
            "field_type": "TEXT",
            "required": False,
            "sensitive": False,
        },
    )
    assert any(field["label"] == "Region" for field in await store.variant_fields(variant_id))

    offer_id = await store.add_offer(
        100,
        variant_id,
        {
            "supplier_name": "Backup Seller",
            "marketplace": "plati",
            "supplier_url": "https://example.invalid/backup",
            "cost_amount": "11",
            "cost_currency": "USD",
            "priority": 2,
        },
    )
    assert isinstance(offer_id, UUID)
    assert len(await store.offers(variant_id)) == 2

    await store.set_variant_active(100, variant_id, False)
    with pytest.raises(InvalidState, match="PRODUCT_UNAVAILABLE"):
        await store.estimate_price(variant_id)
    await store.set_variant_active(100, variant_id, True)
    await store.set_family_active(100, family_id, False)
    assert not (await store.family(family_id))["active"]
    await store.set_family_active(100, family_id, True)


@pytest.mark.asyncio
async def test_variant_callbacks_validation_fixed_price_and_emoji_fallback(variant_env):
    repo, store = variant_env
    category = await configure_base(repo)

    token = await store.issue_callback("catalog", 200, "x", one_time=True)
    state = await store.resolve_callback(token, 200)
    assert state["a"] == "catalog" and state["o"] == "x"
    with pytest.raises(AccessDenied, match="EXPIRED"):
        await store.resolve_callback(token, 200)

    wrong = await store.issue_callback("catalog", 200)
    with pytest.raises(AccessDenied, match="OWNER"):
        await store.resolve_callback(wrong, 201)

    assert store.validate_field(
        {"field_type": "TELEGRAM_USERNAME", "required": True}, "test_user"
    ) == "@test_user"
    assert store.validate_field(
        {"field_type": "BOOLEAN", "required": True}, "بله"
    ) == "yes"
    assert store.validate_field(
        {"field_type": "SESSION_JSON", "required": True}, '{"a": 1}'
    ) == '{"a":1}'
    assert store.validate_field(
        {
            "field_type": "SELECT",
            "required": True,
            "options": ["A", "B"],
        },
        "A",
    ) == "A"
    with pytest.raises(InvalidState, match="INVALID_URL"):
        store.validate_field({"field_type": "URL", "required": True}, "ftp://bad")

    family_id = await store.create_family(100, category.id, "Telegram Premium", "")
    variant_id = await store.create_variant_bundle(
        100,
        family_id,
        variant_data(
            title="3 Months",
            fulfillment_type="activation_code",
            fixed_price_toman=777_000,
            delivery_type="instant",
            delivery_text="آنی",
            warranty_type="none",
            warranty_days=0,
            warranty_text="بدون گارانتی",
        ),
        [],
    )
    assert await store.estimate_price(variant_id) == 777_000

    item = await store.variant(variant_id)
    async with repo.sessions() as session:
        product = await session.get(ProductRow, item["legacy_product_id"])
        assert product.fixed_price_toman == 777_000
        assert product.delivery_minutes == 0

    # The underlying registration method only validates numeric Telegram IDs.
    emoji = await store.register_emoji_with_fallback(100, "gift", "123456789", "🎁")
    resolved = await repo.resolve_rich_emoji("gift")
    assert emoji.fallback == "🎁"
    assert resolved == ("123456789", "🎁")
