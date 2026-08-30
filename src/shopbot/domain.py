from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from hashlib import sha256
from typing import Protocol
from uuid import UUID, uuid4

from .enums import CardStatus, KYCStatus, OrderStatus, PaymentStatus, QuoteStatus, RiskStatus


class ForbiddenError(Exception):
    pass


class ConflictError(Exception):
    pass


@dataclass(frozen=True)
class TermsVersion:
    id: UUID
    version: int
    title: str
    pages: tuple[str, ...]
    effective_at: datetime
    published: bool
    content_hash: str

    @classmethod
    def publish(cls, version: int, title: str, pages: tuple[str, ...], now: datetime):
        canonical = "\n\0\n".join((title, *pages)).encode()
        return cls(uuid4(), version, title, pages, now, True, sha256(canonical).hexdigest())


@dataclass(frozen=True)
class Consent:
    user_id: UUID
    terms_id: UUID
    accepted_at: datetime


@dataclass
class User:
    id: UUID = field(default_factory=uuid4)
    telegram_id: int = 0
    kyc_status: KYCStatus = KYCStatus.NOT_STARTED
    accepted_terms_id: UUID | None = None
    risk_status: RiskStatus = RiskStatus.CLEAR


@dataclass(frozen=True)
class CustomerCard:
    id: UUID
    user_id: UUID
    bank_name: str
    masked_pan: str
    last4: str
    fingerprint: str
    status: CardStatus

    @property
    def display(self) -> str:
        return f"{self.bank_name} — **** {self.last4}"


@dataclass
class Product:
    id: UUID
    title: str
    base_cost_amount: Decimal
    stock: int
    base_cost_currency: str = "USD"
    currency_buffer_percent: Decimal = Decimal("0")
    reserved: int = 0
    active: bool = True
    requires_kyc: bool = True

    def reserve(self) -> None:
        if self.stock - self.reserved < 1:
            raise ConflictError("OUT_OF_STOCK")
        self.reserved += 1

    def release(self) -> None:
        self.reserved = max(0, self.reserved - 1)


@dataclass(frozen=True)
class PricingRule:
    platform_fee_percent: Decimal = Decimal("0")
    payment_fee_percent: Decimal = Decimal("0")
    fixed_cost_toman: int = 0
    warranty_reserve_percent: Decimal = Decimal("0")
    markup_percent: Decimal | None = None
    target_margin_percent: Decimal | None = None
    fixed_price_toman: int | None = None


def decimal_value(value: Decimal | int | str) -> Decimal:
    """Normalize external numerics without ever constructing Decimal from a float."""
    if isinstance(value, float):
        raise TypeError("float values are not accepted; normalize from their source string")
    return value if isinstance(value, Decimal) else Decimal(str(value))


def calculate_price(
    base_cost: Decimal | int | str,
    rate: Decimal | int | str,
    rule: PricingRule,
    currency_buffer_percent: Decimal | int | str = Decimal("0"),
) -> int:
    one, hundred = Decimal("1"), Decimal("100")
    if rule.fixed_price_toman is not None:
        if rule.fixed_price_toman < 0:
            raise ValueError("fixed price cannot be negative")
        return int(rule.fixed_price_toman)
    base = decimal_value(base_cost)
    numeric_rate = decimal_value(rate)
    buffer = decimal_value(currency_buffer_percent)
    if (
        base < 0
        or numeric_rate <= 0
        or rule.fixed_cost_toman < 0
        or not Decimal("0") <= buffer < hundred
    ):
        raise ValueError("base price, rate, and fixed costs must be valid")
    fees = tuple(
        decimal_value(item)
        for item in (
            rule.platform_fee_percent,
            rule.payment_fee_percent,
            rule.warranty_reserve_percent,
        )
    )
    markup = decimal_value(rule.markup_percent or Decimal("0"))
    margin = (
        decimal_value(rule.target_margin_percent)
        if rule.target_margin_percent is not None
        else None
    )
    if any(item < 0 or item >= hundred for item in fees) or markup < 0:
        raise ValueError("fees, reserve, and markup percentages must be non-negative")
    if margin is not None and not Decimal("0") <= margin < hundred:
        raise ValueError("target margin must satisfy 0 <= margin < 100")
    converted = base * numeric_rate
    landed = converted * (one + buffer / hundred)
    landed += landed * sum(fees, Decimal("0")) / hundred
    landed += decimal_value(rule.fixed_cost_toman)
    result = (
        landed / (one - margin / hundred)
        if margin is not None
        else landed * (one + markup / hundred)
    )
    return int(result.quantize(one, rounding=ROUND_HALF_UP))


