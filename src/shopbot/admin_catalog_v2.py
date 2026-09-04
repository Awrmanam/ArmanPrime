from __future__ import annotations

import json
import secrets
from decimal import Decimal, InvalidOperation
from uuid import UUID, uuid4

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import CallbackQuery, Message
from sqlalchemy import delete, func, select, update

from .db import EmojiRow, ProductRow, QuoteRow
from .repository import AccessDenied, InvalidState, ShopRepository
from .telegram_adapter import Button
from .variant_store import (
    ACTIVATION_LABELS,
    checkout_fields,
    checkout_sessions,
    families,
    supplier_offers,
    variants,
    VariantStore,
)

CURRENCIES = ("USD", "EUR", "GBP", "TRY", "AED", "RUB", "CNY", "INR", "SGD", "EGP")
DURATIONS = (("1 ماه", "1 ماه"), ("3 ماه", "3 ماه"), ("6 ماه", "6 ماه"), ("12 ماه", "12 ماه"))
FULFILLMENTS = (
    ("🎁 کد فعال‌سازی", "activation_code"),
    ("🔗 لینک / Gift", "activation_link"),
    ("👤 فعال‌سازی روی حساب — بدون رمز", "account_no_login"),
    ("🔗 Payment Link", "payment_link"),
    ("🔐 ورود به حساب مشتری", "account_login"),
    ("📦 تحویل اکانت آماده", "account_credentials"),
    ("⚙️ روش سفارشی", "custom"),
)


def _duration_label(value: str | None) -> str:
    if not value:
        return "تعیین نشده"
    normalized = value.strip().lower().replace("months", "month")
    aliases = {
        "1month": "1 ماه",
        "1 month": "1 ماه",
        "1 m": "1 ماه",
        "3month": "3 ماه",
        "3 month": "3 ماه",
        "3 m": "3 ماه",
        "6month": "6 ماه",
        "6 month": "6 ماه",
        "6 m": "6 ماه",
        "12month": "12 ماه",
        "12 month": "12 ماه",
        "12 m": "12 ماه",
    }
    return aliases.get(normalized, value)


def _decimal_label(value: object) -> str:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return str(value)
    text = format(number, "f").rstrip("0").rstrip(".")
    return text or "0"


def _field_type_label(value: str) -> str:
    return {
        "TEXT": "متن",
        "EMAIL": "ایمیل",
        "PASSWORD": "رمز عبور",
        "URL": "لینک",
        "TELEGRAM_USERNAME": "نام کاربری تلگرام",
        "SELECT": "انتخاب از لیست",
        "BOOLEAN": "بله / خیر",
        "SESSION_JSON": "Session",
    }.get(value, value)


def _error_message(code: str, state: str = "") -> str:
    mapping = {
        "INVALID_SUPPLIER_URL": "لینک معتبر نیست. لینک باید با http:// یا https:// شروع شود.",
        "INVALID_SUPPLIER_COST": "هزینه خرید معتبر نیست. فقط عدد مثبت یا صفر وارد کنید.",
        "INVALID_FIXED_PRICE": "قیمت فروش معتبر نیست. مبلغ را فقط به تومان و به‌صورت عدد وارد کنید.",
        "INVALID_DELIVERY_RANGE": "بازه زمان تحویل معتبر نیست. حداکثر باید از حداقل کمتر نباشد.",
        "INVALID_WARRANTY_DAYS": "تعداد روز گارانتی باید بیشتر از صفر باشد.",
        "INVALID_STOCK": "موجودی باید صفر یا یک عدد مثبت باشد.",
        "ACTIVE_EMOJI_REQUIRED": "این Emoji دیگر فعال نیست. یک Emoji فعال انتخاب کنید یا حالت بدون Emoji را بزنید.",
        "PRODUCT_HAS_PLANS": "این محصول هنوز پلن دارد. اول پلن‌ها را حذف کنید یا محصول را غیرفعال کنید.",
        "PLAN_HAS_HISTORY": "این پلن سابقه سفارش/قیمت دارد و برای حفظ سوابق قابل حذف نیست؛ آن را غیرفعال کنید.",
        "PRODUCT_FAMILY_NOT_FOUND": "محصول پیدا نشد.",
        "VARIANT_NOT_FOUND": "پلن پیدا نشد.",
    }
    if code in mapping:
        return mapping[code]
    if state.endswith("url"):
        return "لینک معتبر نیست. نمونه صحیح: https://example.com/product"
    if state.endswith(("cost", "fixed_price", "stock", "delivery_min", "delivery_max", "warranty_days")):
        return "مقدار عددی معتبر وارد کنید."
    return "این مقدار معتبر نیست. دوباره تلاش کنید یا با دکمه بازگشت از این مرحله خارج شوید."


