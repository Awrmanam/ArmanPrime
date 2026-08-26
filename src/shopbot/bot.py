from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .enums import CardStatus, KYCStatus
from .security import Callback, CallbackSigner
from .store import ApplicationStore


def keyboard(*rows: tuple[str, str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=data)] for text, data in rows
        ]
    )


def build_router(store: ApplicationStore, signer: CallbackSigner) -> Router:
    router = Router(name="commerce")

    def signed(action: str, object_id: str = "0", version: int = 1) -> str:
        return signer.sign(Callback(action, object_id, version))

    @router.message(Command("start"))
    async def start(message: Message) -> None:
        user = store.user(message.from_user.id)
        try:
            terms = store.current_terms
        except Exception:
            await message.answer("فروشگاه هنوز راه‌اندازی نشده است.")
            return
        if user.accepted_terms_id != terms.id:
            await message.answer(
                f"{terms.title}\n\n{terms.pages[0]}",
                reply_markup=keyboard(
                    ("تأیید قوانین", signed("consent", str(terms.id), terms.version)),
                    ("رد قوانین", signed("reject_terms", str(terms.id), terms.version)),
                ),
            )
            return
        await home(message)

    async def home(message: Message) -> None:
        page = store.pages["home"]
        await message.answer(
            page.text or "صفحه اصلی",
            reply_markup=keyboard(
                ("مشاهده محصولات", signed("browse")), ("حساب کاربری", signed("account"))
            ),
        )

    @router.callback_query(F.data.startswith("1."))
    async def callbacks(query: CallbackQuery) -> None:
        try:
            callback = signer.verify(query.data)
            user = store.user(query.from_user.id)
            if callback.action == "consent":
                terms = store.current_terms
                if str(terms.id) != callback.object_id or terms.version != callback.version:
                    raise ValueError("STALE_TERMS")
                store.accept_terms(query.from_user.id)
                await query.message.answer("قوانین ثبت شد. از /start وارد شوید.")
            elif callback.action == "reject_terms":
                await query.message.answer("بدون پذیرش قوانین دسترسی به فروشگاه ممکن نیست.")
            elif callback.action == "browse":
                store.checkout.assert_store_access(user, store.current_terms)
                products = [p for p in store.products.values() if p.active]
                text = (
                    "\n".join(f"{p.id} | {p.title}" for p in products) or "محصولی منتشر نشده است."
                )
                await query.message.answer(text)
            elif callback.action == "buy":
                # Always re-enter the server use-case; signed callbacks are not access controls.
                product_id = UUID(callback.object_id)
                cards = store.verified_cards(query.from_user.id)
                if user.kyc_status != KYCStatus.VERIFIED:
                    await query.message.answer("برای Checkout احراز هویت تأییدشده لازم است.")
                elif not cards:
                    await query.message.answer("کارت بانکی تأییدشده‌ای ندارید.")
                else:
                    rows = [
                        (card.display, signed("select_card", f"{product_id}:{card.id}"))
                        for card in cards
                    ]
                    await query.message.answer(
                        "کارت مبدأ را انتخاب کنید.", reply_markup=keyboard(*rows)
                    )
            elif callback.action == "select_card":
                product_id, card_id = map(UUID, callback.object_id.split(":"))
                quote = store.create_quote(
                    query.from_user.id, product_id, card_id, datetime.now(UTC)
                )
                await query.message.answer(
                    f"چک نهایی سفارش\nمحصول: {quote.product_snapshot['title']}\n"
                    f"مبلغ: {quote.final_toman} تومان\n"
                    f"کارت مبدأ: {store.customer_cards[card_id].display}\n"
                    "اعتبار: ۳۰ دقیقه\nپرداخت فقط با کارت انتخاب‌شده.",
                    reply_markup=keyboard(
                        ("تأیید و ادامه", signed("final", str(quote.id), quote.version)),
                        ("انصراف", signed("cancel", str(quote.id), quote.version)),
                    ),
                )
            elif callback.action == "final":
                quote = store.quotes[UUID(callback.object_id)]
                if quote.version != callback.version:
                    raise ValueError("STALE_QUOTE")
                order = store.confirm(query.from_user.id, quote.id, datetime.now(UTC))
                merchant, pan = store.receiving_card(
                    query.from_user.id, quote.id, datetime.now(UTC)
                )
                await query.message.answer(
                    f"کارت مقصد: {pan}\nصاحب کارت: {merchant.holder_name}\n"
                    f"مبلغ: {order.amount_toman} تومان\nOrder ID: {order.id}\n"
                    f"کارت مبدأ: {store.customer_cards[quote.selected_card_id].display}\n"
                    "تصویر رسید اثبات پرداخت نیست."
                )
            await query.answer()
        except Exception as exc:
            await query.answer(str(exc), show_alert=True)

    @router.message(Command("setup"))
    async def setup(message: Message) -> None:
        store.require_owner(message.from_user.id)
        await message.answer(
            "راه‌اندازی امن\nابتدا قوانین، نرخ دلار، دسته، محصول و کارت مقصد را با فرمان‌های "
            "پنل مدیریت تنظیم کنید. /admin"
        )

    @router.message(Command("admin"))
    async def admin(message: Message) -> None:
        store.require_owner(message.from_user.id)
        await message.answer(
            "پنل مدیریت\n"
            "/admin_terms عنوان | متن\n/admin_home متن\n/admin_category عنوان\n"
            "/admin_product CATEGORY_ID | عنوان | USD | STOCK\n/admin_rate RATE\n"
            "/admin_pricing markup/margin | PERCENT\n/admin_page SLUG | متن\n"
            "/admin_button SLUG | متن | ACTION | ROW | POS | STYLE | EMOJI_NAME(optional)\n"
            "/admin_emoji NAME (در پاسخ به پیام دارای Custom Emoji)\n"
            "/admin_kyc TELEGRAM_ID | STATUS | دلیل\n/admin_cards\n"
            "/admin_card CARD_ID | STATUS\n/admin_merchant بانک | صاحب | PAN | PRIORITY | LIMIT\n"
            "/admin_orders\n/admin_verify ORDER_ID | REFERENCE | match/mismatch\n"
            "/admin_claim ORDER_ID\n/admin_deliver ORDER_ID | متن | لینک\n/admin_audit"
            "\nProvider: بدون Provider رسمی، کارت‌به‌کارت فقط Manual Reconciliation است؛ "
            "Strong Match و allowed_card غیرفعال‌اند و رسید اثبات پرداخت نیست."
        )

    def args(command: CommandObject, count: int) -> list[str]:
        values = [part.strip() for part in (command.args or "").split("|")]
        if len(values) < count or any(not value for value in values[:count]):
            raise ValueError("INVALID_ARGUMENTS")
        return values

    @router.message(Command("admin_terms"))
    async def admin_terms(message: Message, command: CommandObject) -> None:
        title, body = args(command, 2)
        terms = store.publish_terms(message.from_user.id, title, body)
        await message.answer(f"نسخه {terms.version} قوانین منتشر شد.")

    @router.message(Command("admin_home"))
    async def admin_home(message: Message, command: CommandObject) -> None:
        store.require_owner(message.from_user.id)
        store.pages["home"].text = command.args or ""
        store.record(message.from_user.id, "page.update", "home")
        await message.answer("ذخیره شد.")

    @router.message(Command("admin_category"))
    async def admin_category(message: Message, command: CommandObject) -> None:
        category = store.add_category(message.from_user.id, args(command, 1)[0])
        await message.answer(f"دسته ساخته شد: {category.id}")

    @router.message(Command("admin_product"))
    async def admin_product(message: Message, command: CommandObject) -> None:
        category, title, usd, stock = args(command, 4)
        product = store.add_product(
            message.from_user.id, UUID(category), title, Decimal(usd), int(stock)
        )
        await message.answer(
            f"محصول ساخته شد: {product.id}",
            reply_markup=keyboard(("پیش‌نمایش خرید", signed("buy", str(product.id)))),
        )

    @router.message(Command("admin_rate"))
    async def admin_rate(message: Message, command: CommandObject) -> None:
        store.set_rate(message.from_user.id, int(args(command, 1)[0]))
        await message.answer("نرخ ثبت شد.")

    @router.message(Command("admin_pricing"))
    async def admin_pricing(message: Message, command: CommandObject) -> None:
        mode, percent = args(command, 2)
        value = Decimal(percent)
        store.set_pricing(
            message.from_user.id,
            value if mode == "markup" else None,
            value if mode == "margin" else None,
        )
        await message.answer("فرمول عمومی قیمت ثبت شد.")

    @router.message(Command("admin_page"))
    async def admin_page(message: Message, command: CommandObject) -> None:
        slug, text = args(command, 2)
        page = store.add_page(message.from_user.id, slug, text)
        await message.answer(f"صفحه ذخیره شد: {page.slug}")

    @router.message(Command("admin_button"))
    async def admin_button(message: Message, command: CommandObject) -> None:
        slug, text, action, row, position, style, *emoji = args(command, 6)
        button = store.add_button(
            message.from_user.id,
            slug,
            text,
            action,
            int(row),
            int(position),
            style,
            emoji[0] if emoji else None,
        )
        await message.answer(f"دکمه ذخیره شد: {button.id}")

    @router.message(Command("admin_emoji"))
    async def admin_emoji(message: Message, command: CommandObject) -> None:
        store.require_owner(message.from_user.id)
        name = args(command, 1)[0]
        source = message.reply_to_message
        entities = list((source.entities if source else None) or [])
        ids = [
            entity.custom_emoji_id
            for entity in entities
            if entity.type == "custom_emoji" and entity.custom_emoji_id
        ]
        if not ids:
            raise ValueError("CUSTOM_EMOJI_REQUIRED")
        item = store.register_emoji(message.from_user.id, name, ids[0])
        await message.answer(f"Custom Emoji ثبت شد: {item.name} | {item.custom_emoji_id}")

    @router.message(Command("kyc"))
    async def kyc(message: Message) -> None:
        user = store.user(message.from_user.id)
        if user.kyc_status == KYCStatus.NOT_STARTED:
            user.kyc_status = KYCStatus.PENDING
        await message.answer(f"وضعیت احراز هویت: {user.kyc_status.value}")

    @router.message(Command("admin_kyc"))
    async def admin_kyc(message: Message, command: CommandObject) -> None:
        telegram_id, status, *reason = args(command, 2)
        store.set_kyc(
            message.from_user.id, int(telegram_id), KYCStatus(status), reason[0] if reason else ""
        )
        await message.answer("وضعیت KYC ثبت شد.")

    @router.message(Command("card"))
    async def card(message: Message, command: CommandObject) -> None:
        bank, last4 = args(command, 2)
        item = store.add_customer_card(message.from_user.id, bank, last4)
        await message.answer(f"کارت برای بررسی ثبت شد: {item.id} | {item.display}")

    @router.message(Command("admin_cards"))
    async def admin_cards(message: Message) -> None:
        store.require_owner(message.from_user.id)
        await message.answer(
            "\n".join(f"{c.id} | {c.display} | {c.status}" for c in store.customer_cards.values())
            or "صف خالی است."
        )

    @router.message(Command("admin_card"))
    async def admin_card(message: Message, command: CommandObject) -> None:
        card_id, status = args(command, 2)
        store.review_customer_card(message.from_user.id, UUID(card_id), CardStatus(status))
        await message.answer("وضعیت کارت ثبت شد.")

    @router.message(Command("admin_merchant"))
    async def admin_merchant(message: Message, command: CommandObject) -> None:
        bank, holder, pan, priority, limit = args(command, 5)
        item = store.add_merchant_card(
            message.from_user.id, bank, holder, pan, int(priority), int(limit)
        )
        await message.answer(f"کارت مقصد ثبت شد: {item.id} | {item.masked_pan}")

    @router.message(Command("admin_orders"))
    async def admin_orders(message: Message) -> None:
        store.require_owner(message.from_user.id)
        await message.answer(
            "\n".join(f"{o.id} | {o.status}" for o in store.orders.values()) or "سفارشی نیست."
        )

    @router.message(Command("receipt"))
    async def receipt(message: Message, command: CommandObject) -> None:
        order_id = UUID(args(command, 1)[0])
        order = store.orders[order_id]
        if order.user_id != store.user(message.from_user.id).id:
            raise ValueError("ORDER_OWNER_REQUIRED")
        quote = store.quotes[order.quote_id]
        payment = store.payments[order.id]
        await store.payment_service.submit_receipt(payment, order, quote, datetime.now(UTC))
        await message.answer("رسید برای تطبیق ثبت شد؛ این وضعیت به‌معنای تأیید پرداخت نیست.")

    @router.message(Command("admin_verify"))
    async def admin_verify(message: Message, command: CommandObject) -> None:
        store.require_owner(message.from_user.id)
        order_id, reference, match = args(command, 3)
        order = store.orders[UUID(order_id)]
        payment = store.payments[order.id]
        await store.payment_service.verify(payment, order, reference, True, match == "match")
        await message.answer(f"نتیجه: {payment.status}")

    @router.message(Command("admin_claim"))
    async def admin_claim(message: Message, command: CommandObject) -> None:
        store.require_owner(message.from_user.id)
        order = store.orders[UUID(args(command, 1)[0])]
        store.payment_service.claim(order, message.from_user.id, datetime.now(UTC))
        await message.answer("Claim شد.")

    @router.message(Command("admin_deliver"))
    async def admin_deliver(message: Message, command: CommandObject) -> None:
        order_id, text, *link = args(command, 2)
        store.deliver(message.from_user.id, UUID(order_id), text, link[0] if link else None)
        await message.answer("تحویل ثبت شد.")

    @router.message(Command("admin_audit"))
    async def admin_audit(message: Message) -> None:
        store.require_owner(message.from_user.id)
        await message.answer(
            "\n".join(f"{a.at.isoformat()} | {a.action} | {a.target}" for a in store.audit[-20:])
            or "رویدادی نیست."
        )

    return router
