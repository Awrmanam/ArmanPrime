from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import timedelta
from uuid import UUID

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, Update
from fastapi import FastAPI, Header, HTTPException, Request, status
from redis.asyncio import Redis
from sqlalchemy import text

from .config import Settings
from .db import create_engine_and_session
from .repository import AccessDenied, RedisCoordinator, ShopRepository
from .security import Vault
from .telegram_adapter import Button

log = logging.getLogger(__name__)


def markup(rows: list[list[Button]]) -> InlineKeyboardMarkup:
    # aiogram currently drops new Bot API button fields, so validate centrally and preserve payload.
    return InlineKeyboardMarkup.model_validate(
        {"inline_keyboard": [[button.payload() for button in row] for row in rows]}
    )


def persistent_router(repo: ShopRepository) -> Router:
    router = Router(name="persistent-commerce")

    async def home(message: Message, actor_id: int) -> None:
        actions = {}
        for action in ("catalog", "account", "begin_kyc", "begin_card", "my_orders"):
            actions[action] = await repo.coordinator.issue_callback(action, actor_id)
        await message.answer(
            "صفحه اصلی",
            reply_markup=markup(
                [
                    [Button("فروشگاه", actions["catalog"], "primary")],
                    [Button("حساب کاربری", actions["account"])],
                    [Button("احراز هویت", actions["begin_kyc"])],
                    [Button("کارت‌های بانکی", actions["begin_card"])],
                    [Button("سفارش‌های من", actions["my_orders"])],
                ]
            ),
        )

    @router.message(Command("start"))
    async def start(message: Message) -> None:
        if not await repo.coordinator.rate_limit("start", message.from_user.id, 10, 60):
            await message.answer("درخواست‌های شما بیش از حد مجاز است.")
            return
        async with repo.sessions.begin() as session:
            await repo.user(message.from_user.id, session)
            terms = await repo.current_terms(session)
            accepted = terms and await repo.has_current_consent(message.from_user.id, session)
        if not terms:
            await message.answer("فروشگاه هنوز راه‌اندازی نشده است.")
            return
        if accepted:
            await home(message, message.from_user.id)
            return
        token = await repo.coordinator.issue_callback(
            "consent",
            message.from_user.id,
            str(terms.id),
            terms.version,
            one_time=True,
        )
        await message.answer(
            f"{terms.title}\n\n{terms.pages[0]}",
            reply_markup=markup([[Button("تأیید قوانین", token, "success")]]),
        )

    @router.callback_query(F.data.startswith("c1."))
    async def callback(query: CallbackQuery) -> None:
        try:
            state = await repo.coordinator.resolve_callback(query.data, query.from_user.id)
            if state["a"] == "consent":
                await repo.accept_terms(query.from_user.id, UUID(state["o"]))
                await home(query.message, query.from_user.id)
            elif state["a"] == "catalog":
                rows = []
                for category in await repo.categories():
                    token = await repo.coordinator.issue_callback(
                        "category", query.from_user.id, str(category.id)
                    )
                    rows.append(
                        [Button(category.title, token, "default", category.custom_emoji_id)]
                    )
                await query.message.answer(
                    "دسته‌بندی‌ها" if rows else "دسته فعالی وجود ندارد.",
                    reply_markup=markup(rows) if rows else None,
                )
            elif state["a"] == "account":
                async with repo.sessions.begin() as session:
                    user = await repo.user(query.from_user.id, session)
                cards = await repo.verified_cards(query.from_user.id)
                await query.message.answer(
                    f"وضعیت KYC: {user.kyc_status}\nکارت‌های تأییدشده: {len(cards)}"
                )
            elif state["a"] == "my_orders":
                orders = await repo.customer_orders(query.from_user.id)
                text_value = (
                    "\n".join(
                        f"{item.id} | {item.status} | {item.amount_toman} تومان" for item in orders
                    )
                    or "سفارشی وجود ندارد."
                )
                await query.message.answer(text_value)
            elif state["a"] in {"begin_kyc", "begin_card"}:
                target = "kyc.document" if state["a"] == "begin_kyc" else "card.bank"
                await repo.coordinator.redis.set(f"fsm:{query.from_user.id}", target, ex=900)
                await query.message.answer(
                    "تصویر یا فایل مدرک هویتی را ارسال کنید."
                    if target == "kyc.document"
                    else "نام بانک را ارسال کنید."
                )
            elif state["a"] == "category":
                rows = []
                for product in await repo.products(UUID(state["o"])):
                    token = await repo.coordinator.issue_callback(
                        "product", query.from_user.id, str(product.id)
                    )
                    rows.append([Button(product.title, token, "default", product.custom_emoji_id)])
                await query.message.answer(
                    "محصولات" if rows else "محصول فعالی وجود ندارد.",
                    reply_markup=markup(rows) if rows else None,
                )
            elif state["a"] == "product":
                product = await repo.product(UUID(state["o"]))
                if not product:
                    raise AccessDenied("PRODUCT_NOT_FOUND")
                buy = await repo.coordinator.issue_callback(
                    "buy", query.from_user.id, str(product.id)
                )
                await query.message.answer(
                    f"{product.title}\n\n{product.description}\nمدت: {product.duration or '-'}\n"
                    f"پلن: {product.plan_type or '-'}\n"
                    f"فعال‌سازی: {product.activation_method or '-'}\n"
                    f"گارانتی: {product.warranty_text or '-'}\n"
                    f"زمان تحویل: {product.delivery_minutes} دقیقه",
                    reply_markup=markup([[Button("خرید", buy, "success")]]),
                )
            elif state["a"] == "buy":
                rows = []
                for card in await repo.verified_cards(query.from_user.id):
                    token = await repo.coordinator.issue_callback(
                        "quote", query.from_user.id, f"{state['o']}:{card.id}", one_time=True
                    )
                    rows.append([Button(f"{card.bank_name} — {card.masked_pan}", token)])
                has_verified_cards = bool(rows)
                if not has_verified_cards:
                    kyc = await repo.coordinator.issue_callback(
                        "begin_kyc", query.from_user.id, one_time=False
                    )
                    card = await repo.coordinator.issue_callback(
                        "begin_card", query.from_user.id, one_time=False
                    )
                    rows = [
                        [Button("ارسال مدارک احراز هویت", kyc, "primary")],
                        [Button("ثبت کارت بانکی", card)],
                    ]
                await query.message.answer(
                    "کارت مبدأ تأییدشده را انتخاب کنید."
                    if has_verified_cards
                    else "برای خرید، KYC و کارت بانکی تأییدشده لازم است.",
                    reply_markup=markup(rows),
                )
            elif state["a"] == "quote":
                product_id, card_id = map(UUID, state["o"].split(":"))
                quote = await repo.create_quote(query.from_user.id, product_id, card_id)
                final = await repo.coordinator.issue_callback(
                    "final", query.from_user.id, str(quote.id), quote.version, one_time=True
                )
                await query.message.answer(
                    f"چک نهایی\n{quote.snapshot['title']}\nمبلغ: {quote.final_toman} تومان\n"
                    "اعتبار قیمت: ۳۰ دقیقه",
                    reply_markup=markup([[Button("تأیید و ادامه", final, "success")]]),
                )
            elif state["a"] == "final":
                quote_id = UUID(state["o"])
                try:
                    order = await repo.final_check(query.from_user.id, quote_id)
                except AccessDenied:
                    requote = await repo.coordinator.issue_callback(
                        "requote", query.from_user.id, str(quote_id), one_time=True
                    )
                    await query.message.answer(
                        "اعتبار قیمت تمام شده است.",
                        reply_markup=markup([[Button("محاسبه قیمت جدید", requote, "primary")]]),
                    )
                    await query.answer()
                    return
                pan, holder = await repo.reveal_destination(query.from_user.id, order.id)
                await repo.coordinator.redis.set(
                    f"receipt-order:{query.from_user.id}", str(order.id), ex=1800
                )
                await query.message.answer(
                    f"کارت مقصد: {pan}\nصاحب کارت: {holder}\nمبلغ: {order.amount_toman} تومان\n"
                    "اکنون تصویر یا فایل رسید را ارسال کنید. رسید به‌تنهایی اثبات پرداخت نیست."
                )
            elif state["a"] == "requote":
                quote = await repo.requote(query.from_user.id, UUID(state["o"]))
                final = await repo.coordinator.issue_callback(
                    "final", query.from_user.id, str(quote.id), quote.version, one_time=True
                )
                await query.message.answer(
                    f"چک نهایی جدید\n{quote.snapshot['title']}\n"
                    f"مبلغ: {quote.final_toman} تومان\nاعتبار: ۳۰ دقیقه",
                    reply_markup=markup([[Button("تأیید و ادامه", final, "success")]]),
                )
            elif state["a"] in {"admin.kyc", "admin.cards", "admin.orders", "admin.audit"}:
                repo.owner(query.from_user.id)
                rows = []
                if state["a"] == "admin.kyc":
                    for item in await repo.kyc_queue(query.from_user.id):
                        approve = await repo.coordinator.issue_callback(
                            "admin.kyc.approve", query.from_user.id, str(item.id), one_time=True
                        )
                        reject = await repo.coordinator.issue_callback(
                            "admin.kyc.reject", query.from_user.id, str(item.id), one_time=True
                        )
                        rows.extend(
                            [
                                [Button(f"تأیید KYC {item.id}", approve, "success")],
                                [Button(f"رد KYC {item.id}", reject, "danger")],
                            ]
                        )
                elif state["a"] == "admin.cards":
                    for item in await repo.card_queue(query.from_user.id):
                        approve = await repo.coordinator.issue_callback(
                            "admin.card.approve", query.from_user.id, str(item.id), one_time=True
                        )
                        reject = await repo.coordinator.issue_callback(
                            "admin.card.reject", query.from_user.id, str(item.id), one_time=True
                        )
                        rows.extend(
                            [
                                [
                                    Button(
                                        f"تأیید {item.bank_name} — {item.masked_pan}",
                                        approve,
                                        "success",
                                    )
                                ],
                                [Button("رد کارت", reject, "danger")],
                            ]
                        )
                elif state["a"] == "admin.orders":
                    for item in await repo.order_queue(query.from_user.id):
                        payment = await repo.payment_for_order(query.from_user.id, item.id)
                        if payment and payment.receipt_file_id:
                            await query.message.answer_photo(
                                payment.receipt_file_id,
                                caption=f"Order ID: {item.id}\nوضعیت: {item.status}\n"
                                "رسید به‌تنهایی اثبات پرداخت نیست.",
                            )
                        if item.status in {"AWAITING_RECONCILIATION", "MANUAL_REVIEW"}:
                            approve = await repo.coordinator.issue_callback(
                                "admin.payment.approve",
                                query.from_user.id,
                                str(item.id),
                                one_time=True,
                            )
                            reject = await repo.coordinator.issue_callback(
                                "admin.payment.reject",
                                query.from_user.id,
                                str(item.id),
                                one_time=True,
                            )
                            rows.extend(
                                [
                                    [Button(f"تأیید پرداخت {item.id}", approve, "success")],
                                    [Button("رد پرداخت", reject, "danger")],
                                ]
                            )
                        elif item.status == "READY_FOR_FULFILLMENT":
                            claim = await repo.coordinator.issue_callback(
                                "admin.order.claim", query.from_user.id, str(item.id), one_time=True
                            )
                            rows.append([Button(f"Claim {item.id}", claim, "primary")])
                        elif (
                            item.status == "PROCESSING"
                            and item.assigned_admin_id == query.from_user.id
                        ):
                            deliver = await repo.coordinator.issue_callback(
                                "admin.order.deliver",
                                query.from_user.id,
                                str(item.id),
                                one_time=True,
                            )
                            rows.append([Button(f"ثبت تحویل {item.id}", deliver, "success")])
                else:
                    events = await repo.audit_events(query.from_user.id)
                    text = (
                        "\n".join(
                            f"{item.at.isoformat()} | {item.action} | {item.target}"
                            for item in events
                        )
                        or "رویدادی وجود ندارد."
                    )
                    await query.message.answer(text)
                if rows:
                    await query.message.answer("صف بررسی", reply_markup=markup(rows))
                elif state["a"] != "admin.audit":
                    await query.message.answer("صف بررسی خالی است.")
            elif state["a"] in {"admin.category", "admin.product", "admin.merchant"}:
                repo.owner(query.from_user.id)
                rows = []
                entity = state["a"].split(".")[-1]
                if entity == "category":
                    items = await repo.owner_categories(query.from_user.id)
                elif entity == "product":
                    items = await repo.owner_products(query.from_user.id)
                else:
                    items = await repo.owner_merchant_cards(query.from_user.id)
                for item in items:
                    toggle = await repo.coordinator.issue_callback(
                        f"admin.{entity}.toggle",
                        query.from_user.id,
                        f"{item.id}:{int(not item.active)}",
                        one_time=True,
                    )
                    label = getattr(item, "title", None) or (
                        f"{item.bank_name} — {item.masked_pan}"
                    )
                    rows.append(
                        [
                            Button(label, toggle, "success" if item.active else "danger"),
                        ]
                    )
                    edit = await repo.coordinator.issue_callback(
                        f"admin.{entity}.edit", query.from_user.id, str(item.id), one_time=True
                    )
                    rows.append([Button("ویرایش", edit)])
                    if entity == "product":
                        pricing = await repo.coordinator.issue_callback(
                            "admin.product.pricing",
                            query.from_user.id,
                            str(item.id),
                            one_time=True,
                        )
                        current_mode = (item.pricing_override or {}).get("mode", "inherit")
                        rows.append([Button(f"قیمت اختصاصی — {current_mode}", pricing, "primary")])
                create = await repo.coordinator.issue_callback(
                    f"admin.{entity}.create", query.from_user.id, one_time=True
                )
                rows.append([Button("ایجاد مورد جدید", create, "primary")])
                await query.message.answer("مدیریت", reply_markup=markup(rows))
            elif state["a"] == "admin.page":
                repo.owner(query.from_user.id)
                rows = []
                for page in await repo.pages(query.from_user.id):
                    buttons = await repo.coordinator.issue_callback(
                        "admin.page.buttons", query.from_user.id, str(page.id), one_time=False
                    )
                    rows.append([Button(f"{page.slug} — مدیریت دکمه‌ها", buttons)])
                create = await repo.coordinator.issue_callback(
                    "admin.page.create", query.from_user.id, one_time=True
                )
                rows.append([Button("ایجاد/ویرایش صفحه", create, "primary")])
                await query.message.answer("صفحه‌ها", reply_markup=markup(rows))
            elif state["a"] == "admin.page.create":
                repo.owner(query.from_user.id)
                await repo.coordinator.redis.set(f"fsm:{query.from_user.id}", "admin.page", ex=900)
                await query.message.answer("slug|متن صفحه را ارسال کنید.")
            elif state["a"] == "admin.page.buttons":
                repo.owner(query.from_user.id)
                page_id = UUID(state["o"])
                rows = []
                for item in await repo.page_buttons(query.from_user.id, page_id):
                    toggle = await repo.coordinator.issue_callback(
                        "admin.button.toggle",
                        query.from_user.id,
                        f"{item.id}:{int(not item.active)}",
                        one_time=True,
                    )
                    rows.append([Button(item.text, toggle, "success" if item.active else "danger")])
                    edit = await repo.coordinator.issue_callback(
                        "admin.button.edit", query.from_user.id, str(item.id), one_time=True
                    )
                    rows.append([Button("ویرایش دکمه", edit)])
                create = await repo.coordinator.issue_callback(
                    "admin.button.create", query.from_user.id, str(page_id), one_time=True
                )
                rows.append([Button("ساخت دکمه", create, "primary")])
                await query.message.answer("دکمه‌های صفحه", reply_markup=markup(rows))
            elif state["a"] == "admin.button.create":
                repo.owner(query.from_user.id)
                await repo.coordinator.redis.set(
                    f"fsm:{query.from_user.id}", f"admin.button:{state['o']}", ex=900
                )
                await query.message.answer(
                    "متن|target|row|position|style|custom_emoji_id یا - را ارسال کنید."
                )
            elif state["a"] == "admin.button.edit":
                repo.owner(query.from_user.id)
                await repo.coordinator.redis.set(
                    f"fsm:{query.from_user.id}", f"admin.button.edit:{state['o']}", ex=900
                )
                await query.message.answer(
                    "متن|target|row|position|style|custom_emoji_id یا -|active را ارسال کنید."
                )
            elif state["a"] == "admin.button.toggle":
                repo.owner(query.from_user.id)
                object_id, active_value = state["o"].split(":", 1)
                await repo.update_page_button(
                    query.from_user.id,
                    UUID(object_id),
                    {"active": bool(int(active_value))},
                )
                await query.message.answer("وضعیت دکمه تغییر کرد.")
            elif state["a"] in {
                "admin.category.create",
                "admin.merchant.create",
            }:
                repo.owner(query.from_user.id)
                target = state["a"].removesuffix(".create")
                await repo.coordinator.redis.set(f"fsm:{query.from_user.id}", target, ex=900)
                prompts = {
                    "admin.category": "عنوان|توضیح|ترتیب|custom_emoji_id یا - را ارسال کنید.",
                    "admin.merchant": "بانک|صاحب کارت|PAN|اولویت|سقف روزانه را ارسال کنید.",
                }
                await query.message.answer(prompts[target])
            elif state["a"] == "admin.product.create":
                repo.owner(query.from_user.id)
                rows = []
                for category in await repo.owner_categories(query.from_user.id):
                    if not category.active:
                        continue
                    choose = await repo.coordinator.issue_callback(
                        "admin.product.create.category",
                        query.from_user.id,
                        str(category.id),
                        one_time=True,
                    )
                    rows.append([Button(category.title, choose)])
                await query.message.answer(
                    "ابتدا دسته محصول را انتخاب کنید." if rows else "ابتدا یک دسته فعال بسازید.",
                    reply_markup=markup(rows) if rows else None,
                )
            elif state["a"] == "admin.product.create.category":
                repo.owner(query.from_user.id)
                await repo.coordinator.redis.set(
                    f"fsm:{query.from_user.id}", f"admin.product:{state['o']}", ex=900
                )
                await query.message.answer(
                    "عنوان|توضیح|USD|مدت|پلن|فعال‌سازی|گارانتی|روز گارانتی|"
                    "دقیقه تحویل|موجودی|unlimited|KYC|ترتیب|fixed_toman یا -|"
                    "custom_emoji_id یا - را ارسال کنید."
                )
            elif state["a"] == "admin.product.pricing":
                repo.owner(query.from_user.id)
                await repo.coordinator.redis.set(
                    f"fsm:{query.from_user.id}",
                    f"admin.product.pricing:{state['o']}",
                    ex=900,
                )
                await query.message.answer(
                    "mode|markup|target_margin|platform_fee|payment_fee|warranty_reserve|"
                    "fixed_cost|fixed_toman یا - را ارسال کنید. برای حذف override، mode=inherit."
                )
            elif state["a"].startswith(
                ("admin.category.toggle", "admin.product.toggle", "admin.merchant.toggle")
            ):
                repo.owner(query.from_user.id)
                object_id, active_value = state["o"].split(":", 1)
                active = bool(int(active_value))
                if state["a"].startswith("admin.category"):
                    await repo.update_category(query.from_user.id, UUID(object_id), active=active)
                elif state["a"].startswith("admin.product"):
                    await repo.update_product(
                        query.from_user.id, UUID(object_id), {"active": active}
                    )
                else:
                    cards = await repo.owner_merchant_cards(query.from_user.id)
                    current = next(item for item in cards if item.id == UUID(object_id))
                    await repo.update_merchant_card(
                        query.from_user.id,
                        current.id,
                        active=active,
                        priority=current.priority,
                        daily_limit=current.daily_limit,
                    )
                await query.message.answer("وضعیت با موفقیت تغییر کرد.")
            elif state["a"].startswith(
                ("admin.category.edit", "admin.product.edit", "admin.merchant.edit")
            ):
                repo.owner(query.from_user.id)
                target = state["a"].removesuffix(".edit")
                await repo.coordinator.redis.set(
                    f"fsm:{query.from_user.id}", f"{target}.edit:{state['o']}", ex=900
                )
                prompts = {
                    "admin.category": "عنوان|توضیح|ترتیب|custom_emoji_id یا - را ارسال کنید.",
                    "admin.product": (
                        "category_id|عنوان|توضیح|USD|مدت|پلن|فعال‌سازی|گارانتی|روز گارانتی|"
                        "دقیقه تحویل|موجودی|unlimited|KYC|ترتیب|fixed_toman یا -|"
                        "custom_emoji_id یا - را ارسال کنید."
                    ),
                    "admin.merchant": "بانک|صاحب کارت|اولویت|سقف روزانه|active را ارسال کنید.",
                }
                await query.message.answer(prompts[target])
            elif state["a"] == "admin.terms":
                repo.owner(query.from_user.id)
                await repo.coordinator.redis.set(
                    f"fsm:{query.from_user.id}", "admin.terms.title", ex=900
                )
                await query.message.answer("عنوان نسخه جدید قوانین را ارسال کنید.")
            elif state["a"] in {
                "admin.rate",
                "admin.pricing",
            }:
                repo.owner(query.from_user.id)
                await repo.coordinator.redis.set(f"fsm:{query.from_user.id}", state["a"], ex=900)
                prompts = {
                    "admin.rate": "نرخ صحیح USD/Toman را ارسال کنید.",
                    "admin.pricing": (
                        "تنظیم قیمت را به‌ترتیب mode|markup|target_margin|platform_fee|"
                        "payment_fee|warranty_reserve|fixed_cost ارسال کنید."
                    ),
                }
                await query.message.answer(prompts[state["a"]])
            elif state["a"].startswith(("admin.kyc.", "admin.card.", "admin.payment.")):
                repo.owner(query.from_user.id)
                await repo.coordinator.redis.set(
                    f"fsm:{query.from_user.id}",
                    f"{state['a']}:{state['o']}",
                    ex=900,
                )
                await query.message.answer("دلیل تصمیم دستی را ارسال کنید.")
            elif state["a"] == "admin.order.claim":
                repo.owner(query.from_user.id)
                await repo.claim(query.from_user.id, UUID(state["o"]))
                await query.message.answer("سفارش به شما Assign و وارد PROCESSING شد.")
            elif state["a"] == "admin.order.deliver":
                repo.owner(query.from_user.id)
                await repo.coordinator.redis.set(
                    f"fsm:{query.from_user.id}", f"admin.delivery:{state['o']}", ex=900
                )
                await query.message.answer("متن تحویل|لینک اختیاری را ارسال کنید.")
            elif state["a"] == "admin.delivery.confirm":
                repo.owner(query.from_user.id)
                draft_key = f"delivery-draft:{query.from_user.id}:{state['o']}"
                draft = await repo.coordinator.redis.get(draft_key)
                if not draft:
                    raise AccessDenied("DELIVERY_DRAFT_EXPIRED")
                content, _, activation_link = draft.partition("\0")
                await repo.deliver(
                    query.from_user.id,
                    UUID(state["o"]),
                    content,
                    activation_link or None,
                )
                await repo.coordinator.redis.delete(draft_key, f"fsm:{query.from_user.id}")
                await query.message.answer("تحویل ثبت و اعلان مشتری در Outbox قرار گرفت.")
            elif state["a"] == "admin.emoji":
                repo.owner(query.from_user.id)
                await repo.coordinator.redis.set(f"fsm:{query.from_user.id}", "admin.emoji", ex=900)
                await query.message.answer(
                    "نام ایموجی را در پاسخ به پیامی دارای Premium Custom Emoji ارسال کنید."
                )
            await query.answer()
        except Exception:
            log.exception("callback rejected", extra={"telegram_id": query.from_user.id})
            await query.answer("درخواست معتبر نیست.", show_alert=True)

    @router.message(Command("admin"))
    async def admin(message: Message) -> None:
        try:
            repo.owner(message.from_user.id)
        except AccessDenied:
            await message.answer("دسترسی مجاز نیست.")
            return
        actions = []
        for action in (
            "terms",
            "rate",
            "pricing",
            "category",
            "product",
            "merchant",
            "kyc",
            "cards",
            "orders",
            "page",
            "emoji",
            "audit",
            "close",
        ):
            actions.append(
                await repo.coordinator.issue_callback(
                    f"admin.{action}", message.from_user.id, one_time=False
                )
            )
        await message.answer(
            "پنل مدیریت",
            reply_markup=markup(
                [
                    [Button("قوانین", actions[0], "primary")],
                    [Button("نرخ دلار", actions[1]), Button("قیمت‌گذاری", actions[2])],
                    [Button("دسته", actions[3]), Button("محصول", actions[4])],
                    [Button("کارت مقصد", actions[5])],
                    [Button("احراز هویت", actions[6]), Button("کارت‌ها", actions[7])],
                    [Button("سفارش‌ها", actions[8], "success")],
                    [Button("صفحه‌ها", actions[9])],
                    [Button("Premium Emoji", actions[10])],
                    [Button("Audit", actions[11])],
                    [Button("بازگشت", actions[12], "danger")],
                ]
            ),
        )

    @router.message(Command("kyc"))
    async def begin_kyc(message: Message) -> None:
        if not await repo.coordinator.rate_limit("kyc", message.from_user.id, 3, 3600):
            await message.answer("تعداد درخواست‌های KYC بیش از حد مجاز است.")
            return
        await repo.coordinator.redis.set(f"fsm:{message.from_user.id}", "kyc.document", ex=900)
        await message.answer("تصویر یا فایل مدرک هویتی را ارسال کنید.")

    @router.message(Command("admin_emoji"))
    async def admin_emoji(message: Message) -> None:
        try:
            repo.owner(message.from_user.id)
            parts = message.text.split(maxsplit=1)
            source = message.reply_to_message
            entities = list((source.entities if source else None) or [])
            identifiers = [
                entity.custom_emoji_id
                for entity in entities
                if entity.type == "custom_emoji" and entity.custom_emoji_id
            ]
            if len(parts) != 2 or not identifiers:
                raise ValueError("NAME_AND_CUSTOM_EMOJI_REQUIRED")
            emoji = await repo.register_emoji(message.from_user.id, parts[1], identifiers[0])
            await message.answer(f"Premium Emoji ثبت شد: {emoji.name}")
        except Exception:
            log.exception("emoji registration failed")
            await message.answer("فرمان را در پاسخ به پیام دارای Custom Emoji ارسال کنید.")

    @router.message(Command("card"))
    async def begin_card(message: Message) -> None:
        if not await repo.coordinator.rate_limit("card", message.from_user.id, 3, 3600):
            await message.answer("تعداد درخواست‌های کارت بیش از حد مجاز است.")
            return
        await repo.coordinator.redis.set(f"fsm:{message.from_user.id}", "card.bank", ex=900)
        await message.answer("نام بانک را بدون اطلاعات محرمانه ارسال کنید.")

    @router.message(F.text)
    async def form_text(message: Message) -> None:
        state = await repo.coordinator.redis.get(f"fsm:{message.from_user.id}")
        if state and state.startswith("admin.delivery:"):
            try:
                repo.owner(message.from_user.id)
                order_id = UUID(state.split(":", 1)[1])
                content, *link = [item.strip() for item in message.text.split("|", 1)]
                if not content:
                    raise ValueError("DELIVERY_CONTENT_REQUIRED")
                activation_link = link[0] if link and link[0] else ""
                await repo.coordinator.redis.set(
                    f"delivery-draft:{message.from_user.id}:{order_id}",
                    f"{content}\0{activation_link}",
                    ex=900,
                )
                confirm = await repo.coordinator.issue_callback(
                    "admin.delivery.confirm",
                    message.from_user.id,
                    str(order_id),
                    one_time=True,
                )
                preview = f"پیش‌نمایش تحویل\n\n{content}"
                if activation_link:
                    preview += f"\n{activation_link}"
                await message.answer(
                    preview,
                    reply_markup=markup([[Button("تأیید نهایی تحویل", confirm, "success")]]),
                )
            except Exception:
                log.exception("delivery failed", extra={"telegram_id": message.from_user.id})
                await message.answer("ثبت تحویل انجام نشد.")
        elif state == "admin.emoji":
            try:
                repo.owner(message.from_user.id)
                source = message.reply_to_message
                entities = list((source.entities if source else None) or [])
                identifiers = [
                    entity.custom_emoji_id
                    for entity in entities
                    if entity.type == "custom_emoji" and entity.custom_emoji_id
                ]
                if not message.text.strip() or not identifiers:
                    raise ValueError("NAME_AND_CUSTOM_EMOJI_REQUIRED")
                emoji = await repo.register_emoji(
                    message.from_user.id, message.text.strip(), identifiers[0]
                )
                await repo.coordinator.redis.delete(f"fsm:{message.from_user.id}")
                await message.answer(f"Premium Emoji ثبت شد: {emoji.name}")
            except Exception:
                log.exception("emoji registration failed")
                await message.answer("پیام باید پاسخ به یک Premium Custom Emoji معتبر باشد.")
        elif state and state.startswith("admin.product.pricing:"):
            try:
                repo.owner(message.from_user.id)
                product_id = UUID(state.rsplit(":", 1)[1])
                values = [item.strip() for item in message.text.split("|")]
                if len(values) != 8:
                    raise ValueError("FIELDS_REQUIRED")
                await repo.set_product_pricing_override(
                    message.from_user.id,
                    product_id,
                    {
                        "mode": values[0],
                        "markup": values[1],
                        "target_margin": values[2],
                        "platform_fee": values[3],
                        "payment_fee": values[4],
                        "warranty_reserve": values[5],
                        "fixed_cost_toman": int(values[6]),
                        "fixed_price_toman": int(values[7]) if values[7] != "-" else None,
                    },
                )
                await repo.coordinator.redis.delete(f"fsm:{message.from_user.id}")
                await message.answer("قانون قیمت اختصاصی محصول ثبت شد.")
            except Exception:
                log.exception("product pricing override failed")
                await message.answer("قانون قیمت اختصاصی معتبر نیست.")
        elif state and state.startswith("admin.product:"):
            try:
                repo.owner(message.from_user.id)
                category_id = UUID(state.split(":", 1)[1])
                values = [item.strip() for item in message.text.split("|")]
                if len(values) != 15:
                    raise ValueError("FIELDS_REQUIRED")
                await repo.create_product(
                    message.from_user.id,
                    category_id,
                    {
                        "title": values[0],
                        "description": values[1],
                        "base_price_usd": values[2],
                        "duration": values[3],
                        "plan_type": values[4],
                        "activation_method": values[5],
                        "warranty_text": values[6],
                        "warranty_days": int(values[7]),
                        "delivery_minutes": int(values[8]),
                        "stock": int(values[9]),
                        "unlimited_stock": values[10].lower() == "true",
                        "requires_kyc": values[11].lower() == "true",
                        "position": int(values[12]),
                        "fixed_price_toman": int(values[13]) if values[13] != "-" else None,
                        "custom_emoji_id": values[14] if values[14] != "-" else None,
                    },
                )
                await repo.coordinator.redis.delete(f"fsm:{message.from_user.id}")
                await message.answer("محصول با موفقیت ثبت شد.")
            except Exception:
                log.exception("product creation failed")
                await message.answer("مشخصات محصول معتبر نیست.")
        elif state and state.startswith("admin.button.edit:"):
            try:
                repo.owner(message.from_user.id)
                button_id = UUID(state.rsplit(":", 1)[1])
                values = [item.strip() for item in message.text.split("|")]
                if len(values) != 7:
                    raise ValueError("FIELDS_REQUIRED")
                await repo.update_page_button(
                    message.from_user.id,
                    button_id,
                    {
                        "text": values[0],
                        "action": values[1],
                        "row": int(values[2]),
                        "position": int(values[3]),
                        "style": values[4],
                        "custom_emoji_id": values[5] if values[5] != "-" else None,
                        "active": values[6].lower() == "true",
                    },
                )
                await repo.coordinator.redis.delete(f"fsm:{message.from_user.id}")
                await message.answer("ویرایش دکمه ثبت شد.")
            except Exception:
                log.exception("button edit failed", extra={"telegram_id": message.from_user.id})
                await message.answer("تنظیمات ویرایش دکمه معتبر نیست.")
        elif state and state.startswith("admin.button:"):
            try:
                repo.owner(message.from_user.id)
                page_id = UUID(state.split(":", 1)[1])
                values = [item.strip() for item in message.text.split("|")]
                if len(values) != 6:
                    raise ValueError("FIELDS_REQUIRED")
                button = await repo.create_page_button(
                    message.from_user.id,
                    page_id,
                    values[0],
                    values[1],
                    int(values[2]),
                    int(values[3]),
                    values[4],
                    values[5] if values[5] != "-" else None,
                )
                await repo.coordinator.redis.delete(f"fsm:{message.from_user.id}")
                await message.answer(f"دکمه ثبت شد: {button.text}")
            except Exception:
                log.exception("button creation failed", extra={"telegram_id": message.from_user.id})
                await message.answer("تنظیمات دکمه معتبر نیست.")
        elif state == "admin.terms.title":
            try:
                repo.owner(message.from_user.id)
                await repo.coordinator.redis.set(
                    f"terms-title:{message.from_user.id}", message.text, ex=900
                )
                await repo.coordinator.redis.set(
                    f"fsm:{message.from_user.id}", "admin.terms.body", ex=900
                )
                await message.answer("متن قوانین را ارسال کنید.")
            except AccessDenied:
                await message.answer("دسترسی مجاز نیست.")
        elif state == "admin.terms.body":
            try:
                repo.owner(message.from_user.id)
                title = await repo.coordinator.redis.get(f"terms-title:{message.from_user.id}")
                if not title:
                    raise ValueError("FORM_EXPIRED")
                terms = await repo.publish_terms(message.from_user.id, title, message.text)
                await repo.coordinator.redis.delete(
                    f"fsm:{message.from_user.id}", f"terms-title:{message.from_user.id}"
                )
                await message.answer(f"نسخه {terms.version} قوانین منتشر شد.")
            except Exception:
                log.exception("terms publication failed")
                await message.answer("انتشار قوانین انجام نشد.")
        elif state in {
            "admin.rate",
            "admin.pricing",
            "admin.category",
            "admin.product",
            "admin.merchant",
            "admin.page",
        }:
            try:
                repo.owner(message.from_user.id)
                values = [item.strip() for item in message.text.split("|")]
                if state == "admin.rate":
                    await repo.set_rate(message.from_user.id, int(values[0]))
                elif state == "admin.pricing":
                    if len(values) != 7:
                        raise ValueError("FIELDS_REQUIRED")
                    await repo.set_pricing(
                        message.from_user.id,
                        {
                            "mode": values[0],
                            "markup": values[1],
                            "target_margin": values[2],
                            "platform_fee": values[3],
                            "payment_fee": values[4],
                            "warranty_reserve": values[5],
                            "fixed_cost_toman": int(values[6]),
                        },
                    )
                elif state == "admin.category":
                    if len(values) not in {3, 4}:
                        raise ValueError("FIELDS_REQUIRED")
                    title, description, position = values[:3]
                    emoji_id = values[3] if len(values) == 4 and values[3] != "-" else None
                    await repo.create_category(
                        message.from_user.id, title, description, int(position), emoji_id
                    )
                elif state == "admin.product":
                    if len(values) not in {12, 16}:
                        raise ValueError("FIELDS_REQUIRED")
                    expanded = len(values) == 16
                    await repo.create_product(
                        message.from_user.id,
                        UUID(values[0]),
                        {
                            "title": values[1],
                            "description": values[2],
                            "base_price_usd": values[3],
                            "duration": values[4],
                            "plan_type": values[5],
                            "activation_method": values[6],
                            "warranty_text": values[7],
                            "warranty_days": int(values[8]),
                            "delivery_minutes": int(values[9]),
                            "stock": int(values[10]),
                            "unlimited_stock": values[11].lower() == "true",
                            "requires_kyc": values[12].lower() == "true" if expanded else True,
                            "position": int(values[13]) if expanded else 0,
                            "fixed_price_toman": (
                                int(values[14]) if expanded and values[14] != "-" else None
                            ),
                            "custom_emoji_id": (
                                values[15] if expanded and values[15] != "-" else None
                            ),
                        },
                    )
                elif state == "admin.merchant":
                    bank, holder, pan, priority, limit = values
                    await repo.create_merchant_card(
                        message.from_user.id, bank, holder, pan, int(priority), int(limit)
                    )
                    with contextlib.suppress(Exception):
                        await message.delete()
                elif state == "admin.page":
                    slug, content = values
                    await repo.upsert_page(message.from_user.id, slug, content)
                await repo.coordinator.redis.delete(f"fsm:{message.from_user.id}")
                await message.answer("تنظیم با موفقیت ثبت شد.")
            except Exception:
                log.exception(
                    "admin configuration failed", extra={"telegram_id": message.from_user.id}
                )
                await message.answer("ورودی معتبر نیست؛ فرم را دوباره آغاز کنید.")
        elif state and state.startswith(
            ("admin.category.edit:", "admin.product.edit:", "admin.merchant.edit:")
        ):
            try:
                repo.owner(message.from_user.id)
                action, object_id = state.rsplit(":", 1)
                values = [item.strip() for item in message.text.split("|")]
                if action == "admin.category.edit":
                    if len(values) != 4:
                        raise ValueError("FIELDS_REQUIRED")
                    await repo.update_category(
                        message.from_user.id,
                        UUID(object_id),
                        title=values[0],
                        description=values[1] or None,
                        position=int(values[2]),
                        custom_emoji_id=values[3] if values[3] != "-" else None,
                    )
                elif action == "admin.product.edit":
                    if len(values) != 16:
                        raise ValueError("FIELDS_REQUIRED")
                    await repo.update_product(
                        message.from_user.id,
                        UUID(object_id),
                        {
                            "category_id": UUID(values[0]),
                            "title": values[1],
                            "description": values[2],
                            "base_price_usd": values[3],
                            "duration": values[4],
                            "plan_type": values[5],
                            "activation_method": values[6],
                            "warranty_text": values[7],
                            "warranty_days": int(values[8]),
                            "delivery_minutes": int(values[9]),
                            "stock": int(values[10]),
                            "unlimited_stock": values[11].lower() == "true",
                            "requires_kyc": values[12].lower() == "true",
                            "position": int(values[13]),
                            "fixed_price_toman": int(values[14]) if values[14] != "-" else None,
                            "custom_emoji_id": values[15] if values[15] != "-" else None,
                        },
                    )
                else:
                    if len(values) != 5:
                        raise ValueError("FIELDS_REQUIRED")
                    await repo.update_merchant_card(
                        message.from_user.id,
                        UUID(object_id),
                        bank_name=values[0],
                        holder_name=values[1],
                        priority=int(values[2]),
                        daily_limit=int(values[3]),
                        active=values[4].lower() == "true",
                    )
                await repo.coordinator.redis.delete(f"fsm:{message.from_user.id}")
                await message.answer("ویرایش با موفقیت ثبت شد.")
            except Exception:
                log.exception("admin edit failed", extra={"telegram_id": message.from_user.id})
                await message.answer("ورودی ویرایش معتبر نیست.")
        elif state and state.startswith("admin."):
            try:
                repo.owner(message.from_user.id)
                action, object_id = state.rsplit(":", 1)
                approved = action.endswith(".approve")
                if action.startswith("admin.kyc."):
                    await repo.review_kyc(
                        message.from_user.id, UUID(object_id), approved, message.text
                    )
                elif action.startswith("admin.card."):
                    await repo.review_card(
                        message.from_user.id, UUID(object_id), approved, message.text
                    )
                elif action.startswith("admin.payment."):
                    await repo.manual_reconcile(
                        message.from_user.id, UUID(object_id), approved, message.text
                    )
                await repo.coordinator.redis.delete(f"fsm:{message.from_user.id}")
                await message.answer("تصمیم دستی ثبت و Audit شد.")
            except Exception:
                log.exception("admin decision failed", extra={"telegram_id": message.from_user.id})
                await message.answer("ثبت تصمیم انجام نشد.")
        elif state == "card.bank":
            await repo.coordinator.redis.set(
                f"card-bank:{message.from_user.id}", message.text.strip(), ex=900
            )
            await repo.coordinator.redis.set(f"fsm:{message.from_user.id}", "card.pan", ex=900)
            await message.answer(
                "شماره ۱۶ رقمی کارت را ارسال کنید. CVV2، PIN، OTP یا رمز ارسال نکنید. "
                "پیام شماره کارت پس از پردازش حذف می‌شود."
            )
        elif state == "card.pan":
            digits = "".join(item for item in message.text if item.isdigit())
            if len(digits) != 16:
                await message.answer("شماره کارت باید دقیقاً ۱۶ رقم باشد.")
                return
            encrypted = repo.vault.encrypt(digits)
            await repo.coordinator.redis.set(f"card-pan:{message.from_user.id}", encrypted, ex=900)
            await repo.coordinator.redis.set(f"fsm:{message.from_user.id}", "card.evidence", ex=900)
            with contextlib.suppress(Exception):
                await message.delete()
            await message.answer("تصویر یا فایل مدرک مالکیت کارت را ارسال کنید.")

    @router.message(F.photo | F.document)
    async def uploaded_file(message: Message) -> None:
        if not await repo.coordinator.rate_limit("receipt", message.from_user.id, 5, 300):
            await message.answer("تعداد ارسال فایل بیش از حد مجاز است.")
            return
        try:
            stored_order = await repo.coordinator.redis.get(f"receipt-order:{message.from_user.id}")
            obj = message.photo[-1] if message.photo else message.document
            file_type = "photo" if message.photo else "document"
            state = await repo.coordinator.redis.get(f"fsm:{message.from_user.id}")
            if stored_order:
                await repo.submit_receipt(
                    message.from_user.id,
                    UUID(stored_order),
                    obj.file_id,
                    obj.file_unique_id,
                    file_type,
                )
                await repo.coordinator.redis.delete(f"receipt-order:{message.from_user.id}")
                await message.answer("رسید ثبت شد؛ رسید به‌تنهایی اثبات پرداخت نیست.")
            elif state == "kyc.document":
                await repo.submit_kyc(
                    message.from_user.id, obj.file_id, obj.file_unique_id, file_type
                )
                await repo.coordinator.redis.delete(f"fsm:{message.from_user.id}")
                await message.answer("مدرک KYC برای بررسی دستی ثبت شد.")
            elif state == "card.evidence":
                bank = await repo.coordinator.redis.get(f"card-bank:{message.from_user.id}")
                envelope = await repo.coordinator.redis.get(f"card-pan:{message.from_user.id}")
                if not bank or not envelope:
                    raise ValueError("CARD_FORM_EXPIRED")
                card = await repo.submit_customer_card(
                    message.from_user.id, bank, repo.vault.decrypt(envelope), obj.file_id
                )
                await repo.coordinator.redis.delete(
                    f"fsm:{message.from_user.id}",
                    f"card-bank:{message.from_user.id}",
                    f"card-pan:{message.from_user.id}",
                )
                await message.answer(
                    f"کارت {card.bank_name} — {card.masked_pan} برای بررسی ثبت شد."
                )
            else:
                await message.answer("برای این فایل درخواست فعالی وجود ندارد.")
        except (ValueError, AccessDenied):
            await message.answer("رسید یا شناسه سفارش معتبر نیست.")
        except Exception:
            log.exception("receipt submission failed", extra={"telegram_id": message.from_user.id})
            await message.answer("ثبت رسید انجام نشد؛ بعداً دوباره تلاش کنید.")

    @router.message()
    async def unsupported_message(message: Message) -> None:
        state = await repo.coordinator.redis.get(f"fsm:{message.from_user.id}")
        receipt_order = await repo.coordinator.redis.get(f"receipt-order:{message.from_user.id}")
        if state in {"kyc.document", "card.evidence"} or receipt_order:
            await message.answer("فقط تصویر یا فایل Document پشتیبانی می‌شود.")

    return router


