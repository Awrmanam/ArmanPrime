from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class UserRow(Base):
    __tablename__ = "users"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    kyc_status: Mapped[str] = mapped_column(String(32), default="NOT_STARTED", index=True)
    risk_status: Mapped[str] = mapped_column(String(16), default="CLEAR")


class TermsRow(Base):
    __tablename__ = "terms_versions"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    version: Mapped[int] = mapped_column(Integer, unique=True)
    title: Mapped[str] = mapped_column(Text)
    pages: Mapped[list[str]] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64), unique=True)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    published: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class ConsentRow(Base):
    __tablename__ = "consents"
    __table_args__ = (UniqueConstraint("user_id", "terms_id", name="uq_consent_user_terms"),)
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    terms_id: Mapped[UUID] = mapped_column(ForeignKey("terms_versions.id"))
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class KYCRow(Base):
    __tablename__ = "kyc_submissions"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    file_id: Mapped[str] = mapped_column(Text)
    file_unique_id: Mapped[str] = mapped_column(Text, unique=True)
    file_type: Mapped[str] = mapped_column(String(16))
    evidence_level: Mapped[str] = mapped_column(String(32), default="FORMAT_VALID")
    reviewer_id: Mapped[int | None] = mapped_column(BigInteger)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CustomerCardRow(Base):
    __tablename__ = "customer_cards"
    __table_args__ = (Index("ix_customer_cards_owner_status", "user_id", "status"),)
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    bank_name: Mapped[str] = mapped_column(Text)
    encrypted_pan: Mapped[str] = mapped_column(Text)
    masked_pan: Mapped[str] = mapped_column(String(32))
    last4: Mapped[str] = mapped_column(String(4))
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    evidence_file_id: Mapped[str] = mapped_column(Text)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_by: Mapped[int | None] = mapped_column(BigInteger)


class MerchantCardRow(Base):
    __tablename__ = "merchant_cards"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    bank_name: Mapped[str] = mapped_column(Text)
    holder_name: Mapped[str] = mapped_column(Text)
    encrypted_pan: Mapped[str] = mapped_column(Text)
    masked_pan: Mapped[str] = mapped_column(String(32))
    priority: Mapped[int] = mapped_column(Integer, default=0)
    daily_limit: Mapped[int] = mapped_column(BigInteger, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


class CategoryRow(Base):
    __tablename__ = "categories"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    custom_emoji_id: Mapped[str | None] = mapped_column(Text)


class ProductRow(Base):
    __tablename__ = "products"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    category_id: Mapped[UUID] = mapped_column(ForeignKey("categories.id"), index=True)
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, default="")
    base_price_usd: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    fixed_price_toman: Mapped[int | None] = mapped_column(BigInteger)
    duration: Mapped[str | None] = mapped_column(Text)
    plan_type: Mapped[str | None] = mapped_column(Text)
    activation_method: Mapped[str | None] = mapped_column(Text)
    warranty_text: Mapped[str | None] = mapped_column(Text)
    warranty_days: Mapped[int] = mapped_column(Integer, default=0)
    delivery_minutes: Mapped[int] = mapped_column(Integer, default=0)
    stock: Mapped[int] = mapped_column(Integer, default=0)
    reserved: Mapped[int] = mapped_column(Integer, default=0)
    unlimited_stock: Mapped[bool] = mapped_column(Boolean, default=False)
    requires_kyc: Mapped[bool] = mapped_column(Boolean, default=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    custom_emoji_id: Mapped[str | None] = mapped_column(Text)
    pricing_override: Mapped[dict | None] = mapped_column(JSONB)


class ConfigRow(Base):
    __tablename__ = "configuration"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EmojiRow(Base):
    __tablename__ = "premium_emojis"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(Text, unique=True)
    custom_emoji_id: Mapped[str] = mapped_column(Text, unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class PageRow(Base):
    __tablename__ = "pages"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    slug: Mapped[str] = mapped_column(String(100), unique=True)
    text: Mapped[str] = mapped_column(Text)


class ButtonRow(Base):
    __tablename__ = "page_buttons"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    page_id: Mapped[UUID] = mapped_column(ForeignKey("pages.id", ondelete="CASCADE"), index=True)
    text: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    row: Mapped[int] = mapped_column(Integer)
    position: Mapped[int] = mapped_column(Integer)
    style: Mapped[str] = mapped_column(String(16), default="default")
    custom_emoji_id: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class RateRow(Base):
    __tablename__ = "currency_rates"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    usd_to_toman: Mapped[int] = mapped_column(BigInteger)
    source: Mapped[str] = mapped_column(String(32), default="manual")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class QuoteRow(Base):
    __tablename__ = "price_quotes"
    __table_args__ = (Index("ix_quotes_expiry_status", "expires_at", "status"),)
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    product_id: Mapped[UUID] = mapped_column(ForeignKey("products.id"))
    selected_card_id: Mapped[UUID] = mapped_column(ForeignKey("customer_cards.id"))
    snapshot: Mapped[dict] = mapped_column(JSONB)
    rate: Mapped[int] = mapped_column(BigInteger)
    final_toman: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE")
    version: Mapped[int] = mapped_column(Integer, default=1)
    final_check_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)


class ReservationRow(Base):
    __tablename__ = "inventory_reservations"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    quote_id: Mapped[UUID] = mapped_column(ForeignKey("price_quotes.id"), unique=True)
    product_id: Mapped[UUID] = mapped_column(ForeignKey("products.id"), index=True)
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OrderRow(Base):
    __tablename__ = "orders"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    quote_id: Mapped[UUID] = mapped_column(ForeignKey("price_quotes.id"), unique=True)
    amount_toman: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(32), index=True)
    assigned_admin_id: Mapped[int | None] = mapped_column(BigInteger)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PaymentRow(Base):
    __tablename__ = "payments"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    order_id: Mapped[UUID] = mapped_column(ForeignKey("orders.id"), unique=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    provider_reference: Mapped[str | None] = mapped_column(Text, unique=True)
    receipt_file_id: Mapped[str | None] = mapped_column(Text)
    receipt_unique_id: Mapped[str | None] = mapped_column(Text, unique=True)
    receipt_type: Mapped[str | None] = mapped_column(String(16))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DeliveryRow(Base):
    __tablename__ = "deliveries"
    order_id: Mapped[UUID] = mapped_column(ForeignKey("orders.id"), primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    activation_link: Mapped[str | None] = mapped_column(Text)
    delivered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OutboxRow(Base):
    __tablename__ = "notification_outbox"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    payload: Mapped[dict] = mapped_column(JSONB)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    dead_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditRow(Base):
    __tablename__ = "audit_log"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    actor_id: Mapped[int] = mapped_column(BigInteger, index=True)
    action: Mapped[str] = mapped_column(Text)
    target: Mapped[str] = mapped_column(Text)
    detail: Mapped[str] = mapped_column(Text, default="")


def create_engine_and_session(url: str) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=20)
    return engine, async_sessionmaker(engine, expire_on_commit=False)
