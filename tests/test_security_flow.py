from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet

from shopbot.domain import (
    CheckoutService,
    ConflictError,
    CustomerCard,
    ForbiddenError,
    Ledger,
    Payment,
    PaymentService,
    PricingRule,
    Product,
    TermsVersion,
    User,
    calculate_price,
)
from shopbot.enums import CardStatus, KYCStatus, OrderStatus, PaymentStatus, RiskStatus
from shopbot.security import Callback, CallbackSigner, Vault, mask_pan, pan_fingerprint

NOW = datetime(2026, 1, 1, tzinfo=UTC)


class Notifications:
    def __init__(self):
        self.events = []

    async def payment_review(self, order, payment):
        self.events.append(("review", order.id))

    async def fulfillment_ready(self, order):
        self.events.append(("ready", order.id))


@pytest.fixture
def flow():
    terms = TermsVersion.publish(1, "Terms", ("Page",), NOW)
    user = User(telegram_id=12, kyc_status=KYCStatus.VERIFIED, accepted_terms_id=terms.id)
    card = CustomerCard(uuid4(), user.id, "Bank", "**** 1234", "1234", "fp", CardStatus.VERIFIED)
    product = Product(uuid4(), "Product", Decimal("10"), 3)
    service = CheckoutService()
    return terms, user, card, product, service


def test_terms_required_but_catalog_is_viewable(flow):
    terms, user, card, product, service = flow
    user.accepted_terms_id = None
    assert product.active  # catalog has no KYC/consent gate
    with pytest.raises(ForbiddenError, match="TERMS_REQUIRED"):
        service.create_quote(user, terms, card, product, 50_000, PricingRule(), NOW)


def test_kyc_and_owned_verified_card_are_server_side(flow):
    terms, user, card, product, service = flow
    user.kyc_status = KYCStatus.PENDING
    with pytest.raises(ForbiddenError, match="KYC_REQUIRED"):
        service.create_quote(user, terms, card, product, 1, PricingRule(), NOW)
    user.kyc_status = KYCStatus.VERIFIED
    alien = CustomerCard(uuid4(), uuid4(), "X", "**** 9999", "9999", "x", CardStatus.VERIFIED)
    with pytest.raises(ForbiddenError, match="VERIFIED_OWN_CARD_REQUIRED"):
        service.create_quote(user, terms, alien, product, 1, PricingRule(), NOW)


def test_quote_is_exactly_30_minutes_and_frozen(flow):
    terms, user, card, product, service = flow
    quote = service.create_quote(user, terms, card, product, 50_000, PricingRule(), NOW)
    assert quote.expires_at == NOW + timedelta(minutes=30)
    price = quote.final_toman
    assert quote.is_valid(NOW + timedelta(minutes=29, seconds=59, microseconds=999999))
    assert not quote.is_valid(NOW + timedelta(minutes=30))
    assert quote.final_toman == price  # a later rate cannot mutate the snapshot


def test_final_check_idempotency_expiry_and_reservation_release(flow):
    terms, user, card, product, service = flow
    quote = service.create_quote(user, terms, card, product, 1, PricingRule(), NOW)
    assert product.reserved == 1
    order = service.confirm_final_check(quote, NOW)
    assert service.confirm_final_check(quote, NOW).id == order.id
    assert service.expire(quote, order, product, NOW + timedelta(minutes=30))
    assert order.status == OrderStatus.PAYMENT_EXPIRED and product.reserved == 0
    with pytest.raises(ForbiddenError, match="QUOTE_EXPIRED"):
        service.confirm_final_check(quote, NOW + timedelta(minutes=30))


def test_receiving_pan_requires_every_gate_and_is_dynamic(flow):
    terms, user, card, product, service = flow
    quote = service.create_quote(user, terms, card, product, 1, PricingRule(), NOW)
    with pytest.raises(ForbiddenError):
        service.reveal_receiving_card(user, terms, card, quote, NOW, "cipher", lambda _: "123")
    service.confirm_final_check(quote, NOW)
    assert (
        service.reveal_receiving_card(user, terms, card, quote, NOW, "cipher", lambda _: "123")
        == "123"
    )
    user.risk_status = RiskStatus.BLOCKED
    with pytest.raises(ForbiddenError):
        service.reveal_receiving_card(user, terms, card, quote, NOW, "cipher", lambda _: "123")


@pytest.mark.asyncio
async def test_receipt_never_verifies_and_late_receipt_is_review(flow):
    terms, user, card, product, checkout = flow
    quote = checkout.create_quote(user, terms, card, product, 1, PricingRule(), NOW)
    order = checkout.confirm_final_check(quote, NOW)
    payment, notifications = Payment(uuid4(), order.id), Notifications()
    service = PaymentService(notifications)
    await service.submit_receipt(payment, order, quote, NOW)
    assert payment.status == PaymentStatus.AWAITING_RECONCILIATION
    assert order.status == OrderStatus.AWAITING_RECONCILIATION
    payment2 = Payment(uuid4(), order.id)
    await service.submit_receipt(payment2, order, quote, quote.expires_at)
    assert payment2.status == PaymentStatus.LATE_PAYMENT_REVIEW
    assert notifications.events == [("review", order.id), ("review", order.id)]


@pytest.mark.asyncio
async def test_server_verification_reference_card_match_notification_and_claim(flow):
    terms, user, card, product, checkout = flow
    quote = checkout.create_quote(user, terms, card, product, 1, PricingRule(), NOW)
    order = checkout.confirm_final_check(quote, NOW)
    notifications, service = Notifications(), PaymentService(Notifications())
    service.notifications = notifications
    payment = Payment(uuid4(), order.id)
    with pytest.raises(ForbiddenError):
        await service.verify(payment, order, "r0", False, True)
    await service.verify(payment, order, "r1", True, False)
    assert payment.status == PaymentStatus.CARD_MISMATCH
    order.status = OrderStatus.AWAITING_PAYMENT
    payment2 = Payment(uuid4(), order.id)
    await service.verify(payment2, order, "r2", True, True)
    assert order.status == OrderStatus.READY_FOR_FULFILLMENT
    assert notifications.events == [("ready", order.id)]
    duplicate = Payment(uuid4(), order.id)
    with pytest.raises(ConflictError):
        await service.verify(duplicate, order, "r2", True, True)
    service.claim(order, 1, NOW)
    with pytest.raises(ConflictError):
        service.claim(order, 2, NOW)


def test_pricing_callback_crypto_mask_and_ledger():
    assert (
        calculate_price(Decimal("10"), 50_000, PricingRule(markup_percent=Decimal("10"))) == 550_000
    )
    assert (
        calculate_price(Decimal("10"), 50_000, PricingRule(target_margin_percent=Decimal("20")))
        == 625_000
    )
    signer = CallbackSigner(b"k" * 32)
    token = signer.sign(Callback("pay", "42", 2))
    assert signer.verify(token) == Callback("pay", "42", 2)
    with pytest.raises(ValueError):
        signer.verify(token + "x")
    key = Fernet.generate_key()
    vault = Vault({"v1": key}, "v1")
    encrypted = vault.encrypt("6037990012345678")
    assert "6037990012345678" not in encrypted and vault.decrypt(encrypted) == "6037990012345678"
    assert mask_pan("6037-9900-1234-5678") == "**** 5678"
    assert pan_fingerprint("1", b"k") == pan_fingerprint("1", b"k")
    ledger, account = Ledger(), uuid4()
    ledger.post(account, 100, "ref")
    assert ledger.balance(account) == 100
    with pytest.raises(ConflictError):
        ledger.post(account, 50, "ref")