class Runtime:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.engine, sessions = create_engine_and_session(settings.database_url)
        self.redis = Redis.from_url(settings.redis_url, decode_responses=True)
        self.bot = Bot(settings.bot_token.get_secret_value())
        self.dispatcher = Dispatcher()
        vault = Vault({"v1": settings.encryption_key.get_secret_value().encode()}, "v1")
        self.repo = ShopRepository(
            sessions,
            RedisCoordinator(self.redis),
            vault,
            settings.hmac_key.get_secret_value().encode(),
            settings.admin_telegram_user_id,
            settings.order_notification_chat_id,
        )
        self.dispatcher.include_router(persistent_router(self.repo))
        self.worker: asyncio.Task | None = None

    async def outbox_worker(self) -> None:
        from sqlalchemy import select

        from .db import OutboxRow

        while True:
            try:
                await self.process_outbox_once(select, OutboxRow)
                await self.repo.expire_quotes()
            except Exception:
                log.exception("background worker iteration failed")
            await asyncio.sleep(2)

    async def process_outbox_once(self, select_fn=None, row_type=None) -> bool:
        if select_fn is None or row_type is None:
            from sqlalchemy import select as select_fn

            from .db import OutboxRow as row_type
        async with self.repo.sessions.begin() as session:
            row = await session.scalar(
                select_fn(row_type)
                .where(
                    row_type.sent_at.is_(None),
                    row_type.dead_at.is_(None),
                    row_type.available_at <= self.repo.now(),
                )
                .order_by(row_type.available_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if not row:
                return False
            try:
                if row.payload.get("receipt_file_id"):
                    await self.bot.send_photo(
                        row.chat_id,
                        row.payload["receipt_file_id"],
                        caption=f"{row.kind}\nOrder ID: {row.payload['order_id']}\n"
                        "رسید به‌تنهایی اثبات پرداخت نیست.",
                    )
                else:
                    body = f"{row.kind}\nOrder ID: {row.payload['order_id']}"
                    if row.kind == "ORDER_DELIVERED":
                        body += f"\n\n{row.payload['content']}"
                        if row.payload.get("activation_link"):
                            body += f"\n{row.payload['activation_link']}"
                    await self.bot.send_message(row.chat_id, body)
                row.sent_at = self.repo.now()
            except Exception as exc:
                row.attempts += 1
                row.last_error = type(exc).__name__
                if row.attempts >= 8:
                    row.dead_at = self.repo.now()
                else:
                    delay = min(300, 2 ** min(row.attempts, 8))
                    row.available_at = self.repo.now() + timedelta(seconds=delay)
            return True

    async def close(self) -> None:
        if self.worker:
            self.worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.worker
        await self.bot.session.close()
        await self.redis.aclose()
        await self.engine.dispose()


def create_app(settings: Settings) -> FastAPI:
    runtime = Runtime(settings)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        runtime.worker = asyncio.create_task(runtime.outbox_worker())
        if settings.run_mode == "webhook":
            await runtime.bot.set_webhook(
                settings.webhook_url, secret_token=settings.webhook_secret.get_secret_value()
            )
        try:
            yield
        finally:
            if settings.run_mode == "webhook":
                await runtime.bot.delete_webhook()
            await runtime.close()

    app = FastAPI(title="Telegram commerce core", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.runtime = runtime

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready() -> dict[str, str]:
        try:
            async with runtime.repo.sessions() as session:
                await session.execute(text("SELECT 1"))
            if not await runtime.redis.ping():
                raise RuntimeError("redis unavailable")
            if not settings.bot_token.get_secret_value():
                raise RuntimeError("bot not configured")
        except Exception as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "dependencies unavailable"
            ) from exc
        return {"status": "ready"}

    @app.post("/telegram/webhook")
    async def webhook(
        request: Request, x_telegram_bot_api_secret_token: str | None = Header(default=None)
    ) -> dict[str, bool]:
        expected = settings.webhook_secret.get_secret_value()
        if not expected or x_telegram_bot_api_secret_token != expected:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
        update = Update.model_validate(await request.json(), context={"bot": runtime.bot})
        await runtime.dispatcher.feed_update(runtime.bot, update)
        return {"ok": True}

    return app
