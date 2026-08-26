from __future__ import annotations

import json
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .db import (
    AuditRow,
    CategoryRow,
    ConfigRow,
    ConsentRow,
    CustomerCardRow,
    DeliveryRow,
    KYCRow,
    MerchantCardRow,
    OrderRow,
    OutboxRow,
    PaymentRow,
    ProductRow,
    QuoteRow,
    RateRow,
    ReservationRow,
    TermsRow,
    UserRow,
)
from .domain import PricingRule, calculate_price, decimal_value
from .security import Vault, mask_pan, pan_fingerprint


class AccessDenied(Exception):
    pass


class InvalidState(Exception):
    pass


class RedisCoordinator:
    def __init__(self, redis: Redis):
        self.redis = redis

    async def rate_limit(self, scope: str, actor: int, limit: int, seconds: int) -> bool:
        key = f"rl:{scope}:{actor}"
        count = await self.redis.incr(key)
        if count == 1:
            await self.redis.expire(key, seconds)
        return count <= limit

    async def consume_callback(self, token_hash: str, ttl: int = 1800) -> bool:
        return bool(await self.redis.set(f"cb:{token_hash}", "1", ex=ttl, nx=True))

    async def issue_callback(
        self,
        action: str,
        actor_id: int,
        object_id: str = "",
        version: int = 1,
        *,
        one_time: bool = False,
        ttl: int = 1800,
    ) -> str:
        opaque = secrets.token_urlsafe(12)
        token = f"c1.{opaque}"
        state = json.dumps(
            {
                "a": action,
                "u": actor_id,
                "o": object_id,
                "v": version,
                "once": one_time,
            },
            separators=(",", ":"),
        )
        await self.redis.set(f"callback:{opaque}", state, ex=ttl)
        if len(token.encode()) > 64:
            raise AssertionError("callback_data exceeds Telegram limit")
        return token

    async def resolve_callback(self, token: str, actor_id: int) -> dict:
        if not token.startswith("c1.") or len(token.encode()) > 64:
            raise AccessDenied("CALLBACK_INVALID")
        opaque = token[3:]
        key = f"callback:{opaque}"
        raw = await self.redis.get(key)
        if not raw:
            raise AccessDenied("CALLBACK_EXPIRED")
        state = json.loads(raw)
        if state["u"] != actor_id:
            raise AccessDenied("CALLBACK_OWNER_REQUIRED")
        if state["once"]:
            deleted = await self.redis.delete(key)
            if deleted != 1:
                raise AccessDenied("CALLBACK_REPLAYED")
        return state

    @asynccontextmanager
    async def lock(self, name: str, timeout: int = 15) -> AsyncIterator[None]:
        lock = self.redis.lock(f"lock:{name}", timeout=timeout, blocking_timeout=5)
        if not await lock.acquire():
            raise InvalidState("BUSY")
        try:
            yield
        finally:
            await lock.release()