@dataclass
class PriceQuote:
    id: UUID
    user_id: UUID
    product_id: UUID
    selected_card_id: UUID
    product_snapshot: dict[str, str]
    base_usd: Decimal
    rate: int
    rate_source: str
    rate_timestamp: datetime
    final_toman: int
    created_at: datetime
    expires_at: datetime
    version: int
    status: QuoteStatus = QuoteStatus.ACTIVE
    final_check_confirmed: bool = False

    def is_valid(self, now: datetime) -> bool:
        return self.status == QuoteStatus.ACTIVE and now < self.expires_at


@dataclass
class Order:
    id: UUID
    user_id: UUID
    quote_id: UUID
    amount_toman: int
    status: OrderStatus
    assigned_admin_id: int | None = None
    started_at: datetime | None = None


@dataclass
class Payment:
    id: UUID
    order_id: UUID
    status: PaymentStatus = PaymentStatus.CREATED
    provider_reference: str | None = None


class NotificationSink(Protocol):
    async def payment_review(self, order: Order, payment: Payment) -> None: ...
    async def fulfillment_ready(self, order: Order) -> None: ...


class CheckoutService:
    def __init__(self, ttl_minutes: int = 30):
        self.ttl = timedelta(minutes=ttl_minutes)
        self.orders_by_quote: dict[UUID, Order] = {}

    @staticmethod
    def assert_store_access(user: User, current_terms: TermsVersion) -> None:
        if user.accepted_terms_id != current_terms.id:
            raise ForbiddenError("TERMS_REQUIRED")

    def create_quote(
        self,
        user: User,
        current_terms: TermsVersion,
        card: CustomerCard,
        product: Product,
        rate: int,
        rule: PricingRule,
        now: datetime,
        version: int = 1,
    ) -> PriceQuote:
        self.assert_store_access(user, current_terms)
        if user.kyc_status != KYCStatus.VERIFIED:
            raise ForbiddenError("KYC_REQUIRED")
        if card.user_id != user.id or card.status != CardStatus.VERIFIED:
            raise ForbiddenError("VERIFIED_OWN_CARD_REQUIRED")
        if user.risk_status == RiskStatus.BLOCKED:
            raise ForbiddenError("RISK_BLOCKED")
        product.reserve()
        return PriceQuote(
            uuid4(),
            user.id,
            product.id,
            card.id,
            {"title": product.title},
            product.base_cost_amount,
            rate,
            "manual",
            now,
            calculate_price(product.base_cost_amount, rate, rule, product.currency_buffer_percent),
            now,
            now + self.ttl,
            version,
        )

    def confirm_final_check(self, quote: PriceQuote, now: datetime) -> Order:
        if not quote.is_valid(now):
            raise ForbiddenError("QUOTE_EXPIRED")
        quote.final_check_confirmed = True
        if quote.id not in self.orders_by_quote:
            self.orders_by_quote[quote.id] = Order(
                uuid4(), quote.user_id, quote.id, quote.final_toman, OrderStatus.AWAITING_PAYMENT
            )
        return self.orders_by_quote[quote.id]

    @staticmethod
    def reveal_receiving_card(
        user: User,
        current_terms: TermsVersion,
        card: CustomerCard,
        quote: PriceQuote,
        now: datetime,
        encrypted_pan: str,
        decrypt: callable,
    ) -> str:
        if (
            user.accepted_terms_id != current_terms.id
            or user.kyc_status != KYCStatus.VERIFIED
            or user.risk_status == RiskStatus.BLOCKED
            or card.user_id != user.id
            or card.status != CardStatus.VERIFIED
            or quote.user_id != user.id
            or quote.selected_card_id != card.id
            or not quote.final_check_confirmed
            or not quote.is_valid(now)
        ):
            raise ForbiddenError("FORBIDDEN")
        return decrypt(encrypted_pan)

    @staticmethod
    def expire(quote: PriceQuote, order: Order | None, product: Product, now: datetime) -> bool:
        if quote.status == QuoteStatus.ACTIVE and now >= quote.expires_at:
            quote.status = QuoteStatus.EXPIRED
            product.release()
            if order and order.status == OrderStatus.AWAITING_PAYMENT:
                order.status = OrderStatus.PAYMENT_EXPIRED
            return True
        return False


