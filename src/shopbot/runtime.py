from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from uuid import UUID, uuid4

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, Update
from fastapi import FastAPI, Header, HTTPException, Request, status
from redis.asyncio import Redis
from sqlalchemy import text

from .config import Settings
from .db import create_engine_and_session
from .repository import AccessDenied, RedisCoordinator, ShopRepository
from .security import Vault, mask_pan
from .telegram_adapter import Button, extract_message_custom_emoji

log = logging.getLogger(__name__)


class EditablePanel:
    """Minimal message facade used to edit the actor's single admin panel."""

    def __init__(self, source: Message, chat_id: int, message_id: int):
        self.bot = source.bot
        self.chat = SimpleNamespace(id=chat_id)
        self.message_id = message_id
        self.from_user = SimpleNamespace(is_bot=True)

    async def edit_text(self, text_value: str, **kwargs):
        return await self.bot.edit_message_text(
            chat_id=self.chat.id, message_id=self.message_id, text=text_value, **kwargs
        )


def markup(rows: list[list[Button]]) -> InlineKeyboardMarkup:
    # aiogram currently drops new Bot API button fields, so validate centrally and preserve payload.
    return InlineKeyboardMarkup.model_validate(
        {"inline_keyboard": [[button.payload() for button in row] for row in rows]}
    )


def _button_feature_rejected(exc: TelegramBadRequest) -> bool:
    detail = str(exc).lower()
    return "style" in detail or "icon_custom_emoji_id" in detail or "custom emoji" in detail


async def answer_keyboard(message: Message, text_value: str, rows: list[list[Button]]) -> Message:
    """Send a rich keyboard, retrying once only for unsupported Bot API button fields."""

    async def send(reply_markup: InlineKeyboardMarkup) -> Message:
        if getattr(getattr(message, "from_user", None), "is_bot", False):
            return await message.edit_text(text_value, reply_markup=reply_markup)
        return await message.answer(text_value, reply_markup=reply_markup)

    try:
        return await send(markup(rows))
    except TelegramBadRequest as exc:
        if not _button_feature_rejected(exc):
            raise
        plain_rows = [[Button(button.text, button.callback_data) for button in row] for row in rows]
        return await send(markup(plain_rows))


