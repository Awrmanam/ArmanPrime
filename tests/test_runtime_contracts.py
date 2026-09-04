from unittest.mock import AsyncMock

import pytest

from shopbot.config import Settings
from shopbot.domain import Order, Payment, PaymentService
from shopbot.enums import OrderStatus, PaymentStatus


def test_quote_ttl_is_protected_at_exactly_30_minutes():
    assert Settings(price_quote_ttl_minutes=30).price_quote_ttl_minutes == 30
    with pytest.raises(ValueError):
        Settings(price_quote_ttl_minutes=29)
    with pytest.raises(ValueError):
        Settings(price_quote_ttl_minutes=31)


@pytest.mark.asyncio
async def test_manual_reconciliation_is_separate_from_provider_verification():
    notifications = AsyncMock()
    service = PaymentService(notifications)
    order = Order(
        __import__("uuid").uuid4(),
        __import__("uuid").uuid4(),
        __import__("uuid").uuid4(),
        100,
        OrderStatus.AWAITING_RECONCILIATION,
    )
    payment = Payment(__import__("uuid").uuid4(), order.id, PaymentStatus.AWAITING_RECONCILIATION)
    await service.manual_reconcile(payment, order, True, 123, "bank statement reviewed")
    assert payment.status == PaymentStatus.VERIFIED
    assert order.status == OrderStatus.READY_FOR_FULFILLMENT
    notifications.fulfillment_ready.assert_awaited_once_with(order)


@pytest.mark.asyncio
async def test_receipt_alone_never_verifies_payment():
    notifications = AsyncMock()
    service = PaymentService(notifications)
    order = Order(
        __import__("uuid").uuid4(),
        __import__("uuid").uuid4(),
        __import__("uuid").uuid4(),
        100,
        OrderStatus.AWAITING_PAYMENT,
    )
    payment = Payment(__import__("uuid").uuid4(), order.id)
    quote = type(
        "Quote",
        (),
        {
            "expires_at": __import__("datetime").datetime.max.replace(
                tzinfo=__import__("datetime").UTC
            )
        },
    )()
    await service.submit_receipt(
        payment, order, quote, __import__("datetime").datetime.now(__import__("datetime").UTC)
    )
    assert payment.status == PaymentStatus.AWAITING_RECONCILIATION