class CatalogAdminV2:
    TTL = 1800

    def __init__(self, repo: ShopRepository, store: VariantStore):
        self.repo = repo
        self.store = store
        self.router = Router(name="catalog-admin-v2")
        self._register()

    def _owner(self, actor: int) -> None:
        self.repo.owner(actor)

    async def _token(self, actor: int, action: str, object_id: str = "", *, once: bool = False) -> str:
        opaque = secrets.token_urlsafe(12)
        payload = json.dumps(
            {"a": action, "u": actor, "o": object_id, "once": once},
            separators=(",", ":"),
        )
        await self.repo.coordinator.redis.set(f"catalog2:cb:{opaque}", payload, ex=self.TTL)
        token = f"c2.{opaque}"
        if len(token.encode()) > 64:
            raise AssertionError("catalog v2 callback exceeds Telegram limit")
        return token

    async def _resolve(self, token: str, actor: int) -> dict:
        if not token.startswith("c2."):
            raise AccessDenied("CATALOG_CALLBACK_INVALID")
        key = f"catalog2:cb:{token[3:]}"
        raw = await self.repo.coordinator.redis.get(key)
        if not raw:
            raise AccessDenied("CATALOG_CALLBACK_EXPIRED")
        state = json.loads(raw)
        if int(state["u"]) != actor:
            raise AccessDenied("CATALOG_CALLBACK_OWNER_REQUIRED")
        if state.get("once") and await self.repo.coordinator.redis.delete(key) != 1:
            raise AccessDenied("CATALOG_CALLBACK_REPLAYED")
        return state

    async def _peek_adminux(self, token: str, actor: int) -> dict | None:
        if not token.startswith("u1."):
            return None
        raw = await self.repo.coordinator.redis.get(f"adminux:{token[3:]}")
        if not raw:
            return None
        state = json.loads(raw)
        if int(state.get("u", -1)) != actor:
            return None
        return state

    async def _set_fsm(self, actor: int, state: str) -> None:
        await self.repo.coordinator.redis.set(f"catalog2:fsm:{actor}", state, ex=3600)

    async def _fsm(self, actor: int) -> str | None:
        return await self.repo.coordinator.redis.get(f"catalog2:fsm:{actor}")

    async def _draft(self, actor: int) -> dict:
        raw = await self.repo.coordinator.redis.get(f"catalog2:draft:{actor}")
        return json.loads(raw) if raw else {}

    async def _save_draft(self, actor: int, data: dict) -> None:
        await self.repo.coordinator.redis.set(
            f"catalog2:draft:{actor}", json.dumps(data, ensure_ascii=False), ex=3600
        )

    async def _clear_draft(self, actor: int) -> None:
        await self.repo.coordinator.redis.delete(
            f"catalog2:draft:{actor}", f"catalog2:fsm:{actor}"
        )

    async def _render(self, message: Message, text: str, rows: list[list[Button]]) -> None:
        from . import runtime as runtime_module

        await runtime_module.answer_keyboard(message, text, rows)

    async def _emojis(self) -> list[EmojiRow]:
        async with self.repo.sessions() as session:
            return list(
                (
                    await session.scalars(
                        select(EmojiRow).where(EmojiRow.active.is_(True)).order_by(EmojiRow.name)
                    )
                ).all()
            )

    async def _product(self, family_id: UUID) -> dict:
        item = await self.store.family(family_id)
        if not item:
            raise InvalidState("PRODUCT_FAMILY_NOT_FOUND")
        return item

    async def _plan(self, variant_id: UUID) -> tuple[dict, ProductRow]:
        item = await self.store.variant_with_family(variant_id)
        if not item:
            raise InvalidState("VARIANT_NOT_FOUND")
        async with self.repo.sessions() as session:
            product = await session.get(ProductRow, item["legacy_product_id"])
            if not product:
                raise InvalidState("VARIANT_NOT_FOUND")
            return item, product

    async def _category_title(self, actor: int, category_id: UUID) -> str:
        for category in await self.repo.owner_categories(actor):
            if category.id == category_id:
                return category.title
        return "نامشخص"

    async def home(self, message: Message, actor: int) -> None:
        self._owner(actor)
        await self._render(
            message,
            "🛍 مدیریت فروشگاه\n\nمحصولات و پلن‌های فروش را از اینجا مدیریت کنید.",
            [
                [Button("📦 محصولات من", await self._token(actor, "products"), "primary")],
                [Button("➕ افزودن محصول جدید", await self._token(actor, "product.new"), "success")],
                [Button("🧾 سفارش‌ها", await self.repo.coordinator.issue_callback("admin.orders", actor))],
                [Button("⬅️ بازگشت به پنل مدیریت", await self.repo.coordinator.issue_callback("admin.close", actor))],
            ],
        )

    async def products(self, message: Message, actor: int) -> None:
        self._owner(actor)
        items = await self.store.owner_families()
        rows: list[list[Button]] = []
        active = 0
        for item in items:
            plans = await self.store.family_variants(item["id"], owner=True)
            active += int(bool(item["active"]))
            label = f"{item['title']} — {len(plans)} پلن"
            rows.append(
                [
                    Button(
                        label,
                        await self._token(actor, "product", str(item["id"])),
                        "primary" if item["active"] else "default",
                    )
                ]
            )
        rows.extend(
            [
                [Button("➕ افزودن محصول", await self._token(actor, "product.new"), "success")],
                [Button("⬅️ بازگشت", await self._token(actor, "home"))],
            ]
        )
        body = "هنوز محصولی نساخته‌اید." if not items else f"{len(items)} محصول • {active} فعال"
        await self._render(message, f"📦 محصولات من\n\n{body}", rows)

    async def product_page(self, message: Message, actor: int, family_id: UUID) -> None:
        item = await self._product(family_id)
        plans = await self.store.family_variants(family_id, owner=True)
        category = await self._category_title(actor, item["category_id"])
        active_plans = sum(1 for plan in plans if plan["active"])
        status = "فعال ✅" if item["active"] else "غیرفعال ⏸"
        text = (
            f"📦 {item['title']}\n\n"
            f"وضعیت: {status}\n"
            f"دسته‌بندی: {category}\n"
            f"پلن‌ها: {len(plans)} پلن • {active_plans} فعال"
        )
        if item.get("description"):
            text += f"\n\n{item['description']}"
        await self._render(
            message,
            text,
            [
                [Button("💳 پلن‌های فروش", await self._token(actor, "plans", str(family_id)), "primary")],
                [Button("➕ افزودن پلن", await self._token(actor, "plan.new", str(family_id)), "success")],
                [Button("✏️ ویرایش محصول", await self._token(actor, "product.edit", str(family_id)))],
                [Button("🎨 ظاهر و Emoji", await self._token(actor, "product.emoji", str(family_id)))],
                [
                    Button(
                        "⏸ غیرفعال کردن" if item["active"] else "▶️ فعال کردن",
                        await self._token(actor, "product.toggle", str(family_id), once=True),
                        "danger" if item["active"] else "success",
                    )
                ],
                [Button("🗑 حذف محصول", await self._token(actor, "product.delete.ask", str(family_id)), "danger")],
                [Button("⬅️ محصولات من", await self._token(actor, "products"))],
            ],
        )

    async def plans(self, message: Message, actor: int, family_id: UUID) -> None:
        family = await self._product(family_id)
        plans = await self.store.family_variants(family_id, owner=True)
        rows: list[list[Button]] = []
        for plan in plans:
            _, product = await self._plan(plan["id"])
            label = f"{'✅' if plan['active'] else '⏸'} {plan['title']} — {_duration_label(product.duration)}"
            rows.append([Button(label, await self._token(actor, "plan", str(plan["id"])), "primary" if plan["active"] else "default")])
        rows.extend(
            [
                [Button("➕ افزودن پلن جدید", await self._token(actor, "plan.new", str(family_id)), "success")],
                [Button("⬅️ بازگشت به محصول", await self._token(actor, "product", str(family_id)))],
            ]
        )
        body = f"{len(plans)} پلن ثبت شده" if plans else "هنوز پلنی برای این محصول نساخته‌اید."
        await self._render(message, f"💳 پلن‌های {family['title']}\n\n{body}", rows)

    async def plan_page(self, message: Message, actor: int, variant_id: UUID) -> None:
        item, product = await self._plan(variant_id)
        fields = await self.store.variant_fields(variant_id)
        offers = await self.store.offers(variant_id)
        try:
            price = f"{await self.store.estimate_price(variant_id):,} تومان"
        except InvalidState:
            price = "هنوز قابل محاسبه نیست"
        stock = "نامحدود" if product.unlimited_stock else f"{product.stock} عدد"
        text = (
            f"💳 {item['family_title']} • {item['title']}\n\n"
            f"وضعیت: {'فعال ✅' if item['active'] else 'غیرفعال ⏸'}\n"
            f"مدت: {_duration_label(product.duration)}\n"
            f"💰 قیمت فروش: {price}\n"
            f"💵 هزینه مبنا: {_decimal_label(product.base_cost_amount)} {product.base_cost_currency}\n"
            f"⚡ تحویل: {self.store.delivery_label(item)}\n"
            f"🛡 گارانتی: {self.store.warranty_label(item)}\n"
            f"⚙️ انجام سفارش: {ACTIVATION_LABELS.get(item['fulfillment_type'], item['activation_method'])}\n"
            f"🪪 احراز هویت: {'لازم' if item['requires_kyc'] else 'لازم نیست'}\n"
            f"💳 کارت تأییدشده: {'لازم' if item['requires_verified_source_card'] else 'لازم نیست'}\n"
            f"📦 موجودی: {stock}\n"
            f"👤 اطلاعات مشتری: {len(fields)} مورد\n"
            f"🏪 تأمین‌کننده: {len(offers)} مورد"
        )
        await self._render(
            message,
            text,
            [
                [Button("✏️ اطلاعات اصلی", await self._token(actor, "plan.basic", str(variant_id)))],
                [Button("💰 قیمت و موجودی", await self._token(actor, "plan.price", str(variant_id)), "primary")],
                [Button("⚙️ روش انجام سفارش", await self._token(actor, "plan.fulfillment", str(variant_id)))],
                [Button("👤 اطلاعات موردنیاز مشتری", await self._token(actor, "plan.fields", str(variant_id)))],
                [Button("🚚 تحویل و گارانتی", await self._token(actor, "plan.delivery", str(variant_id)))],
                [Button("🔐 احراز هویت و کارت", await self._token(actor, "plan.security", str(variant_id)))],
                [Button("🏪 تأمین‌کنندگان", await self._token(actor, "plan.offers", str(variant_id)))],
                [Button("🎨 ظاهر و Emoji", await self._token(actor, "plan.emoji", str(variant_id)))],
                [Button("👁 پیش‌نمایش مشتری", await self._token(actor, "plan.preview", str(variant_id)))],
                [
                    Button(
                        "⏸ غیرفعال کردن" if item["active"] else "▶️ فعال کردن",
                        await self._token(actor, "plan.toggle", str(variant_id), once=True),
                        "danger" if item["active"] else "success",
                    )
                ],
                [Button("🗑 حذف پلن", await self._token(actor, "plan.delete.ask", str(variant_id)), "danger")],
                [Button("⬅️ پلن‌های محصول", await self._token(actor, "plans", str(item["family_id"])))],
            ],
        )

    async def _product_edit_menu(self, message: Message, actor: int, family_id: UUID) -> None:
        item = await self._product(family_id)
        await self._render(
            message,
            f"✏️ ویرایش {item['title']}\n\nچه بخشی را تغییر می‌دهید؟",
            [
                [Button("نام محصول", await self._token(actor, "product.edit.title", str(family_id)))],
                [Button("توضیحات", await self._token(actor, "product.edit.description", str(family_id)))],
                [Button("⬅️ بازگشت", await self._token(actor, "product", str(family_id)))],
            ],
        )

    async def _emoji_menu(self, message: Message, actor: int, object_kind: str, object_id: UUID) -> None:
        emojis = await self._emojis()
        rows: list[list[Button]] = []
        for emoji in emojis:
            rows.append([Button(f"{emoji.fallback} {emoji.name}", await self._token(actor, f"{object_kind}.emoji.set", f"{object_id}:{emoji.name}", once=True))])
        rows.append([Button("بدون Emoji", await self._token(actor, f"{object_kind}.emoji.set", f"{object_id}:-", once=True))])
        back_action = "product" if object_kind == "product" else "plan"
        rows.append([Button("⬅️ بازگشت", await self._token(actor, back_action, str(object_id)))])
        note = "Emoji موردنظر را انتخاب کنید. لازم نیست نام Registry یا Placeholder را تایپ کنید."
        if not emojis:
            note += "\n\nهنوز Premium Emoji فعالی در Registry ندارید؛ فعلاً حالت بدون Emoji را انتخاب کنید."
        await self._render(message, f"🎨 ظاهر و Emoji\n\n{note}", rows)

    async def _plan_basic(self, message: Message, actor: int, variant_id: UUID) -> None:
        item, product = await self._plan(variant_id)
        await self._render(
            message,
            f"✏️ اطلاعات اصلی\n\n{item['title']} • {_duration_label(product.duration)}",
            [
                [Button("نام پلن", await self._token(actor, "plan.edit.title", str(variant_id)))],
                [Button("مدت پلن", await self._token(actor, "plan.edit.duration", str(variant_id)))],
                [Button("توضیحات", await self._token(actor, "plan.edit.description", str(variant_id)))],
                [Button("⬅️ بازگشت", await self._token(actor, "plan", str(variant_id)))],
            ],
        )

    async def _plan_price(self, message: Message, actor: int, variant_id: UUID) -> None:
        _, product = await self._plan(variant_id)
        fixed = f"{product.fixed_price_toman:,} تومان" if product.fixed_price_toman is not None else "فرمول عمومی فروشگاه"
        stock = "نامحدود" if product.unlimited_stock else f"{product.stock} عدد"
        await self._render(
            message,
            f"💰 قیمت و موجودی\n\nهزینه مبنا: {_decimal_label(product.base_cost_amount)} {product.base_cost_currency}\nقیمت فروش: {fixed}\nموجودی: {stock}",
            [
                [Button("تغییر هزینه مبنا", await self._token(actor, "plan.edit.cost", str(variant_id)))],
                [Button("تغییر ارز هزینه", await self._token(actor, "plan.edit.currency", str(variant_id)))],
                [Button("قیمت ثابت تومان", await self._token(actor, "plan.edit.fixed", str(variant_id)))],
                [Button("استفاده از فرمول عمومی", await self._token(actor, "plan.edit.inherit", str(variant_id), once=True))],
                [Button("تغییر موجودی", await self._token(actor, "plan.edit.stock", str(variant_id)))],
                [Button("⬅️ بازگشت", await self._token(actor, "plan", str(variant_id)))],
            ],
        )

    async def _plan_security(self, message: Message, actor: int, variant_id: UUID) -> None:
        item, _ = await self._plan(variant_id)
        await self._render(
            message,
            "🔐 احراز هویت و کارت\n\n"
            f"احراز هویت: {'لازم ✅' if item['requires_kyc'] else 'لازم نیست'}\n"
            f"کارت مبدأ تأییدشده: {'لازم ✅' if item['requires_verified_source_card'] else 'لازم نیست'}",
            [
                [Button(f"🪪 KYC: {'روشن' if item['requires_kyc'] else 'خاموش'}", await self._token(actor, "plan.security.kyc", str(variant_id), once=True), "primary" if item["requires_kyc"] else "default")],
                [Button(f"💳 کارت: {'روشن' if item['requires_verified_source_card'] else 'خاموش'}", await self._token(actor, "plan.security.card", str(variant_id), once=True), "primary" if item["requires_verified_source_card"] else "default")],
                [Button("⬅️ بازگشت", await self._token(actor, "plan", str(variant_id)))],
            ],
        )

    async def _plan_delivery(self, message: Message, actor: int, variant_id: UUID) -> None:
        item, _ = await self._plan(variant_id)
        await self._render(
            message,
            f"🚚 تحویل و گارانتی\n\nتحویل: {self.store.delivery_label(item)}\nگارانتی: {self.store.warranty_label(item)}",
            [
                [Button("تغییر زمان تحویل", await self._token(actor, "plan.delivery.edit", str(variant_id)))],
                [Button("تغییر گارانتی", await self._token(actor, "plan.warranty.edit", str(variant_id)))],
                [Button("⬅️ بازگشت", await self._token(actor, "plan", str(variant_id)))],
            ],
        )

    async def _plan_fields(self, message: Message, actor: int, variant_id: UUID) -> None:
        item, _ = await self._plan(variant_id)
        fields = await self.store.variant_fields(variant_id)
        rows: list[list[Button]] = []
        for field in fields:
            flags = []
            if field["required"]:
                flags.append("اجباری")
            if field["sensitive"]:
                flags.append("حساس")
            suffix = f" • {'، '.join(flags)}" if flags else ""
            rows.append([Button(f"{field['label']} • {_field_type_label(field['field_type'])}{suffix}", await self._token(actor, "field", str(field["id"])))])
        rows.extend(
            [
                [Button("➕ افزودن فیلد", await self._token(actor, "field.add.menu", str(variant_id)), "success")],
                [Button("⬅️ بازگشت", await self._token(actor, "plan", str(variant_id)))],
            ]
        )
        body = f"{len(fields)} فیلد ثبت شده" if fields else "برای این پلن اطلاعاتی از مشتری دریافت نمی‌شود."
        await self._render(message, f"👤 اطلاعات موردنیاز مشتری\n\n{item['family_title']} • {item['title']}\n\n{body}", rows)

    async def _field_page(self, message: Message, actor: int, field_id: UUID) -> None:
        self._owner(actor)
        async with self.repo.sessions() as session:
            row = (await session.execute(select(checkout_fields).where(checkout_fields.c.id == field_id))).mappings().first()
        if not row:
            raise InvalidState("FIELD_NOT_FOUND")
        field = dict(row)
        await self._render(
            message,
            f"👤 {field['label']}\n\nنوع: {_field_type_label(field['field_type'])}\nاجباری: {'بله' if field['required'] else 'خیر'}\nحساس: {'بله' if field['sensitive'] else 'خیر'}",
            [
                [Button("تغییر اجباری/اختیاری", await self._token(actor, "field.required", str(field_id), once=True))],
                [Button("تغییر حساس/عادی", await self._token(actor, "field.sensitive", str(field_id), once=True))],
                [Button("🗑 حذف فیلد", await self._token(actor, "field.delete", str(field_id), once=True), "danger")],
                [Button("⬅️ بازگشت", await self._token(actor, "plan.fields", str(field['variant_id'])))],
            ],
        )

    async def _plan_offers(self, message: Message, actor: int, variant_id: UUID) -> None:
        item, _ = await self._plan(variant_id)
        offers = await self.store.offers(variant_id)
        rows: list[list[Button]] = []
        for offer in offers:
            label = f"{'✅' if offer['active'] else '⏸'} {offer['supplier_name']} • {_decimal_label(offer['cost_amount'])} {offer['cost_currency']}"
            rows.append([Button(label, await self._token(actor, "offer", str(offer["id"])))])
        rows.extend(
            [
                [Button("➕ افزودن تأمین‌کننده", await self._token(actor, "offer.new", str(variant_id)), "success")],
                [Button("⬅️ بازگشت", await self._token(actor, "plan", str(variant_id)))],
            ]
        )
        body = f"{len(offers)} تأمین‌کننده ثبت شده" if offers else "هنوز تأمین‌کننده‌ای ثبت نشده. پلن بدون تأمین‌کننده هم قابل نگهداری است."
        await self._render(message, f"🏪 تأمین‌کنندگان\n\n{item['family_title']} • {item['title']}\n\n{body}", rows)

    async def _offer_page(self, message: Message, actor: int, offer_id: UUID) -> None:
        self._owner(actor)
        async with self.repo.sessions() as session:
            row = (await session.execute(
                select(supplier_offers).where(supplier_offers.c.id == offer_id)
            )).mappings().first()
        if not row:
            raise InvalidState("OFFER_NOT_FOUND")
        offer = dict(row)
        details = next((x for x in await self.store.offers(offer["variant_id"]) if x["id"] == offer_id), None)
        if not details:
            raise InvalidState("OFFER_NOT_FOUND")
        await self._render(
            message,
            f"🏪 {details['supplier_name']}\n\nمارکت: {details['marketplace']}\nهزینه: {_decimal_label(details['cost_amount'])} {details['cost_currency']}\nاولویت: {details['priority']}\nوضعیت: {'فعال ✅' if details['active'] else 'غیرفعال ⏸'}" + (f"\nلینک: {details['supplier_url']}" if details.get("supplier_url") else ""),
            [
                [Button("فعال/غیرفعال", await self._token(actor, "offer.toggle", str(offer_id), once=True))],
                [Button("🗑 حذف تأمین‌کننده", await self._token(actor, "offer.delete", str(offer_id), once=True), "danger")],
                [Button("⬅️ بازگشت", await self._token(actor, "plan.offers", str(offer['variant_id'])))],
            ],
        )

    async def _plan_fulfillment(self, message: Message, actor: int, variant_id: UUID) -> None:
        item, _ = await self._plan(variant_id)
        rows = [
            [Button(label, await self._token(actor, "plan.fulfillment.set", f"{variant_id}:{value}", once=True), "primary" if item["fulfillment_type"] == value else "default")]
            for label, value in FULFILLMENTS
        ]
        rows.append([Button("⬅️ بازگشت", await self._token(actor, "plan", str(variant_id)))])
        await self._render(message, f"⚙️ روش انجام سفارش\n\nروش فعلی: {ACTIVATION_LABELS.get(item['fulfillment_type'], item['activation_method'])}\n\nتغییر روش، فیلدهای مشتری را خودکار حذف نمی‌کند؛ آن‌ها را جداگانه مدیریت کنید.", rows)

    async def _plan_preview(self, message: Message, actor: int, variant_id: UUID) -> None:
        item, _ = await self._plan(variant_id)
        try:
            price = f"{await self.store.estimate_price(variant_id):,} تومان"
        except InvalidState:
            price = "قیمت هنوز آماده نیست"
        text = (
            f"👁 پیش‌نمایش مشتری\n\n{item['family_title']} — {item['title']}\n\n"
            f"{item['description'] or ''}\n\n"
            f"روش فعال‌سازی: {item['activation_method']}\n"
            f"زمان تحویل: {self.store.delivery_label(item)}\n"
            f"گارانتی: {self.store.warranty_label(item)}\n"
            f"قیمت فعلی: {price}"
        )
        await self._render(message, text, [[Button("⬅️ بازگشت", await self._token(actor, "plan", str(variant_id)))]] )

    async def _update_family_text(self, actor: int, family_id: UUID, field: str, value: str) -> None:
        self._owner(actor)
        async with self.repo.sessions.begin() as session:
            result = await session.execute(update(families).where(families.c.id == family_id).values(**{field: value}))
            if result.rowcount != 1:
                raise InvalidState("PRODUCT_FAMILY_NOT_FOUND")
            if field == "title":
                plan_rows = (await session.execute(select(variants).where(variants.c.family_id == family_id))).mappings().all()
                for plan in plan_rows:
                    product = await session.get(ProductRow, plan["legacy_product_id"])
                    if product:
                        product.title = f"{value} — {plan['title']}"
            await self.repo.audit(session, actor, f"product_family.{field}", str(family_id))

    async def _set_family_emoji(self, actor: int, family_id: UUID, name: str | None) -> None:
        self._owner(actor)
        if name and not await self.repo.resolve_emoji_key(name):
            raise InvalidState("ACTIVE_EMOJI_REQUIRED")
        async with self.repo.sessions.begin() as session:
            await session.execute(update(families).where(families.c.id == family_id).values(button_emoji_key=name))
            await self.repo.audit(session, actor, "product_family.emoji", str(family_id), name or "none")

    async def _delete_family(self, actor: int, family_id: UUID) -> None:
        self._owner(actor)
        if await self.store.family_variants(family_id, owner=True):
            raise InvalidState("PRODUCT_HAS_PLANS")
        async with self.repo.sessions.begin() as session:
            await session.execute(delete(families).where(families.c.id == family_id))
            await self.repo.audit(session, actor, "product_family.delete", str(family_id))

    async def _update_plan(self, actor: int, variant_id: UUID, variant_values: dict | None = None, product_values: dict | None = None) -> None:
        self._owner(actor)
        item, _ = await self._plan(variant_id)
        async with self.repo.sessions.begin() as session:
            if variant_values:
                await session.execute(update(variants).where(variants.c.id == variant_id).values(**variant_values))
            if product_values:
                product = await session.get(ProductRow, item["legacy_product_id"], with_for_update=True)
                if not product:
                    raise InvalidState("VARIANT_NOT_FOUND")
                for key, value in product_values.items():
                    setattr(product, key, value)
            await self.repo.audit(session, actor, "product_variant.update", str(variant_id))

    async def _delete_plan(self, actor: int, variant_id: UUID) -> UUID:
        self._owner(actor)
        item, _ = await self._plan(variant_id)
        async with self.repo.sessions.begin() as session:
            history = await session.scalar(select(func.count()).select_from(checkout_sessions).where(checkout_sessions.c.variant_id == variant_id))
            quote_history = await session.scalar(select(func.count()).select_from(QuoteRow).where(QuoteRow.product_id == item["legacy_product_id"]))
            if int(history or 0) or int(quote_history or 0):
                raise InvalidState("PLAN_HAS_HISTORY")
            await session.execute(delete(checkout_fields).where(checkout_fields.c.variant_id == variant_id))
            await session.execute(delete(supplier_offers).where(supplier_offers.c.variant_id == variant_id))
            await session.execute(delete(variants).where(variants.c.id == variant_id))
            product = await session.get(ProductRow, item["legacy_product_id"])
            if product:
                await session.delete(product)
            await self.repo.audit(session, actor, "product_variant.delete", str(variant_id))
        return item["family_id"]

    async def _create_variant_no_supplier(self, actor: int, family_id: UUID, data: dict) -> UUID:
        self._owner(actor)
        family = await self._product(family_id)
        title = str(data.get("title", "")).strip()
        if not title:
            raise InvalidState("INVALID_VARIANT")
        currency = str(data.get("cost_currency", "USD")).upper()
        if currency not in CURRENCIES:
            raise InvalidState("INVALID_CURRENCY")
        cost = Decimal(str(data.get("cost_amount", "0")))
        fixed = data.get("fixed_price_toman")
        now = self.store.now()
        variant_id = uuid4()
        product_id = uuid4()
        warranty_text = self.store.warranty_label(data)
        delivery_text = self.store.delivery_label(data)
        fields_data = data.get("fields", [])
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
                fixed_price_toman=int(fixed) if fixed not in {None, ""} else None,
                duration=data.get("duration"),
                plan_type=title,
                activation_method=ACTIVATION_LABELS[data["fulfillment_type"]],
                warranty_text=warranty_text,
                warranty_days=int(data.get("warranty_days", 0)),
                delivery_minutes=self.store._delivery_minutes(data),
                stock=int(data.get("stock", 0)),
                reserved=0,
                unlimited_stock=bool(data.get("unlimited_stock", True)),
                requires_kyc=bool(data.get("requires_kyc", False)),
                requires_verified_source_card=bool(data.get("requires_verified_source_card", False)),
                active=True,
                position=0,
                custom_emoji_id=None,
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
                    payment_method="card_to_card",
                    delivery_type=data.get("delivery_type", "instant"),
                    delivery_min=data.get("delivery_min"),
                    delivery_max=data.get("delivery_max"),
                    delivery_unit=data.get("delivery_unit"),
                    delivery_text=delivery_text,
                    warranty_type=data.get("warranty_type", "none"),
                    warranty_days=int(data.get("warranty_days", 0)),
                    warranty_text=warranty_text,
                    requires_kyc=bool(data.get("requires_kyc", False)),
                    requires_verified_source_card=bool(data.get("requires_verified_source_card", False)),
                    active=True,
                    position=0,
                    button_emoji_key=None,
                    created_at=now,
                )
            )
            for index, field in enumerate(fields_data):
                await session.execute(
                    checkout_fields.insert().values(
                        id=uuid4(),
                        variant_id=variant_id,
                        field_key=field["field_key"],
                        label=field["label"],
                        field_type=field["field_type"],
                        required=field.get("required", True),
                        sensitive=field.get("sensitive", False),
                        help_text=None,
                        options=None,
                        position=index,
                        delete_after_fulfillment=field.get("delete_after_fulfillment", field.get("sensitive", False)),
                    )
                )
            await self.repo.audit(session, actor, "product_variant.create_v2", str(variant_id))
        return variant_id

    def _template_fields(self, template: str) -> list[dict]:
        templates = {
            "none": [],
            "email": [{"field_key": "account_email", "label": "ایمیل حساب", "field_type": "EMAIL", "required": True, "sensitive": False}],
            "username": [{"field_key": "account_username", "label": "نام کاربری / شناسه حساب", "field_type": "TEXT", "required": True, "sensitive": False}],
            "payment_link": [{"field_key": "payment_link", "label": "لینک پرداخت", "field_type": "URL", "required": True, "sensitive": True, "delete_after_fulfillment": True}],
            "login": [
                {"field_key": "account_email", "label": "ایمیل / نام کاربری حساب", "field_type": "TEXT", "required": True, "sensitive": False},
                {"field_key": "account_password", "label": "رمز موقت حساب", "field_type": "PASSWORD", "required": True, "sensitive": True, "delete_after_fulfillment": True},
            ],
        }
        return templates.get(template, [])

    async def _new_plan_after_warranty(self, message: Message, actor: int) -> None:
        data = await self._draft(actor)
        data.setdefault("requires_kyc", False)
        data.setdefault("requires_verified_source_card", False)
        data.setdefault("unlimited_stock", True)
        data.setdefault("stock", 0)
        await self._save_draft(actor, data)
        await self._new_plan_review(message, actor)

    async def _new_plan_review(self, message: Message, actor: int) -> None:
        data = await self._draft(actor)
        price = f"{int(data['fixed_price_toman']):,} تومان" if data.get("fixed_price_toman") is not None else "فرمول عمومی"
        stock = "نامحدود" if data.get("unlimited_stock", True) else f"{data.get('stock', 0)} عدد"
        text = (
            "✅ مرور پلن جدید\n\n"
            f"نام: {data.get('title')}\n"
            f"مدت: {_duration_label(data.get('duration'))}\n"
            f"روش انجام: {ACTIVATION_LABELS.get(data.get('fulfillment_type'), '-')}\n"
            f"هزینه مبنا: {data.get('cost_amount')} {data.get('cost_currency')}\n"
            f"قیمت فروش: {price}\n"
            f"تحویل: {self.store.delivery_label(data)}\n"
            f"گارانتی: {self.store.warranty_label(data)}\n"
            f"KYC: {'لازم' if data.get('requires_kyc') else 'لازم نیست'}\n"
            f"کارت تأییدشده: {'لازم' if data.get('requires_verified_source_card') else 'لازم نیست'}\n"
            f"موجودی: {stock}\n"
            f"اطلاعات مشتری: {len(data.get('fields', []))} مورد\n\n"
            "تأمین‌کننده و Premium Emoji بعد از ساخت پلن و از صفحه مدیریت آن اضافه می‌شوند."
        )
        await self._render(
            message,
            text,
            [
                [Button(f"🪪 KYC: {'روشن' if data.get('requires_kyc') else 'خاموش'}", await self._token(actor, "draft.kyc", once=True))],
                [Button(f"💳 کارت: {'روشن' if data.get('requires_verified_source_card') else 'خاموش'}", await self._token(actor, "draft.card", once=True))],
                [Button(f"📦 موجودی: {stock}", await self._token(actor, "draft.stock"))],
                [Button("✅ ساخت پلن", await self._token(actor, "draft.confirm", once=True), "success")],
                [Button("❌ لغو", await self._token(actor, "draft.cancel"), "danger")],
            ],
        )

    async def _new_plan_fulfillment_choices(self, message: Message, actor: int) -> None:
        rows = [[Button(label, await self._token(actor, "draft.fulfillment", value, once=True))] for label, value in FULFILLMENTS]
        rows.append([Button("❌ لغو", await self._token(actor, "draft.cancel"), "danger")])
        await self._render(message, "⚙️ روش انجام سفارش\n\nاین پلن چطور انجام یا تحویل می‌شود؟", rows)

    async def _new_plan_field_choices(self, message: Message, actor: int, fulfillment: str) -> None:
        recommended = {"account_no_login": "email", "payment_link": "payment_link", "account_login": "login"}.get(fulfillment, "none")
        choices = (("بدون اطلاعات مشتری", "none"), ("ایمیل حساب", "email"), ("نام کاربری / شناسه", "username"), ("لینک پرداخت", "payment_link"), ("ایمیل/نام کاربری + رمز موقت", "login"))
        rows = []
        for label, value in choices:
            prefix = "⭐ " if value == recommended else ""
            rows.append([Button(prefix + label, await self._token(actor, "draft.fields", value, once=True), "primary" if value == recommended else "default")])
        await self._render(message, "👤 اطلاعات موردنیاز مشتری\n\nیک قالب اولیه انتخاب کنید. بعداً می‌توانید فیلدها را جداگانه اضافه، حذف یا تنظیم کنید.", rows)

    async def _new_plan_currency_choices(self, message: Message, actor: int) -> None:
        rows = [[Button(code, await self._token(actor, "draft.currency", code, once=True))] for code in CURRENCIES]
        await self._render(message, "💵 ارز هزینه خرید را انتخاب کنید.", rows)

    async def _new_plan_price_mode(self, message: Message, actor: int) -> None:
        await self._render(
            message,
            "💰 قیمت فروش\n\nقیمت مشتری چطور محاسبه شود؟",
            [
                [Button("فرمول عمومی فروشگاه", await self._token(actor, "draft.price.inherit", once=True), "primary")],
                [Button("قیمت ثابت تومان", await self._token(actor, "draft.price.fixed", once=True))],
            ],
        )

    async def _new_plan_delivery_choices(self, message: Message, actor: int) -> None:
        await self._render(
            message,
            "🚚 زمان تحویل را انتخاب کنید.",
            [
                [Button("آنی", await self._token(actor, "draft.delivery", "instant", once=True))],
                [Button("بازه زمانی", await self._token(actor, "draft.delivery", "range", once=True), "primary")],
                [Button("متن سفارشی", await self._token(actor, "draft.delivery", "custom", once=True))],
            ],
        )

    async def _new_plan_warranty_choices(self, message: Message, actor: int) -> None:
        await self._render(
            message,
            "🛡 گارانتی این پلن چگونه است؟",
            [
                [Button("بدون گارانتی", await self._token(actor, "draft.warranty", "none", once=True))],
                [Button("تعداد روز مشخص", await self._token(actor, "draft.warranty", "days", once=True))],
                [Button("تا پایان اشتراک", await self._token(actor, "draft.warranty", "subscription", once=True), "primary")],
                [Button("متن سفارشی", await self._token(actor, "draft.warranty", "custom", once=True))],
            ],
        )

    async def _set_plan_emoji(self, actor: int, variant_id: UUID, name: str | None) -> None:
        if name and not await self.repo.resolve_emoji_key(name):
            raise InvalidState("ACTIVE_EMOJI_REQUIRED")
        await self._update_plan(actor, variant_id, {"button_emoji_key": name}, {"custom_emoji_id": name})

    async def _register_field_template(self, actor: int, variant_id: UUID, template: str) -> None:
        fields = self._template_fields(template)
        for field in fields:
            await self.store.add_field(actor, variant_id, field)

    async def _callback(self, query: CallbackQuery, state: dict) -> None:
        actor = query.from_user.id
        action = state["a"]
        obj = str(state.get("o") or "")

        if action == "home":
            await self.home(query.message, actor)
        elif action == "products":
            await self.products(query.message, actor)
        elif action == "product":
            await self.product_page(query.message, actor, UUID(obj))
        elif action == "plans":
            await self.plans(query.message, actor, UUID(obj))
        elif action == "plan":
            await self.plan_page(query.message, actor, UUID(obj))
        elif action == "product.new":
            rows = []
            for category in await self.repo.owner_categories(actor):
                if category.active:
                    rows.append([Button(category.title, await self._token(actor, "product.new.category", str(category.id), once=True))])
            rows.append([Button("❌ لغو", await self._token(actor, "home"), "danger")])
            await self._render(query.message, "➕ افزودن محصول\n\nابتدا دسته‌بندی محصول را انتخاب کنید.", rows)
        elif action == "product.new.category":
            await self._save_draft(actor, {"kind": "product", "category_id": obj})
            await self._set_fsm(actor, "product.new.title")
            await self._render(query.message, "نام محصول را بدون Emoji ارسال کنید.\nمثال: ChatGPT", [[Button("❌ لغو", await self._token(actor, "draft.cancel"), "danger")]])
        elif action == "product.edit":
            await self._product_edit_menu(query.message, actor, UUID(obj))
        elif action in {"product.edit.title", "product.edit.description"}:
            field = action.rsplit(".", 1)[-1]
            await self._save_draft(actor, {"kind": "product_edit", "family_id": obj, "field": field})
            await self._set_fsm(actor, f"product.edit.{field}")
            prompt = "نام جدید محصول را بدون Emoji ارسال کنید." if field == "title" else "توضیحات جدید را ارسال کنید. برای پاک‌کردن توضیحات، فقط کلمه «خالی» را بفرستید."
            await self._render(query.message, prompt, [[Button("⬅️ انصراف", await self._token(actor, "product", obj))]])
        elif action == "product.emoji":
            await self._emoji_menu(query.message, actor, "product", UUID(obj))
        elif action == "product.emoji.set":
            product_raw, name = obj.split(":", 1)
            await self._set_family_emoji(actor, UUID(product_raw), None if name == "-" else name)
            await self.product_page(query.message, actor, UUID(product_raw))
        elif action == "product.toggle":
            item = await self._product(UUID(obj))
            await self.store.set_family_active(actor, UUID(obj), not item["active"])
            await self.product_page(query.message, actor, UUID(obj))
        elif action == "product.delete.ask":
            item = await self._product(UUID(obj))
            await self._render(query.message, f"🗑 حذف محصول\n\nآیا «{item['title']}» حذف شود؟ اگر پلن داشته باشد حذف انجام نمی‌شود.", [[Button("بله، حذف شود", await self._token(actor, "product.delete", obj, once=True), "danger")], [Button("انصراف", await self._token(actor, "product", obj))]])
        elif action == "product.delete":
            await self._delete_family(actor, UUID(obj))
            await self.products(query.message, actor)
        elif action == "plan.new":
            await self._save_draft(actor, {"kind": "plan", "family_id": obj, "description": "", "requires_kyc": False, "requires_verified_source_card": False, "unlimited_stock": True, "stock": 0})
            await self._set_fsm(actor, "plan.new.title")
            await self._render(query.message, "➕ پلن جدید\n\nنام پلن را ارسال کنید.\nمثال: Plus", [[Button("❌ لغو", await self._token(actor, "draft.cancel"), "danger")]])
        elif action == "draft.duration":
            data = await self._draft(actor)
            data["duration"] = obj
            await self._save_draft(actor, data)
            await self._new_plan_fulfillment_choices(query.message, actor)
        elif action == "draft.duration.custom":
            await self._set_fsm(actor, "plan.new.duration.custom")
            await self._render(query.message, "مدت پلن را وارد کنید. مثال: 45 روز", [[Button("❌ لغو", await self._token(actor, "draft.cancel"), "danger")]])
        elif action == "draft.fulfillment":
            data = await self._draft(actor)
            data["fulfillment_type"] = obj
            await self._save_draft(actor, data)
            await self._new_plan_field_choices(query.message, actor, obj)
        elif action == "draft.fields":
            data = await self._draft(actor)
            data["fields"] = self._template_fields(obj)
            await self._save_draft(actor, data)
            await self._set_fsm(actor, "plan.new.cost")
            await self._render(query.message, "💵 هزینه مبنای خرید را فقط به‌صورت عدد وارد کنید.\nمثال: 4.5", [[Button("❌ لغو", await self._token(actor, "draft.cancel"), "danger")]])
        elif action == "draft.currency":
            data = await self._draft(actor)
            data["cost_currency"] = obj
            await self._save_draft(actor, data)
            await self._new_plan_price_mode(query.message, actor)
        elif action == "draft.price.inherit":
            data = await self._draft(actor)
            data["fixed_price_toman"] = None
            await self._save_draft(actor, data)
            await self._new_plan_delivery_choices(query.message, actor)
        elif action == "draft.price.fixed":
            await self._set_fsm(actor, "plan.new.fixed_price")
            await self._render(query.message, "قیمت فروش را به تومان و فقط به‌صورت عدد وارد کنید.", [[Button("❌ لغو", await self._token(actor, "draft.cancel"), "danger")]])
        elif action == "draft.delivery":
            data = await self._draft(actor)
            data["delivery_type"] = obj
            if obj == "instant":
                data.update({"delivery_min": 0, "delivery_max": 0, "delivery_unit": "minute", "delivery_text": "آنی"})
                await self._save_draft(actor, data)
                await self._new_plan_warranty_choices(query.message, actor)
            elif obj == "range":
                await self._save_draft(actor, data)
                await self._set_fsm(actor, "plan.new.delivery_min")
                await self._render(query.message, "حداقل زمان تحویل را فقط به‌صورت عدد وارد کنید.", [])
            else:
                await self._save_draft(actor, data)
                await self._set_fsm(actor, "plan.new.delivery_text")
                await self._render(query.message, "متن زمان تحویل را وارد کنید. مثال: بین 10 تا 30 دقیقه", [])
        elif action == "draft.delivery.unit":
            data = await self._draft(actor)
            data["delivery_unit"] = obj
            await self._save_draft(actor, data)
            await self._new_plan_warranty_choices(query.message, actor)
        elif action == "draft.warranty":
            data = await self._draft(actor)
            data["warranty_type"] = obj
            if obj == "none":
                data.update({"warranty_days": 0, "warranty_text": "بدون گارانتی"})
                await self._save_draft(actor, data)
                await self._new_plan_after_warranty(query.message, actor)
            elif obj == "subscription":
                data.update({"warranty_days": 0, "warranty_text": "تا پایان مدت اشتراک"})
                await self._save_draft(actor, data)
                await self._new_plan_after_warranty(query.message, actor)
            elif obj == "days":
                await self._save_draft(actor, data)
                await self._set_fsm(actor, "plan.new.warranty_days")
                await self._render(query.message, "تعداد روز گارانتی را وارد کنید.", [])
            else:
                await self._save_draft(actor, data)
                await self._set_fsm(actor, "plan.new.warranty_text")
                await self._render(query.message, "متن گارانتی را وارد کنید.", [])
        elif action in {"draft.kyc", "draft.card"}:
            data = await self._draft(actor)
            key = "requires_kyc" if action == "draft.kyc" else "requires_verified_source_card"
            data[key] = not bool(data.get(key))
            await self._save_draft(actor, data)
            await self._new_plan_review(query.message, actor)
        elif action == "draft.stock":
            data = await self._draft(actor)
            if data.get("unlimited_stock", True):
                await self._set_fsm(actor, "plan.new.stock")
                await self._render(query.message, "تعداد موجودی را وارد کنید. برای برگشت به نامحدود از دکمه زیر استفاده کنید.", [[Button("نامحدود", await self._token(actor, "draft.stock.unlimited", once=True), "success")]])
            else:
                data.update({"unlimited_stock": True, "stock": 0})
                await self._save_draft(actor, data)
                await self._new_plan_review(query.message, actor)
        elif action == "draft.stock.unlimited":
            data = await self._draft(actor)
            data.update({"unlimited_stock": True, "stock": 0})
            await self._save_draft(actor, data)
            await self._new_plan_review(query.message, actor)
        elif action == "draft.confirm":
            data = await self._draft(actor)
            if data.get("kind") != "plan":
                raise InvalidState("VARIANT_DRAFT_EXPIRED")
            variant_id = await self._create_variant_no_supplier(actor, UUID(data["family_id"]), data)
            await self._clear_draft(actor)
            await self.plan_page(query.message, actor, variant_id)
        elif action == "draft.cancel":
            data = await self._draft(actor)
            await self._clear_draft(actor)
            if data.get("family_id"):
                await self.product_page(query.message, actor, UUID(data["family_id"]))
            else:
                await self.home(query.message, actor)
        elif action == "plan.basic":
            await self._plan_basic(query.message, actor, UUID(obj))
        elif action in {"plan.edit.title", "plan.edit.duration", "plan.edit.description", "plan.edit.cost", "plan.edit.fixed"}:
            field = action.rsplit(".", 1)[-1]
            await self._save_draft(actor, {"kind": "plan_edit", "variant_id": obj, "field": field})
            await self._set_fsm(actor, f"plan.edit.{field}")
            prompts = {"title": "نام جدید پلن را ارسال کنید.", "duration": "مدت جدید را وارد کنید. مثال: 3 ماه", "description": "توضیحات جدید را ارسال کنید. برای خالی‌کردن، «خالی» را بفرستید.", "cost": "هزینه مبنای جدید را فقط به‌صورت عدد وارد کنید.", "fixed": "قیمت ثابت جدید را به تومان و فقط به‌صورت عدد وارد کنید."}
            await self._render(query.message, prompts[field], [[Button("⬅️ انصراف", await self._token(actor, "plan", obj))]])
        elif action == "plan.edit.currency":
            rows = [[Button(code, await self._token(actor, "plan.edit.currency.set", f"{obj}:{code}", once=True))] for code in CURRENCIES]
            rows.append([Button("⬅️ بازگشت", await self._token(actor, "plan.price", obj))])
            await self._render(query.message, "ارز هزینه مبنا را انتخاب کنید.", rows)
        elif action == "plan.edit.currency.set":
            variant_raw, code = obj.split(":", 1)
            await self._update_plan(actor, UUID(variant_raw), None, {"base_cost_currency": code, "base_price_usd": Decimal("0")})
            await self._plan_price(query.message, actor, UUID(variant_raw))
        elif action == "plan.edit.inherit":
            await self._update_plan(actor, UUID(obj), None, {"fixed_price_toman": None})
            await self._plan_price(query.message, actor, UUID(obj))
        elif action == "plan.edit.stock":
            _, product = await self._plan(UUID(obj))
            if product.unlimited_stock:
                await self._save_draft(actor, {"kind": "plan_edit", "variant_id": obj, "field": "stock"})
                await self._set_fsm(actor, "plan.edit.stock")
                await self._render(query.message, "تعداد موجودی را وارد کنید یا «نامحدود» را بزنید.", [[Button("نامحدود", await self._token(actor, "plan.edit.stock.unlimited", obj, once=True), "success")]])
            else:
                await self._update_plan(actor, UUID(obj), None, {"unlimited_stock": True, "stock": 0})
                await self._plan_price(query.message, actor, UUID(obj))
        elif action == "plan.edit.stock.unlimited":
            await self._update_plan(actor, UUID(obj), None, {"unlimited_stock": True, "stock": 0})
            await self._plan_price(query.message, actor, UUID(obj))
        elif action == "plan.price":
            await self._plan_price(query.message, actor, UUID(obj))
        elif action == "plan.security":
            await self._plan_security(query.message, actor, UUID(obj))
        elif action in {"plan.security.kyc", "plan.security.card"}:
            item, _ = await self._plan(UUID(obj))
            if action.endswith("kyc"):
                value = not item["requires_kyc"]
                await self._update_plan(actor, UUID(obj), {"requires_kyc": value}, {"requires_kyc": value})
            else:
                value = not item["requires_verified_source_card"]
                await self._update_plan(actor, UUID(obj), {"requires_verified_source_card": value}, {"requires_verified_source_card": value})
            await self._plan_security(query.message, actor, UUID(obj))
        elif action == "plan.delivery":
            await self._plan_delivery(query.message, actor, UUID(obj))
        elif action == "plan.delivery.edit":
            await self._render(query.message, "زمان تحویل را انتخاب کنید.", [[Button("آنی", await self._token(actor, "plan.delivery.set", f"{obj}:instant", once=True))], [Button("بازه زمانی", await self._token(actor, "plan.delivery.set", f"{obj}:range", once=True), "primary")], [Button("متن سفارشی", await self._token(actor, "plan.delivery.set", f"{obj}:custom", once=True))], [Button("⬅️ بازگشت", await self._token(actor, "plan.delivery", obj))]])
        elif action == "plan.delivery.set":
            variant_raw, kind = obj.split(":", 1)
            if kind == "instant":
                await self._update_plan(actor, UUID(variant_raw), {"delivery_type": "instant", "delivery_min": 0, "delivery_max": 0, "delivery_unit": "minute", "delivery_text": "آنی"}, {"delivery_minutes": 0})
                await self._plan_delivery(query.message, actor, UUID(variant_raw))
            else:
                await self._save_draft(actor, {"kind": "plan_edit", "variant_id": variant_raw, "field": f"delivery_{kind}"})
                await self._set_fsm(actor, f"plan.edit.delivery_{kind}" if kind == "custom" else "plan.edit.delivery_min")
                prompt = "متن زمان تحویل را وارد کنید." if kind == "custom" else "حداقل زمان تحویل را به‌صورت عدد وارد کنید."
                await self._render(query.message, prompt, [])
        elif action == "plan.warranty.edit":
            rows = [[Button("بدون گارانتی", await self._token(actor, "plan.warranty.set", f"{obj}:none", once=True))], [Button("تعداد روز مشخص", await self._token(actor, "plan.warranty.set", f"{obj}:days", once=True))], [Button("تا پایان اشتراک", await self._token(actor, "plan.warranty.set", f"{obj}:subscription", once=True), "primary")], [Button("متن سفارشی", await self._token(actor, "plan.warranty.set", f"{obj}:custom", once=True))], [Button("⬅️ بازگشت", await self._token(actor, "plan.delivery", obj))]]
            await self._render(query.message, "گارانتی را انتخاب کنید.", rows)
        elif action == "plan.warranty.set":
            variant_raw, kind = obj.split(":", 1)
            if kind == "none":
                await self._update_plan(actor, UUID(variant_raw), {"warranty_type": "none", "warranty_days": 0, "warranty_text": "بدون گارانتی"}, {"warranty_days": 0, "warranty_text": "بدون گارانتی"})
                await self._plan_delivery(query.message, actor, UUID(variant_raw))
            elif kind == "subscription":
                await self._update_plan(actor, UUID(variant_raw), {"warranty_type": "subscription", "warranty_days": 0, "warranty_text": "تا پایان مدت اشتراک"}, {"warranty_days": 0, "warranty_text": "تا پایان مدت اشتراک"})
                await self._plan_delivery(query.message, actor, UUID(variant_raw))
            else:
                await self._save_draft(actor, {"kind": "plan_edit", "variant_id": variant_raw, "field": f"warranty_{kind}"})
                await self._set_fsm(actor, f"plan.edit.warranty_{kind}")
                await self._render(query.message, "تعداد روز گارانتی را وارد کنید." if kind == "days" else "متن گارانتی را وارد کنید.", [])
        elif action == "plan.fulfillment":
            await self._plan_fulfillment(query.message, actor, UUID(obj))
        elif action == "plan.fulfillment.set":
            variant_raw, fulfillment = obj.split(":", 1)
            label = ACTIVATION_LABELS[fulfillment]
            await self._update_plan(actor, UUID(variant_raw), {"fulfillment_type": fulfillment, "activation_method": label}, {"activation_method": label})
            await self._plan_fulfillment(query.message, actor, UUID(variant_raw))
        elif action == "plan.fields":
            await self._plan_fields(query.message, actor, UUID(obj))
        elif action == "field":
            await self._field_page(query.message, actor, UUID(obj))
        elif action == "field.add.menu":
            rows = [
                [Button("ایمیل حساب", await self._token(actor, "field.add.template", f"{obj}:email", once=True))],
                [Button("نام کاربری / شناسه", await self._token(actor, "field.add.template", f"{obj}:username", once=True))],
                [Button("لینک پرداخت", await self._token(actor, "field.add.template", f"{obj}:payment_link", once=True))],
                [Button("ایمیل/نام کاربری + رمز", await self._token(actor, "field.add.template", f"{obj}:login", once=True))],
                [Button("فیلد دلخواه", await self._token(actor, "field.add.custom", obj))],
                [Button("⬅️ بازگشت", await self._token(actor, "plan.fields", obj))],
            ]
            await self._render(query.message, "➕ افزودن اطلاعات مشتری\n\nیکی از قالب‌های آماده را انتخاب کنید یا فیلد دلخواه بسازید.", rows)
        elif action == "field.add.template":
            variant_raw, template = obj.split(":", 1)
            await self._register_field_template(actor, UUID(variant_raw), template)
            await self._plan_fields(query.message, actor, UUID(variant_raw))
        elif action == "field.add.custom":
            await self._save_draft(actor, {"kind": "field", "variant_id": obj})
            await self._set_fsm(actor, "field.custom.label")
            await self._render(query.message, "عنوان فیلد را وارد کنید. مثال: ایمیل حساب", [[Button("⬅️ انصراف", await self._token(actor, "plan.fields", obj))]])
        elif action == "field.required":
            async with self.repo.sessions.begin() as session:
                row = (await session.execute(select(checkout_fields).where(checkout_fields.c.id == UUID(obj)))).mappings().first()
                if not row:
                    raise InvalidState("FIELD_NOT_FOUND")
                await session.execute(update(checkout_fields).where(checkout_fields.c.id == UUID(obj)).values(required=not row["required"]))
            await self._field_page(query.message, actor, UUID(obj))
        elif action == "field.sensitive":
            async with self.repo.sessions.begin() as session:
                row = (await session.execute(select(checkout_fields).where(checkout_fields.c.id == UUID(obj)))).mappings().first()
                if not row:
                    raise InvalidState("FIELD_NOT_FOUND")
                value = not row["sensitive"]
                await session.execute(update(checkout_fields).where(checkout_fields.c.id == UUID(obj)).values(sensitive=value, delete_after_fulfillment=value))
            await self._field_page(query.message, actor, UUID(obj))
        elif action == "field.delete":
            async with self.repo.sessions.begin() as session:
                row = (await session.execute(select(checkout_fields).where(checkout_fields.c.id == UUID(obj)))).mappings().first()
                if not row:
                    raise InvalidState("FIELD_NOT_FOUND")
                variant_id = row["variant_id"]
                await session.execute(delete(checkout_fields).where(checkout_fields.c.id == UUID(obj)))
            await self._plan_fields(query.message, actor, variant_id)
        elif action == "plan.offers":
            await self._plan_offers(query.message, actor, UUID(obj))
        elif action == "offer":
            await self._offer_page(query.message, actor, UUID(obj))
        elif action == "offer.new":
            await self._save_draft(actor, {"kind": "offer", "variant_id": obj})
            await self._set_fsm(actor, "offer.new.name")
            await self._render(query.message, "نام فروشنده / تأمین‌کننده را وارد کنید. مثال: Plati Seller A", [[Button("⬅️ انصراف", await self._token(actor, "plan.offers", obj))]])
        elif action == "offer.url.skip":
            data = await self._draft(actor)
            data["supplier_url"] = None
            await self._save_draft(actor, data)
            await self._set_fsm(actor, "offer.new.cost")
            await self._render(query.message, "هزینه خرید از این تأمین‌کننده را فقط به‌صورت عدد وارد کنید.", [])
        elif action == "offer.currency":
            data = await self._draft(actor)
            data["cost_currency"] = obj
            await self._save_draft(actor, data)
            await self._set_fsm(actor, "offer.new.priority")
            await self._render(query.message, "اولویت را وارد کنید. 1 یعنی تأمین‌کننده اصلی.", [])
        elif action == "offer.toggle":
            async with self.repo.sessions.begin() as session:
                row = (await session.execute(select(supplier_offers).where(supplier_offers.c.id == UUID(obj)))).mappings().first()
                if not row:
                    raise InvalidState("OFFER_NOT_FOUND")
                await session.execute(update(supplier_offers).where(supplier_offers.c.id == UUID(obj)).values(active=not row["active"]))
            await self._offer_page(query.message, actor, UUID(obj))
        elif action == "offer.delete":
            async with self.repo.sessions.begin() as session:
                row = (await session.execute(select(supplier_offers).where(supplier_offers.c.id == UUID(obj)))).mappings().first()
                if not row:
                    raise InvalidState("OFFER_NOT_FOUND")
                variant_id = row["variant_id"]
                await session.execute(delete(supplier_offers).where(supplier_offers.c.id == UUID(obj)))
            await self._plan_offers(query.message, actor, variant_id)
        elif action == "plan.emoji":
            await self._emoji_menu(query.message, actor, "plan", UUID(obj))
        elif action == "plan.emoji.set":
            variant_raw, name = obj.split(":", 1)
            await self._set_plan_emoji(actor, UUID(variant_raw), None if name == "-" else name)
            await self.plan_page(query.message, actor, UUID(variant_raw))
        elif action == "plan.preview":
            await self._plan_preview(query.message, actor, UUID(obj))
        elif action == "plan.toggle":
            item, _ = await self._plan(UUID(obj))
            await self.store.set_variant_active(actor, UUID(obj), not item["active"])
            await self.plan_page(query.message, actor, UUID(obj))
        elif action == "plan.delete.ask":
            item, _ = await self._plan(UUID(obj))
            await self._render(query.message, f"🗑 حذف پلن\n\nآیا «{item['title']}» حذف شود؟ پلن دارای سابقه سفارش حذف نخواهد شد.", [[Button("بله، حذف شود", await self._token(actor, "plan.delete", obj, once=True), "danger")], [Button("انصراف", await self._token(actor, "plan", obj))]])
        elif action == "plan.delete":
            family_id = await self._delete_plan(actor, UUID(obj))
            await self.plans(query.message, actor, family_id)
        else:
            raise SkipHandler()

    async def _message(self, message: Message, actor: int, state: str, value: str) -> None:
        data = await self._draft(actor)
        if state == "product.new.title":
            if not value:
                raise InvalidState("TITLE_REQUIRED")
            data["title"] = value
            await self._save_draft(actor, data)
            await self._set_fsm(actor, "product.new.description")
            await self._render(message, "توضیحات محصول را ارسال کنید یا «بدون توضیحات» را بزنید.", [[Button("بدون توضیحات", await self._token(actor, "product.new.description.skip", once=True))], [Button("❌ لغو", await self._token(actor, "draft.cancel"), "danger")]])
        elif state == "product.new.description":
            data["description"] = value
            family_id = await self.store.create_family(actor, UUID(data["category_id"]), data["title"], data["description"], button_emoji_key=None)
            await self._clear_draft(actor)
            await self.product_page(message, actor, family_id)
        elif state.startswith("product.edit."):
            field = state.rsplit(".", 1)[-1]
            family_id = UUID(data["family_id"])
            actual = "" if field == "description" and value == "خالی" else value
            await self._update_family_text(actor, family_id, field, actual)
            await self._clear_draft(actor)
            await self.product_page(message, actor, family_id)
        elif state == "plan.new.title":
            data["title"] = value
            await self._save_draft(actor, data)
            rows = [[Button(label, await self._token(actor, "draft.duration", duration, once=True))] for label, duration in DURATIONS]
            rows.append([Button("مدت سفارشی", await self._token(actor, "draft.duration.custom"))])
            rows.append([Button("❌ لغو", await self._token(actor, "draft.cancel"), "danger")])
            await self._render(message, "مدت پلن را انتخاب کنید.", rows)
        elif state == "plan.new.duration.custom":
            data["duration"] = value
            await self._save_draft(actor, data)
            await self._new_plan_fulfillment_choices(message, actor)
        elif state == "plan.new.cost":
            amount = Decimal(value.replace(",", ""))
            if amount < 0:
                raise InvalidState("INVALID_SUPPLIER_COST")
            data["cost_amount"] = str(amount)
            await self._save_draft(actor, data)
            await self._new_plan_currency_choices(message, actor)
        elif state == "plan.new.fixed_price":
            amount = int(value.replace(",", ""))
            if amount < 0:
                raise InvalidState("INVALID_FIXED_PRICE")
            data["fixed_price_toman"] = amount
            await self._save_draft(actor, data)
            await self._new_plan_delivery_choices(message, actor)
        elif state == "plan.new.delivery_min":
            amount = int(value)
            if amount < 0:
                raise InvalidState("INVALID_DELIVERY_RANGE")
            data["delivery_min"] = amount
            await self._save_draft(actor, data)
            await self._set_fsm(actor, "plan.new.delivery_max")
            await self._render(message, "حداکثر زمان تحویل را وارد کنید.", [])
        elif state == "plan.new.delivery_max":
            amount = int(value)
            if amount < int(data.get("delivery_min", 0)):
                raise InvalidState("INVALID_DELIVERY_RANGE")
            data["delivery_max"] = amount
            await self._save_draft(actor, data)
            await self._render(message, "واحد زمان را انتخاب کنید.", [[Button("دقیقه", await self._token(actor, "draft.delivery.unit", "minute", once=True))], [Button("ساعت", await self._token(actor, "draft.delivery.unit", "hour", once=True))], [Button("روز", await self._token(actor, "draft.delivery.unit", "day", once=True))]])
        elif state == "plan.new.delivery_text":
            data.update({"delivery_text": value, "delivery_min": None, "delivery_max": None, "delivery_unit": None})
            await self._save_draft(actor, data)
            await self._new_plan_warranty_choices(message, actor)
        elif state == "plan.new.warranty_days":
            days = int(value)
            if days <= 0:
                raise InvalidState("INVALID_WARRANTY_DAYS")
            data.update({"warranty_days": days, "warranty_text": f"{days} روز"})
            await self._save_draft(actor, data)
            await self._new_plan_after_warranty(message, actor)
        elif state == "plan.new.warranty_text":
            data.update({"warranty_days": 0, "warranty_text": value})
            await self._save_draft(actor, data)
            await self._new_plan_after_warranty(message, actor)
        elif state == "plan.new.stock":
            stock = int(value)
            if stock < 0:
                raise InvalidState("INVALID_STOCK")
            data.update({"unlimited_stock": False, "stock": stock})
            await self._save_draft(actor, data)
            await self._new_plan_review(message, actor)
        elif state.startswith("plan.edit."):
            variant_id = UUID(data["variant_id"])
            field = data["field"]
            if field == "title":
                item, _ = await self._plan(variant_id)
                await self._update_plan(actor, variant_id, {"title": value}, {"title": f"{item['family_title']} — {value}", "plan_type": value})
            elif field == "duration":
                await self._update_plan(actor, variant_id, None, {"duration": value})
            elif field == "description":
                actual = "" if value == "خالی" else value
                await self._update_plan(actor, variant_id, {"description": actual}, {"description": actual})
            elif field == "cost":
                amount = Decimal(value.replace(",", ""))
                if amount < 0:
                    raise InvalidState("INVALID_SUPPLIER_COST")
                item, product = await self._plan(variant_id)
                base_usd = amount if product.base_cost_currency == "USD" else Decimal("0")
                await self._update_plan(actor, variant_id, None, {"base_cost_amount": amount, "base_price_usd": base_usd})
            elif field == "fixed":
                amount = int(value.replace(",", ""))
                if amount < 0:
                    raise InvalidState("INVALID_FIXED_PRICE")
                await self._update_plan(actor, variant_id, None, {"fixed_price_toman": amount})
            elif field == "stock":
                stock = int(value)
                if stock < 0:
                    raise InvalidState("INVALID_STOCK")
                await self._update_plan(actor, variant_id, None, {"unlimited_stock": False, "stock": stock})
            elif field == "delivery_custom":
                await self._update_plan(actor, variant_id, {"delivery_type": "custom", "delivery_min": None, "delivery_max": None, "delivery_unit": None, "delivery_text": value}, {"delivery_minutes": 0})
            elif field == "delivery_range":
                pass
            elif field == "warranty_days":
                days = int(value)
                if days <= 0:
                    raise InvalidState("INVALID_WARRANTY_DAYS")
                await self._update_plan(actor, variant_id, {"warranty_type": "days", "warranty_days": days, "warranty_text": f"{days} روز"}, {"warranty_days": days, "warranty_text": f"{days} روز"})
            elif field == "warranty_custom":
                await self._update_plan(actor, variant_id, {"warranty_type": "custom", "warranty_days": 0, "warranty_text": value}, {"warranty_days": 0, "warranty_text": value})
            await self._clear_draft(actor)
            await self.plan_page(message, actor, variant_id)
        elif state == "field.custom.label":
            data["label"] = value
            await self._save_draft(actor, data)
            choices = (("متن", "TEXT"), ("ایمیل", "EMAIL"), ("رمز عبور", "PASSWORD"), ("لینک", "URL"), ("نام کاربری تلگرام", "TELEGRAM_USERNAME"), ("Session", "SESSION_JSON"))
            await self._render(message, "نوع فیلد را انتخاب کنید.", [[Button(label, await self._token(actor, "field.custom.type", code, once=True))] for label, code in choices])
        elif state == "offer.new.name":
            data["supplier_name"] = value
            await self._save_draft(actor, data)
            await self._set_fsm(actor, "offer.new.marketplace")
            await self._render(message, "نام مارکت/منبع را وارد کنید. مثال: Plati", [])
        elif state == "offer.new.marketplace":
            data["marketplace"] = value
            await self._save_draft(actor, data)
            await self._set_fsm(actor, "offer.new.url")
            await self._render(message, "لینک صفحه تأمین‌کننده را ارسال کنید یا دکمه «بدون لینک» را بزنید.", [[Button("بدون لینک", await self._token(actor, "offer.url.skip", once=True))]])
        elif state == "offer.new.url":
            if not value.startswith(("http://", "https://")):
                raise InvalidState("INVALID_SUPPLIER_URL")
            data["supplier_url"] = value
            await self._save_draft(actor, data)
            await self._set_fsm(actor, "offer.new.cost")
            await self._render(message, "هزینه خرید را فقط به‌صورت عدد وارد کنید.", [])
        elif state == "offer.new.cost":
            amount = Decimal(value.replace(",", ""))
            if amount < 0:
                raise InvalidState("INVALID_SUPPLIER_COST")
            data["cost_amount"] = str(amount)
            await self._save_draft(actor, data)
            rows = [[Button(code, await self._token(actor, "offer.currency", code, once=True))] for code in CURRENCIES]
            await self._render(message, "ارز هزینه را انتخاب کنید.", rows)
        elif state == "offer.new.priority":
            data["priority"] = max(1, int(value))
            variant_id = UUID(data["variant_id"])
            await self.store.add_offer(actor, variant_id, data)
            await self._clear_draft(actor)
            await self._plan_offers(message, actor, variant_id)
        else:
            raise SkipHandler()

    def _register(self) -> None:
        @self.router.callback_query(F.data.startswith("u1."))
        async def enter_from_admin(query: CallbackQuery) -> None:
            if not query.from_user or not query.data or not query.message:
                return
            state = await self._peek_adminux(query.data, query.from_user.id)
            if not state or state.get("a") != "store.home":
                raise SkipHandler()
            await self.home(query.message, query.from_user.id)
            await query.answer()

        @self.router.callback_query(F.data.startswith("c2."))
        async def catalog_callback(query: CallbackQuery) -> None:
            if not query.from_user or not query.data or not query.message:
                return
            try:
                state = await self._resolve(query.data, query.from_user.id)
                await self._callback(query, state)
                await query.answer()
            except SkipHandler:
                raise
            except (AccessDenied, InvalidState, ValueError, InvalidOperation) as exc:
                code = str(exc.args[0]) if getattr(exc, "args", None) else "INVALID"
                await query.answer(_error_message(code), show_alert=True)

        @self.router.message()
        async def catalog_message(message: Message) -> None:
            if not message.from_user or message.text is None:
                raise SkipHandler()
            actor = message.from_user.id
            state = await self._fsm(actor)
            if not state:
                raise SkipHandler()
            value = message.text.strip()
            try:
                self._owner(actor)
                await self._message(message, actor, state, value)
            except SkipHandler:
                raise
            except (AccessDenied, InvalidState, ValueError, InvalidOperation) as exc:
                code = str(exc.args[0]) if getattr(exc, "args", None) else "INVALID"
                await message.answer(_error_message(code, state))


def build_admin_catalog_v2_router(repo: ShopRepository, store: VariantStore) -> Router:
    return CatalogAdminV2(repo, store).router
