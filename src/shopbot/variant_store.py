from __future__ import annotations

import json
import re
import secrets
from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .db import (
    CategoryRow,
    ConfigRow,
    ConsentRow,
    CustomerCardRow,
    MerchantCardRow,
    OrderRow,
    PaymentRow,
    ProductRow,
    QuoteRow,
    RateRow,
    ReservationRow,
    UserRow,
)
from .domain import PricingRule, calculate_price, decimal_value
from .fx import validate_currency
from .repository import AccessDenied, InvalidState, ShopRepository

metadata = MetaData()

families = Table(
    "product_families",
    metadata,
    Column("id", PGUUID(as_uuid=True), primary_key=True),
    Column("category_id", PGUUID(as_uuid=True), nullable=False),
    Column("title", Text, nullable=False),
    Column("description", Text, nullable=False),
    Column("active", Boolean, nullable=False),
    Column("position", Integer, nullable=False),
    Column("button_emoji_key", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

variants = Table(
    "product_variants",
    metadata,
    Column("id", PGUUID(as_uuid=True), primary_key=True),
    Column("family_id", PGUUID(as_uuid=True), nullable=False),
    Column("legacy_product_id", PGUUID(as_uuid=True), nullable=False),
    Column("title", Text, nullable=False),
    Column("description", Text, nullable=False),
    Column("activation_method", String(48), nullable=False),
    Column("fulfillment_type", String(48), nullable=False),
    Column("payment_method", String(32), nullable=False),
    Column("delivery_type", String(24), nullable=False),
    Column("delivery_min", Integer),
    Column("delivery_max", Integer),
    Column("delivery_unit", String(16)),
    Column("delivery_text", Text),
    Column("warranty_type", String(24), nullable=False),
    Column("warranty_days", Integer, nullable=False),
    Column("warranty_text", Text),
    Column("requires_kyc", Boolean, nullable=False),
    Column("requires_verified_source_card", Boolean, nullable=False),
    Column("active", Boolean, nullable=False),
    Column("position", Integer, nullable=False),
    Column("button_emoji_key", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

checkout_fields = Table(
    "checkout_fields",
    metadata,
    Column("id", PGUUID(as_uuid=True), primary_key=True),
    Column("variant_id", PGUUID(as_uuid=True), nullable=False),
    Column("field_key", String(64), nullable=False),
    Column("label", Text, nullable=False),
    Column("field_type", String(32), nullable=False),
    Column("required", Boolean, nullable=False),
    Column("sensitive", Boolean, nullable=False),
    Column("help_text", Text),
    Column("options", JSONB),
    Column("position", Integer, nullable=False),
    Column("delete_after_fulfillment", Boolean, nullable=False),
    UniqueConstraint("variant_id", "field_key", name="uq_checkout_field_variant_key"),
)

suppliers = Table(
    "suppliers",
    metadata,
    Column("id", PGUUID(as_uuid=True), primary_key=True),
    Column("name", Text, nullable=False),
    Column("marketplace", String(64), nullable=False),
    Column("active", Boolean, nullable=False),
    Column("notes", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

supplier_offers = Table(
    "supplier_offers",
    metadata,
    Column("id", PGUUID(as_uuid=True), primary_key=True),
    Column("variant_id", PGUUID(as_uuid=True), nullable=False),
    Column("supplier_id", PGUUID(as_uuid=True), nullable=False),
    Column("supplier_url", Text),
    Column("cost_amount", Numeric(24, 8), nullable=False),
    Column("cost_currency", String(3), nullable=False),
    Column("priority", Integer, nullable=False),
    Column("delivery_mode", String(24), nullable=False),
    Column("warranty_text", Text),
    Column("active", Boolean, nullable=False),
    Column("metadata", JSONB),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

checkout_sessions = Table(
    "variant_checkout_sessions",
    metadata,
    Column("id", PGUUID(as_uuid=True), primary_key=True),
    Column("user_id", PGUUID(as_uuid=True), nullable=False),
    Column("variant_id", PGUUID(as_uuid=True), nullable=False),
    Column("quote_id", PGUUID(as_uuid=True)),
    Column("order_id", PGUUID(as_uuid=True)),
    Column("status", String(24), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
)

checkout_values = Table(
    "variant_checkout_values",
    metadata,
    Column("id", PGUUID(as_uuid=True), primary_key=True),
    Column(
        "checkout_session_id",
        PGUUID(as_uuid=True),
        nullable=False,
    ),
    Column("field_id", PGUUID(as_uuid=True), nullable=False),
    Column("value_text", Text),
    Column("value_ciphertext", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("redacted_at", DateTime(timezone=True)),
)


ACTIVATION_LABELS = {
    "activation_code": "کد فعال‌سازی",
    "activation_link": "لینک/گیفت فعال‌سازی",
    "account_no_login": "فعال‌سازی روی حساب بدون دریافت رمز",
    "payment_link": "فعال‌سازی با Payment Link",
    "account_login": "فعال‌سازی با ورود به حساب",
    "account_credentials": "تحویل اکانت آماده",
    "custom": "روش سفارشی",
}

FIELD_TYPES = {
    "TEXT",
    "EMAIL",
    "PASSWORD",
    "URL",
    "TELEGRAM_USERNAME",
    "SELECT",
    "BOOLEAN",
    "SESSION_JSON",
}


class VariantStore:
    CHECKOUT_TTL = timedelta(hours=24)

    def __init__(self, repo: ShopRepository):
        self.repo = repo

    def now(self):
        return self.repo.now()

    async def issue_callback(
        self,
        action: str,
        actor_id: int,
        object_id: str = "",
        *,
        one_time: bool = False,
        ttl: int = 1800,
    ) -> str:
        opaque = secrets.token_urlsafe(12)
        state = json.dumps(
            {"a": action, "u": actor_id, "o": object_id, "once": one_time},
            separators=(",", ":"),
        )
        await self.repo.coordinator.redis.set(f"vcallback:{opaque}", state, ex=ttl)
        token = f"v1.{opaque}"
        if len(token.encode()) > 64:
            raise AssertionError("variant callback exceeds Telegram limit")
        return token

    async def resolve_callback(self, token: str, actor_id: int) -> dict:
        if not token.startswith("v1.") or len(token.encode()) > 64:
            raise AccessDenied("VARIANT_CALLBACK_INVALID")
        opaque = token[3:]
        key = f"vcallback:{opaque}"
        raw = await self.repo.coordinator.redis.get(key)
        if not raw:
            raise AccessDenied("VARIANT_CALLBACK_EXPIRED")
        state = json.loads(raw)
        if int(state["u"]) != actor_id:
            raise AccessDenied("VARIANT_CALLBACK_OWNER_REQUIRED")
        if state.get("once") and await self.repo.coordinator.redis.delete(key) != 1:
            raise AccessDenied("VARIANT_CALLBACK_REPLAYED")
        return state

    async def storefront_families(self, category_id: UUID) -> list[dict]:
        async with self.repo.sessions() as session:
            result = await session.execute(
                select(families)
                .where(families.c.category_id == category_id, families.c.active.is_(True))
                .order_by(families.c.position, families.c.created_at)
            )
            return [dict(row) for row in result.mappings().all()]

    async def owner_families(self) -> list[dict]:
        async with self.repo.sessions() as session:
            result = await session.execute(
                select(families).order_by(families.c.category_id, families.c.position)
            )
            return [dict(row) for row in result.mappings().all()]

    async def family(self, family_id: UUID) -> dict | None:
        async with self.repo.sessions() as session:
            result = await session.execute(
                select(families).where(families.c.id == family_id)
            )
            row = result.mappings().first()
            return dict(row) if row else None

    async def family_variants(self, family_id: UUID, *, owner: bool = False) -> list[dict]:
        async with self.repo.sessions() as session:
            statement = select(variants).where(variants.c.family_id == family_id)
            if not owner:
                statement = statement.where(variants.c.active.is_(True))
            result = await session.execute(
                statement.order_by(variants.c.position, variants.c.created_at)
            )
            return [dict(row) for row in result.mappings().all()]

    async def variant(self, variant_id: UUID) -> dict | None:
        async with self.repo.sessions() as session:
            result = await session.execute(
                select(variants).where(variants.c.id == variant_id)
            )
            row = result.mappings().first()
            return dict(row) if row else None

    async def variant_with_family(self, variant_id: UUID) -> dict | None:
        async with self.repo.sessions() as session:
            statement = (
                select(
                    variants,
                    families.c.title.label("family_title"),
                    families.c.description.label("family_description"),
                    families.c.category_id.label("category_id"),
                )
                .join(families, families.c.id == variants.c.family_id)
                .where(variants.c.id == variant_id)
            )
            row = (await session.execute(statement)).mappings().first()
            return dict(row) if row else None

    async def legacy_products_for_category(self, category_id: UUID) -> list[ProductRow]:
        async with self.repo.sessions() as session:
            managed = select(variants.c.legacy_product_id)
            return list(
                (
                    await session.scalars(
                        select(ProductRow)
                        .where(
                            ProductRow.category_id == category_id,
                            ProductRow.active.is_(True),
                            ProductRow.id.not_in(managed),
                        )
                        .order_by(ProductRow.position, ProductRow.title)
                    )
                ).all()
            )

    async def variant_fields(self, variant_id: UUID) -> list[dict]:
        async with self.repo.sessions() as session:
            result = await session.execute(
                select(checkout_fields)
                .where(checkout_fields.c.variant_id == variant_id)
                .order_by(checkout_fields.c.position, checkout_fields.c.field_key)
            )
            return [dict(row) for row in result.mappings().all()]

    async def offers(self, variant_id: UUID) -> list[dict]:
        async with self.repo.sessions() as session:
            result = await session.execute(
                select(
                    supplier_offers,
                    suppliers.c.name.label("supplier_name"),
                    suppliers.c.marketplace.label("marketplace"),
                )
                .join(suppliers, suppliers.c.id == supplier_offers.c.supplier_id)
                .where(supplier_offers.c.variant_id == variant_id)
                .order_by(supplier_offers.c.priority, supplier_offers.c.created_at)
            )
            return [dict(row) for row in result.mappings().all()]

    async def create_family(
        self,
        actor: int,
        category_id: UUID,
        title: str,
        description: str,
        *,
        position: int = 0,
        button_emoji_key: str | None = None,
    ) -> UUID:
        self.repo.owner(actor)
        if not title.strip() or position < 0:
            raise InvalidState("INVALID_PRODUCT_FAMILY")
        if button_emoji_key and not await self.repo.resolve_emoji_key(button_emoji_key):
            raise InvalidState("ACTIVE_EMOJI_REQUIRED")
        family_id = uuid4()
        async with self.repo.sessions.begin() as session:
            if not await session.get(CategoryRow, category_id):
                raise InvalidState("CATEGORY_NOT_FOUND")
            await session.execute(
                families.insert().values(
                    id=family_id,
                    category_id=category_id,
                    title=title.strip(),
                    description=description.strip(),
                    active=True,
                    position=position,
                    button_emoji_key=button_emoji_key,
                    created_at=self.now(),
                )
            )
            await self.repo.audit(session, actor, "product_family.create", str(family_id))
        return family_id

    async def set_family_active(self, actor: int, family_id: UUID, active: bool) -> None:
        self.repo.owner(actor)
        async with self.repo.sessions.begin() as session:
            result = await session.execute(
                update(families).where(families.c.id == family_id).values(active=active)
            )
            if result.rowcount != 1:
                raise InvalidState("PRODUCT_FAMILY_NOT_FOUND")
            await self.repo.audit(
                session, actor, "product_family.status", str(family_id), f"active={active}"
            )

    @staticmethod
    def delivery_label(item: dict) -> str:
        kind = item.get("delivery_type")
        if kind == "instant":
            return item.get("delivery_text") or "آنی"
        if kind == "range":
            low, high = item.get("delivery_min"), item.get("delivery_max")
            unit = {"minute": "دقیقه", "hour": "ساعت", "day": "روز"}.get(
                item.get("delivery_unit"), item.get("delivery_unit") or ""
            )
            if low == high:
                return f"حدود {low} {unit}"
            return f"{low} تا {high} {unit}"
        return item.get("delivery_text") or "طبق توضیحات سفارش"

    @staticmethod
    def warranty_label(item: dict) -> str:
        kind = item.get("warranty_type")
        if kind == "none":
            return "بدون گارانتی"
        if kind == "days":
            return item.get("warranty_text") or f"{item.get('warranty_days', 0)} روز"
        if kind == "subscription":
            return item.get("warranty_text") or "تا پایان مدت اشتراک"
        return item.get("warranty_text") or "طبق شرایط اعلام‌شده"

    @staticmethod
    def _delivery_minutes(data: dict) -> int:
        if data.get("delivery_type") != "range":
            return 0
        maximum = int(data.get("delivery_max") or 0)
        multiplier = {"minute": 1, "hour": 60, "day": 1440}.get(data.get("delivery_unit"), 1)
        return maximum * multiplier

    async def create_variant_bundle(
        self,
        actor: int,
        family_id: UUID,
        data: dict,
        fields: list[dict],
    ) -> UUID:
        self.repo.owner(actor)
        family = await self.family(family_id)
        if not family:
            raise InvalidState("PRODUCT_FAMILY_NOT_FOUND")
        title = str(data.get("title", "")).strip()
        if not title:
            raise InvalidState("INVALID_VARIANT")
        currency = validate_currency(data.get("cost_currency", "USD"))
        cost = decimal_value(data.get("cost_amount", "0"))
        if cost < 0:
            raise InvalidState("INVALID_SUPPLIER_COST")
        fixed_price = data.get("fixed_price_toman")
        if fixed_price not in {None, ""} and int(fixed_price) < 0:
            raise InvalidState("INVALID_FIXED_PRICE")
        if data.get("button_emoji_key") and not await self.repo.resolve_emoji_key(
            data["button_emoji_key"]
        ):
            raise InvalidState("ACTIVE_EMOJI_REQUIRED")
        if data.get("fulfillment_type") not in ACTIVATION_LABELS:
            raise InvalidState("INVALID_FULFILLMENT_TYPE")

        variant_id = uuid4()
        product_id = uuid4()
        supplier_id = uuid4()
        offer_id = uuid4()
        now = self.now()
        warranty_text = self.warranty_label(data)
        delivery_text = self.delivery_label(data)
        stock = int(data.get("stock", 0))
        unlimited = bool(data.get("unlimited_stock", True))
        if stock < 0:
            raise InvalidState("INVALID_STOCK")

        async with self.repo.sessions.begin() as session:
            product = ProductRow(
                id=product_id,
                category_id=family["category_id"],
                title=f"{family['title']} — {title}",
                description=str(data.get("description", "")),
                base_price_usd=cost if currency == "USD" else Decimal("0"),
                base_cost_amount=cost,
                base_cost_currency=currency,
                currency_buffer_percent=Decimal("0"),
                fixed_price_toman=int(fixed_price) if fixed_price not in {None, ""} else None,
                duration=data.get("duration"),
                plan_type=title,
                activation_method=ACTIVATION_LABELS[data["fulfillment_type"]],
                warranty_text=warranty_text,
                warranty_days=int(data.get("warranty_days", 0)),
                delivery_minutes=self._delivery_minutes(data),
                stock=stock,
                reserved=0,
                unlimited_stock=unlimited,
                requires_kyc=bool(data.get("requires_kyc", False)),
                requires_verified_source_card=bool(
                    data.get("requires_verified_source_card", True)
                ),
                active=True,
                position=int(data.get("position", 0)),
                custom_emoji_id=data.get("button_emoji_key"),
                pricing_override=None,
            )
            session.add(product)
            await session.flush()

            await session.execute(
                variants.insert().values(
                    id=variant_id,
                    family_id=family_id,
                    legacy_product_id=product_id,
                    title=title,
                    description=str(data.get("description", "")),
                    activation_method=ACTIVATION_LABELS[data["fulfillment_type"]],
                    fulfillment_type=data["fulfillment_type"],
                    payment_method=data.get("payment_method", "card_to_card"),
                    delivery_type=data.get("delivery_type", "instant"),
                    delivery_min=data.get("delivery_min"),
                    delivery_max=data.get("delivery_max"),
                    delivery_unit=data.get("delivery_unit"),
                    delivery_text=delivery_text,
                    warranty_type=data.get("warranty_type", "none"),
                    warranty_days=int(data.get("warranty_days", 0)),
                    warranty_text=warranty_text,
                    requires_kyc=bool(data.get("requires_kyc", False)),
                    requires_verified_source_card=bool(
                        data.get("requires_verified_source_card", True)
                    ),
                    active=True,
                    position=int(data.get("position", 0)),
                    button_emoji_key=data.get("button_emoji_key"),
                    created_at=now,
                )
            )

            supplier_name = str(data.get("supplier_name", "Manual Supplier")).strip()
            marketplace = str(data.get("marketplace", "manual")).strip().lower()
            existing_supplier = (
                await session.execute(
                    select(suppliers).where(
                        func.lower(suppliers.c.name) == supplier_name.lower(),
                        suppliers.c.marketplace == marketplace,
                    )
                )
            ).mappings().first()
            if existing_supplier:
                supplier_id = existing_supplier["id"]
            else:
                await session.execute(
                    suppliers.insert().values(
                        id=supplier_id,
                        name=supplier_name,
                        marketplace=marketplace,
                        active=True,
                        notes=None,
                        created_at=now,
                    )
                )
            await session.execute(
                supplier_offers.insert().values(
                    id=offer_id,
                    variant_id=variant_id,
                    supplier_id=supplier_id,
                    supplier_url=data.get("supplier_url") or None,
                    cost_amount=cost,
                    cost_currency=currency,
                    priority=1,
                    delivery_mode=data.get("supplier_delivery_mode", "manual"),
                    warranty_text=data.get("supplier_warranty_text") or None,
                    active=True,
                    metadata={},
                    created_at=now,
                )
            )

            for index, item in enumerate(fields):
                field_type = str(item.get("field_type", "TEXT")).upper()
                if field_type not in FIELD_TYPES:
                    raise InvalidState("INVALID_CHECKOUT_FIELD_TYPE")
                key = str(item.get("field_key") or f"field_{index + 1}").strip()
                if not re.fullmatch(r"[a-z0-9_]{2,64}", key):
                    raise InvalidState("INVALID_CHECKOUT_FIELD_KEY")
                sensitive = bool(item.get("sensitive", False))
                await session.execute(
                    checkout_fields.insert().values(
                        id=uuid4(),
                        variant_id=variant_id,
                        field_key=key,
                        label=str(item.get("label") or key).strip(),
                        field_type=field_type,
                        required=bool(item.get("required", True)),
                        sensitive=sensitive,
                        help_text=item.get("help_text") or None,
                        options=item.get("options"),
                        position=int(item.get("position", index)),
                        delete_after_fulfillment=bool(
                            item.get("delete_after_fulfillment", sensitive)
                        ),
                    )
                )
            await self.repo.audit(session, actor, "product_variant.create", str(variant_id))
        return variant_id

    async def set_variant_active(self, actor: int, variant_id: UUID, active: bool) -> None:
        self.repo.owner(actor)
        item = await self.variant(variant_id)
        if not item:
            raise InvalidState("VARIANT_NOT_FOUND")
        async with self.repo.sessions.begin() as session:
            await session.execute(
                update(variants).where(variants.c.id == variant_id).values(active=active)
            )
            product = await session.get(ProductRow, item["legacy_product_id"], with_for_update=True)
            if product:
                product.active = active
            await self.repo.audit(
                session, actor, "product_variant.status", str(variant_id), f"active={active}"
            )

    async def add_field(self, actor: int, variant_id: UUID, data: dict) -> UUID:
        self.repo.owner(actor)
        field_type = str(data.get("field_type", "TEXT")).upper()
        if field_type not in FIELD_TYPES:
            raise InvalidState("INVALID_CHECKOUT_FIELD_TYPE")
        label = str(data.get("label", "")).strip()
        if not label:
            raise InvalidState("INVALID_CHECKOUT_FIELD")
        field_id = uuid4()
        key = str(data.get("field_key") or f"custom_{field_id.hex[:10]}")
        if not re.fullmatch(r"[a-z0-9_]{2,64}", key):
            raise InvalidState("INVALID_CHECKOUT_FIELD_KEY")
        async with self.repo.sessions.begin() as session:
            await session.execute(
                checkout_fields.insert().values(
                    id=field_id,
                    variant_id=variant_id,
                    field_key=key,
                    label=label,
                    field_type=field_type,
                    required=bool(data.get("required", True)),
                    sensitive=bool(data.get("sensitive", False)),
                    help_text=data.get("help_text"),
                    options=data.get("options"),
                    position=int(data.get("position", 100)),
                    delete_after_fulfillment=bool(
                        data.get("delete_after_fulfillment", data.get("sensitive", False))
                    ),
                )
            )
            await self.repo.audit(session, actor, "checkout_field.create", str(field_id))
        return field_id

    async def add_offer(self, actor: int, variant_id: UUID, data: dict) -> UUID:
        self.repo.owner(actor)
        currency = validate_currency(data.get("cost_currency", "USD"))
        cost = decimal_value(data.get("cost_amount", "0"))
        if cost < 0:
            raise InvalidState("INVALID_SUPPLIER_COST")
        name = str(data.get("supplier_name", "")).strip()
        marketplace = str(data.get("marketplace", "manual")).strip().lower()
        if not name:
            raise InvalidState("INVALID_SUPPLIER")
        now = self.now()
        offer_id = uuid4()
        async with self.repo.sessions.begin() as session:
            supplier = (
                await session.execute(
                    select(suppliers).where(
                        func.lower(suppliers.c.name) == name.lower(),
                        suppliers.c.marketplace == marketplace,
                    )
                )
            ).mappings().first()
            if supplier:
                supplier_id = supplier["id"]
            else:
                supplier_id = uuid4()
                await session.execute(
                    suppliers.insert().values(
                        id=supplier_id,
                        name=name,
                        marketplace=marketplace,
                        active=True,
                        notes=None,
                        created_at=now,
                    )
                )
            await session.execute(
                supplier_offers.insert().values(
                    id=offer_id,
                    variant_id=variant_id,
                    supplier_id=supplier_id,
                    supplier_url=data.get("supplier_url") or None,
                    cost_amount=cost,
                    cost_currency=currency,
                    priority=max(1, int(data.get("priority", 1))),
                    delivery_mode=data.get("delivery_mode", "manual"),
                    warranty_text=data.get("warranty_text"),
                    active=True,
                    metadata={},
                    created_at=now,
                )
            )
            await self.repo.audit(session, actor, "supplier_offer.create", str(offer_id))
        return offer_id

    async def estimate_price(self, variant_id: UUID) -> int:
        item = await self.variant(variant_id)
        if not item:
            raise InvalidState("VARIANT_NOT_FOUND")
        async with self.repo.sessions() as session:
            product = await session.get(ProductRow, item["legacy_product_id"])
            if not product or not product.active:
                raise InvalidState("PRODUCT_UNAVAILABLE")
            if product.fixed_price_toman is not None:
                return int(product.fixed_price_toman)
            rate = await session.scalar(
                select(RateRow)
                .where(
                    RateRow.currency_code == product.base_cost_currency,
                    RateRow.active.is_(True),
                )
                .order_by(
                    (RateRow.source == "manual_override").desc(),
                    RateRow.version.desc(),
                )
                .limit(1)
            )
            if not rate:
                raise InvalidState("CURRENCY_RATE_NOT_CONFIGURED")
            if rate.source == "api" and rate.valid_until <= self.now():
                raise InvalidState("CURRENCY_RATE_STALE")
            pricing = await session.get(ConfigRow, "pricing.global")
            if not pricing:
                raise InvalidState("PRICING_NOT_CONFIGURED")
            config = dict(pricing.value)
            config.update(product.pricing_override or {})
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
                rounding_increment_toman=int(config.get("rounding_increment_toman", 1)),
            )
            buffer = product.currency_buffer_percent + rate.buffer_percent
            return calculate_price(product.base_cost_amount, rate.toman_per_unit, rule, buffer)

    async def start_checkout(self, telegram_id: int, variant_id: UUID) -> UUID:
        item = await self.variant_with_family(variant_id)
        if not item or not item["active"]:
            raise InvalidState("VARIANT_UNAVAILABLE")
        now = self.now()
        checkout_id = uuid4()
        async with self.repo.sessions.begin() as session:
            user = await self.repo.user(telegram_id, session)
            await session.execute(
                update(checkout_sessions)
                .where(
                    checkout_sessions.c.user_id == user.id,
                    checkout_sessions.c.status.in_(
                        ("INPUT", "READY", "WAITING_GATE", "QUOTED")
                    ),
                )
                .values(status="ABANDONED")
            )
            await session.execute(
                checkout_sessions.insert().values(
                    id=checkout_id,
                    user_id=user.id,
                    variant_id=variant_id,
                    quote_id=None,
                    order_id=None,
                    status="INPUT",
                    created_at=now,
                    expires_at=now + self.CHECKOUT_TTL,
                )
            )
        return checkout_id

    async def checkout(self, checkout_id: UUID, telegram_id: int | None = None) -> dict | None:
        async with self.repo.sessions.begin() as session:
            statement = (
                select(
                    checkout_sessions,
                    variants.c.legacy_product_id,
                    variants.c.family_id,
                    variants.c.title.label("variant_title"),
                    variants.c.description.label("variant_description"),
                    variants.c.activation_method,
                    variants.c.fulfillment_type,
                    variants.c.payment_method,
                    variants.c.delivery_type,
                    variants.c.delivery_min,
                    variants.c.delivery_max,
                    variants.c.delivery_unit,
                    variants.c.delivery_text,
                    variants.c.warranty_type,
                    variants.c.warranty_days,
                    variants.c.warranty_text,
                    variants.c.requires_kyc,
                    variants.c.requires_verified_source_card,
                    families.c.title.label("family_title"),
                    families.c.description.label("family_description"),
                    UserRow.telegram_id.label("telegram_id"),
                    UserRow.kyc_status.label("kyc_status"),
                    UserRow.risk_status.label("risk_status"),
                )
                .join(variants, variants.c.id == checkout_sessions.c.variant_id)
                .join(families, families.c.id == variants.c.family_id)
                .join(UserRow, UserRow.id == checkout_sessions.c.user_id)
                .where(checkout_sessions.c.id == checkout_id)
            )
            if telegram_id is not None:
                statement = statement.where(UserRow.telegram_id == telegram_id)
            row = (await session.execute(statement)).mappings().first()
            if not row:
                return None
            item = dict(row)
            if item["expires_at"] <= self.now() and item["status"] not in {"ORDERED", "DELIVERED"}:
                await session.execute(
                    update(checkout_sessions)
                    .where(checkout_sessions.c.id == checkout_id)
                    .values(status="EXPIRED")
                )
                item["status"] = "EXPIRED"
            return item

    async def pending_for_legacy_product(
        self, telegram_id: int, legacy_product_id: UUID
    ) -> dict | None:
        async with self.repo.sessions() as session:
            statement = (
                select(checkout_sessions)
                .join(UserRow, UserRow.id == checkout_sessions.c.user_id)
                .join(variants, variants.c.id == checkout_sessions.c.variant_id)
                .where(
                    UserRow.telegram_id == telegram_id,
                    variants.c.legacy_product_id == legacy_product_id,
                    checkout_sessions.c.status.in_(("READY", "WAITING_GATE", "QUOTED")),
                    checkout_sessions.c.expires_at > self.now(),
                )
                .order_by(checkout_sessions.c.created_at.desc())
                .limit(1)
            )
            row = (await session.execute(statement)).mappings().first()
            return dict(row) if row else None

    @staticmethod
    def validate_field(field: dict, value: str) -> str:
        value = value.strip()
        if not value and not field["required"]:
            return ""
        if not value:
            raise InvalidState("FIELD_REQUIRED")
        field_type = field["field_type"]
        if len(value) > 4096:
            raise InvalidState("FIELD_TOO_LONG")
        if field_type == "EMAIL" and (
            "@" not in value or value.startswith("@") or value.endswith("@")
        ):
            raise InvalidState("INVALID_EMAIL")
        if field_type == "URL" and not value.startswith(("https://", "http://")):
            raise InvalidState("INVALID_URL")
        if field_type == "TELEGRAM_USERNAME":
            value = value.removeprefix("@")
            if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", value):
                raise InvalidState("INVALID_TELEGRAM_USERNAME")
            value = f"@{value}"
        if field_type == "BOOLEAN":
            normalized = value.lower()
            if normalized in {"بله", "yes", "y", "1", "true"}:
                value = "yes"
            elif normalized in {"خیر", "no", "n", "0", "false"}:
                value = "no"
            else:
                raise InvalidState("INVALID_BOOLEAN")
        if field_type == "SESSION_JSON":
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as exc:
                raise InvalidState("INVALID_SESSION_JSON") from exc
            if not isinstance(parsed, (dict, list)):
                raise InvalidState("INVALID_SESSION_JSON")
            value = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
        if field_type == "SELECT":
            options = field.get("options") or []
            if value not in options:
                raise InvalidState("INVALID_SELECTION")
        return value

    async def save_field_value(
        self,
        checkout_id: UUID,
        telegram_id: int,
        field_id: UUID,
        value: str,
    ) -> None:
        checkout = await self.checkout(checkout_id, telegram_id)
        if not checkout or checkout["status"] not in {"INPUT", "READY"}:
            raise AccessDenied("CHECKOUT_SESSION_INVALID")
        async with self.repo.sessions.begin() as session:
            field = (
                await session.execute(
                    select(checkout_fields).where(
                        checkout_fields.c.id == field_id,
                        checkout_fields.c.variant_id == checkout["variant_id"],
                    )
                )
            ).mappings().first()
            if not field:
                raise InvalidState("CHECKOUT_FIELD_NOT_FOUND")
            normalized = self.validate_field(dict(field), value)
            encrypted = (
                self.repo.vault.encrypt(normalized)
                if field["sensitive"] and normalized
                else None
            )
            plain = None if field["sensitive"] else normalized
            statement = pg_insert(checkout_values).values(
                id=uuid4(),
                checkout_session_id=checkout_id,
                field_id=field_id,
                value_text=plain,
                value_ciphertext=encrypted,
                created_at=self.now(),
                redacted_at=None,
            )
            statement = statement.on_conflict_do_update(
                constraint="uq_variant_checkout_value_session_field",
                set_={
                    "value_text": plain,
                    "value_ciphertext": encrypted,
                    "created_at": self.now(),
                    "redacted_at": None,
                },
            )
            await session.execute(statement)

    async def mark_input_ready(self, checkout_id: UUID, telegram_id: int) -> None:
        checkout = await self.checkout(checkout_id, telegram_id)
        if not checkout:
            raise AccessDenied("CHECKOUT_SESSION_INVALID")
        async with self.repo.sessions.begin() as session:
            fields = (
                await session.execute(
                    select(checkout_fields).where(
                        checkout_fields.c.variant_id == checkout["variant_id"]
                    )
                )
            ).mappings().all()
            values = (
                await session.execute(
                    select(checkout_values.c.field_id).where(
                        checkout_values.c.checkout_session_id == checkout_id
                    )
                )
            ).scalars().all()
            present = set(values)
            missing = [
                field["label"]
                for field in fields
                if field["required"] and field["id"] not in present
            ]
            if missing:
                raise InvalidState("REQUIRED_FIELDS_MISSING")
            await session.execute(
                update(checkout_sessions)
                .where(checkout_sessions.c.id == checkout_id)
                .values(status="READY")
            )

    async def checkout_values_summary(
        self, checkout_id: UUID, *, reveal_sensitive: bool = False
    ) -> list[dict]:
        async with self.repo.sessions() as session:
            result = await session.execute(
                select(
                    checkout_fields.c.label,
                    checkout_fields.c.field_key,
                    checkout_fields.c.field_type,
                    checkout_fields.c.sensitive,
                    checkout_fields.c.delete_after_fulfillment,
                    checkout_values.c.value_text,
                    checkout_values.c.value_ciphertext,
                    checkout_values.c.redacted_at,
                )
                .join(checkout_values, checkout_values.c.field_id == checkout_fields.c.id)
                .where(checkout_values.c.checkout_session_id == checkout_id)
                .order_by(checkout_fields.c.position)
            )
            output = []
            for row in result.mappings():
                item = dict(row)
                if item["redacted_at"]:
                    value = "پاک شده"
                elif item["sensitive"]:
                    value = (
                        self.repo.vault.decrypt(item["value_ciphertext"])
                        if reveal_sensitive and item["value_ciphertext"]
                        else "••••••••"
                    )
                else:
                    value = item["value_text"] or "-"
                output.append(
                    {
                        "label": item["label"],
                        "field_key": item["field_key"],
                        "field_type": item["field_type"],
                        "sensitive": item["sensitive"],
                        "value": value,
                    }
                )
            return output

    async def source_cards(self, telegram_id: int) -> list[CustomerCardRow]:
        return await self.repo.verified_cards(telegram_id)

    async def _pricing_context(self, session, product: ProductRow):
        rate = None
        if product.fixed_price_toman is None:
            rate = await session.scalar(
                select(RateRow)
                .where(
                    RateRow.currency_code == product.base_cost_currency,
                    RateRow.active.is_(True),
                )
                .order_by(
                    (RateRow.source == "manual_override").desc(),
                    RateRow.version.desc(),
                )
                .limit(1)
            )
            if not rate:
                raise InvalidState("CURRENCY_RATE_NOT_CONFIGURED")
            if rate.source == "api" and rate.valid_until <= self.now():
                raise InvalidState("CURRENCY_RATE_STALE")
        pricing = await session.get(ConfigRow, "pricing.global")
        if not pricing:
            raise InvalidState("PRICING_NOT_CONFIGURED")
        config = dict(pricing.value)
        config.update(product.pricing_override or {})
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
            rounding_increment_toman=int(config.get("rounding_increment_toman", 1)),
        )
        effective_rate = rate.toman_per_unit if rate else Decimal("1")
        currency_buffer = product.currency_buffer_percent + (
            rate.buffer_percent if rate else Decimal("0")
        )
        final = calculate_price(
            product.base_cost_amount, effective_rate, rule, currency_buffer
        )
        return rate, config, effective_rate, currency_buffer, final

    async def create_quote(
        self,
        checkout_id: UUID,
        telegram_id: int,
        card_id: UUID | None,
    ) -> QuoteRow:
        checkout = await self.checkout(checkout_id, telegram_id)
        if not checkout or checkout["status"] not in {"READY", "WAITING_GATE", "QUOTED"}:
            raise AccessDenied("CHECKOUT_SESSION_INVALID")
        async with self.repo.coordinator.lock(f"variant-quote:{checkout_id}"):
            async with self.repo.sessions.begin() as session:
                user = await self.repo.user(telegram_id, session)
                terms = await self.repo.current_terms(session)
                consent = terms and await session.scalar(
                    select(ConsentRow.id).where(
                        ConsentRow.user_id == user.id, ConsentRow.terms_id == terms.id
                    )
                )
                if not consent or user.risk_status == "BLOCKED":
                    raise AccessDenied("CHECKOUT_FORBIDDEN")
                if checkout["requires_kyc"] and user.kyc_status != "VERIFIED":
                    raise AccessDenied("KYC_REQUIRED")
                card = None
                if checkout["requires_verified_source_card"]:
                    if not card_id:
                        raise AccessDenied("VERIFIED_OWN_CARD_REQUIRED")
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
                    select(ProductRow)
                    .where(ProductRow.id == checkout["legacy_product_id"])
                    .with_for_update()
                )
                if not product or not product.active:
                    raise InvalidState("PRODUCT_UNAVAILABLE")
                if not product.unlimited_stock and product.stock - product.reserved < 1:
                    raise InvalidState("OUT_OF_STOCK")
                rate, config, effective_rate, buffer, final = await self._pricing_context(
                    session, product
                )
                now = self.now()
                snapshot = {
                    "title": f"{checkout['family_title']} — {checkout['variant_title']}",
                    "description": checkout["variant_description"],
                    "family_id": str(checkout["family_id"]),
                    "variant_id": str(checkout["variant_id"]),
                    "family_title": checkout["family_title"],
                    "variant_title": checkout["variant_title"],
                    "duration": product.duration,
                    "activation": checkout["activation_method"],
                    "delivery": self.delivery_label(checkout),
                    "warranty": self.warranty_label(checkout),
                    "base_cost_amount": str(product.base_cost_amount),
                    "base_cost_currency": product.base_cost_currency,
                    "toman_per_currency_unit": str(effective_rate),
                    "rate_source": rate.source if rate else "fixed_toman",
                    "rate_provider": rate.provider_name if rate else None,
                    "provider_timestamp": (
                        rate.provider_timestamp.isoformat() if rate else None
                    ),
                    "rate_version": rate.version if rate else None,
                    "currency_buffer_percent": str(buffer),
                    "pricing": config,
                    "selected_card_id": str(card.id) if card else None,
                    "selected_card_bank": card.bank_name if card else None,
                    "selected_card_masked": card.masked_pan if card else None,
                }
                quote = QuoteRow(
                    user_id=user.id,
                    product_id=product.id,
                    selected_card_id=card.id if card else None,
                    snapshot=snapshot,
                    rate=int(effective_rate),
                    final_toman=final,
                    created_at=now,
                    expires_at=now + self.repo.QUOTE_TTL,
                    status="ACTIVE",
                    version=1,
                )
                session.add(quote)
                await session.flush()
                session.add(ReservationRow(quote_id=quote.id, product_id=product.id))
                if not product.unlimited_stock:
                    product.reserved += 1
                await session.execute(
                    update(checkout_sessions)
                    .where(checkout_sessions.c.id == checkout_id)
                    .values(quote_id=quote.id, status="QUOTED")
                )
                await self.repo.audit(
                    session,
                    telegram_id,
                    "variant_quote.create",
                    str(quote.id),
                    f"variant={checkout['variant_id']};amount={final}",
                )
                return quote

    async def mark_waiting_gate(self, checkout_id: UUID) -> None:
        async with self.repo.sessions.begin() as session:
            await session.execute(
                update(checkout_sessions)
                .where(checkout_sessions.c.id == checkout_id)
                .values(status="WAITING_GATE")
            )

    async def final_check(
        self, checkout_id: UUID, telegram_id: int, quote_id: UUID
    ) -> OrderRow:
        checkout = await self.checkout(checkout_id, telegram_id)
        if not checkout or checkout["quote_id"] != quote_id:
            raise AccessDenied("QUOTE_INVALID")
        async with self.repo.coordinator.lock(f"variant-final:{quote_id}"):
            async with self.repo.sessions.begin() as session:
                user = await self.repo.user(telegram_id, session)
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
                if checkout["requires_verified_source_card"]:
                    card = await session.scalar(
                        select(CustomerCardRow)
                        .where(
                            CustomerCardRow.id == quote.selected_card_id,
                            CustomerCardRow.user_id == user.id,
                            CustomerCardRow.status == "VERIFIED",
                        )
                        .with_for_update()
                    )
                    if not card:
                        raise AccessDenied("VERIFIED_OWN_CARD_REQUIRED")
                today = self.now().replace(hour=0, minute=0, second=0, microsecond=0)
                merchant_rows = list(
                    (
                        await session.scalars(
                            select(MerchantCardRow)
                            .where(MerchantCardRow.active.is_(True))
                            .order_by(MerchantCardRow.priority)
                            .with_for_update()
                        )
                    ).all()
                )
                merchant = None
                for candidate in merchant_rows:
                    allocated = await session.scalar(
                        select(func.coalesce(func.sum(OrderRow.amount_toman), 0)).where(
                            OrderRow.merchant_card_id == candidate.id,
                            OrderRow.created_at >= today,
                            OrderRow.status.not_in(
                                {"PAYMENT_EXPIRED", "CANCELLED", "REFUNDED"}
                            ),
                        )
                    )
                    if (
                        candidate.daily_limit == 0
                        or allocated + quote.final_toman <= candidate.daily_limit
                    ):
                        merchant = candidate
                        break
                if not merchant:
                    raise InvalidState("DESTINATION_LIMIT_REACHED")
                quote.final_check_confirmed = True
                quote.snapshot = {
                    **quote.snapshot,
                    "merchant_card_id": str(merchant.id),
                    "merchant_card_masked": merchant.masked_pan,
                }
                order = OrderRow(
                    user_id=user.id,
                    quote_id=quote.id,
                    amount_toman=quote.final_toman,
                    status="AWAITING_PAYMENT",
                    merchant_card_id=merchant.id,
                    created_at=self.now(),
                )
                session.add(order)
                await session.flush()
                session.add(PaymentRow(order_id=order.id, status="AWAITING_PAYMENT"))
                await session.execute(
                    update(checkout_sessions)
                    .where(checkout_sessions.c.id == checkout_id)
                    .values(order_id=order.id, status="ORDERED")
                )
                return order

    async def requote(
        self, checkout_id: UUID, telegram_id: int, quote_id: UUID
    ) -> QuoteRow:
        checkout = await self.checkout(checkout_id, telegram_id)
        if not checkout or checkout["quote_id"] != quote_id:
            raise AccessDenied("REQUOTE_FORBIDDEN")
        async with self.repo.coordinator.lock(f"variant-requote:{quote_id}"):
            async with self.repo.sessions.begin() as session:
                old = await session.scalar(
                    select(QuoteRow).where(QuoteRow.id == quote_id).with_for_update()
                )
                if not old or self.now() < old.expires_at:
                    raise AccessDenied("REQUOTE_FORBIDDEN")
                successor = await session.scalar(
                    select(QuoteRow).where(QuoteRow.predecessor_quote_id == old.id)
                )
                if successor:
                    return successor
                old.status = "EXPIRED"
                reservation = await session.scalar(
                    select(ReservationRow).where(
                        ReservationRow.quote_id == old.id,
                        ReservationRow.released_at.is_(None),
                    )
                )
                if reservation:
                    reservation.released_at = self.now()
                    product = await session.get(
                        ProductRow, old.product_id, with_for_update=True
                    )
                    if product and not product.unlimited_stock:
                        product.reserved = max(
                            0, product.reserved - reservation.quantity
                        )
                card_id = old.selected_card_id
                version = old.version + 1
            fresh = await self.create_quote(
                checkout_id, telegram_id, card_id
            )
            async with self.repo.sessions.begin() as session:
                locked = await session.get(QuoteRow, fresh.id, with_for_update=True)
                locked.version = version
                locked.predecessor_quote_id = quote_id
            fresh.version = version
            fresh.predecessor_quote_id = quote_id
            return fresh

    async def order_context(self, order_id: UUID) -> dict | None:
        async with self.repo.sessions() as session:
            row = (
                await session.execute(
                    select(
                        checkout_sessions.c.id.label("checkout_id"),
                        checkout_sessions.c.order_id,
                        variants.c.id.label("variant_id"),
                        variants.c.title.label("variant_title"),
                        variants.c.fulfillment_type,
                        variants.c.activation_method,
                        variants.c.delivery_text,
                        variants.c.warranty_text,
                        families.c.title.label("family_title"),
                        ProductRow.id.label("legacy_product_id"),
                    )
                    .join(variants, variants.c.id == checkout_sessions.c.variant_id)
                    .join(families, families.c.id == variants.c.family_id)
                    .join(ProductRow, ProductRow.id == variants.c.legacy_product_id)
                    .where(checkout_sessions.c.order_id == order_id)
                )
            ).mappings().first()
            if not row:
                return None
            output = dict(row)
        output["inputs"] = await self.checkout_values_summary(output["checkout_id"])
        return output

    async def reveal_order_values(self, actor: int, order_id: UUID) -> list[dict]:
        self.repo.owner(actor)
        context = await self.order_context(order_id)
        if not context:
            raise InvalidState("VARIANT_ORDER_NOT_FOUND")
        return await self.checkout_values_summary(
            context["checkout_id"], reveal_sensitive=True
        )

    async def purge_sensitive(self, order_id: UUID) -> int:
        context = await self.order_context(order_id)
        if not context:
            return 0
        async with self.repo.sessions.begin() as session:
            sensitive_fields = select(checkout_fields.c.id).where(
                checkout_fields.c.delete_after_fulfillment.is_(True)
            )
            result = await session.execute(
                update(checkout_values)
                .where(
                    checkout_values.c.checkout_session_id == context["checkout_id"],
                    checkout_values.c.field_id.in_(sensitive_fields),
                    checkout_values.c.redacted_at.is_(None),
                )
                .values(
                    value_text=None,
                    value_ciphertext=None,
                    redacted_at=self.now(),
                )
            )
            await session.execute(
                update(checkout_sessions)
                .where(checkout_sessions.c.id == context["checkout_id"])
                .values(status="DELIVERED")
            )
            return int(result.rowcount or 0)

    async def customer_order_contexts(self, telegram_id: int) -> list[dict]:
        orders = await self.repo.customer_orders(telegram_id)
        result = []
        for order in orders:
            context = await self.order_context(order.id)
            result.append({"order": order, "variant": context})
        return result

    async def register_emoji_with_fallback(
        self, actor: int, name: str, custom_emoji_id: str, fallback: str
    ):
        emoji = await self.repo.register_emoji(actor, name, custom_emoji_id)
        async with self.repo.sessions.begin() as session:
            from .db import EmojiRow

            row = await session.get(EmojiRow, emoji.id, with_for_update=True)
            row.fallback = fallback[:16] or "•"
        emoji.fallback = fallback[:16] or "•"
        return emoji
