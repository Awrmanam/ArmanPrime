from __future__ import annotations

import asyncio
import contextlib
import html
import json
from decimal import Decimal, InvalidOperation
from uuid import UUID

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from .repository import AccessDenied, InvalidState, ShopRepository
from .rich_text import PLACEHOLDER, render_rich_text
from .telegram_adapter import Button, extract_message_custom_emoji
from .variant_store import ACTIVATION_LABELS, FIELD_TYPES, VariantStore

CURRENCIES = ("USD", "EUR", "GBP", "TRY", "AED", "RUB", "CNY", "INR", "SGD", "EGP")


def markup(rows: list[list[Button]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup.model_validate(
        {"inline_keyboard": [[button.payload() for button in row] for row in rows]}
    )


async def _rich_button(
    repo: ShopRepository,
    text_value: str,
    callback_data: str,
    style: str = "default",
    emoji_key: str | None = None,
) -> Button:
    rendered = await render_rich_text(text_value, repo.resolve_rich_emoji)
    icon = await repo.resolve_emoji_key(emoji_key) if emoji_key else None
    if not icon:
        match = PLACEHOLDER.search(text_value)
        if match:
            resolved = await repo.resolve_rich_emoji(match.group(1))
            icon = resolved[0] if resolved else None
    return Button(rendered.fallback, callback_data, style, icon)


async def _send_rich(
    repo: ShopRepository,
    message: Message,
    text_value: str,
    rows: list[list[Button]] | None = None,
) -> Message:
    rendered = await render_rich_text(text_value, repo.resolve_rich_emoji)
    keyboard = markup(rows) if rows else None
    try:
        return await message.answer(
            rendered.html,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    except TelegramBadRequest as exc:
        detail = str(exc).lower()
        if "custom emoji" not in detail and "tg-emoji" not in detail and "style" not in detail:
            raise
        plain_rows = None
        if rows:
            plain_rows = [
                [Button(button.text, button.callback_data) for button in row] for row in rows
            ]
        return await message.answer(
            rendered.fallback,
            reply_markup=markup(plain_rows) if plain_rows else None,
        )


def _template_fields(template: str) -> list[dict]:
    if template == "none":
        return []
    if template == "email":
        return [
            {
                "field_key": "account_email",
                "label": "ایمیل حساب",
                "field_type": "EMAIL",
                "required": True,
                "sensitive": False,
            }
        ]
    if template == "username":
        return [
            {
                "field_key": "account_username",
                "label": "نام کاربری / شناسه حساب",
                "field_type": "TEXT",
                "required": True,
                "sensitive": False,
            }
        ]
    if template == "telegram_username":
        return [
            {
                "field_key": "telegram_username",
                "label": "نام کاربری تلگرام",
                "field_type": "TELEGRAM_USERNAME",
                "required": True,
                "sensitive": False,
            }
        ]
    if template == "payment_link":
        return [
            {
                "field_key": "payment_link",
                "label": "Payment Link",
                "field_type": "URL",
                "required": True,
                "sensitive": True,
                "delete_after_fulfillment": True,
            }
        ]
    if template == "login":
        return [
            {
                "field_key": "account_email",
                "label": "ایمیل / نام کاربری حساب",
                "field_type": "TEXT",
                "required": True,
                "sensitive": False,
            },
            {
                "field_key": "account_password",
                "label": "رمز موقت حساب",
                "field_type": "PASSWORD",
                "required": True,
                "sensitive": True,
                "delete_after_fulfillment": True,
            },
        ]
    if template == "session":
        return [
            {
                "field_key": "session_json",
                "label": "Session JSON",
                "field_type": "SESSION_JSON",
                "required": True,
                "sensitive": True,
                "delete_after_fulfillment": True,
            }
        ]
    return []


def _activation_default_template(fulfillment_type: str) -> str:
    return {
        "activation_code": "none",
        "activation_link": "none",
        "account_no_login": "email",
        "payment_link": "payment_link",
        "account_login": "login",
        "account_credentials": "none",
        "custom": "none",
    }.get(fulfillment_type, "none")


def _status_fa(status: str) -> str:
    return {
        "AWAITING_PAYMENT": "در انتظار پرداخت",
        "AWAITING_RECONCILIATION": "در انتظار بررسی پرداخت",
        "MANUAL_REVIEW": "بررسی دستی",
        "READY_FOR_FULFILLMENT": "آماده انجام",
        "PROCESSING": "در حال انجام",
        "DELIVERED": "تحویل شده",
        "PAYMENT_EXPIRED": "منقضی",
        "CANCELLED": "لغو شده",
        "REFUNDED": "بازپرداخت",
    }.get(status, status)


def build_variant_router(repo: ShopRepository, store: VariantStore) -> Router:
    router = Router(name="variant-commerce")

    legacy_issue = getattr(repo, "_legacy_issue_callback", repo.coordinator.issue_callback)

    async def set_fsm(actor: int, state: str, *, ttl: int = 1800) -> None:
        await repo.coordinator.redis.set(f"fsm:{actor}", state, ex=ttl)

    async def draft(actor: int) -> dict:
        raw = await repo.coordinator.redis.get(f"variant-draft:{actor}")
        return json.loads(raw) if raw else {}

    async def save_draft(actor: int, value: dict) -> None:
        await repo.coordinator.redis.set(
            f"variant-draft:{actor}",
            json.dumps(value, ensure_ascii=False),
            ex=3600,
        )

    async def clear_variant_draft(actor: int) -> None:
        await repo.coordinator.redis.delete(
            f"variant-draft:{actor}",
            f"fsm:{actor}",
        )

    async def nav_home(actor: int) -> list[Button]:
        token = await store.issue_callback("catalog", actor, one_time=False)
        return [Button("فروشگاه", token, "primary")]

    async def admin_family_page(message: Message, actor: int, family_id: UUID) -> None:
        repo.owner(actor)
        family = await store.family(family_id)
        if not family:
            raise InvalidState("PRODUCT_FAMILY_NOT_FOUND")
        items = await store.family_variants(family_id, owner=True)
        rows: list[list[Button]] = []
        for item in items:
            open_token = await store.issue_callback(
                "admin.variant.open", actor, str(item["id"]), one_time=False
            )
            toggle = await store.issue_callback(
                "admin.variant.toggle",
                actor,
                f"{item['id']}:{int(not item['active'])}",
                one_time=True,
            )
            rows.append(
                [
                    await _rich_button(
                        repo,
                        item["title"],
                        open_token,
                        "success" if item["active"] else "danger",
                        item.get("button_emoji_key"),
                    )
                ]
            )
            rows.append(
                [
                    Button(
                        "غیرفعال کردن" if item["active"] else "فعال کردن",
                        toggle,
                        "danger" if item["active"] else "success",
                    )
                ]
            )
        create = await store.issue_callback(
            "admin.variant.new", actor, str(family_id), one_time=True
        )
        family_toggle = await store.issue_callback(
            "admin.family.toggle",
            actor,
            f"{family_id}:{int(not family['active'])}",
            one_time=True,
        )
        back = await store.issue_callback("admin.home", actor, one_time=False)
        rows.extend(
            [
                [Button("افزودن گزینه خرید", create, "primary")],
                [
                    Button(
                        "غیرفعال کردن محصول" if family["active"] else "فعال کردن محصول",
                        family_toggle,
                        "danger" if family["active"] else "success",
                    )
                ],
                [Button("بازگشت به محصولات", back)],
            ]
        )
        await _send_rich(
            repo,
            message,
            f"{family['title']}\n\n{family['description'] or 'بدون توضیحات'}\n\n"
            "هر گزینه خرید قیمت، روش فعال‌سازی، ورودی مشتری، گارانتی و زمان تحویل مستقل دارد.",
            rows,
        )

    async def admin_home(message: Message, actor: int) -> None:
        repo.owner(actor)
        families_rows = await store.owner_families()
        rows: list[list[Button]] = []
        for family in families_rows:
            token = await store.issue_callback(
                "admin.family.open", actor, str(family["id"]), one_time=False
            )
            rows.append(
                [
                    await _rich_button(
                        repo,
                        family["title"],
                        token,
                        "success" if family["active"] else "danger",
                        family.get("button_emoji_key"),
                    )
                ]
            )
        create = await store.issue_callback("admin.family.new", actor, one_time=True)
        orders = await store.issue_callback("admin.orders", actor, one_time=False)
        emoji = await legacy_issue("admin.emoji", actor, one_time=False)
        back = await legacy_issue("admin.close", actor, one_time=False)
        rows.extend(
            [
                [Button("ساخت محصول جدید", create, "primary")],
                [Button("سفارش‌های محصولات جدید", orders, "success")],
                [Button("Premium Emoji Registry", emoji)],
                [Button("بازگشت به پنل مدیریت", back)],
            ]
        )
        await _send_rich(
            repo,
            message,
            "محصولات و گزینه‌های خرید\n\n"
            "ساختار جدید: محصول ← گزینه خرید ← فرم سفارش ← تأمین‌کننده.\n"
            "در نام و توضیحات می‌توانید از {emoji:name} استفاده کنید.",
            rows,
        )

    async def admin_variant_page(message: Message, actor: int, variant_id: UUID) -> None:
        repo.owner(actor)
        item = await store.variant_with_family(variant_id)
        if not item:
            raise InvalidState("VARIANT_NOT_FOUND")
        fields = await store.variant_fields(variant_id)
        offers = await store.offers(variant_id)
        field_lines = "\n".join(
            f"• {field['label']} — {field['field_type']}"
            + (" — حساس" if field["sensitive"] else "")
            for field in fields
        ) or "• بدون ورودی از مشتری"
        offer_lines = "\n".join(
            f"• {offer['marketplace']} / {offer['supplier_name']} — "
            f"{offer['cost_amount']} {offer['cost_currency']} — اولویت {offer['priority']}"
            for offer in offers
        ) or "• تأمین‌کننده‌ای ثبت نشده"
        add_field = await store.issue_callback(
            "admin.field.new", actor, str(variant_id), one_time=True
        )
        add_offer = await store.issue_callback(
            "admin.offer.new", actor, str(variant_id), one_time=True
        )
        back = await store.issue_callback(
            "admin.family.open", actor, str(item["family_id"]), one_time=False
        )
        payment_label = (
            "کارت‌به‌کارت"
            if item["payment_method"] == "card_to_card"
            else item["payment_method"]
        )
        await _send_rich(
            repo,
            message,
            f"{item['family_title']} — {item['title']}\n\n"
            f"{item['description'] or 'بدون توضیحات'}\n\n"
            f"روش انجام: {item['activation_method']}\n"
            f"پرداخت: {payment_label}\n"
            f"زمان تحویل: {store.delivery_label(item)}\n"
            f"گارانتی: {store.warranty_label(item)}\n"
            f"KYC: {'لازم است' if item['requires_kyc'] else 'لازم نیست'}\n"
            "کارت مبدأ تأییدشده: "
            f"{'لازم است' if item['requires_verified_source_card'] else 'لازم نیست'}\n\n"
            f"اطلاعات مشتری:\n{field_lines}\n\n"
            f"تأمین‌کننده‌ها:\n{offer_lines}",
            [
                [Button("افزودن فیلد سفارش", add_field, "primary")],
                [Button("افزودن تأمین‌کننده جایگزین", add_offer, "primary")],
                [Button("بازگشت", back)],
            ],
        )

    async def prompt_field(message: Message, actor: int, checkout_id: UUID, index: int) -> None:
        checkout = await store.checkout(checkout_id, actor)
        if not checkout:
            raise AccessDenied("CHECKOUT_SESSION_INVALID")
        fields = await store.variant_fields(checkout["variant_id"])
        if index >= len(fields):
            await store.mark_input_ready(checkout_id, actor)
            await repo.coordinator.redis.delete(f"fsm:{actor}")
            await continue_checkout(message, actor, checkout_id)
            return
        field = fields[index]
        await set_fsm(actor, f"variant.input:{checkout_id}:{index}")
        note = field.get("help_text") or ""
        security = (
            "\nاین مقدار رمزنگاری می‌شود و پس از تحویل پاک خواهد شد."
            if field["sensitive"]
            else ""
        )
        optional = "\nاگر لازم نیست «-» بفرستید." if not field["required"] else ""
        await _send_rich(
            repo,
            message,
            f"{field['label']}\n\n{note}{security}{optional}".strip(),
        )

    async def show_quote(message: Message, actor: int, checkout_id: UUID, quote) -> None:
        checkout = await store.checkout(checkout_id, actor)
        values = await store.checkout_values_summary(checkout_id)
        input_lines = "\n".join(
            f"• {item['label']}: {item['value']}" for item in values
        ) or "• اطلاعات اضافه‌ای لازم نیست"
        card_line = ""
        if quote.snapshot.get("selected_card_masked"):
            card_line = (
                f"\nکارت مبدأ: {quote.snapshot['selected_card_bank']} — "
                f"{quote.snapshot['selected_card_masked']}"
            )
        final = await store.issue_callback(
            "final",
            actor,
            f"{checkout_id}:{quote.id}",
            one_time=True,
            ttl=1800,
        )
        await _send_rich(
            repo,
            message,
            "چک نهایی سفارش\n\n"
            f"{checkout['family_title']} — {checkout['variant_title']}\n"
            f"روش انجام: {checkout['activation_method']}\n"
            f"زمان تحویل: {store.delivery_label(checkout)}\n"
            f"گارانتی: {store.warranty_label(checkout)}\n"
            f"مبلغ: {quote.final_toman:,} تومان"
            f"{card_line}\n"
            "اعتبار قیمت: ۳۰ دقیقه\n\n"
            f"اطلاعات سفارش:\n{input_lines}",
            [[Button("تأیید و دریافت اطلاعات پرداخت", final, "success")]],
        )

    async def continue_checkout(message: Message, actor: int, checkout_id: UUID) -> None:
        checkout = await store.checkout(checkout_id, actor)
        if not checkout or checkout["status"] in {"EXPIRED", "ABANDONED"}:
            raise AccessDenied("CHECKOUT_SESSION_INVALID")
        if checkout["requires_kyc"] and checkout["kyc_status"] != "VERIFIED":
            await store.mark_waiting_gate(checkout_id)
            await repo.coordinator.redis.set(
                f"pending-checkout:{actor}",
                str(checkout["legacy_product_id"]),
                ex=86400,
            )
            begin = await legacy_issue("begin_kyc", actor, one_time=False)
            await _send_rich(
                repo,
                message,
                "برای این گزینه احراز هویت لازم است.\n"
                "اطلاعاتی که تا اینجا وارد کرده‌اید حفظ شده و بعد از تأیید می‌توانید ادامه دهید.",
                [[Button("شروع / مشاهده احراز هویت", begin, "primary")]],
            )
            return
        if checkout["requires_verified_source_card"]:
            cards = await store.source_cards(actor)
            if not cards:
                await store.mark_waiting_gate(checkout_id)
                await repo.coordinator.redis.set(
                    f"pending-checkout:{actor}",
                    str(checkout["legacy_product_id"]),
                    ex=86400,
                )
                begin = await legacy_issue("begin_card", actor, one_time=False)
                await _send_rich(
                    repo,
                    message,
                    "برای این گزینه کارت مبدأ تأییدشده لازم است.\n"
                    "پس از تأیید کارت، همین سفارش قابل ادامه خواهد بود.",
                    [[Button("ثبت / مشاهده کارت بانکی", begin, "primary")]],
                )
                return
            rows = []
            for card in cards:
                token = await store.issue_callback(
                    "quote",
                    actor,
                    f"{checkout_id}:{card.id}",
                    one_time=True,
                )
                rows.append([Button(f"{card.bank_name} — {card.masked_pan}", token)])
            await _send_rich(repo, message, "کارت مبدأ را انتخاب کنید.", rows)
            return
        quote = await store.create_quote(checkout_id, actor, None)
        await repo.coordinator.redis.delete(f"pending-checkout:{actor}")
        await show_quote(message, actor, checkout_id, quote)

    async def send_warranty_choices(message: Message, actor: int) -> None:
        rows = []
        for label, value in (
            ("بدون گارانتی", "none"),
            ("تعداد روز مشخص", "days"),
            ("تا پایان اشتراک", "subscription"),
            ("متن سفارشی", "custom"),
        ):
            token = await store.issue_callback(
                "admin.variant.warranty", actor, value, one_time=True
            )
            rows.append([Button(label, token)])
        await _send_rich(repo, message, "گارانتی این گزینه چگونه است؟", rows)

    async def send_kyc_choice(message: Message, actor: int) -> None:
        yes = await store.issue_callback("admin.variant.kyc", actor, "1", one_time=True)
        no = await store.issue_callback("admin.variant.kyc", actor, "0", one_time=True)
        await _send_rich(
            repo,
            message,
            "آیا برای خرید این گزینه احراز هویت لازم است؟",
            [[Button("لازم است", yes)], [Button("لازم نیست", no)]],
        )

    async def send_stock_choice(message: Message, actor: int) -> None:
        unlimited = await store.issue_callback(
            "admin.variant.stock", actor, "unlimited", one_time=True
        )
        limited = await store.issue_callback(
            "admin.variant.stock", actor, "limited", one_time=True
        )
        await _send_rich(
            repo,
            message,
            "موجودی این گزینه:",
            [
                [Button("نامحدود", unlimited, "success")],
                [Button("محدود", limited)],
            ],
        )

    async def send_delivery_choices(message: Message, actor: int) -> None:
        rows = []
        for label, value in (
            ("آنی", "instant"),
            ("بازه زمانی", "range"),
            ("متن سفارشی", "custom"),
        ):
            token = await store.issue_callback(
                "admin.variant.delivery", actor, value, one_time=True
            )
            rows.append([Button(label, token)])
        await _send_rich(repo, message, "زمان تحویل را تعیین کنید.", rows)

    async def finish_variant_preview(message: Message, actor: int) -> None:
        data = await draft(actor)
        fields = data.get("fields", [])
        field_lines = "\n".join(
            f"• {item['label']} ({item['field_type']})"
            + (" — حساس" if item.get("sensitive") else "")
            for item in fields
        ) or "• ورودی از مشتری ندارد"
        fixed = data.get("fixed_price_toman")
        price_text = (
            f"{int(fixed):,} تومان"
            if fixed not in {None, ""}
            else "طبق فرمول قیمت‌گذاری فروشگاه"
        )
        confirm = await store.issue_callback(
            "admin.variant.confirm", actor, one_time=True, ttl=3600
        )
        cancel = await store.issue_callback("admin.home", actor, one_time=False)
        await _send_rich(
            repo,
            message,
            "پیش‌نمایش گزینه خرید\n\n"
            f"{data['title']}\n"
            f"{data.get('description') or ''}\n\n"
            f"مدت: {data.get('duration') or '-'}\n"
            f"روش انجام: {ACTIVATION_LABELS[data['fulfillment_type']]}\n"
            f"Supplier: {data['marketplace']} / {data['supplier_name']}\n"
            f"هزینه خرید: {data['cost_amount']} {data['cost_currency']}\n"
            f"قیمت فروش: {price_text}\n"
            f"زمان تحویل: {store.delivery_label(data)}\n"
            f"گارانتی: {store.warranty_label(data)}\n"
            f"KYC: {'لازم است' if data['requires_kyc'] else 'لازم نیست'}\n"
            "کارت مبدأ تأییدشده: "
            f"{'لازم است' if data['requires_verified_source_card'] else 'لازم نیست'}\n"
            f"موجودی: {'نامحدود' if data['unlimited_stock'] else data['stock']}\n\n"
            f"اطلاعات مشتری:\n{field_lines}",
            [
                [Button("ثبت گزینه خرید", confirm, "success")],
                [Button("لغو", cancel, "danger")],
            ],
        )

    @router.message(Command("variants"))
    async def variants_command(message: Message) -> None:
        try:
            await admin_home(message, message.from_user.id)
        except AccessDenied:
            await message.answer("دسترسی مجاز نیست.")

    @router.message(Command("emoji_add"))
    async def emoji_add(message: Message) -> None:
        try:
            repo.owner(message.from_user.id)
            parts = message.text.split(maxsplit=2)
            source = message.reply_to_message
            ids = extract_message_custom_emoji(source)
            if len(parts) < 2 or not ids:
                raise ValueError("USAGE")
            name = parts[1].strip()
            fallback = parts[2].strip() if len(parts) == 3 else "•"
            emoji = await store.register_emoji_with_fallback(
                message.from_user.id, name, ids[0], fallback
            )
            await message.answer(
                f"ثبت شد: {{emoji:{emoji.name}}}\n"
                f"Fallback: {emoji.fallback}\n"
                "حالا همین Placeholder را در نام یا توضیحات محصول استفاده کنید."
            )
        except Exception:
            await message.answer(
                "روی یک پیام دارای Premium Emoji ریپلای کنید و بفرستید:\n"
                "/emoji_add name ⭐"
            )

    @router.callback_query(F.data.startswith("v1."))
    async def variant_callback(query: CallbackQuery) -> None:
        try:
            state = await store.resolve_callback(query.data, query.from_user.id)
            action = state["a"]
            actor = query.from_user.id

            if action == "catalog":
                rows = []
                for category in await repo.categories():
                    token = await store.issue_callback(
                        "category", actor, str(category.id), one_time=False
                    )
                    rows.append(
                        [
                            await _rich_button(
                                repo,
                                category.title,
                                token,
                                emoji_key=category.custom_emoji_id,
                            )
                        ]
                    )
                rows.append([Button("منوی اصلی", await legacy_issue("nav.home", actor))])
                await _send_rich(repo, query.message, "دسته‌بندی‌ها", rows)

            elif action == "category":
                category_id = UUID(state["o"])
                rows = []
                for family in await store.storefront_families(category_id):
                    token = await store.issue_callback(
                        "family", actor, str(family["id"]), one_time=False
                    )
                    rows.append(
                        [
                            await _rich_button(
                                repo,
                                family["title"],
                                token,
                                emoji_key=family.get("button_emoji_key"),
                            )
                        ]
                    )
                for product in await store.legacy_products_for_category(category_id):
                    token = await legacy_issue("product", actor, str(product.id))
                    rows.append(
                        [
                            await _rich_button(
                                repo,
                                product.title,
                                token,
                                emoji_key=product.custom_emoji_id,
                            )
                        ]
                    )
                if not rows:
                    await query.message.answer("محصول فعالی در این دسته وجود ندارد.")
                else:
                    catalog_back = await store.issue_callback("catalog", actor)
                    rows.append([Button("بازگشت به دسته‌ها", catalog_back)])
                    await _send_rich(repo, query.message, "محصولات", rows)

            elif action == "family":
                family_id = UUID(state["o"])
                family = await store.family(family_id)
                if not family or not family["active"]:
                    raise InvalidState("PRODUCT_FAMILY_UNAVAILABLE")
                rows = []
                for item in await store.family_variants(family_id):
                    token = await store.issue_callback(
                        "variant", actor, str(item["id"]), one_time=False
                    )
                    rows.append(
                        [
                            await _rich_button(
                                repo,
                                item["title"],
                                token,
                                emoji_key=item.get("button_emoji_key"),
                            )
                        ]
                    )
                rows.append([Button("بازگشت", await store.issue_callback("catalog", actor))])
                await _send_rich(
                    repo,
                    query.message,
                    f"{family['title']}\n\n"
                    f"{family['description'] or 'گزینه موردنظر را انتخاب کنید.'}",
                    rows,
                )

            elif action == "variant":
                item = await store.variant_with_family(UUID(state["o"]))
                if not item or not item["active"]:
                    raise InvalidState("VARIANT_UNAVAILABLE")
                price = await store.estimate_price(item["id"])
                buy = await store.issue_callback(
                    "buy", actor, str(item["id"]), one_time=True
                )
                back = await store.issue_callback(
                    "family", actor, str(item["family_id"]), one_time=False
                )
                await _send_rich(
                    repo,
                    query.message,
                    f"{item['family_title']} — {item['title']}\n\n"
                    f"{item['description'] or ''}\n\n"
                    f"روش فعال‌سازی: {item['activation_method']}\n"
                    f"زمان تحویل: {store.delivery_label(item)}\n"
                    f"گارانتی: {store.warranty_label(item)}\n"
                    f"قیمت فعلی: {price:,} تومان",
                    [
                        [Button("خرید", buy, "success")],
                        [Button("بازگشت", back)],
                    ],
                )

            elif action == "buy":
                checkout_id = await store.start_checkout(actor, UUID(state["o"]))
                fields = await store.variant_fields(UUID(state["o"]))
                if fields:
                    await prompt_field(query.message, actor, checkout_id, 0)
                else:
                    await store.mark_input_ready(checkout_id, actor)
                    await continue_checkout(query.message, actor, checkout_id)

            elif action == "resume":
                checkout_id = UUID(state["o"])
                checkout = await store.checkout(checkout_id, actor)
                if not checkout:
                    raise AccessDenied("CHECKOUT_SESSION_INVALID")
                if checkout["status"] == "INPUT":
                    fields = await store.variant_fields(checkout["variant_id"])
                    values = await store.checkout_values_summary(checkout_id)
                    await prompt_field(query.message, actor, checkout_id, len(values))
                else:
                    await continue_checkout(query.message, actor, checkout_id)

            elif action == "quote":
                checkout_raw, card_raw = state["o"].split(":", 1)
                checkout_id, card_id = UUID(checkout_raw), UUID(card_raw)
                quote = await store.create_quote(checkout_id, actor, card_id)
                await repo.coordinator.redis.delete(f"pending-checkout:{actor}")
                await show_quote(query.message, actor, checkout_id, quote)

            elif action == "final":
                checkout_raw, quote_raw = state["o"].split(":", 1)
                checkout_id, quote_id = UUID(checkout_raw), UUID(quote_raw)
                try:
                    order = await store.final_check(checkout_id, actor, quote_id)
                except AccessDenied:
                    requote = await store.issue_callback(
                        "requote",
                        actor,
                        f"{checkout_id}:{quote_id}",
                        one_time=True,
                    )
                    await _send_rich(
                        repo,
                        query.message,
                        "اعتبار قیمت تمام شده است.",
                        [[Button("محاسبه قیمت جدید", requote, "primary")]],
                    )
                    await query.answer()
                    return
                pan, holder = await repo.reveal_destination(actor, order.id)
                await repo.coordinator.redis.set(
                    f"receipt-order:{actor}", str(order.id), ex=1800
                )
                await _send_rich(
                    repo,
                    query.message,
                    f"کارت مقصد: {pan}\n"
                    f"صاحب کارت: {holder}\n"
                    f"مبلغ: {order.amount_toman:,} تومان\n\n"
                    "اکنون تصویر یا فایل رسید را ارسال کنید.\n"
                    "رسید به‌تنهایی اثبات پرداخت نیست.",
                )

            elif action == "requote":
                checkout_raw, quote_raw = state["o"].split(":", 1)
                checkout_id, quote_id = UUID(checkout_raw), UUID(quote_raw)
                quote = await store.requote(checkout_id, actor, quote_id)
                await show_quote(query.message, actor, checkout_id, quote)

            elif action == "orders":
                contexts = await store.customer_order_contexts(actor)
                if not contexts:
                    await _send_rich(
                        repo,
                        query.message,
                        "سفارشی وجود ندارد.",
                        [await nav_home(actor)],
                    )
                else:
                    lines = []
                    for entry in contexts:
                        order = entry["order"]
                        context = entry["variant"]
                        title = (
                            f"{context['family_title']} — {context['variant_title']}"
                            if context
                            else "سفارش فروشگاه"
                        )
                        lines.append(
                            f"{order.public_code}\n{title}\n"
                            f"{order.amount_toman:,} تومان — {_status_fa(order.status)}"
                        )
                    await _send_rich(
                        repo,
                        query.message,
                        "سفارش‌های من\n\n" + "\n\n".join(lines),
                        [await nav_home(actor)],
                    )

            elif action == "admin.home":
                await admin_home(query.message, actor)

            elif action == "admin.family.new":
                repo.owner(actor)
                rows = []
                for category in await repo.owner_categories(actor):
                    if not category.active:
                        continue
                    token = await store.issue_callback(
                        "admin.family.category",
                        actor,
                        str(category.id),
                        one_time=True,
                    )
                    rows.append([await _rich_button(repo, category.title, token)])
                await _send_rich(
                    repo,
                    query.message,
                    "دسته محصول را انتخاب کنید.",
                    rows
                    or [[
                        Button(
                            "دسته فعالی وجود ندارد",
                            await store.issue_callback("admin.home", actor),
                        )
                    ]],
                )

            elif action == "admin.family.category":
                repo.owner(actor)
                await save_draft(
                    actor,
                    {"kind": "family", "category_id": state["o"]},
                )
                await set_fsm(actor, "vadmin.family.title")
                await _send_rich(
                    repo,
                    query.message,
                    "نام محصول را ارسال کنید.\n"
                    "Premium Emoji مجاز است؛ مثال: {emoji:chatgpt} ChatGPT",
                )

            elif action == "admin.family.open":
                await admin_family_page(query.message, actor, UUID(state["o"]))

            elif action == "admin.family.toggle":
                repo.owner(actor)
                object_id, active = state["o"].split(":", 1)
                await store.set_family_active(actor, UUID(object_id), bool(int(active)))
                await admin_family_page(query.message, actor, UUID(object_id))

            elif action == "admin.variant.new":
                repo.owner(actor)
                await save_draft(
                    actor,
                    {
                        "kind": "variant",
                        "family_id": state["o"],
                        "payment_method": "card_to_card",
                        "position": 0,
                    },
                )
                await set_fsm(actor, "vadmin.variant.title")
                await _send_rich(
                    repo,
                    query.message,
                    "نام گزینه خرید را ارسال کنید.\nمثال: Plus — 1 Month",
                )

            elif action == "admin.variant.open":
                await admin_variant_page(query.message, actor, UUID(state["o"]))

            elif action == "admin.variant.toggle":
                repo.owner(actor)
                object_id, active = state["o"].split(":", 1)
                await store.set_variant_active(actor, UUID(object_id), bool(int(active)))
                item = await store.variant(UUID(object_id))
                await admin_family_page(query.message, actor, item["family_id"])

            elif action == "admin.variant.fulfillment":
                data = await draft(actor)
                data["fulfillment_type"] = state["o"]
                await save_draft(actor, data)
                default = _activation_default_template(state["o"])
                choices = [
                    ("بدون ورودی از مشتری", "none"),
                    ("ایمیل حساب", "email"),
                    ("نام کاربری / شناسه", "username"),
                    ("نام کاربری تلگرام", "telegram_username"),
                    ("Payment Link", "payment_link"),
                    ("ایمیل/نام کاربری + رمز", "login"),
                    ("Session JSON", "session"),
                    ("بعداً فیلد سفارشی می‌سازم", "custom"),
                ]
                rows = []
                for label, value in choices:
                    token = await store.issue_callback(
                        "admin.variant.fields_template",
                        actor,
                        value,
                        one_time=True,
                    )
                    style = "primary" if value == default else "default"
                    rows.append([Button(label, token, style)])
                await _send_rich(
                    repo,
                    query.message,
                    "چه اطلاعاتی باید از مشتری گرفته شود؟\n"
                    "گزینه پیشنهادی با رنگ اصلی مشخص شده است.",
                    rows,
                )

            elif action == "admin.variant.fields_template":
                data = await draft(actor)
                data["fields"] = _template_fields(state["o"])
                await save_draft(actor, data)
                await set_fsm(actor, "vadmin.variant.supplier_name")
                await _send_rich(
                    repo,
                    query.message,
                    "تأمین‌کننده\n\nنام فروشنده / Supplier را ارسال کنید.\nمثال: Plati Seller A",
                )

            elif action == "admin.variant.currency":
                data = await draft(actor)
                data["cost_currency"] = state["o"]
                await save_draft(actor, data)
                inherit = await store.issue_callback(
                    "admin.variant.price_mode", actor, "inherit", one_time=True
                )
                fixed = await store.issue_callback(
                    "admin.variant.price_mode", actor, "fixed", one_time=True
                )
                await _send_rich(
                    repo,
                    query.message,
                    "قیمت فروش چگونه محاسبه شود؟",
                    [
                        [Button("فرمول عمومی فروشگاه", inherit, "primary")],
                        [Button("قیمت ثابت تومان", fixed)],
                    ],
                )

            elif action == "admin.variant.price_mode":
                data = await draft(actor)
                data["price_mode"] = state["o"]
                await save_draft(actor, data)
                if state["o"] == "fixed":
                    await set_fsm(actor, "vadmin.variant.fixed_price")
                    await _send_rich(repo, query.message, "قیمت فروش ثابت را به تومان وارد کنید.")
                else:
                    data["fixed_price_toman"] = None
                    await save_draft(actor, data)
                    await send_delivery_choices(query.message, actor)

            elif action == "admin.variant.delivery":
                data = await draft(actor)
                data["delivery_type"] = state["o"]
                if state["o"] == "instant":
                    data["delivery_min"] = 0
                    data["delivery_max"] = 0
                    data["delivery_unit"] = "minute"
                    data["delivery_text"] = "آنی"
                    await save_draft(actor, data)
                    await send_warranty_choices(query.message, actor)
                elif state["o"] == "range":
                    await save_draft(actor, data)
                    await set_fsm(actor, "vadmin.variant.delivery_min")
                    await _send_rich(
                        repo,
                        query.message,
                        "حداقل زمان تحویل را به‌صورت عدد وارد کنید.",
                    )
                else:
                    await save_draft(actor, data)
                    await set_fsm(actor, "vadmin.variant.delivery_text")
                    await _send_rich(
                        repo,
                        query.message,
                        "متن زمان تحویل را وارد کنید.\nمثال: معمولاً بین ۱۰ تا ۱۵۰ دقیقه",
                    )

            elif action == "admin.variant.delivery_unit":
                data = await draft(actor)
                data["delivery_unit"] = state["o"]
                await save_draft(actor, data)
                await send_warranty_choices(query.message, actor)

            elif action == "admin.variant.warranty":
                data = await draft(actor)
                data["warranty_type"] = state["o"]
                if state["o"] == "none":
                    data["warranty_days"] = 0
                    data["warranty_text"] = "بدون گارانتی"
                    await save_draft(actor, data)
                    await send_kyc_choice(query.message, actor)
                elif state["o"] == "subscription":
                    data["warranty_days"] = 0
                    data["warranty_text"] = "تا پایان مدت اشتراک"
                    await save_draft(actor, data)
                    await send_kyc_choice(query.message, actor)
                elif state["o"] == "days":
                    await save_draft(actor, data)
                    await set_fsm(actor, "vadmin.variant.warranty_days")
                    await _send_rich(repo, query.message, "تعداد روز گارانتی را وارد کنید.")
                else:
                    await save_draft(actor, data)
                    await set_fsm(actor, "vadmin.variant.warranty_text")
                    await _send_rich(repo, query.message, "متن کامل گارانتی را وارد کنید.")

            elif action == "admin.variant.kyc":
                data = await draft(actor)
                data["requires_kyc"] = bool(int(state["o"]))
                await save_draft(actor, data)
                yes = await store.issue_callback(
                    "admin.variant.source_card", actor, "1", one_time=True
                )
                no = await store.issue_callback(
                    "admin.variant.source_card", actor, "0", one_time=True
                )
                await _send_rich(
                    repo,
                    query.message,
                    "آیا کارت مبدأ مشتری باید از قبل تأیید شده باشد؟",
                    [
                        [Button("لازم است", yes, "primary")],
                        [Button("لازم نیست", no)],
                    ],
                )

            elif action == "admin.variant.source_card":
                data = await draft(actor)
                data["requires_verified_source_card"] = bool(int(state["o"]))
                await save_draft(actor, data)
                await send_stock_choice(query.message, actor)

            elif action == "admin.variant.stock":
                data = await draft(actor)
                if state["o"] == "unlimited":
                    data["unlimited_stock"] = True
                    data["stock"] = 0
                    await save_draft(actor, data)
                    await set_fsm(actor, "vadmin.variant.emoji")
                    await _send_rich(
                        repo,
                        query.message,
                        "نام Emoji Registry برای آیکون دکمه را بفرستید یا «-» بفرستید.\n"
                        "داخل خود نام/توضیحات همچنان می‌توانید {emoji:name} بنویسید.",
                    )
                else:
                    data["unlimited_stock"] = False
                    await save_draft(actor, data)
                    await set_fsm(actor, "vadmin.variant.stock")
                    await _send_rich(repo, query.message, "تعداد موجودی را وارد کنید.")

            elif action == "admin.variant.confirm":
                data = await draft(actor)
                if data.get("kind") != "variant":
                    raise InvalidState("VARIANT_DRAFT_EXPIRED")
                variant_id = await store.create_variant_bundle(
                    actor,
                    UUID(data["family_id"]),
                    data,
                    data.get("fields", []),
                )
                await clear_variant_draft(actor)
                await admin_variant_page(query.message, actor, variant_id)

            elif action == "admin.field.new":
                repo.owner(actor)
                await save_draft(
                    actor,
                    {"kind": "field", "variant_id": state["o"]},
                )
                await set_fsm(actor, "vadmin.field.label")
                await _send_rich(repo, query.message, "عنوان فیلد را وارد کنید.")

            elif action == "admin.field.type":
                data = await draft(actor)
                data["field_type"] = state["o"]
                await save_draft(actor, data)
                yes = await store.issue_callback("admin.field.required", actor, "1", one_time=True)
                no = await store.issue_callback("admin.field.required", actor, "0", one_time=True)
                await _send_rich(
                    repo,
                    query.message,
                    "پر کردن این فیلد اجباری است؟",
                    [[Button("بله", yes)], [Button("خیر", no)]],
                )

            elif action == "admin.field.required":
                data = await draft(actor)
                data["required"] = bool(int(state["o"]))
                await save_draft(actor, data)
                yes = await store.issue_callback("admin.field.sensitive", actor, "1", one_time=True)
                no = await store.issue_callback("admin.field.sensitive", actor, "0", one_time=True)
                await _send_rich(
                    repo,
                    query.message,
                    "این اطلاعات حساس است؟\n"
                    "مثل Password، Session و لینک خصوصی باید «بله» باشد.",
                    [[Button("بله", yes, "danger")], [Button("خیر", no)]],
                )

            elif action == "admin.field.sensitive":
                data = await draft(actor)
                data["sensitive"] = bool(int(state["o"]))
                data["delete_after_fulfillment"] = bool(int(state["o"]))
                field_id = await store.add_field(actor, UUID(data["variant_id"]), data)
                await clear_variant_draft(actor)
                field = str(field_id)
                await _send_rich(repo, query.message, f"فیلد ثبت شد.\nID: {field[:8]}")
                await admin_variant_page(query.message, actor, UUID(data["variant_id"]))

            elif action == "admin.offer.new":
                repo.owner(actor)
                await save_draft(
                    actor,
                    {"kind": "offer", "variant_id": state["o"]},
                )
                await set_fsm(actor, "vadmin.offer.name")
                await _send_rich(repo, query.message, "نام Supplier / فروشنده را وارد کنید.")

            elif action == "admin.offer.currency":
                data = await draft(actor)
                data["cost_currency"] = state["o"]
                await save_draft(actor, data)
                await set_fsm(actor, "vadmin.offer.priority")
                await _send_rich(
                    repo,
                    query.message,
                    "اولویت Supplier را وارد کنید؛ 1 یعنی اولویت اول.",
                )

            elif action == "admin.orders":
                repo.owner(actor)
                orders = await repo.order_queue(actor)
                if not orders:
                    await _send_rich(repo, query.message, "صف سفارش‌ها خالی است.")
                for order in orders:
                    context = await store.order_context(order.id)
                    payment = await repo.payment_for_order(actor, order.id)
                    if context:
                        input_lines = "\n".join(
                            f"• {item['label']}: {item['value']}"
                            for item in context["inputs"]
                        ) or "• ورودی ندارد"
                        body = (
                            f"{order.public_code}\n"
                            f"{context['family_title']} — {context['variant_title']}\n"
                            f"مبلغ: {order.amount_toman:,} تومان\n"
                            f"وضعیت: {_status_fa(order.status)}\n"
                            f"روش انجام: {context['activation_method']}\n\n"
                            f"اطلاعات مشتری:\n{input_lines}"
                        )
                    else:
                        body = (
                            f"{order.public_code}\n"
                            f"مبلغ: {order.amount_toman:,} تومان\n"
                            f"وضعیت: {_status_fa(order.status)}"
                        )
                    rows = []
                    if context and any(item["sensitive"] for item in context["inputs"]):
                        reveal = await store.issue_callback(
                            "admin.order.reveal", actor, str(order.id), one_time=True
                        )
                        rows.append([Button("نمایش موقت اطلاعات حساس", reveal, "danger")])
                    if order.status in {"AWAITING_RECONCILIATION", "MANUAL_REVIEW"}:
                        approve = await legacy_issue(
                            "admin.payment.approve", actor, str(order.id), one_time=True
                        )
                        reject = await legacy_issue(
                            "admin.payment.reject", actor, str(order.id), one_time=True
                        )
                        rows.extend(
                            [
                                [Button("تأیید پرداخت", approve, "success")],
                                [Button("رد پرداخت", reject, "danger")],
                            ]
                        )
                    elif order.status == "READY_FOR_FULFILLMENT":
                        claim = await legacy_issue(
                            "admin.order.claim", actor, str(order.id), one_time=True
                        )
                        rows.append([Button("دریافت سفارش", claim, "primary")])
                    elif order.status == "PROCESSING" and order.assigned_admin_id == actor:
                        deliver = await legacy_issue(
                            "admin.order.deliver", actor, str(order.id), one_time=True
                        )
                        rows.append([Button("ثبت تحویل", deliver, "success")])
                    if payment and payment.receipt_file_id:
                        sender = (
                            query.message.answer_photo
                            if payment.receipt_type == "photo"
                            else query.message.answer_document
                        )
                        await sender(
                            payment.receipt_file_id,
                            caption=f"رسید {order.public_code} — فقط برای بررسی دستی",
                        )
                    await _send_rich(repo, query.message, body, rows or None)

            elif action == "admin.order.reveal":
                repo.owner(actor)
                if query.message.chat.type != "private":
                    await query.answer(
                        "اطلاعات حساس فقط در گفت‌وگوی خصوصی با ربات نمایش داده می‌شود.",
                        show_alert=True,
                    )
                    return
                values = await store.reveal_order_values(actor, UUID(state["o"]))
                lines = []
                for item in values:
                    safe_label = html.escape(item["label"])
                    safe_value = html.escape(item["value"])
                    lines.append(
                        f"{safe_label}: "
                        + (
                            f"<tg-spoiler>{safe_value}</tg-spoiler>"
                            if item["sensitive"]
                            else safe_value
                        )
                    )
                sent = await query.message.answer(
                    "اطلاعات سفارش — این پیام تا یک دقیقه دیگر حذف می‌شود.\n\n"
                    + "\n".join(lines),
                    parse_mode="HTML",
                )

                async def delete_later():
                    await asyncio.sleep(60)
                    with contextlib.suppress(Exception):
                        await sent.delete()

                asyncio.create_task(delete_later())

            elif action == "admin.delivery.confirm":
                repo.owner(actor)
                order_id = UUID(state["o"])
                draft_key = f"delivery-draft:{actor}:{order_id}"
                raw = await repo.coordinator.redis.get(draft_key)
                if not raw:
                    raise AccessDenied("DELIVERY_DRAFT_EXPIRED")
                content, _, activation_link = raw.partition("\0")
                await repo.deliver(actor, order_id, content, activation_link or None)
                await store.purge_sensitive(order_id)
                await repo.coordinator.redis.delete(draft_key, f"fsm:{actor}")
                await _send_rich(
                    repo,
                    query.message,
                    "تحویل ثبت شد و اطلاعات حساسِ علامت‌خورده از دیتابیس پاک شدند.",
                )

            await query.answer()

        except (AccessDenied, InvalidState) as exc:
            with contextlib.suppress(Exception):
                await query.answer(
                    f"درخواست قابل انجام نیست: {exc}",
                    show_alert=True,
                )
        except Exception:
            with contextlib.suppress(Exception):
                await query.answer("خطای غیرمنتظره رخ داد.", show_alert=True)
            raise

    @router.message(F.text)
    async def variant_text(message: Message) -> None:
        state = await repo.coordinator.redis.get(f"fsm:{message.from_user.id}")
        if not state or not (
            state.startswith("variant.input:")
            or state.startswith("vadmin.family.")
            or state.startswith("vadmin.variant.")
            or state.startswith("vadmin.field.")
            or state.startswith("vadmin.offer.")
        ):
            raise SkipHandler

        actor = message.from_user.id
        value = message.text.strip()

        try:
            if state.startswith("variant.input:"):
                _, _, checkout_raw, index_raw = state.split(":", 3)
                checkout_id = UUID(checkout_raw)
                index = int(index_raw)
                checkout = await store.checkout(checkout_id, actor)
                if not checkout:
                    raise AccessDenied("CHECKOUT_SESSION_INVALID")
                fields = await store.variant_fields(checkout["variant_id"])
                field = fields[index]
                actual = "" if value == "-" and not field["required"] else value
                await store.save_field_value(checkout_id, actor, field["id"], actual)
                if field["sensitive"]:
                    with contextlib.suppress(Exception):
                        await message.delete()
                await prompt_field(message, actor, checkout_id, index + 1)
                return

            repo.owner(actor)
            data = await draft(actor)
            if state == "vadmin.family.title":
                if not value:
                    raise InvalidState("TITLE_REQUIRED")
                data["title"] = value
                await save_draft(actor, data)
                await set_fsm(actor, "vadmin.family.description")
                await _send_rich(
                    repo,
                    message,
                    "توضیحات کلی محصول را ارسال کنید.\n"
                    "می‌توانید {emoji:name} استفاده کنید؛ برای توضیح خالی «-» بفرستید.",
                )
            elif state == "vadmin.family.description":
                data["description"] = "" if value == "-" else value
                await save_draft(actor, data)
                await set_fsm(actor, "vadmin.family.emoji")
                await _send_rich(
                    repo,
                    message,
                    "نام Emoji Registry برای آیکون دکمه را بفرستید یا «-» بفرستید.",
                )
            elif state == "vadmin.family.emoji":
                data["button_emoji_key"] = None if value == "-" else value
                family_id = await store.create_family(
                    actor,
                    UUID(data["category_id"]),
                    data["title"],
                    data.get("description", ""),
                    button_emoji_key=data.get("button_emoji_key"),
                )
                await clear_variant_draft(actor)
                await admin_family_page(message, actor, family_id)

            elif state == "vadmin.variant.title":
                data["title"] = value
                await save_draft(actor, data)
                await set_fsm(actor, "vadmin.variant.description")
                await _send_rich(
                    repo,
                    message,
                    "توضیحات این گزینه را ارسال کنید؛ برای خالی «-» بفرستید.",
                )
            elif state == "vadmin.variant.description":
                data["description"] = "" if value == "-" else value
                await save_draft(actor, data)
                await set_fsm(actor, "vadmin.variant.duration")
                await _send_rich(
                    repo,
                    message,
                    "مدت / نوع پلن را وارد کنید.\nمثال: 1 Month یا 12 Months",
                )
            elif state == "vadmin.variant.duration":
                data["duration"] = value
                await save_draft(actor, data)
                rows = []
                labels = {
                    "activation_code": "کد فعال‌سازی",
                    "activation_link": "لینک / Gift",
                    "account_no_login": "روی اکانت — بدون رمز",
                    "payment_link": "Payment Link",
                    "account_login": "ورود به اکانت",
                    "account_credentials": "اکانت آماده",
                    "custom": "سفارشی",
                }
                for key, label in labels.items():
                    token = await store.issue_callback(
                        "admin.variant.fulfillment", actor, key, one_time=True
                    )
                    rows.append([Button(label, token)])
                await _send_rich(repo, message, "روش انجام / تحویل این گزینه چیست؟", rows)

            elif state == "vadmin.variant.supplier_name":
                data["supplier_name"] = value
                await save_draft(actor, data)
                await set_fsm(actor, "vadmin.variant.marketplace")
                await _send_rich(
                    repo,
                    message,
                    "مارکت یا منبع Supplier را وارد کنید.\nمثال: Plati",
                )
            elif state == "vadmin.variant.marketplace":
                data["marketplace"] = value
                await save_draft(actor, data)
                await set_fsm(actor, "vadmin.variant.supplier_url")
                await _send_rich(
                    repo,
                    message,
                    "لینک صفحه Supplier را ارسال کنید؛ اگر ندارد «-» بفرستید.",
                )
            elif state == "vadmin.variant.supplier_url":
                if value != "-" and not value.startswith(("https://", "http://")):
                    raise InvalidState("INVALID_SUPPLIER_URL")
                data["supplier_url"] = None if value == "-" else value
                await save_draft(actor, data)
                await set_fsm(actor, "vadmin.variant.cost")
                await _send_rich(
                    repo,
                    message,
                    "هزینه خرید از Supplier را فقط به‌صورت عدد وارد کنید.",
                )
            elif state == "vadmin.variant.cost":
                amount = Decimal(value.replace(",", ""))
                if amount < 0:
                    raise InvalidState("INVALID_SUPPLIER_COST")
                data["cost_amount"] = str(amount)
                await save_draft(actor, data)
                rows = []
                for code in CURRENCIES:
                    token = await store.issue_callback(
                        "admin.variant.currency", actor, code, one_time=True
                    )
                    rows.append([Button(code, token)])
                await _send_rich(repo, message, "ارز هزینه خرید را انتخاب کنید.", rows)

            elif state == "vadmin.variant.fixed_price":
                amount = int(value.replace(",", ""))
                if amount < 0:
                    raise InvalidState("INVALID_FIXED_PRICE")
                data["fixed_price_toman"] = amount
                await save_draft(actor, data)
                await send_delivery_choices(message, actor)

            elif state == "vadmin.variant.delivery_min":
                amount = int(value)
                if amount < 0:
                    raise InvalidState("INVALID_DELIVERY_RANGE")
                data["delivery_min"] = amount
                await save_draft(actor, data)
                await set_fsm(actor, "vadmin.variant.delivery_max")
                await _send_rich(repo, message, "حداکثر زمان تحویل را وارد کنید.")
            elif state == "vadmin.variant.delivery_max":
                amount = int(value)
                if amount < int(data.get("delivery_min", 0)):
                    raise InvalidState("INVALID_DELIVERY_RANGE")
                data["delivery_max"] = amount
                await save_draft(actor, data)
                rows = []
                for label, unit in (("دقیقه", "minute"), ("ساعت", "hour"), ("روز", "day")):
                    token = await store.issue_callback(
                        "admin.variant.delivery_unit", actor, unit, one_time=True
                    )
                    rows.append([Button(label, token)])
                await _send_rich(repo, message, "واحد زمان را انتخاب کنید.", rows)
            elif state == "vadmin.variant.delivery_text":
                data["delivery_text"] = value
                data["delivery_min"] = None
                data["delivery_max"] = None
                data["delivery_unit"] = None
                await save_draft(actor, data)
                await send_warranty_choices(message, actor)

            elif state == "vadmin.variant.warranty_days":
                days = int(value)
                if days <= 0:
                    raise InvalidState("INVALID_WARRANTY_DAYS")
                data["warranty_days"] = days
                data["warranty_text"] = f"{days} روز"
                await save_draft(actor, data)
                await send_kyc_choice(message, actor)
            elif state == "vadmin.variant.warranty_text":
                data["warranty_days"] = 0
                data["warranty_text"] = value
                await save_draft(actor, data)
                await send_kyc_choice(message, actor)

            elif state == "vadmin.variant.stock":
                stock = int(value)
                if stock < 0:
                    raise InvalidState("INVALID_STOCK")
                data["stock"] = stock
                await save_draft(actor, data)
                await set_fsm(actor, "vadmin.variant.emoji")
                await _send_rich(
                    repo,
                    message,
                    "نام Emoji Registry برای آیکون دکمه را بفرستید یا «-» بفرستید.",
                )
            elif state == "vadmin.variant.emoji":
                data["button_emoji_key"] = None if value == "-" else value
                await save_draft(actor, data)
                await repo.coordinator.redis.delete(f"fsm:{actor}")
                await finish_variant_preview(message, actor)

            elif state == "vadmin.field.label":
                data["label"] = value
                await save_draft(actor, data)
                rows = []
                for field_type in sorted(FIELD_TYPES):
                    token = await store.issue_callback(
                        "admin.field.type", actor, field_type, one_time=True
                    )
                    rows.append([Button(field_type, token)])
                await _send_rich(repo, message, "نوع فیلد را انتخاب کنید.", rows)

            elif state == "vadmin.offer.name":
                data["supplier_name"] = value
                await save_draft(actor, data)
                await set_fsm(actor, "vadmin.offer.marketplace")
                await _send_rich(repo, message, "Marketplace را وارد کنید؛ مثال: Plati")
            elif state == "vadmin.offer.marketplace":
                data["marketplace"] = value
                await save_draft(actor, data)
                await set_fsm(actor, "vadmin.offer.url")
                await _send_rich(repo, message, "لینک Supplier را بفرستید یا «-».")
            elif state == "vadmin.offer.url":
                if value != "-" and not value.startswith(("https://", "http://")):
                    raise InvalidState("INVALID_SUPPLIER_URL")
                data["supplier_url"] = None if value == "-" else value
                await save_draft(actor, data)
                await set_fsm(actor, "vadmin.offer.cost")
                await _send_rich(repo, message, "هزینه خرید را وارد کنید.")
            elif state == "vadmin.offer.cost":
                amount = Decimal(value.replace(",", ""))
                if amount < 0:
                    raise InvalidState("INVALID_SUPPLIER_COST")
                data["cost_amount"] = str(amount)
                await save_draft(actor, data)
                rows = []
                for code in CURRENCIES:
                    token = await store.issue_callback(
                        "admin.offer.currency", actor, code, one_time=True
                    )
                    rows.append([Button(code, token)])
                await _send_rich(repo, message, "ارز Supplier را انتخاب کنید.", rows)
            elif state == "vadmin.offer.priority":
                data["priority"] = max(1, int(value))
                await store.add_offer(actor, UUID(data["variant_id"]), data)
                variant_id = UUID(data["variant_id"])
                await clear_variant_draft(actor)
                await admin_variant_page(message, actor, variant_id)

        except (ValueError, InvalidOperation, InvalidState, AccessDenied):
            await message.answer(
                "این مقدار معتبر نیست یا فرم منقضی شده است. "
                "مقدار خواسته‌شده را با قالب صحیح دوباره ارسال کنید."
            )

    return router