def persistent_router(repo: ShopRepository) -> Router:
    router = Router(name="persistent-commerce")

    async def clear_actor_state(actor_id: int) -> None:
        keys = [
            f"fsm:{actor_id}",
            f"terms-title:{actor_id}",
            f"card-bank:{actor_id}",
            f"card-pan:{actor_id}",
            f"receipt-order:{actor_id}",
            f"admin-draft:{actor_id}",
        ]
        scan_iter = getattr(repo.coordinator.redis, "scan_iter", None)
        if scan_iter:
            async for key in scan_iter(match=f"delivery-draft:{actor_id}:*"):
                keys.append(key)
        await repo.coordinator.redis.delete(*keys)

    def normalize_digits(value: str) -> str:
        table = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
        return value.translate(table).strip()

    def valid_card_number(digits: str) -> bool:
        if len(digits) != 16 or not digits.isdigit():
            return False
        total = 0
        for index, char in enumerate(digits):
            value = int(char) * (2 if index % 2 == 0 else 1)
            total += value - 9 if value > 9 else value
        return total % 10 == 0

    async def load_draft(actor: int) -> dict:
        raw = await repo.coordinator.redis.get(f"admin-draft:{actor}")
        return json.loads(raw) if raw else {}

    async def save_draft(actor: int, draft: dict) -> None:
        await repo.coordinator.redis.set(
            f"admin-draft:{actor}", json.dumps(draft, ensure_ascii=False), ex=900
        )

    async def wizard_buttons(actor: int, *, skip: bool = False) -> list[Button]:
        result = []
        if skip:
            token = await repo.coordinator.issue_callback("admin.wizard.skip", actor, one_time=True)
            result.append(Button("رد کردن", token))
        back = await repo.coordinator.issue_callback("admin.wizard.back", actor, one_time=True)
        cancel = await repo.coordinator.issue_callback("admin.wizard.cancel", actor, one_time=True)
        result.extend((Button("بازگشت", back), Button("لغو", cancel, "danger")))
        return result

    async def set_wizard(message: Message, actor: int, kind: str, step: int, data: dict) -> None:
        repo.owner(actor)
        await save_draft(actor, {"kind": kind, "step": step, "data": data})
        await repo.coordinator.redis.set(f"fsm:{actor}", "admin.wizard", ex=900)
        target = message
        if getattr(getattr(message, "from_user", None), "is_bot", False):
            if getattr(message, "message_id", None) and getattr(message, "chat", None):
                await repo.coordinator.redis.set(
                    f"admin-panel:{actor}",
                    json.dumps({"chat": message.chat.id, "message": message.message_id}),
                    ex=86400,
                )
        elif getattr(message, "bot", None):
            stored_panel = await repo.coordinator.redis.get(f"admin-panel:{actor}")
            if stored_panel:
                panel = json.loads(stored_panel)
                target = EditablePanel(message, panel["chat"], panel["message"])
        await render_wizard(target, actor)

    async def choice_rows(actor: int, choices: list[tuple[str, str]]) -> list[list[Button]]:
        rows = []
        for label, value in choices:
            token = await repo.coordinator.issue_callback(
                "admin.wizard.choice", actor, value, one_time=True
            )
            rows.append([Button(label, token)])
        rows.append(await wizard_buttons(actor))
        return rows

    async def render_wizard(message: Message, actor: int) -> None:
        draft = await load_draft(actor)
        kind, step, data = draft["kind"], draft["step"], draft["data"]
        if kind == "pricing" and step == 0:
            await render_wizard_choice_or_preview(message, actor, kind, step, data)
            return
        if kind in {"merchant", "merchant_edit"} and step == 40:
            await answer_keyboard(
                message,
                "مرحله ۵ از ۷\nسقف روزانه را به تومان وارد کنید.",
                [await wizard_buttons(actor)],
            )
            return
        if kind == "product" and step == 11:
            await answer_keyboard(
                message,
                "مرحله ۱۲ از ۲۰\nتعداد موجودی را وارد کنید.",
                [await wizard_buttons(actor)],
            )
            return
        if kind == "product" and step == 160:
            await answer_keyboard(
                message,
                "مرحله ۱۹ از ۲۰\nمقدار قیمت‌گذاری اختصاصی را وارد کنید.",
                [await wizard_buttons(actor)],
            )
            return
        if kind == "button" and step == 20:
            await answer_keyboard(
                message,
                "مرحله ۳ از ۷\nنشانی کامل HTTPS را وارد کنید.",
                [await wizard_buttons(actor)],
            )
            return
        definitions = {
            "terms": [("عنوان قوانین", False), ("متن کامل قوانین", False), ("صفحه تکمیلی", True)],
            "rate": [("نرخ هر یک دلار را به تومان وارد کنید.", False)],
            "merchant": [("شماره کارت مقصد", False), ("نام بانک", False), ("نام صاحب کارت", False)],
            "merchant_edit": [("نام بانک", False), ("نام صاحب کارت", False)],
            "category": [("عنوان دسته‌بندی", False), ("توضیح دسته‌بندی", True)],
            "page": [("عنوان نمایشی صفحه", False), ("متن صفحه", False)],
            "delivery": [("متن تحویل", False), ("پیوند فعال‌سازی", True)],
            "button": [("متن دکمه", False)],
            "appearance": [("برچسب فارسی دکمه", False)],
            "product": [
                ("عنوان محصول", False),
                ("توضیح کامل محصول", False),
                ("قیمت پایه دلاری", False),
                ("قیمت ثابت تومانی", True),
                ("مدت سرویس", True),
                ("نوع یا پلن", True),
                ("روش فعال‌سازی", True),
                ("متن گارانتی", True),
                ("مدت گارانتی به روز", True),
                ("زمان تقریبی تحویل به دقیقه", False),
            ],
            "pricing": [
                ("درصد روش انتخاب‌شده", False),
                ("درصد هزینه پلتفرم", True),
                ("درصد هزینه پرداخت", True),
                ("درصد ذخیره گارانتی", True),
                ("هزینه ثابت اضافه به تومان", True),
            ],
        }
        text_steps = definitions.get(kind, [])
        text_index = step - 1 if kind == "pricing" else step
        if 0 <= text_index < len(text_steps):
            title, optional = text_steps[text_index]
            await answer_keyboard(
                message,
                f"{title}\n\nمرحله {step + 1} از {len(text_steps) + 2}\n"
                "هر بار فقط همین مقدار را ارسال کنید.",
                [await wizard_buttons(actor, skip=optional)],
            )
            return
        await render_wizard_choice_or_preview(message, actor, kind, step, data)

    async def render_wizard_choice_or_preview(
        message: Message, actor: int, kind: str, step: int, data: dict
    ) -> None:
        if kind in {"merchant", "merchant_edit"}:
            offset = 3 if kind == "merchant" else 2
            choices = {
                offset: [("اولویت ۱", "1"), ("اولویت ۲", "2"), ("اولویت ۳", "3")],
                offset + 1: [("بدون سقف", "0"), ("تعیین سقف روزانه", "limit")],
                offset + 2: [("فعال", "1"), ("غیرفعال", "0")],
                offset + 3: [("تأیید و ثبت", "confirm")],
            }
        elif kind == "category":
            choices = {
                2: [("فعال", "1"), ("غیرفعال", "0")],
                3: [("انتهای فهرست", "0"), ("ابتدای فهرست", "1")],
                4: [("بدون آیکون", "")],
                5: [("تأیید و ثبت", "confirm")],
            }
        elif kind == "pricing":
            # Step five still collects the fixed cost. Confirmation belongs to
            # the following preview step, after every pricing value is present.
            choices = {6: [("تأیید و ثبت", "confirm")]}
        elif kind == "rate":
            choices = {1: [("تأیید نرخ", "confirm")]}
        elif kind == "terms":
            choices = {3: [("انتشار قوانین", "confirm")]}
        elif kind in {"page", "delivery", "button", "appearance", "product"}:
            choices = {}
        else:
            choices = {}
        if kind == "category" and step == 4:
            choices[4] += [
                (emoji.name, emoji.name) for emoji in await repo.emojis(actor, active_only=True)
            ]
        if step in choices and choices[step]:
            if step == max(choices):
                preview = wizard_preview(kind, data)
                await answer_keyboard(message, preview, await choice_rows(actor, choices[step]))
            else:
                await answer_keyboard(
                    message, "گزینه مناسب را انتخاب کنید.", await choice_rows(actor, choices[step])
                )
            return
        # Wizard-specific continuations with inline choices.
        if kind == "pricing" and step == 0:
            await answer_keyboard(
                message,
                "مرحله ۱ از ۷\nروش قیمت‌گذاری را انتخاب کنید.",
                await choice_rows(
                    actor, [("درصد افزایش روی هزینه", "markup"), ("حاشیه سود هدف", "target_margin")]
                ),
            )
        elif kind == "product" and step == 10:
            await answer_keyboard(
                message,
                "مرحله ۱۱ از ۲۰\nنوع موجودی را انتخاب کنید.",
                await choice_rows(
                    actor, [("موجودی نامحدود", "unlimited"), ("موجودی محدود", "limited")]
                ),
            )
        elif kind == "product" and step == 12:
            await answer_keyboard(
                message,
                "مرحله ۱۴ از ۲۰\nآیا احراز هویت لازم است؟",
                await choice_rows(actor, [("لازم است", "1"), ("لازم نیست", "0")]),
            )
        elif kind == "product" and step == 13:
            await answer_keyboard(
                message,
                "مرحله ۱۵ از ۲۰\nوضعیت محصول",
                await choice_rows(actor, [("فعال", "1"), ("غیرفعال", "0")]),
            )
        elif kind == "product" and step == 14:
            await answer_keyboard(
                message,
                "مرحله ۱۶ از ۲۰\nجایگاه نمایش",
                await choice_rows(actor, [("انتهای فهرست", "0"), ("ابتدای فهرست", "1")]),
            )
        elif kind == "product" and step == 15:
            emoji_choices = [("بدون آیکون", "")] + [
                (item.name, item.name) for item in await repo.emojis(actor, active_only=True)
            ]
            await answer_keyboard(
                message,
                "مرحله ۱۷ از ۲۰\nآیکون Premium Emoji",
                await choice_rows(actor, emoji_choices),
            )
        elif kind == "product" and step == 16:
            await answer_keyboard(
                message,
                "مرحله ۱۸ از ۲۰\nروش قیمت‌گذاری محصول",
                await choice_rows(
                    actor,
                    [
                        ("استفاده از قیمت‌گذاری عمومی", "inherit"),
                        ("درصد اختصاصی", "markup"),
                        ("حاشیه سود اختصاصی", "target_margin"),
                        ("قیمت ثابت تومان", "fixed"),
                    ],
                ),
            )
        elif kind == "product" and step == 17:
            await answer_keyboard(
                message,
                wizard_preview(kind, data),
                await choice_rows(actor, [("تأیید و ثبت", "confirm")]),
            )
        elif kind == "page" and step == 2:
            await answer_keyboard(
                message,
                "مرحله ۳ از ۶\nوضعیت صفحه",
                await choice_rows(actor, [("فعال", "1"), ("غیرفعال", "0")]),
            )
        elif kind == "page" and step == 3:
            await answer_keyboard(
                message,
                wizard_preview(kind, data),
                await choice_rows(actor, [("تأیید و ثبت", "confirm")]),
            )
        elif kind == "appearance" and step == 1:
            await answer_keyboard(
                message,
                "مرحله ۲ از ۶\nرنگ رسمی تلگرام",
                await choice_rows(
                    actor,
                    [
                        ("پیش‌فرض", "default"),
                        ("اصلی", "primary"),
                        ("موفق", "success"),
                        ("خطر", "danger"),
                    ],
                ),
            )
        elif kind == "appearance" and step == 2:
            emoji_choices = [("بدون آیکون", "")] + [
                (item.name, item.name) for item in await repo.emojis(actor, active_only=True)
            ]
            await answer_keyboard(
                message, "مرحله ۳ از ۶\nآیکون Registry", await choice_rows(actor, emoji_choices)
            )
        elif kind == "appearance" and step == 3:
            await answer_keyboard(
                message,
                "مرحله ۴ از ۶\nردیف نمایش",
                await choice_rows(actor, [(str(i + 1), str(i)) for i in range(6)]),
            )
        elif kind == "appearance" and step == 4:
            await answer_keyboard(
                message,
                "مرحله ۵ از ۶\nترتیب در ردیف",
                await choice_rows(actor, [(str(i + 1), str(i)) for i in range(4)]),
            )
        elif kind == "appearance" and step == 5:
            await answer_keyboard(
                message,
                wizard_preview(kind, data),
                await choice_rows(actor, [("تأیید و ثبت", "confirm")]),
            )
        elif kind == "button" and step == 1:
            await answer_keyboard(
                message,
                "مرحله ۲ از ۷\nهدف دکمه",
                await choice_rows(
                    actor,
                    [
                        ("فروشگاه", "catalog"),
                        ("حساب کاربری", "account"),
                        ("سفارش‌های من", "my_orders"),
                        ("نشانی اینترنتی امن", "url"),
                    ],
                ),
            )
        elif kind == "button" and step == 2:
            await answer_keyboard(
                message,
                "مرحله ۴ از ۷\nسبک رسمی تلگرام",
                await choice_rows(
                    actor,
                    [
                        ("پیش‌فرض", "default"),
                        ("اصلی", "primary"),
                        ("موفق", "success"),
                        ("خطر", "danger"),
                    ],
                ),
            )
        elif kind == "button" and step == 3:
            emoji_choices = [("بدون آیکون", "")] + [
                (item.name, item.name) for item in await repo.emojis(actor, active_only=True)
            ]
            await answer_keyboard(
                message, "مرحله ۵ از ۷\nآیکون Registry", await choice_rows(actor, emoji_choices)
            )
        elif kind == "button" and step == 4:
            await answer_keyboard(
                message,
                "مرحله ۶ از ۷\nجایگاه دکمه",
                await choice_rows(actor, [("ردیف اول", "0"), ("ردیف بعد", "1")]),
            )
        elif kind == "button" and step == 5:
            await answer_keyboard(
                message,
                wizard_preview(kind, data),
                await choice_rows(actor, [("تأیید و ثبت", "confirm")]),
            )
        else:
            preview = wizard_preview(kind, data)
            await answer_keyboard(
                message, preview, await choice_rows(actor, [("تأیید و ثبت", "confirm")])
            )

    def wizard_preview(kind: str, data: dict) -> str:
        safe = {key: value for key, value in data.items() if key not in {"encrypted_pan"}}
        labels = {
            "terms": "پیش‌نمایش قوانین",
            "rate": "پیش‌نمایش نرخ",
            "pricing": "پیش‌نمایش فرمول قیمت‌گذاری",
            "merchant": "پیش‌نمایش کارت مقصد",
            "category": "پیش‌نمایش دسته‌بندی",
            "product": "پیش‌نمایش محصول",
            "page": "پیش‌نمایش صفحه سفارشی",
            "button": "پیش‌نمایش دکمه",
            "delivery": "پیش‌نمایش تحویل",
            "appearance": "پیش‌نمایش ظاهر پنل",
        }
        return (
            labels.get(kind, "پیش‌نمایش")
            + "\n\n"
            + "\n".join(f"{key}: {value}" for key, value in safe.items())
        )

    async def finish_wizard(message: Message, actor: int, draft: dict) -> None:
        kind, data = draft["kind"], draft["data"]
        if kind == "terms":
            body = data["body"] + (f"\f{data['extra']}" if data.get("extra") else "")
            await repo.publish_terms(actor, data["title"], body)
        elif kind == "rate":
            await repo.set_rate(actor, int(data["rate"]))
        elif kind == "pricing":
            config = {
                "mode": data["mode"],
                "markup": data["percent"] if data["mode"] == "markup" else "0",
                "target_margin": data["percent"] if data["mode"] == "target_margin" else "0",
                "platform_fee": data.get("platform_fee", "0"),
                "payment_fee": data.get("payment_fee", "0"),
                "warranty_reserve": data.get("warranty_reserve", "0"),
                "fixed_cost_toman": int(data.get("fixed_cost_toman", 0)),
            }
            if data.get("product_id"):
                await repo.set_product_pricing_override(actor, UUID(data["product_id"]), config)
            else:
                await repo.set_pricing(actor, config)
        elif kind == "merchant":
            await repo.create_merchant_card_encrypted(
                actor,
                data["bank"],
                data["holder"],
                data["encrypted_pan"],
                data["masked_pan"],
                int(data["priority"]),
                int(data.get("daily_limit", 0)),
                bool(data["active"]),
            )
        elif kind == "merchant_edit":
            await repo.update_merchant_card(
                actor,
                UUID(data["editing_id"]),
                bank_name=data["bank"],
                holder_name=data["holder"],
                priority=int(data["priority"]),
                daily_limit=int(data.get("daily_limit", 0)),
                active=bool(data["active"]),
            )
        elif kind == "category":
            if data.get("editing_id"):
                row = await repo.update_category(
                    actor,
                    UUID(data["editing_id"]),
                    title=data["title"],
                    description=data.get("description"),
                    position=int(data["position"]),
                    active=bool(data["active"]),
                )
            else:
                row = await repo.create_category(
                    actor, data["title"], data.get("description"), int(data["position"])
                )
            await repo.set_entity_emoji(actor, "category", row.id, data.get("emoji"))
            if not data["active"]:
                await repo.update_category(actor, row.id, active=False)
        elif kind == "product":
            values = {
                "title": data["title"],
                "description": data["description"],
                "base_price_usd": data["base_price_usd"],
                "fixed_price_toman": data.get("fixed_price_toman"),
                "duration": data.get("duration"),
                "plan_type": data.get("plan_type"),
                "activation_method": data.get("activation_method"),
                "warranty_text": data.get("warranty_text"),
                "warranty_days": int(data.get("warranty_days", 0)),
                "delivery_minutes": int(data["delivery_minutes"]),
                "stock": int(data.get("stock", 0)),
                "unlimited_stock": bool(data.get("unlimited_stock")),
                "requires_kyc": bool(data.get("requires_kyc", True)),
                "position": int(data.get("position", 0)),
            }
            pricing_mode = data.get("pricing_mode", "inherit")
            if pricing_mode == "fixed":
                values["fixed_price_toman"] = int(data["pricing_value"])
            elif pricing_mode in {"markup", "target_margin"}:
                values["pricing_override"] = {
                    "mode": pricing_mode,
                    "markup": data["pricing_value"] if pricing_mode == "markup" else "0",
                    "target_margin": (
                        data["pricing_value"] if pricing_mode == "target_margin" else "0"
                    ),
                    "platform_fee": "0",
                    "payment_fee": "0",
                    "warranty_reserve": "0",
                    "fixed_cost_toman": 0,
                }
            if data.get("editing_id"):
                values["category_id"] = UUID(data["category_id"])
                values["active"] = bool(data.get("active", True))
                row = await repo.update_product(actor, UUID(data["editing_id"]), values)
            else:
                row = await repo.create_product(actor, UUID(data["category_id"]), values)
                if not data.get("active", True):
                    row = await repo.update_product(actor, row.id, {"active": False})
            await repo.set_entity_emoji(actor, "product", row.id, data.get("emoji"))
        elif kind == "page":
            # The internal key is generated and is never requested from the owner.
            slug = "page-" + re.sub(r"[^a-z0-9]+", "-", data["title"].lower()).strip("-")
            if slug == "page-":
                slug += uuid4().hex[:10]
            await repo.create_page(
                actor, slug[:100], data["title"], data["content"], bool(data["active"])
            )
        elif kind == "button":
            values = {
                "text": data["text"],
                "action": data.get("action", "catalog"),
                "row": int(data.get("row", 0)),
                "position": int(data.get("position", 0)),
                "style": data.get("style", "default"),
                "custom_emoji_id": data.get("emoji"),
            }
            if data.get("button_id"):
                await repo.update_page_button(actor, UUID(data["button_id"]), values)
            else:
                await repo.create_page_button(
                    actor,
                    UUID(data["page_id"]),
                    values["text"],
                    values["action"],
                    values["row"],
                    values["position"],
                    values["style"],
                    values["custom_emoji_id"],
                )
        elif kind == "delivery":
            await repo.deliver(
                actor, UUID(data["order_id"]), data["content"], data.get("activation_link")
            )
        elif kind == "appearance":
            await repo.set_admin_button_preference(actor, data["action"], data)
        await clear_actor_state(actor)
        await admin_home(message, actor)

    async def handle_wizard_choice(message: Message, actor: int, value: str) -> None:
        repo.owner(actor)
        draft = await load_draft(actor)
        if not draft:
            raise AccessDenied("FORM_EXPIRED")
        kind, step, data = draft["kind"], draft["step"], draft["data"]
        if value == "confirm":
            await finish_wizard(message, actor, draft)
            return
        if kind == "pricing" and step == 0:
            data["mode"], step = value, 1
        elif kind == "merchant" and step == 3:
            data["priority"], step = int(value), 4
        elif kind == "merchant" and step == 4:
            if value == "limit":
                step = 40
            else:
                data["daily_limit"], step = 0, 5
        elif kind == "merchant" and step == 5:
            data["active"], step = bool(int(value)), 6
        elif kind == "merchant_edit" and step == 2:
            data["priority"], step = int(value), 3
        elif kind == "merchant_edit" and step == 3:
            if value == "limit":
                step = 40
            else:
                data["daily_limit"], step = 0, 4
        elif kind == "merchant_edit" and step == 4:
            data["active"], step = bool(int(value)), 5
        elif kind == "category" and step == 2:
            data["active"], step = bool(int(value)), 3
        elif kind == "category" and step == 3:
            data["position"], step = int(value), 4
        elif kind == "category" and step == 4:
            data["emoji"], step = value or None, 5
        elif kind == "product" and step == 10:
            data["unlimited_stock"] = value == "unlimited"
            step = 12 if value == "unlimited" else 11
        elif kind == "product" and step == 12:
            data["requires_kyc"], step = bool(int(value)), 13
        elif kind == "product" and step == 13:
            data["active"], step = bool(int(value)), 14
        elif kind == "product" and step == 14:
            data["position"], step = int(value), 15
        elif kind == "product" and step == 15:
            data["emoji"], step = value or None, 16
        elif kind == "product" and step == 16:
            data["pricing_mode"] = value
            step = 17 if value == "inherit" else 160
        elif kind == "page" and step == 2:
            data["active"], step = bool(int(value)), 3
        elif kind == "appearance" and step == 1:
            data["style"], step = value, 2
        elif kind == "appearance" and step == 2:
            data["emoji"], step = value or None, 3
        elif kind == "appearance" and step == 3:
            data["row"], step = int(value), 4
        elif kind == "appearance" and step == 4:
            data["order"], step = int(value), 5
        elif kind == "button" and step == 1:
            step = 20 if value == "url" else 2
            if value != "url":
                data["action"] = value
        elif kind == "button" and step == 2:
            data["style"], step = value, 3
        elif kind == "button" and step == 3:
            data["emoji"], step = value or None, 4
        elif kind == "button" and step == 4:
            data["row"], data["position"], step = int(value), 0, 5
        else:
            data["choice"], step = value, step + 1
        await set_wizard(message, actor, kind, step, data)

    async def admin_home(message: Message, actor_id: int) -> None:
        repo.owner(actor_id)
        status = await repo.setup_status(actor_id)
        preferences = (
            await repo.admin_button_preferences(actor_id)
            if hasattr(repo, "admin_button_preferences")
            else {}
        )

        def mark(ready: bool) -> str:
            return "کامل" if ready else "نیازمند تنظیم"

        names = (
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
            "appearance",
            "audit",
            "close",
        )
        actions = [
            await repo.coordinator.issue_callback(f"admin.{name}", actor_id, one_time=False)
            for name in names
        ]
        defaults = {
            "terms": (f"قوانین فروشگاه — {mark(status['terms'])}", "primary", 0),
            "rate": (f"نرخ دلار — {mark(status['rate'])}", "default", 1),
            "pricing": (f"فرمول قیمت‌گذاری — {mark(status['pricing'])}", "default", 2),
            "merchant": (f"کارت مقصد — {mark(status['merchant'])}", "default", 3),
            "category": (f"دسته‌بندی — {mark(status['category'])}", "default", 4),
            "product": (f"محصول — {mark(status['product'])}", "default", 5),
            "kyc": ("احراز هویت", "default", 6),
            "cards": ("کارت‌ها", "default", 6),
            "orders": ("سفارش‌ها", "success", 7),
            "page": ("صفحات سفارشی", "default", 8),
            "emoji": ("Premium Emoji", "default", 9),
            "appearance": ("ظاهر پنل", "primary", 10),
            "audit": ("Audit", "default", 11),
            "close": ("بازگشت", "danger", 12),
        }
        grouped: dict[int, list[tuple[int, Button]]] = {}
        for index, name in enumerate(names):
            label, style, row = defaults[name]
            pref = preferences.get(name, {})
            icon = await repo.resolve_emoji_key(pref.get("emoji")) if pref.get("emoji") else None
            button = Button(
                pref.get("label", label), actions[index], pref.get("style", style), icon
            )
            grouped.setdefault(int(pref.get("row", row)), []).append(
                (int(pref.get("order", index)), button)
            )
        rows = [[item[1] for item in sorted(grouped[row])] for row in sorted(grouped)]
        await answer_keyboard(
            message,
            "پنل مدیریت\n\n"
            f"وضعیت آمادگی فروشگاه: {'آماده فروش' if all(status.values()) else 'نیازمند تکمیل'}",
            rows,
        )

    async def home(message: Message, actor_id: int) -> None:
        actions = {}
        for action in ("catalog", "account", "begin_kyc", "begin_card", "my_orders"):
            actions[action] = await repo.coordinator.issue_callback(action, actor_id)
        await answer_keyboard(
            message,
            "صفحه اصلی",
            [
                [Button("فروشگاه", actions["catalog"], "primary")],
                [Button("حساب کاربری", actions["account"])],
                [Button("احراز هویت", actions["begin_kyc"])],
                [Button("کارت‌های بانکی", actions["begin_card"])],
                [Button("سفارش‌های من", actions["my_orders"])],
            ],
        )

    @router.message(Command("start"))
    async def start(message: Message) -> None:
        await clear_actor_state(message.from_user.id)
        try:
            repo.owner(message.from_user.id)
        except AccessDenied:
            pass
        else:
            await admin_home(message, message.from_user.id)
            return
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
        await answer_keyboard(
            message,
            f"{terms.title}\n\n{terms.pages[0]}",
            [[Button("تأیید قوانین", token, "success")]],
        )

    @router.callback_query(F.data.startswith("c1."))
    async def callback(query: CallbackQuery) -> None:
        try:
            state = await repo.coordinator.resolve_callback(query.data, query.from_user.id)
            if state["a"] == "consent":
                await repo.accept_terms(query.from_user.id, UUID(state["o"]))
                await home(query.message, query.from_user.id)
            elif state["a"] == "admin.wizard.choice":
                await handle_wizard_choice(query.message, query.from_user.id, state["o"])
            elif state["a"] == "admin.wizard.skip":
                draft = await load_draft(query.from_user.id)
                if not draft:
                    raise AccessDenied("FORM_EXPIRED")
                kind, step, data = draft["kind"], draft["step"], draft["data"]
                fields = {
                    "terms": ["title", "body", "extra"],
                    "pricing": [
                        None,
                        "percent",
                        "platform_fee",
                        "payment_fee",
                        "warranty_reserve",
                        "fixed_cost_toman",
                    ],
                    "category": ["title", "description"],
                    "product": [
                        "title",
                        "description",
                        "base_price_usd",
                        "fixed_price_toman",
                        "duration",
                        "plan_type",
                        "activation_method",
                        "warranty_text",
                        "warranty_days",
                        "delivery_minutes",
                    ],
                    "delivery": ["content", "activation_link"],
                    "button": ["text"],
                }
                index = step - 1 if kind == "pricing" else step
                field = fields.get(kind, [])[index]
                if field:
                    data[field] = "0" if kind == "pricing" else None
                await set_wizard(query.message, query.from_user.id, kind, step + 1, data)
            elif state["a"] == "admin.wizard.back":
                draft = await load_draft(query.from_user.id)
                if not draft:
                    raise AccessDenied("FORM_EXPIRED")
                if draft["step"] == 40:
                    draft["step"] = 4 if draft["kind"] == "merchant" else 3
                elif draft["step"] == 160:
                    draft["step"] = 16
                else:
                    draft["step"] = max(0, draft["step"] - 1)
                await set_wizard(
                    query.message, query.from_user.id, draft["kind"], draft["step"], draft["data"]
                )
            elif state["a"] == "admin.wizard.cancel":
                repo.owner(query.from_user.id)
                await clear_actor_state(query.from_user.id)
                await admin_home(query.message, query.from_user.id)
            elif state["a"] == "catalog":
                rows = []
                for category in await repo.categories():
                    token = await repo.coordinator.issue_callback(
                        "category", query.from_user.id, str(category.id)
                    )
                    icon = await repo.resolve_emoji_key(category.custom_emoji_id)
                    rows.append([Button(category.title, token, "default", icon)])
                if rows:
                    await answer_keyboard(query.message, "دسته‌بندی‌ها", rows)
                else:
                    await query.message.answer("دسته فعالی وجود ندارد.")
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
                    icon = await repo.resolve_emoji_key(product.custom_emoji_id)
                    rows.append([Button(product.title, token, "default", icon)])
                if rows:
                    await answer_keyboard(query.message, "محصولات", rows)
                else:
                    await query.message.answer("محصول فعالی وجود ندارد.")
            elif state["a"] == "product":
                product = await repo.product(UUID(state["o"]))
                if not product:
                    raise AccessDenied("PRODUCT_NOT_FOUND")
                buy = await repo.coordinator.issue_callback(
                    "buy", query.from_user.id, str(product.id)
                )
                await answer_keyboard(
                    query.message,
                    f"{product.title}\n\n{product.description}\nمدت: {product.duration or '-'}\n"
                    f"پلن: {product.plan_type or '-'}\n"
                    f"فعال‌سازی: {product.activation_method or '-'}\n"
                    f"گارانتی: {product.warranty_text or '-'}\n"
                    f"زمان تحویل: {product.delivery_minutes} دقیقه",
                    [[Button("خرید", buy, "success")]],
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
                await answer_keyboard(
                    query.message,
                    "کارت مبدأ تأییدشده را انتخاب کنید."
                    if has_verified_cards
                    else "برای خرید، KYC و کارت بانکی تأییدشده لازم است.",
                    rows,
                )
            elif state["a"] == "quote":
                product_id, card_id = map(UUID, state["o"].split(":"))
                quote = await repo.create_quote(query.from_user.id, product_id, card_id)
                final = await repo.coordinator.issue_callback(
                    "final", query.from_user.id, str(quote.id), quote.version, one_time=True
                )
                await answer_keyboard(
                    query.message,
                    f"چک نهایی\n{quote.snapshot['title']}\nمبلغ: {quote.final_toman} تومان\n"
                    "اعتبار قیمت: ۳۰ دقیقه",
                    [[Button("تأیید و ادامه", final, "success")]],
                )
            elif state["a"] == "final":
                quote_id = UUID(state["o"])
                try:
                    order = await repo.final_check(query.from_user.id, quote_id)
                except AccessDenied:
                    requote = await repo.coordinator.issue_callback(
                        "requote", query.from_user.id, str(quote_id), one_time=True
                    )
                    await answer_keyboard(
                        query.message,
                        "اعتبار قیمت تمام شده است.",
                        [[Button("محاسبه قیمت جدید", requote, "primary")]],
                    )
                    await query.answer()
                    return
                pan, holder = await repo.reveal_destination(query.from_user.id, order.id)
                await repo.coordinator.redis.set(
                    f"receipt-order:{query.from_user.id}", str(order.id), ex=1800
                )
                await query.message.answer(
                    f"کارت مقصد: {pan}\nصاحب کارت: {holder}\nمبلغ: {order.amount_toman} تومان\n"
                    "اکنون تصویر یا فایل رسید را ارسال کنید. رسید به‌تنهایی اثبات پرداخت نیست.",
                )
            elif state["a"] == "requote":
                quote = await repo.requote(query.from_user.id, UUID(state["o"]))
                final = await repo.coordinator.issue_callback(
                    "final", query.from_user.id, str(quote.id), quote.version, one_time=True
                )
                await answer_keyboard(
                    query.message,
                    f"چک نهایی جدید\n{quote.snapshot['title']}\n"
                    f"مبلغ: {quote.final_toman} تومان\nاعتبار: ۳۰ دقیقه",
                    [[Button("تأیید و ادامه", final, "success")]],
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
                    await answer_keyboard(query.message, "صف بررسی", rows)
                elif state["a"] != "admin.audit":
                    await query.message.answer("صف بررسی خالی است.")
            elif state["a"] == "admin.appearance":
                repo.owner(query.from_user.id)
                labels = {
                    "terms": "قوانین",
                    "rate": "نرخ دلار",
                    "pricing": "قیمت‌گذاری",
                    "merchant": "کارت مقصد",
                    "category": "دسته‌بندی",
                    "product": "محصول",
                    "kyc": "احراز هویت",
                    "cards": "کارت‌ها",
                    "orders": "سفارش‌ها",
                    "page": "صفحات سفارشی",
                    "emoji": "Premium Emoji",
                    "audit": "Audit",
                }
                rows = []
                for action, label in labels.items():
                    token = await repo.coordinator.issue_callback(
                        "admin.appearance.action", query.from_user.id, action, one_time=True
                    )
                    rows.append([Button(label, token)])
                await answer_keyboard(
                    query.message, "ظاهر پنل\nدکمه مورد نظر را انتخاب کنید.", rows
                )
            elif state["a"] == "admin.appearance.action":
                await set_wizard(
                    query.message, query.from_user.id, "appearance", 0, {"action": state["o"]}
                )
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
                    if entity in {"category", "product"}:
                        choose_emoji = await repo.coordinator.issue_callback(
                            "admin.entity.emoji",
                            query.from_user.id,
                            f"{entity}:{item.id}",
                            one_time=True,
                        )
                        rows.append([Button("انتخاب Premium Emoji", choose_emoji)])
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
                await answer_keyboard(query.message, "مدیریت", rows)
            elif state["a"] == "admin.page":
                repo.owner(query.from_user.id)
                rows = []
                for page in await repo.pages(query.from_user.id):
                    buttons = await repo.coordinator.issue_callback(
                        "admin.page.buttons", query.from_user.id, str(page.id), one_time=False
                    )
                    rows.append([Button(f"{page.title} — مدیریت دکمه‌ها", buttons)])
                create = await repo.coordinator.issue_callback(
                    "admin.page.create", query.from_user.id, one_time=True
                )
                rows.append([Button("ایجاد/ویرایش صفحه", create, "primary")])
                await answer_keyboard(query.message, "صفحه‌ها", rows)
            elif state["a"] == "admin.entity.emoji":
                repo.owner(query.from_user.id)
                entity, object_id = state["o"].split(":", 1)
                rows = []
                for emoji in await repo.emojis(query.from_user.id, active_only=True):
                    select_emoji = await repo.coordinator.issue_callback(
                        "admin.entity.emoji.select",
                        query.from_user.id,
                        f"{entity}:{object_id}:{emoji.name}",
                        one_time=True,
                    )
                    rows.append([Button(emoji.name, select_emoji)])
                remove = await repo.coordinator.issue_callback(
                    "admin.entity.emoji.select",
                    query.from_user.id,
                    f"{entity}:{object_id}:",
                    one_time=True,
                )
                rows.append([Button("بدون آیکون", remove, "danger")])
                await answer_keyboard(query.message, "انتخاب ایموجی Registry", rows)
            elif state["a"] == "admin.entity.emoji.select":
                repo.owner(query.from_user.id)
                entity, object_id, emoji_name = state["o"].split(":", 2)
                await repo.set_entity_emoji(
                    query.from_user.id, entity, UUID(object_id), emoji_name or None
                )
                await query.message.answer("Premium Emoji انتخاب شد.")
            elif state["a"] == "admin.page.create":
                await set_wizard(query.message, query.from_user.id, "page", 0, {})
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
                    icon = await repo.resolve_emoji_key(item.custom_emoji_id)
                    rows.append(
                        [
                            Button(
                                item.text,
                                toggle,
                                "success" if item.active else "danger",
                                icon,
                            )
                        ]
                    )
                    edit = await repo.coordinator.issue_callback(
                        "admin.button.edit", query.from_user.id, str(item.id), one_time=True
                    )
                    rows.append([Button("ویرایش دکمه", edit)])
                    emoji = await repo.coordinator.issue_callback(
                        "admin.button.emoji", query.from_user.id, str(item.id), one_time=True
                    )
                    rows.append([Button("انتخاب Premium Emoji", emoji)])
                create = await repo.coordinator.issue_callback(
                    "admin.button.create", query.from_user.id, str(page_id), one_time=True
                )
                rows.append([Button("ساخت دکمه", create, "primary")])
                await answer_keyboard(query.message, "دکمه‌های صفحه", rows)
            elif state["a"] == "admin.button.create":
                await set_wizard(
                    query.message, query.from_user.id, "button", 0, {"page_id": state["o"]}
                )
            elif state["a"] == "admin.button.edit":
                await set_wizard(
                    query.message,
                    query.from_user.id,
                    "button",
                    0,
                    {"button_id": state["o"], "editing": True},
                )
            elif state["a"] == "admin.button.emoji":
                repo.owner(query.from_user.id)
                rows = []
                for emoji in await repo.emojis(query.from_user.id, active_only=True):
                    select_emoji = await repo.coordinator.issue_callback(
                        "admin.button.emoji.select",
                        query.from_user.id,
                        f"{state['o']}:{emoji.name}",
                        one_time=True,
                    )
                    rows.append([Button(emoji.name, select_emoji)])
                remove = await repo.coordinator.issue_callback(
                    "admin.button.emoji.select",
                    query.from_user.id,
                    f"{state['o']}:",
                    one_time=True,
                )
                rows.append([Button("بدون آیکون", remove, "danger")])
                await answer_keyboard(query.message, "انتخاب ایموجی Registry", rows)
            elif state["a"] == "admin.button.emoji.select":
                repo.owner(query.from_user.id)
                button_id, emoji_name = state["o"].split(":", 1)
                await repo.set_button_emoji(query.from_user.id, UUID(button_id), emoji_name or None)
                await query.message.answer("Premium Emoji دکمه انتخاب شد.")
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
                kind = state["a"].split(".")[1]
                await set_wizard(query.message, query.from_user.id, kind, 0, {})
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
                if rows:
                    await answer_keyboard(query.message, "ابتدا دسته محصول را انتخاب کنید.", rows)
                else:
                    await query.message.answer("ابتدا یک دسته فعال بسازید.")
            elif state["a"] == "admin.product.create.category":
                await set_wizard(
                    query.message,
                    query.from_user.id,
                    "product",
                    0,
                    {"category_id": state["o"]},
                )
            elif state["a"] == "admin.product.pricing":
                await set_wizard(
                    query.message,
                    query.from_user.id,
                    "pricing",
                    0,
                    {"product_id": state["o"]},
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
                kind = state["a"].split(".")[1]
                object_id = UUID(state["o"])
                if kind == "category":
                    item = next(
                        x
                        for x in await repo.owner_categories(query.from_user.id)
                        if x.id == object_id
                    )
                    data = {
                        "editing_id": state["o"],
                        "title": item.title,
                        "description": item.description or "",
                        "active": item.active,
                        "position": item.position,
                        "emoji": item.custom_emoji_id,
                    }
                elif kind == "merchant":
                    item = next(
                        x
                        for x in await repo.owner_merchant_cards(query.from_user.id)
                        if x.id == object_id
                    )
                    data = {
                        "editing_id": state["o"],
                        "bank": item.bank_name,
                        "holder": item.holder_name,
                        "masked_pan": item.masked_pan,
                        "active": item.active,
                        "priority": item.priority,
                        "daily_limit": item.daily_limit,
                    }
                    kind = "merchant_edit"
                else:
                    item = next(
                        x
                        for x in await repo.owner_products(query.from_user.id)
                        if x.id == object_id
                    )
                    data = {
                        "editing_id": state["o"],
                        "category_id": str(item.category_id),
                        "title": item.title,
                        "description": item.description,
                        "base_price_usd": str(item.base_price_usd),
                        "fixed_price_toman": item.fixed_price_toman,
                        "duration": item.duration,
                        "plan_type": item.plan_type,
                        "activation_method": item.activation_method,
                        "warranty_text": item.warranty_text,
                        "warranty_days": item.warranty_days,
                        "delivery_minutes": item.delivery_minutes,
                        "stock": item.stock,
                        "unlimited_stock": item.unlimited_stock,
                        "requires_kyc": item.requires_kyc,
                        "active": item.active,
                        "position": item.position,
                        "emoji": item.custom_emoji_id,
                    }
                await set_wizard(query.message, query.from_user.id, kind, 0, data)
            elif state["a"] == "admin.terms":
                await set_wizard(query.message, query.from_user.id, "terms", 0, {})
            elif state["a"] in {
                "admin.rate",
                "admin.pricing",
            }:
                await set_wizard(query.message, query.from_user.id, state["a"].split(".")[1], 0, {})
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
                await set_wizard(
                    query.message,
                    query.from_user.id,
                    "delivery",
                    0,
                    {"order_id": state["o"]},
                )
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
            elif state["a"] == "admin.close":
                repo.owner(query.from_user.id)
                await clear_actor_state(query.from_user.id)
                await admin_home(query.message, query.from_user.id)
            elif state["a"] == "admin.emoji":
                repo.owner(query.from_user.id)
                rows = []
                for emoji in await repo.emojis(query.from_user.id):
                    toggle = await repo.coordinator.issue_callback(
                        "admin.emoji.toggle",
                        query.from_user.id,
                        f"{emoji.id}:{int(not emoji.active)}",
                        one_time=True,
                    )
                    rows.append(
                        [Button(emoji.name, toggle, "success" if emoji.active else "danger")]
                    )
                register = await repo.coordinator.issue_callback(
                    "admin.emoji.register", query.from_user.id, one_time=True
                )
                rows.append([Button("ثبت Premium Emoji", register, "primary")])
                await answer_keyboard(query.message, "Premium Emoji Registry", rows)
            elif state["a"] == "admin.emoji.register":
                repo.owner(query.from_user.id)
                await repo.coordinator.redis.set(f"fsm:{query.from_user.id}", "admin.emoji", ex=900)
                await query.message.answer(
                    "نام ایموجی را در پاسخ به پیامی دارای Premium Custom Emoji ارسال کنید."
                )
            elif state["a"] == "admin.emoji.toggle":
                repo.owner(query.from_user.id)
                emoji_id, active = state["o"].split(":", 1)
                await repo.set_emoji_active(query.from_user.id, UUID(emoji_id), bool(int(active)))
                await query.message.answer("وضعیت Premium Emoji تغییر کرد.")
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
        await clear_actor_state(message.from_user.id)
        await admin_home(message, message.from_user.id)

    @router.message(Command("setup"))
    async def setup(message: Message) -> None:
        await admin(message)

    @router.message(Command("cancel"))
    async def cancel(message: Message) -> None:
        await clear_actor_state(message.from_user.id)
        try:
            repo.owner(message.from_user.id)
        except AccessDenied:
            await start(message)
        else:
            await admin_home(message, message.from_user.id)

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
            identifiers = extract_message_custom_emoji(source)
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
        if state == "admin.wizard":
            try:
                repo.owner(message.from_user.id)
                draft = await load_draft(message.from_user.id)
                if not draft:
                    raise ValueError("FORM_EXPIRED")
                kind, step, data = draft["kind"], draft["step"], draft["data"]
                value = normalize_digits(message.text).strip()
                fields = {
                    "terms": ["title", "body", "extra"],
                    "rate": ["rate"],
                    "merchant": ["pan", "bank", "holder"],
                    "merchant_edit": ["bank", "holder"],
                    "category": ["title", "description"],
                    "pricing": [
                        None,
                        "percent",
                        "platform_fee",
                        "payment_fee",
                        "warranty_reserve",
                        "fixed_cost_toman",
                    ],
                    "product": [
                        "title",
                        "description",
                        "base_price_usd",
                        "fixed_price_toman",
                        "duration",
                        "plan_type",
                        "activation_method",
                        "warranty_text",
                        "warranty_days",
                        "delivery_minutes",
                    ],
                    "page": ["title", "content"],
                    "button": ["text", "url"],
                    "delivery": ["content", "activation_link"],
                    "appearance": ["label"],
                }
                if kind in {"merchant", "merchant_edit"} and step == 40:
                    if not value.isdigit() or int(value) <= 0:
                        raise ValueError("POSITIVE_AMOUNT_REQUIRED")
                    data["daily_limit"] = int(value)
                    next_step = 5 if kind == "merchant" else 4
                elif kind == "product" and step == 160:
                    number = Decimal(value)
                    if number < 0 or (data.get("pricing_mode") != "fixed" and number >= 100):
                        raise ValueError("INVALID_PRODUCT_PRICING")
                    data["pricing_value"], next_step = str(number), 17
                elif kind == "button" and step == 20:
                    if not value.startswith("https://"):
                        raise ValueError("HTTPS_URL_REQUIRED")
                    data["action"], next_step = value, 2
                elif kind == "product" and step == 11:
                    if not value.isdigit() or int(value) < 0:
                        raise ValueError("NON_NEGATIVE_NUMBER_REQUIRED")
                    data["stock"], next_step = int(value), 12
                else:
                    index = step - 1 if kind == "pricing" else step
                    field = fields.get(kind, [])[index]
                    if not value:
                        raise ValueError("VALUE_REQUIRED")
                    if kind == "merchant" and field == "pan":
                        digits = "".join(char for char in value if char.isdigit())
                        if not valid_card_number(digits):
                            raise ValueError("CARD_NUMBER_REQUIRED")
                        data["encrypted_pan"] = repo.vault.encrypt(digits)
                        data["masked_pan"] = mask_pan(digits)
                        with contextlib.suppress(Exception):
                            await message.delete()
                    elif kind == "rate":
                        amount = int(value.replace(",", ""))
                        if amount <= 0:
                            raise ValueError("POSITIVE_RATE_REQUIRED")
                        data[field] = amount
                    elif kind == "pricing":
                        number = Decimal(value)
                        if number < 0 or (field != "fixed_cost_toman" and number >= 100):
                            raise ValueError("INVALID_PERCENT")
                        data[field] = str(number)
                    elif kind == "product" and field in {
                        "base_price_usd",
                        "fixed_price_toman",
                        "warranty_days",
                        "delivery_minutes",
                    }:
                        number = Decimal(value)
                        if number < 0:
                            raise ValueError("NON_NEGATIVE_NUMBER_REQUIRED")
                        data[field] = int(number) if field != "base_price_usd" else str(number)
                    else:
                        data[field] = value
                    next_step = step + 1
                await save_draft(
                    message.from_user.id, {"kind": kind, "step": next_step, "data": data}
                )
                with contextlib.suppress(Exception):
                    await message.delete()
                await set_wizard(message, message.from_user.id, kind, next_step, data)
            except (ValueError, InvalidOperation, IndexError):
                await message.answer(
                    "این مقدار معتبر نیست. لطفاً فقط مقدار خواسته‌شده در همین مرحله "
                    "را با قالب درست ارسال کنید."
                )
            except AccessDenied:
                await message.answer("دسترسی مجاز نیست.")
        elif state == "admin.emoji":
            try:
                repo.owner(message.from_user.id)
                identifiers = extract_message_custom_emoji(message.reply_to_message)
                if not message.text.strip() or not identifiers:
                    raise ValueError("NAME_AND_CUSTOM_EMOJI_REQUIRED")
                emoji = await repo.register_emoji(
                    message.from_user.id, message.text.strip(), identifiers[0]
                )
                await clear_actor_state(message.from_user.id)
                await message.answer(f"Premium Emoji ثبت شد: {emoji.name}")
            except Exception:
                log.exception("emoji registration failed")
                await message.answer("پیام باید پاسخ به یک Premium Custom Emoji معتبر باشد.")
        elif state and state.startswith(("admin.kyc.", "admin.card.", "admin.payment.")):
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
                else:
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
