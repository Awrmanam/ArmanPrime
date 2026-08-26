from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from cryptography.fernet import Fernet

from shopbot.domain import ConflictError, ForbiddenError
from shopbot.enums import CardStatus, KYCStatus, OrderStatus, PaymentStatus
from shopbot.security import Vault
from shopbot.store import ApplicationStore

NOW = datetime(2026, 1, 1, tzinfo=UTC)
OWNER = 100
CUSTOMER = 200


@pytest.fixture
def store():
    return ApplicationStore(OWNER, Vault({"v1": Fernet.generate_key()}, "v1"))


def configured(store):
    terms = store.publish_terms(OWNER, "قوانین", "متن")
    category = store.add_category(OWNER, "دسته")
    product = store.add_product(OWNER, category.id, "محصول", Decimal("10"), 5)
    store.set_rate(OWNER, 50_000)
    store.add_merchant_card(OWNER, "بانک", "صاحب", "6037990012345678", 1, 10_000_000)
    return terms, product


@pytest.mark.asyncio
async def test_complete_commerce_flow(store):
    terms, product = configured(store)
    user = store.user(CUSTOMER)  # Start
    assert product.active  # Browse is available before KYC
    store.accept_terms(CUSTOMER)  # Consent
    assert user.accepted_terms_id == terms.id
    user.kyc_status = KYCStatus.PENDING  # KYC submission
    store.set_kyc(OWNER, CUSTOMER, KYCStatus.UNDER_REVIEW)
    store.set_kyc(OWNER, CUSTOMER, KYCStatus.VERIFIED)  # admin review
    card = store.add_customer_card(CUSTOMER, "بانک مشتری", "1111")
    store.review_customer_card(OWNER, card.id, CardStatus.VERIFIED)
    assert store.verified_cards(CUSTOMER) == [store.customer_cards[card.id]]
    quote = store.create_quote(CUSTOMER, product.id, card.id, NOW)
    assert quote.final_toman == 500_000
    order = store.confirm(CUSTOMER, quote.id, NOW)  # Final check
    merchant, pan = store.receiving_card(CUSTOMER, quote.id, NOW)
    assert pan == "6037990012345678" and merchant.masked_pan == "**** 5678"
    payment = store.payments[order.id]
    await store.payment_service.submit_receipt(payment, order, quote, NOW)
    assert payment.status == PaymentStatus.AWAITING_RECONCILIATION
    assert store.notifications.payment_reviews == [order.id]
    await store.payment_service.verify(payment, order, "bank-reference", True, True)
    assert order.status == OrderStatus.READY_FOR_FULFILLMENT
    assert store.notifications.fulfillment_orders == [order.id]
    store.payment_service.claim(order, OWNER, NOW)
    with pytest.raises(ConflictError):
        store.payment_service.claim(order, OWNER + 1, NOW)
    delivery = store.deliver(OWNER, order.id, "اطلاعات تحویل", "https://example.invalid")
    assert delivery.order_id == order.id and order.status == OrderStatus.DELIVERED


def test_access_requote_and_no_cross_user_card(store):
    _, product = configured(store)
    store.accept_terms(CUSTOMER)
    user = store.user(CUSTOMER)
    alien = store.user(CUSTOMER + 1)
    alien.kyc_status = KYCStatus.VERIFIED
    card = store.add_customer_card(CUSTOMER + 1, "بانک", "2222")
    store.review_customer_card(OWNER, card.id, CardStatus.VERIFIED)
    with pytest.raises(ForbiddenError):
        store.create_quote(CUSTOMER, product.id, card.id, NOW)
    user.kyc_status = KYCStatus.VERIFIED
    own = store.add_customer_card(CUSTOMER, "بانک", "3333")
    store.review_customer_card(OWNER, own.id, CardStatus.VERIFIED)
    quote = store.create_quote(CUSTOMER, product.id, own.id, NOW)
    store.confirm(CUSTOMER, quote.id, NOW)
    original = quote.final_toman
    store.set_rate(OWNER, 60_000)
    assert quote.final_toman == original
    with pytest.raises(ForbiddenError):
        store.receiving_card(CUSTOMER, quote.id, NOW + timedelta(minutes=30))
    fresh = store.requote(quote, NOW + timedelta(minutes=30))
    assert fresh.final_toman == 600_000 and fresh.version == 2


@pytest.mark.asyncio
async def test_late_duplicate_mismatch_and_audit_redaction(store):
    _, product = configured(store)
    store.accept_terms(CUSTOMER)
    store.set_kyc(OWNER, CUSTOMER, KYCStatus.VERIFIED)
    card = store.add_customer_card(CUSTOMER, "بانک", "4444")
    store.review_customer_card(OWNER, card.id, CardStatus.VERIFIED)
    quote = store.create_quote(CUSTOMER, product.id, card.id, NOW)
    order = store.confirm(CUSTOMER, quote.id, NOW)
    payment = store.payments[order.id]
    await store.payment_service.submit_receipt(payment, order, quote, quote.expires_at)
    assert payment.status == PaymentStatus.LATE_PAYMENT_REVIEW
    order.status = OrderStatus.AWAITING_PAYMENT
    await store.payment_service.verify(payment, order, "same", True, False)
    assert order.status == OrderStatus.MANUAL_REVIEW
    with pytest.raises(ConflictError):
        await store.payment_service.verify(payment, order, "same", True, True)
    audit_text = repr(store.audit)
    assert "6037990012345678" not in audit_text and "**** 5678" in audit_text