class PaymentService:
    def __init__(self, notifications: NotificationSink):
        self.notifications = notifications
        self.references: set[str] = set()

    async def submit_receipt(
        self, payment: Payment, order: Order, quote: PriceQuote, now: datetime
    ) -> None:
        payment.status = (
            PaymentStatus.LATE_PAYMENT_REVIEW
            if now >= quote.expires_at
            else PaymentStatus.AWAITING_RECONCILIATION
        )
        order.status = (
            OrderStatus.MANUAL_REVIEW
            if now >= quote.expires_at
            else OrderStatus.AWAITING_RECONCILIATION
        )
        await self.notifications.payment_review(order, payment)

    async def verify(
        self,
        payment: Payment,
        order: Order,
        reference: str,
        server_verified: bool,
        card_match: bool,
    ) -> None:
        if not server_verified:
            raise ForbiddenError("SERVER_VERIFICATION_REQUIRED")
        if reference in self.references:
            raise ConflictError("DUPLICATE_REFERENCE")
        self.references.add(reference)
        payment.provider_reference = reference
        if not card_match:
            payment.status = PaymentStatus.CARD_MISMATCH
            order.status = OrderStatus.MANUAL_REVIEW
            return
        payment.status = PaymentStatus.VERIFIED
        order.status = OrderStatus.READY_FOR_FULFILLMENT
        await self.notifications.fulfillment_ready(order)

    async def manual_reconcile(
        self, payment: Payment, order: Order, approved: bool, actor_id: int, reason: str
    ) -> None:
        """Manual evidence decision; deliberately separate from provider verification."""
        if not actor_id or not reason.strip():
            raise ForbiddenError("MANUAL_ACTOR_AND_REASON_REQUIRED")
        if approved:
            payment.status = PaymentStatus.VERIFIED
            order.status = OrderStatus.READY_FOR_FULFILLMENT
            await self.notifications.fulfillment_ready(order)
        else:
            payment.status = PaymentStatus.REJECTED
            order.status = OrderStatus.MANUAL_REVIEW

    @staticmethod
    def claim(order: Order, admin_id: int, now: datetime) -> None:
        if order.status != OrderStatus.READY_FOR_FULFILLMENT or order.assigned_admin_id:
            raise ConflictError("ALREADY_CLAIMED")
        order.assigned_admin_id = admin_id
        order.status = OrderStatus.PROCESSING
        order.started_at = now


class Ledger:
    def __init__(self):
        self._entries: list[tuple[UUID, int, str]] = []

    def post(self, account_id: UUID, amount: int, reference: str) -> None:
        if not amount or any(entry[2] == reference for entry in self._entries):
            raise ConflictError("INVALID_OR_DUPLICATE_ENTRY")
        self._entries.append((account_id, amount, reference))

    def balance(self, account_id: UUID) -> int:
        return sum(amount for owner, amount, _ in self._entries if owner == account_id)


def utcnow() -> datetime:
    return datetime.now(UTC)