class ShopRepository:
    QUOTE_TTL = timedelta(minutes=30)

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        coordinator: RedisCoordinator,
        vault: Vault,
        hmac_key: bytes,
        owner_id: int,
        order_chat_id: int,
    ):
        self.sessions = sessions
        self.coordinator = coordinator
        self.vault = vault
        self.hmac_key = hmac_key
        self.owner_id = owner_id
        self.order_chat_id = order_chat_id or owner_id

    def owner(self, actor: int) -> None:
        if actor != self.owner_id:
            raise AccessDenied("OWNER_REQUIRED")

    @staticmethod
    def now() -> datetime:
        return datetime.now(UTC)

    async def user(self, telegram_id: int, session: AsyncSession) -> UserRow:
        row = await session.scalar(select(UserRow).where(UserRow.telegram_id == telegram_id))
        if row:
            return row
        row = UserRow(telegram_id=telegram_id)
        session.add(row)
        await session.flush()
        return row

    async def current_terms(self, session: AsyncSession) -> TermsRow | None:
        return await session.scalar(
            select(TermsRow)
            .where(TermsRow.published.is_(True))
            .order_by(TermsRow.version.desc())
            .limit(1)
        )

    async def publish_terms(self, actor: int, title: str, body: str) -> TermsRow:
        self.owner(actor)
        async with self.sessions.begin() as session:
            version = (await session.scalar(select(func.max(TermsRow.version)))) or 0
            digest = sha256(f"{title}\0{body}".encode()).hexdigest()
            row = TermsRow(
                version=version + 1,
                title=title,
                pages=body.split("\f"),
                content_hash=digest,
                effective_at=self.now(),
                published=True,
            )
            session.add(row)
            await self.audit(session, actor, "terms.publish", str(row.id), f"version={row.version}")
            return row

    async def accept_terms(self, telegram_id: int, terms_id: UUID) -> None:
        async with self.sessions.begin() as session:
            user = await self.user(telegram_id, session)
            current = await self.current_terms(session)
            if not current or current.id != terms_id:
                raise InvalidState("STALE_TERMS")
            session.add(ConsentRow(user_id=user.id, terms_id=terms_id, accepted_at=self.now()))
            await self.audit(session, telegram_id, "terms.accept", str(terms_id))

    async def categories(self) -> list[CategoryRow]:
        async with self.sessions() as session:
            return list(
                (
                    await session.scalars(
                        select(CategoryRow)
                        .where(CategoryRow.active.is_(True))
                        .order_by(CategoryRow.position, CategoryRow.title)
                    )
                ).all()
            )

    async def products(self, category_id: UUID) -> list[ProductRow]:
        async with self.sessions() as session:
            return list(
                (
                    await session.scalars(
                        select(ProductRow)
                        .where(ProductRow.category_id == category_id, ProductRow.active.is_(True))
                        .order_by(ProductRow.position, ProductRow.title)
                    )
                ).all()
            )

    async def product(self, product_id: UUID) -> ProductRow | None:
        async with self.sessions() as session:
            return await session.scalar(
                select(ProductRow).where(ProductRow.id == product_id, ProductRow.active.is_(True))
            )

    async def verified_cards(self, telegram_id: int) -> list[CustomerCardRow]:
        async with self.sessions.begin() as session:
            user = await self.user(telegram_id, session)
            return list(
                (
                    await session.scalars(
                        select(CustomerCardRow)
                        .where(
                            CustomerCardRow.user_id == user.id, CustomerCardRow.status == "VERIFIED"
                        )
                        .order_by(CustomerCardRow.verified_at)
                    )
                ).all()
            )

    async def submit_kyc(
        self, telegram_id: int, file_id: str, unique_id: str, file_type: str
    ) -> KYCRow:
        async with self.sessions.begin() as session:
            user = await self.user(telegram_id, session)
            user.kyc_status = "PENDING"
            row = KYCRow(
                user_id=user.id,
                status="PENDING",
                file_id=file_id,
                file_unique_id=unique_id,
                file_type=file_type,
                evidence_level="FORMAT_VALID",
                created_at=self.now(),
            )
            session.add(row)
            await self.audit(session, telegram_id, "kyc.submit", str(row.id))
            return row

    async def review_kyc(
        self, actor: int, submission_id: UUID, approved: bool, reason: str
    ) -> None:
        self.owner(actor)
        async with self.sessions.begin() as session:
            row = await session.scalar(
                select(KYCRow).where(KYCRow.id == submission_id).with_for_update()
            )
            if not row or not reason.strip():
                raise InvalidState("KYC_AND_REASON_REQUIRED")
            user = await session.get(UserRow, row.user_id, with_for_update=True)
            row.status = user.kyc_status = "VERIFIED" if approved else "REJECTED"
            row.evidence_level = "MANUALLY_REVIEWED"
            row.reviewer_id, row.reason, row.reviewed_at = actor, reason, self.now()
            await self.audit(
                session, actor, "kyc.manual_review", str(row.id), f"decision={row.status}"
            )

    async def review_card(self, actor: int, card_id: UUID, approved: bool, reason: str) -> None:
        self.owner(actor)
        if not reason.strip():
            raise InvalidState("REASON_REQUIRED")
        async with self.sessions.begin() as session:
            card = await session.scalar(
                select(CustomerCardRow).where(CustomerCardRow.id == card_id).with_for_update()
            )
            if not card:
                raise InvalidState("CARD_NOT_FOUND")
            card.status = "VERIFIED" if approved else "REJECTED"
            card.verified_by, card.verified_at = actor, self.now() if approved else None
            await self.audit(
                session,
                actor,
                "customer_card.manual_review",
                str(card.id),
                f"decision={card.status}",
            )

    async def kyc_queue(self, actor: int) -> list[KYCRow]:
        self.owner(actor)
        async with self.sessions() as session:
            return list(
                (
                    await session.scalars(
                        select(KYCRow)
                        .where(KYCRow.status.in_(("PENDING", "UNDER_REVIEW")))
                        .order_by(KYCRow.created_at)
                        .limit(50)
                    )
                ).all()
            )

    async def card_queue(self, actor: int) -> list[CustomerCardRow]:
        self.owner(actor)
        async with self.sessions() as session:
            return list(
                (
                    await session.scalars(
                        select(CustomerCardRow)
                        .where(CustomerCardRow.status == "PENDING_VERIFICATION")
                        .limit(50)
                    )
                ).all()
            )

    async def order_queue(self, actor: int) -> list[OrderRow]:
        self.owner(actor)
        async with self.sessions() as session:
            return list(
                (
                    await session.scalars(
                        select(OrderRow)
                        .where(
                            OrderRow.status.in_(
                                (
                                    "AWAITING_RECONCILIATION",
                                    "MANUAL_REVIEW",
                                    "READY_FOR_FULFILLMENT",
                                    "PROCESSING",
                                )
                            )
                        )
                        .limit(50)
                    )
                ).all()
            )

    async def payment_for_order(self, actor: int, order_id: UUID) -> PaymentRow | None:
        self.owner(actor)
        async with self.sessions() as session:
            return await session.scalar(select(PaymentRow).where(PaymentRow.order_id == order_id))

    async def audit_events(self, actor: int, limit: int = 30) -> list[AuditRow]:
        self.owner(actor)
        async with self.sessions() as session:
            return list(
                (
                    await session.scalars(
                        select(AuditRow).order_by(AuditRow.at.desc()).limit(min(limit, 100))
                    )
                ).all()
            )

    async def set_rate(self, actor: int, rate: int) -> RateRow:
        self.owner(actor)
        if rate <= 0:
            raise InvalidState("INVALID_RATE")
        async with self.sessions.begin() as session:
            row = RateRow(usd_to_toman=rate, source="manual", created_at=self.now())
            session.add(row)
            await self.audit(session, actor, "currency.rate", "USD_TOMAN", str(rate))
            return row

    async def set_pricing(self, actor: int, config: dict) -> None:
        self.owner(actor)
        # Constructing a rule validates all externally supplied percentage strings.
        rule = PricingRule(
            platform_fee_percent=decimal_value(config.get("platform_fee", "0")),
            payment_fee_percent=decimal_value(config.get("payment_fee", "0")),
            warranty_reserve_percent=decimal_value(config.get("warranty_reserve", "0")),
            markup_percent=decimal_value(config.get("markup", "0")),
            target_margin_percent=(
                decimal_value(config["target_margin"])
                if config.get("mode") == "target_margin"
                else None
            ),
        )
        calculate_price("1", 1, rule)
        async with self.sessions.begin() as session:
            row = await session.get(ConfigRow, "pricing.global")
            if row:
                row.value, row.updated_at = config, self.now()
            else:
                session.add(ConfigRow(key="pricing.global", value=config, updated_at=self.now()))
            await self.audit(session, actor, "pricing.update", "global")

    async def submit_customer_card(
        self, telegram_id: int, bank: str, pan: str, evidence_file_id: str
    ) -> CustomerCardRow:
        digits = "".join(c for c in pan if c.isdigit())
        if len(digits) != 16:
            raise InvalidState("INVALID_PAN")
        async with self.sessions.begin() as session:
            user = await self.user(telegram_id, session)
            row = CustomerCardRow(
                user_id=user.id,
                bank_name=bank,
                encrypted_pan=self.vault.encrypt(digits),
                masked_pan=mask_pan(digits),
                last4=digits[-4:],
                fingerprint=pan_fingerprint(digits, self.hmac_key),
                status="PENDING_VERIFICATION",
                evidence_file_id=evidence_file_id,
            )
            session.add(row)
            await self.audit(session, telegram_id, "customer_card.submit", str(row.id))
            return row

    async def create_quote(self, telegram_id: int, product_id: UUID, card_id: UUID) -> QuoteRow:
        async with self.coordinator.lock(f"quote:{telegram_id}:{product_id}"):
            async with self.sessions.begin() as session:
                user = await self.user(telegram_id, session)
                terms = await self.current_terms(session)
                consent = terms and await session.scalar(
                    select(ConsentRow.id).where(
                        ConsentRow.user_id == user.id, ConsentRow.terms_id == terms.id
                    )
                )
                if not consent or user.kyc_status != "VERIFIED" or user.risk_status == "BLOCKED":
                    raise AccessDenied("CHECKOUT_FORBIDDEN")
                card = await session.scalar(
                    select(CustomerCardRow).where(
                        CustomerCardRow.id == card_id,
                        CustomerCardRow.user_id == user.id,
                        CustomerCardRow.status == "VERIFIED",
                    )
                )
                if not card:
                    raise AccessDenied("VERIFIED_OWN_CARD_REQUIRED")
                product = await session.scalar(
                    select(ProductRow).where(ProductRow.id == product_id).with_for_update()
                )
                if not product or not product.active:
                    raise InvalidState("PRODUCT_UNAVAILABLE")
                if not product.unlimited_stock and product.stock - product.reserved < 1:
                    raise InvalidState("OUT_OF_STOCK")
                rate = await session.scalar(
                    select(RateRow).order_by(RateRow.created_at.desc()).limit(1)
                )
                if not rate:
                    raise InvalidState("RATE_NOT_CONFIGURED")
                pricing = await session.get(ConfigRow, "pricing.global")
                if not pricing:
                    raise InvalidState("PRICING_NOT_CONFIGURED")
                config = dict(pricing.value)
                override = product.pricing_override or {}
                config.update(override)
                rule = PricingRule(
                    platform_fee_percent=decimal_value(config.get("platform_fee", "0")),
                    payment_fee_percent=decimal_value(config.get("payment_fee", "0")),
                    fixed_cost_toman=int(config.get("fixed_cost_toman", 0)),
                    warranty_reserve_percent=decimal_value(config.get("warranty_reserve", "0")),
                    markup_percent=decimal_value(config.get("markup", "0")),
                    target_margin_percent=(
                        decimal_value(config["target_margin"])
                        if config.get("mode") == "target_margin"
                        else None
                    ),
                    fixed_price_toman=product.fixed_price_toman,
                )
                final = calculate_price(product.base_price_usd, rate.usd_to_toman, rule)
                now = self.now()
                quote = QuoteRow(
                    user_id=user.id,
                    product_id=product.id,
                    selected_card_id=card.id,
                    snapshot={
                        "title": product.title,
                        "description": product.description,
                        "duration": product.duration,
                        "plan": product.plan_type,
                        "activation": product.activation_method,
                        "warranty": product.warranty_text,
                        "base_usd": str(product.base_price_usd),
                        "rate_at": rate.created_at.isoformat(),
                        "pricing": config,
                    },
                    rate=rate.usd_to_toman,
                    final_toman=final,
                    created_at=now,
                    expires_at=now + self.QUOTE_TTL,
                    status="ACTIVE",
                    version=1,
                )
                session.add(quote)
                await session.flush()
                session.add(ReservationRow(quote_id=quote.id, product_id=product.id))
                if not product.unlimited_stock:
                    product.reserved += 1
                await self.audit(
                    session, telegram_id, "quote.create", str(quote.id), f"amount={final}"
                )
                return quote

    async def final_check(self, telegram_id: int, quote_id: UUID) -> OrderRow:
        async with self.coordinator.lock(f"final:{quote_id}"):
            async with self.sessions.begin() as session:
                user = await self.user(telegram_id, session)
                quote = await session.scalar(
                    select(QuoteRow).where(QuoteRow.id == quote_id).with_for_update()
                )
                if (
                    not quote
                    or quote.user_id != user.id
                    or quote.status != "ACTIVE"
                    or self.now() >= quote.expires_at
                ):
                    raise AccessDenied("QUOTE_INVALID")
                existing = await session.scalar(
                    select(OrderRow).where(OrderRow.quote_id == quote.id)
                )
                if existing:
                    return existing
                quote.final_check_confirmed = True
                order = OrderRow(
                    user_id=user.id,
                    quote_id=quote.id,
                    amount_toman=quote.final_toman,
                    status="AWAITING_PAYMENT",
                )
                session.add(order)
                await session.flush()
                session.add(PaymentRow(order_id=order.id, status="AWAITING_PAYMENT"))
                return order

    async def reveal_destination(self, telegram_id: int, order_id: UUID) -> tuple[str, str]:
        async with self.sessions() as session:
            user = await session.scalar(select(UserRow).where(UserRow.telegram_id == telegram_id))
            order = await session.get(OrderRow, order_id)
            if not user or not order or order.user_id != user.id:
                raise AccessDenied("ORDER_OWNER_REQUIRED")
            quote = await session.get(QuoteRow, order.quote_id)
            if (
                not quote.final_check_confirmed
                or quote.status != "ACTIVE"
                or self.now() >= quote.expires_at
                or order.status != "AWAITING_PAYMENT"
            ):
                raise AccessDenied("DESTINATION_FORBIDDEN")
            merchant = await session.scalar(
                select(MerchantCardRow)
                .where(MerchantCardRow.active.is_(True))
                .order_by(MerchantCardRow.priority)
                .limit(1)
            )
            if not merchant:
                raise InvalidState("DESTINATION_UNAVAILABLE")
            return self.vault.decrypt(merchant.encrypted_pan), merchant.holder_name

    async def requote(self, telegram_id: int, quote_id: UUID) -> QuoteRow:
        async with self.sessions.begin() as session:
            user = await self.user(telegram_id, session)
            old = await session.scalar(
                select(QuoteRow).where(QuoteRow.id == quote_id).with_for_update()
            )
            if not old or old.user_id != user.id or self.now() < old.expires_at:
                raise AccessDenied("REQUOTE_FORBIDDEN")
            old.status = "EXPIRED"
            reservation = await session.scalar(
                select(ReservationRow).where(
                    ReservationRow.quote_id == old.id, ReservationRow.released_at.is_(None)
                )
            )
            if reservation:
                reservation.released_at = self.now()
                product = await session.get(ProductRow, old.product_id, with_for_update=True)
                if not product.unlimited_stock:
                    product.reserved = max(0, product.reserved - reservation.quantity)
            product_id, card_id, version = old.product_id, old.selected_card_id, old.version + 1
        fresh = await self.create_quote(telegram_id, product_id, card_id)
        async with self.sessions.begin() as session:
            locked = await session.get(QuoteRow, fresh.id, with_for_update=True)
            locked.version = version
        fresh.version = version
        return fresh

    async def submit_receipt(
        self, telegram_id: int, order_id: UUID, file_id: str, unique_id: str, file_type: str
    ) -> PaymentRow:
        async with self.sessions.begin() as session:
            user = await self.user(telegram_id, session)
            order = await session.scalar(
                select(OrderRow).where(OrderRow.id == order_id).with_for_update()
            )
            if not order or order.user_id != user.id:
                raise AccessDenied("ORDER_OWNER_REQUIRED")
            quote = await session.get(QuoteRow, order.quote_id)
            payment = await session.scalar(
                select(PaymentRow).where(PaymentRow.order_id == order.id).with_for_update()
            )
            payment.receipt_file_id, payment.receipt_unique_id = file_id, unique_id
            payment.receipt_type, payment.submitted_at = file_type, self.now()
            late = self.now() >= quote.expires_at
            payment.status = "LATE_PAYMENT_REVIEW" if late else "AWAITING_RECONCILIATION"
            order.status = "MANUAL_REVIEW" if late else "AWAITING_RECONCILIATION"
            session.add(
                OutboxRow(
                    kind="PAYMENT_REVIEW",
                    chat_id=self.order_chat_id,
                    payload={
                        "order_id": str(order.id),
                        "receipt_file_id": file_id,
                        "warning": "receipt_is_not_payment_proof",
                    },
                    available_at=self.now(),
                )
            )
            await self.audit(session, telegram_id, "receipt.submit", str(order.id))
            return payment

    async def manual_reconcile(
        self, actor: int, order_id: UUID, approved: bool, reason: str
    ) -> None:
        self.owner(actor)
        if not reason.strip():
            raise InvalidState("REASON_REQUIRED")
        async with self.sessions.begin() as session:
            order = await session.scalar(
                select(OrderRow).where(OrderRow.id == order_id).with_for_update()
            )
            payment = await session.scalar(
                select(PaymentRow).where(PaymentRow.order_id == order_id).with_for_update()
            )
            if not payment.receipt_file_id:
                raise InvalidState("RECEIPT_REQUIRED")
            payment.status = "VERIFIED" if approved else "REJECTED"
            order.status = "READY_FOR_FULFILLMENT" if approved else "MANUAL_REVIEW"
            await self.audit(
                session,
                actor,
                "payment.manual_approve" if approved else "payment.manual_reject",
                str(order_id),
                reason,
            )
            if approved:
                session.add(
                    OutboxRow(
                        kind="FULFILLMENT_READY",
                        chat_id=self.order_chat_id,
                        payload={"order_id": str(order_id)},
                        available_at=self.now(),
                    )
                )

    async def claim(self, actor: int, order_id: UUID) -> bool:
        self.owner(actor)
        async with self.sessions.begin() as session:
            result = await session.execute(
                update(OrderRow)
                .where(
                    OrderRow.id == order_id,
                    OrderRow.status == "READY_FOR_FULFILLMENT",
                    OrderRow.assigned_admin_id.is_(None),
                )
                .values(status="PROCESSING", assigned_admin_id=actor, started_at=self.now())
            )
            if result.rowcount != 1:
                raise InvalidState("ALREADY_CLAIMED")
            await self.audit(session, actor, "order.claim", str(order_id))
            return True

    async def deliver(
        self, actor: int, order_id: UUID, content: str, activation_link: str | None = None
    ) -> None:
        self.owner(actor)
        if not content.strip():
            raise InvalidState("DELIVERY_CONTENT_REQUIRED")
        async with self.sessions.begin() as session:
            order = await session.scalar(
                select(OrderRow).where(OrderRow.id == order_id).with_for_update()
            )
            if not order or order.status != "PROCESSING" or order.assigned_admin_id != actor:
                raise AccessDenied("CLAIMING_ADMIN_REQUIRED")
            user = await session.get(UserRow, order.user_id)
            session.add(
                DeliveryRow(
                    order_id=order.id,
                    text=content,
                    activation_link=activation_link,
                    delivered_at=self.now(),
                )
            )
            order.status = "DELIVERED"
            session.add(
                OutboxRow(
                    kind="ORDER_DELIVERED",
                    chat_id=user.telegram_id,
                    payload={
                        "order_id": str(order.id),
                        "content": content,
                        "activation_link": activation_link,
                    },
                    available_at=self.now(),
                )
            )
            await self.audit(session, actor, "order.deliver", str(order.id))

    async def expire_quotes(self) -> int:
        async with self.sessions.begin() as session:
            rows = list(
                (
                    await session.scalars(
                        select(QuoteRow)
                        .where(QuoteRow.status == "ACTIVE", QuoteRow.expires_at <= self.now())
                        .with_for_update()
                    )
                ).all()
            )
            for quote in rows:
                quote.status = "EXPIRED"
                reservation = await session.scalar(
                    select(ReservationRow).where(
                        ReservationRow.quote_id == quote.id, ReservationRow.released_at.is_(None)
                    )
                )
                if reservation:
                    reservation.released_at = self.now()
                    product = await session.get(
                        ProductRow, reservation.product_id, with_for_update=True
                    )
                    if not product.unlimited_stock:
                        product.reserved = max(0, product.reserved - reservation.quantity)
                order = await session.scalar(select(OrderRow).where(OrderRow.quote_id == quote.id))
                if order and order.status == "AWAITING_PAYMENT":
                    order.status = "PAYMENT_EXPIRED"
            return len(rows)

    async def audit(
        self, session: AsyncSession, actor: int, action: str, target: str, detail: str = ""
    ) -> None:
        session.add(
            AuditRow(
                at=self.now(), actor_id=actor, action=action, target=target, detail=detail[:500]
            )
        )
