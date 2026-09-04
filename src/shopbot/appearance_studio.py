from __future__ import annotations

import json
import re
import secrets
import types
from dataclasses import dataclass
from uuid import UUID

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .db import ConfigRow, EmojiRow
from .repository import AccessDenied, ShopRepository
from .rich_text import render_rich_text
from .telegram_adapter import Button

THEME_KEY = "appearance.theme.v2"
THEME_CACHE = "appearance:theme:v2"
EMOJI_CACHE = "appearance:emoji:v2"
NAV_KEY = "appearance:admin-nav"
FSM_KEY = "appearance:fsm"
CALLBACK_PREFIX = "a2."

_TRANSPORT_PATCHED = False
_RESOLVE_PATCHED: set[int] = set()


@dataclass(frozen=True)
class Target:
    group: str
    key: str
    label: str
    samples: tuple[str, ...]
    keywords: tuple[str, ...]


CUSTOMER_BUTTON_TARGETS = (
    Target(
        "buttons",
        "catalog",
        "فروشگاه و خرید",
        ("فروشگاه و خرید اشتراک",),
        ("فروشگاه", "خرید", "دسته‌بندی"),
    ),
    Target("buttons", "orders", "سفارش‌های من", ("سفارش‌های من",), ("سفارش", "پیگیری")),
    Target("buttons", "account", "حساب کاربری", ("حساب کاربری",), ("حساب", "پروفایل")),
    Target("buttons", "kyc", "احراز هویت", ("احراز هویت",), ("احراز", "هویت", "kyc")),
    Target(
        "buttons",
        "cards",
        "کارت‌های بانکی",
        ("کارت‌های بانکی من",),
        ("کارت", "بانکی"),
    ),
    Target("buttons", "support", "پشتیبانی", ("پشتیبانی",), ("پشتیبانی", "ساپورت")),
    Target(
        "buttons",
        "continue",
        "ادامه",
        ("ادامه خرید", "تأیید و ادامه"),
        ("ادامه", "خرید"),
    ),
    Target(
        "buttons",
        "confirm",
        "تأیید",
        ("تأیید قوانین", "تأیید و ثبت"),
        ("تأیید", "ثبت"),
    ),
    Target("buttons", "back", "بازگشت", ("بازگشت",), ("بازگشت", "برگشت")),
    Target("buttons", "home", "منوی اصلی", ("منوی اصلی",), ("خانه", "اصلی", "home")),
)

MESSAGE_TARGETS = (
    Target(
        "messages",
        "welcome",
        "پیام خوش‌آمد",
        ("به فروشگاه خوش آمدید",),
        ("خوش آمد", "شروع", "welcome"),
    ),
    Target(
        "messages",
        "catalog",
        "پیام‌های فروشگاه و کاتالوگ",
        ("دسته‌بندی‌ها", "محصولات", "پلن‌ها"),
        ("فروشگاه", "دسته", "محصول", "پلن"),
    ),
    Target(
        "messages",
        "account",
        "پیام‌های حساب کاربری",
        ("حساب کاربری",),
        ("حساب", "پروفایل"),
    ),
    Target(
        "messages",
        "orders",
        "پیام‌های سفارش",
        ("سفارش‌های من", "وضعیت سفارش"),
        ("سفارش", "پیگیری"),
    ),
    Target(
        "messages",
        "kyc",
        "پیام‌های احراز هویت",
        ("احراز هویت", "مدرک KYC"),
        ("احراز", "هویت", "kyc", "مدرک"),
    ),
    Target(
        "messages",
        "cards",
        "پیام‌های کارت بانکی",
        ("کارت بانکی", "شماره کارت"),
        ("کارت", "بانک", "pan"),
    ),
    Target("messages", "support", "پیام‌های پشتیبانی", ("پشتیبانی",), ("پشتیبانی", "ساپورت")),
    Target(
        "messages",
        "checkout",
        "پیام‌های خرید و چک نهایی",
        ("چک نهایی", "اعتبار قیمت"),
        ("چک نهایی", "قیمت", "quote", "اعتبار"),
    ),
    Target(
        "messages",
        "payment",
        "پیام‌های پرداخت و رسید",
        ("کارت مقصد", "رسید ثبت شد"),
        ("پرداخت", "رسید", "کارت مقصد"),
    ),
    Target(
        "messages",
        "delivery",
        "پیام‌های تحویل",
        ("سفارش شما آماده شد", "تحویل"),
        ("تحویل", "آماده شد", "فعال‌سازی"),
    ),
    Target(
        "messages",
        "success",
        "پیام‌های موفقیت",
        ("با موفقیت ثبت شد", "تأیید شد"),
        ("موفق", "ثبت شد", "تأیید شد"),
    ),
    Target(
        "messages",
        "warning",
        "هشدارها و خطاها",
        ("نامعتبر", "منقضی", "انجام نشد"),
        ("خطا", "نامعتبر", "منقضی", "انجام نشد", "وجود ندارد"),
    ),
)

ADMIN_BUTTON_TARGETS = (
    Target(
        "admin_buttons",
        "catalog",
        "فروشگاه و محصولات",
        ("فروشگاه و محصولات", "مدیریت محصولات"),
        ("فروشگاه", "محصول", "دسته‌بندی"),
    ),
    Target(
        "admin_buttons",
        "operations",
        "سفارش‌ها و بررسی‌ها",
        ("سفارش‌ها و بررسی‌ها",),
        ("سفارش", "احراز", "کارت‌های بانکی مشتریان"),
    ),
    Target(
        "admin_buttons",
        "finance",
        "مالی و پرداخت",
        ("مالی و پرداخت", "نرخ ارزها"),
        ("مالی", "پرداخت", "نرخ", "قیمت‌گذاری", "کارت مقصد"),
    ),
    Target(
        "admin_buttons",
        "content",
        "ظاهر و محتوا",
        ("ظاهر و محتوا", "قوانین فروشگاه"),
        ("ظاهر", "محتوا", "قوانین", "صفحات", "emoji"),
    ),
    Target(
        "admin_buttons",
        "system",
        "سیستم و گزارش‌ها",
        ("سیستم و گزارش‌ها",),
        ("سیستم", "گزارش", "audit", "مرکز بررسی"),
    ),
    Target(
        "admin_buttons",
        "create",
        "ساخت / افزودن",
        ("افزودن محصول", "ایجاد مورد جدید"),
        ("افزودن", "ایجاد", "ساخت"),
    ),
    Target("admin_buttons", "edit", "ویرایش", ("ویرایش",), ("ویرایش", "ادیت")),
    Target(
        "admin_buttons",
        "delete",
        "حذف",
        ("حذف محصول", "حذف پلن"),
        ("حذف", "پاک"),
    ),
    Target(
        "admin_buttons",
        "back",
        "بازگشت",
        ("بازگشت به پنل مدیریت",),
        ("بازگشت", "برگشت"),
    ),
)

ALL_TARGETS = CUSTOMER_BUTTON_TARGETS + MESSAGE_TARGETS + ADMIN_BUTTON_TARGETS
TARGET_MAP = {(item.group, item.key): item for item in ALL_TARGETS}
GROUP_LABELS = {
    "buttons": "👤 دکمه‌های اصلی کاربر",
    "messages": "💬 پیام‌های ربات",
    "admin_buttons": "🧩 دکمه‌های پنل مدیریت",
}

WARNING_WORDS = (
    "خطا",
    "نامعتبر",
    "منقضی",
    "انجام نشد",
    "وجود ندارد",
    "مجاز نیست",
    "بیش از حد",
)
SUCCESS_WORDS = ("با موفقیت", "ثبت شد", "تأیید شد", "آماده شد")


def _norm(value: str) -> str:
    return " ".join(str(value or "").replace("‌", " ").lower().split())


def _search_targets(query: str) -> list[Target]:
    needle = _norm(query)
    if not needle:
        return []
    scored: list[tuple[int, Target]] = []
    for target in ALL_TARGETS:
        haystacks = (target.label, *target.samples, *target.keywords)
        normalized = [_norm(item) for item in haystacks]
        if any(needle == item for item in normalized):
            scored.append((0, target))
        elif any(needle in item for item in normalized):
            scored.append((1, target))
    return [item for _, item in sorted(scored, key=lambda pair: (pair[0], pair[1].label))]


def _classify_message(text: str) -> str | None:
    value = _norm(text)
    if any(word in value for word in WARNING_WORDS):
        return "warning"
    if any(word in value for word in SUCCESS_WORDS):
        return "success"
    if "به فروشگاه خوش آمدید" in value or "به فروشگاه خوش آمد" in value:
        return "welcome"
    if any(word in value for word in ("تحویل", "فعال سازی", "فعال‌سازی", "سفارش شما آماده")):
        return "delivery"
    if any(word in value for word in ("پرداخت", "رسید", "کارت مقصد")):
        return "payment"
    if any(word in value for word in ("چک نهایی", "اعتبار قیمت", "محاسبه قیمت", "قیمت جدید")):
        return "checkout"
    if any(word in value for word in ("احراز هویت", "kyc", "مدرک هویتی")):
        return "kyc"
    if any(word in value for word in ("کارت بانکی", "شماره کارت", "کارت های", "کارت‌های")):
        return "cards"
    if "پشتیبانی" in value:
        return "support"
    if "حساب کاربری" in value:
        return "account"
    if "سفارش" in value:
        return "orders"
    if any(word in value for word in ("دسته بندی", "دسته‌بندی", "محصول", "پلن", "فروشگاه")):
        return "catalog"
    return None


def _classify_button(text: str, *, admin: bool) -> str | None:
    value = _norm(text)
    if "بازگشت" in value or "برگشت" in value:
        return "back"
    if not admin:
        if "منوی اصلی" in value or value == "خانه":
            return "home"
        if "ادامه" in value:
            return "continue"
        if "تأیید" in value or "تایید" in value:
            return "confirm"
        if "پشتیبانی" in value:
            return "support"
        if "احراز" in value:
            return "kyc"
        if "کارت" in value:
            return "cards"
        if "حساب" in value:
            return "account"
        if "سفارش" in value:
            return "orders"
        if "فروشگاه" in value or "خرید اشتراک" in value:
            return "catalog"
        return None
    if any(word in value for word in ("حذف", "پاک")):
        return "delete"
    if "ویرایش" in value:
        return "edit"
    if any(word in value for word in ("افزودن", "ایجاد", "ساخت")):
        return "create"
    if any(word in value for word in ("سیستم", "گزارش", "audit", "مرکز بررسی")):
        return "system"
    if any(word in value for word in ("ظاهر", "محتوا", "قوانین", "صفحات", "emoji")):
        return "content"
    if any(word in value for word in ("مالی", "نرخ ارز", "قیمت گذاری", "قیمت‌گذاری", "کارت مقصد")):
        return "finance"
    if any(
        word in value
        for word in (
            "سفارش",
            "احراز",
            "کارت های بانکی مشتریان",
            "کارت‌های بانکی مشتریان",
        )
    ):
        return "operations"
    if any(word in value for word in ("فروشگاه", "محصول", "دسته بندی", "دسته‌بندی", "پلن")):
        return "catalog"
    return None


def _utf16_slice(text: str, offset: int, length: int) -> str:
    raw = text.encode("utf-16-le")
    return raw[offset * 2 : (offset + length) * 2].decode("utf-16-le")


async def _theme(repo: ShopRepository) -> dict:
    cached = await repo.coordinator.redis.get(THEME_CACHE)
    if cached:
        return json.loads(cached)
    async with repo.sessions() as session:
        row = await session.get(ConfigRow, THEME_KEY)
        value = dict(row.value) if row else {}
    value.setdefault("buttons", {})
    value.setdefault("messages", {})
    value.setdefault("admin_buttons", {})
    value.setdefault("auto_unicode", True)
    await repo.coordinator.redis.set(THEME_CACHE, json.dumps(value, ensure_ascii=False), ex=60)
    return value


async def _active_emojis(repo: ShopRepository) -> list[dict]:
    cached = await repo.coordinator.redis.get(EMOJI_CACHE)
    if cached:
        return json.loads(cached)
    async with repo.sessions() as session:
        rows = list(
            (
                await session.scalars(
                    select(EmojiRow).where(EmojiRow.active.is_(True)).order_by(EmojiRow.name)
                )
            ).all()
        )
    value = [
        {
            "id": str(row.id),
            "name": row.name,
            "custom_emoji_id": row.custom_emoji_id,
            "fallback": row.fallback,
        }
        for row in rows
    ]
    await repo.coordinator.redis.set(EMOJI_CACHE, json.dumps(value, ensure_ascii=False), ex=60)
    return value


async def _all_emojis(repo: ShopRepository) -> list[EmojiRow]:
    async with repo.sessions() as session:
        return list((await session.scalars(select(EmojiRow).order_by(EmojiRow.name))).all())


async def _set_theme_target(
    repo: ShopRepository,
    actor: int,
    group: str,
    key: str,
    emoji_name: str | None,
) -> None:
    repo.owner(actor)
    if (group, key) not in TARGET_MAP:
        raise AccessDenied("APPEARANCE_TARGET_INVALID")
    if emoji_name and not await repo.resolve_emoji_key(emoji_name):
        raise AccessDenied("ACTIVE_EMOJI_REQUIRED")
    async with repo.sessions.begin() as session:
        row = await session.get(ConfigRow, THEME_KEY, with_for_update=True)
        value = dict(row.value) if row else {}
        value.setdefault("buttons", {})
        value.setdefault("messages", {})
        value.setdefault("admin_buttons", {})
        value.setdefault("auto_unicode", True)
        if emoji_name:
            value[group][key] = emoji_name
        else:
            value[group].pop(key, None)
        if row:
            row.value, row.updated_at = value, repo.now()
        else:
            session.add(ConfigRow(key=THEME_KEY, value=value, updated_at=repo.now()))
        await repo.audit(
            session,
            actor,
            "appearance.target",
            f"{group}:{key}",
            emoji_name or "none",
        )
    await repo.coordinator.redis.delete(THEME_CACHE)
    if (
        group == "buttons"
        and key
        in {
            "catalog",
            "orders",
            "account",
            "support",
            "kyc",
            "cards",
            "back",
            "home",
        }
        and hasattr(repo, "storefront_config")
    ):
        storefront = await repo.storefront_config()
        buttons = storefront.setdefault("buttons", {})
        button = buttons.setdefault(key, {})
        if emoji_name:
            button["emoji"] = emoji_name
        else:
            button.pop("emoji", None)
        await repo.set_storefront_config(actor, storefront)


async def _toggle_auto_unicode(repo: ShopRepository, actor: int) -> bool:
    repo.owner(actor)
    async with repo.sessions.begin() as session:
        row = await session.get(ConfigRow, THEME_KEY, with_for_update=True)
        value = dict(row.value) if row else {}
        current = bool(value.get("auto_unicode", True))
        value["auto_unicode"] = not current
        value.setdefault("buttons", {})
        value.setdefault("messages", {})
        value.setdefault("admin_buttons", {})
        if row:
            row.value, row.updated_at = value, repo.now()
        else:
            session.add(ConfigRow(key=THEME_KEY, value=value, updated_at=repo.now()))
        await repo.audit(
            session,
            actor,
            "appearance.auto_unicode",
            "theme",
            str(not current),
        )
    await repo.coordinator.redis.delete(THEME_CACHE)
    return not current


async def _token(
    repo: ShopRepository,
    actor: int,
    action: str,
    object_id: str = "",
    *,
    one_time: bool = False,
    ttl: int = 1800,
) -> str:
    opaque = secrets.token_urlsafe(12)
    state = json.dumps(
        {"a": action, "u": actor, "o": object_id, "once": one_time},
        separators=(",", ":"),
    )
    await repo.coordinator.redis.set(f"appearance:cb:{opaque}", state, ex=ttl)
    value = f"{CALLBACK_PREFIX}{opaque}"
    if len(value.encode()) > 64:
        raise AssertionError("appearance callback exceeds Telegram limit")
    return value


async def _resolve_token(repo: ShopRepository, token: str, actor: int) -> dict:
    if not token.startswith(CALLBACK_PREFIX) or len(token.encode()) > 64:
        raise AccessDenied("APPEARANCE_CALLBACK_INVALID")
    key = f"appearance:cb:{token[len(CALLBACK_PREFIX) :]}"
    raw = await repo.coordinator.redis.get(key)
    if not raw:
        raise AccessDenied("APPEARANCE_CALLBACK_EXPIRED")
    state = json.loads(raw)
    if int(state["u"]) != actor:
        raise AccessDenied("APPEARANCE_CALLBACK_OWNER_REQUIRED")
    if state.get("once") and await repo.coordinator.redis.delete(key) != 1:
        raise AccessDenied("APPEARANCE_CALLBACK_REPLAYED")
    return state


async def _set_nav(repo: ShopRepository, section: str) -> None:
    await repo.coordinator.redis.set(NAV_KEY, section, ex=7200)


async def _clear_admin_flow(repo: ShopRepository, actor: int) -> None:
    await repo.coordinator.redis.delete(
        f"fsm:{actor}",
        f"admin-draft:{actor}",
        f"emoji-name:{actor}",
        FSM_KEY,
    )


def _infer_admin_section(action: str) -> str | None:
    if action.startswith(("admin.rate", "admin.pricing", "admin.merchant")):
        return "finance"
    if action.startswith(
        (
            "admin.kyc",
            "admin.card",
            "admin.cards",
            "admin.orders",
            "admin.payment",
            "admin.order",
        )
    ):
        return "operations"
    if action.startswith(
        (
            "admin.terms",
            "admin.page",
            "admin.button",
            "admin.emoji",
            "admin.appearance",
            "admin.kyc_page",
        )
    ):
        return "content"
    if action.startswith(("admin.management", "admin.audit")):
        return "system"
    if action.startswith(("admin.category", "admin.product", "admin.family", "admin.variant")):
        return "catalog"
    return None


async def _peek_legacy_state(repo: ShopRepository, data: str | None) -> dict | None:
    if not data:
        return None
    token = data[2:] if data.startswith("m:") else data
    if not token.startswith("c1."):
        return None
    raw = await repo.coordinator.redis.get(f"callback:{token[3:]}")
    return json.loads(raw) if raw else None


class _ContentSectionFilter(BaseFilter):
    def __init__(self, repo: ShopRepository):
        self.repo = repo

    async def __call__(self, query: CallbackQuery) -> bool:
        state = await _peek_legacy_state(self.repo, query.data)
        return bool(
            query.data
            and query.data.startswith("m:")
            and state
            and state.get("a") == "admin.menu.section"
            and state.get("o") == "content"
            and int(state.get("u", -1)) == query.from_user.id
        )


class _LegacyAppearanceFilter(BaseFilter):
    def __init__(self, repo: ShopRepository):
        self.repo = repo

    async def __call__(self, query: CallbackQuery) -> bool:
        state = await _peek_legacy_state(self.repo, query.data)
        return bool(
            state
            and state.get("a") in {"admin.emoji", "admin.appearance"}
            and int(state.get("u", -1)) == query.from_user.id
        )


class _AppearanceMessageFilter(BaseFilter):
    def __init__(self, repo: ShopRepository):
        self.repo = repo

    async def __call__(self, message: Message) -> bool:
        return bool(
            message.from_user
            and message.from_user.id == self.repo.owner_id
            and await self.repo.coordinator.redis.get(FSM_KEY)
        )


def _navigation_present(markup: InlineKeyboardMarkup | None) -> bool:
    if not markup:
        return False
    for row in markup.inline_keyboard:
        for button in row:
            text = _norm(button.text)
            if any(
                word in text
                for word in (
                    "بازگشت",
                    "برگشت",
                    "لغو",
                    "انصراف",
                    "منوی اصلی",
                    "پنل مدیریت",
                )
            ):
                return True
    return False


async def _back_button(repo: ShopRepository, actor: int) -> Button:
    section = await repo.coordinator.redis.get(NAV_KEY)
    if section and section != "home":
        return Button("⬅️ بازگشت", await _token(repo, actor, "nav.section", section))
    return Button(
        "⬅️ بازگشت به پنل مدیریت",
        await _token(repo, actor, "nav.home"),
    )


async def _ensure_admin_back(
    repo: ShopRepository,
    chat_id: int | None,
    markup: InlineKeyboardMarkup | None,
) -> InlineKeyboardMarkup | None:
    if chat_id != repo.owner_id or not await repo.coordinator.redis.get(NAV_KEY):
        return markup
    if _navigation_present(markup):
        return markup
    button = await _back_button(repo, repo.owner_id)
    payload = markup.model_dump(exclude_none=True) if markup else {"inline_keyboard": []}
    payload["inline_keyboard"].append([button.payload()])
    return InlineKeyboardMarkup.model_validate(payload)


def _plain_markup(markup: InlineKeyboardMarkup | None) -> InlineKeyboardMarkup | None:
    if not markup:
        return None
    payload = markup.model_dump(exclude_none=True)
    for row in payload.get("inline_keyboard", []):
        for button in row:
            button.pop("icon_custom_emoji_id", None)
            button.pop("style", None)
    return InlineKeyboardMarkup.model_validate(payload)


async def _premium_markup(
    repo: ShopRepository,
    chat_id: int | None,
    markup: InlineKeyboardMarkup | None,
) -> InlineKeyboardMarkup | None:
    if not markup:
        return None
    theme = await _theme(repo)
    emojis = await _active_emojis(repo)
    by_name = {item["name"]: item for item in emojis}
    fallback_map = {
        item["fallback"]: item
        for item in emojis
        if item.get("fallback") and item["fallback"] not in {"•", "-", "."}
    }
    admin = chat_id == repo.owner_id and bool(await repo.coordinator.redis.get(NAV_KEY))
    group = "admin_buttons" if admin else "buttons"
    payload = markup.model_dump(exclude_none=True)
    for row in payload.get("inline_keyboard", []):
        for button in row:
            if button.get("icon_custom_emoji_id"):
                continue
            text = str(button.get("text") or "")
            item = None
            for fallback in sorted(fallback_map, key=len, reverse=True):
                if text.startswith(fallback):
                    item = fallback_map[fallback]
                    button["text"] = text[len(fallback) :].lstrip() or text
                    break
            if item is None:
                key = _classify_button(text, admin=admin)
                name = theme.get(group, {}).get(key) if key else None
                item = by_name.get(name) if name else None
            if item:
                button["icon_custom_emoji_id"] = item["custom_emoji_id"]
    return InlineKeyboardMarkup.model_validate(payload)


async def _premium_text(
    repo: ShopRepository,
    text: str,
    parse_mode: str | None = None,
) -> tuple[str, str, bool]:
    theme = await _theme(repo)
    emojis = await _active_emojis(repo)
    by_name = {item["name"]: item for item in emojis}
    replacements = [
        item for item in emojis if item.get("fallback") and item["fallback"] not in {"•", "-", "."}
    ]
    slot = _classify_message(re.sub(r"<[^>]+>", " ", text))
    name = theme.get("messages", {}).get(slot) if slot else None

    if parse_mode and str(parse_mode).upper() != "HTML":
        return text, text, False

    if parse_mode and str(parse_mode).upper() == "HTML":
        transformed = text
        changed = False
        if theme.get("auto_unicode", True) and "<tg-emoji" not in transformed:
            for item in sorted(
                replacements,
                key=lambda value: len(value["fallback"]),
                reverse=True,
            ):
                fallback = item["fallback"]
                if fallback in transformed:
                    transformed = transformed.replace(
                        fallback,
                        f'<tg-emoji emoji-id="{item["custom_emoji_id"]}">{fallback}</tg-emoji>',
                    )
                    changed = True
        if name and name in by_name and not transformed.lstrip().startswith("<tg-emoji"):
            item = by_name[name]
            transformed = (
                f'<tg-emoji emoji-id="{item["custom_emoji_id"]}">{item["fallback"]}</tg-emoji> '
                + transformed
            )
            changed = True
        if not changed:
            return text, text, False
        fallback_text = re.sub(
            r"<tg-emoji[^>]*>(.*?)</tg-emoji>",
            r"\1",
            transformed,
        )
        return transformed, fallback_text, True

    transformed = text
    changed = False
    if theme.get("auto_unicode", True):
        for item in sorted(
            replacements,
            key=lambda value: len(value["fallback"]),
            reverse=True,
        ):
            fallback = item["fallback"]
            if fallback in transformed:
                transformed = transformed.replace(
                    fallback,
                    f"{{emoji:{item['name']}}}",
                )
                changed = True
    if name and name in by_name and not transformed.lstrip().startswith("{emoji:"):
        transformed = f"{{emoji:{name}}} {transformed}"
        changed = True
    if not changed and "{emoji:" not in transformed:
        return text, text, False

    async def resolver(name_value: str):
        item = by_name.get(name_value)
        return (item["custom_emoji_id"], item["fallback"]) if item else None

    rendered = await render_rich_text(transformed, resolver)
    return rendered.html, rendered.fallback, True


def _premium_error(exc: TelegramBadRequest) -> bool:
    detail = str(exc).lower()
    return any(
        word in detail
        for word in (
            "custom emoji",
            "tg-emoji",
            "icon_custom_emoji_id",
            "style",
        )
    )


async def _prepare_send(
    repo: ShopRepository,
    chat_id: int | None,
    text: str,
    kwargs: dict,
) -> tuple[str, str, dict, dict]:
    generic_delivery = "یک رویداد جدید فروشگاه ثبت شد.\n\n"
    if text.startswith(generic_delivery):
        text = "✅ سفارش شما آماده شد\n\n" + text[len(generic_delivery) :]
    if "به فروشگاه خوش آمد" in text:
        await repo.coordinator.redis.delete(NAV_KEY)
    base_markup = await _ensure_admin_back(
        repo,
        chat_id,
        kwargs.get("reply_markup"),
    )
    rich_markup = await _premium_markup(repo, chat_id, base_markup)
    fallback_markup = _plain_markup(base_markup)
    rich_kwargs = dict(kwargs)
    fallback_kwargs = dict(kwargs)
    rich_kwargs["reply_markup"] = rich_markup
    fallback_kwargs["reply_markup"] = fallback_markup
    parse_mode = kwargs.get("parse_mode")
    rich_text, fallback_text, changed = await _premium_text(repo, text, parse_mode)
    if changed and not parse_mode:
        rich_kwargs["parse_mode"] = "HTML"
        fallback_kwargs.pop("parse_mode", None)
    elif changed and parse_mode and str(parse_mode).upper() == "HTML":
        fallback_kwargs["parse_mode"] = "HTML"
    return rich_text, fallback_text, rich_kwargs, fallback_kwargs


def install_appearance_layer(repo: ShopRepository) -> None:
    if not all(
        hasattr(repo, name)
        for name in (
            "coordinator",
            "sessions",
            "owner_id",
        )
    ):
        return
    coordinator_id = id(repo.coordinator)
    if coordinator_id not in _RESOLVE_PATCHED:
        original_resolve = repo.coordinator.resolve_callback

        async def resolve_with_navigation(_self, token: str, actor_id: int) -> dict:
            state = await original_resolve(token, actor_id)
            if actor_id == repo.owner_id:
                action = str(state.get("a") or "")
                if action == "nav.home":
                    await repo.coordinator.redis.delete(NAV_KEY)
                elif action == "admin.menu.home":
                    await _set_nav(repo, "home")
                elif action == "admin.menu.section":
                    await _set_nav(repo, str(state.get("o") or "home"))
                else:
                    section = _infer_admin_section(action)
                    if section:
                        await _set_nav(repo, section)
            return state

        repo.coordinator.resolve_callback = types.MethodType(
            resolve_with_navigation,
            repo.coordinator,
        )
        _RESOLVE_PATCHED.add(coordinator_id)

    global _TRANSPORT_PATCHED
    if _TRANSPORT_PATCHED:
        return
    current_send = Bot.send_message
    current_edit = Bot.edit_message_text
    current_photo = Bot.send_photo
    current_document = Bot.send_document

    def connected_repo(bot: Bot):
        from .enhanced import _BOT_REPOS

        return _BOT_REPOS.get(id(bot))

    async def send_message(self, chat_id, text, *args, **kwargs):
        active_repo = connected_repo(self)
        if not active_repo or not isinstance(text, str):
            return await current_send(self, chat_id, text, *args, **kwargs)
        rich, fallback, rich_kwargs, fallback_kwargs = await _prepare_send(
            active_repo,
            chat_id,
            text,
            kwargs,
        )
        try:
            return await current_send(self, chat_id, rich, *args, **rich_kwargs)
        except TelegramBadRequest as exc:
            if not _premium_error(exc):
                raise
            return await current_send(
                self,
                chat_id,
                fallback,
                *args,
                **fallback_kwargs,
            )

    async def edit_message_text(self, text, *args, **kwargs):
        active_repo = connected_repo(self)
        if not active_repo or not isinstance(text, str):
            return await current_edit(self, text, *args, **kwargs)
        chat_id = kwargs.get("chat_id")
        if chat_id is None and args:
            chat_id = args[0]
        rich, fallback, rich_kwargs, fallback_kwargs = await _prepare_send(
            active_repo,
            chat_id,
            text,
            kwargs,
        )
        try:
            return await current_edit(self, rich, *args, **rich_kwargs)
        except TelegramBadRequest as exc:
            if not _premium_error(exc):
                raise
            return await current_edit(
                self,
                fallback,
                *args,
                **fallback_kwargs,
            )

    async def media_call(original, self, chat_id, media, *args, **kwargs):
        active_repo = connected_repo(self)
        caption = kwargs.get("caption")
        if not active_repo:
            return await original(self, chat_id, media, *args, **kwargs)
        base_markup = await _ensure_admin_back(
            active_repo,
            chat_id,
            kwargs.get("reply_markup"),
        )
        rich_markup = await _premium_markup(active_repo, chat_id, base_markup)
        fallback_markup = _plain_markup(base_markup)
        rich_kwargs = dict(kwargs)
        fallback_kwargs = dict(kwargs)
        rich_kwargs["reply_markup"] = rich_markup
        fallback_kwargs["reply_markup"] = fallback_markup
        if isinstance(caption, str):
            parse_mode = kwargs.get("parse_mode")
            rich, fallback, changed = await _premium_text(
                active_repo,
                caption,
                parse_mode,
            )
            if changed:
                rich_kwargs["caption"] = rich
                fallback_kwargs["caption"] = fallback
                if not parse_mode:
                    rich_kwargs["parse_mode"] = "HTML"
                    fallback_kwargs.pop("parse_mode", None)
                elif str(parse_mode).upper() == "HTML":
                    fallback_kwargs["parse_mode"] = "HTML"
        try:
            return await original(self, chat_id, media, *args, **rich_kwargs)
        except TelegramBadRequest as exc:
            if not _premium_error(exc):
                raise
            return await original(
                self,
                chat_id,
                media,
                *args,
                **fallback_kwargs,
            )

    async def send_photo(self, chat_id, photo, *args, **kwargs):
        return await media_call(
            current_photo,
            self,
            chat_id,
            photo,
            *args,
            **kwargs,
        )

    async def send_document(self, chat_id, document, *args, **kwargs):
        return await media_call(
            current_document,
            self,
            chat_id,
            document,
            *args,
            **kwargs,
        )

    Bot.send_message = send_message
    Bot.edit_message_text = edit_message_text
    Bot.send_photo = send_photo
    Bot.send_document = send_document
    _TRANSPORT_PATCHED = True


async def _render(
    message: Message,
    text: str,
    rows: list[list[Button]],
) -> None:
    from . import runtime as runtime_module

    await runtime_module.answer_keyboard(message, text, rows)


async def _render_content_section(
    message: Message,
    repo: ShopRepository,
    actor: int,
) -> None:
    from .admin_menu import _home_token

    await _set_nav(repo, "content")
    rows = [
        [
            Button(
                "✨ استودیو ظاهر و Premium Emoji",
                await _token(repo, actor, "home"),
                "primary",
            )
        ],
        [
            Button(
                "قوانین فروشگاه",
                await repo.coordinator.issue_callback("admin.terms", actor),
            )
        ],
        [
            Button(
                "صفحات سفارشی",
                await repo.coordinator.issue_callback("admin.page", actor),
            )
        ],
        [
            Button(
                "صفحه احراز هویت",
                await repo.coordinator.issue_callback("admin.kyc_page", actor),
            )
        ],
        [
            Button(
                "⬅️ بازگشت به پنل مدیریت",
                await _home_token(repo, actor),
            )
        ],
    ]
    await _render(
        message,
        "🎨 ظاهر و محتوا\n\nمتن‌های فروشگاه و تمام Premium Emojiهای ربات را از اینجا مدیریت کنید.",
        rows,
    )


async def _render_home(
    message: Message,
    repo: ShopRepository,
    actor: int,
) -> None:
    theme = await _theme(repo)
    count = len(await _active_emojis(repo))
    auto = "روشن ✅" if theme.get("auto_unicode", True) else "خاموش ⏸"
    rows = [
        [
            Button(
                "😀 کتابخانه Premium Emoji",
                await _token(repo, actor, "library"),
                "primary",
            )
        ],
        [
            Button(
                "👤 دکمه‌های اصلی کاربر",
                await _token(repo, actor, "group", "buttons"),
            )
        ],
        [
            Button(
                "💬 پیام‌های ربات",
                await _token(repo, actor, "group", "messages"),
            )
        ],
        [
            Button(
                "🧩 دکمه‌های پنل مدیریت",
                await _token(repo, actor, "group", "admin_buttons"),
            )
        ],
        [
            Button(
                "🔎 جستجوی متن یا دکمه",
                await _token(repo, actor, "target.search"),
                "success",
            )
        ],
        [
            Button(
                f"⚡ تبدیل خودکار ایموجی معمولی: {auto}",
                await _token(repo, actor, "auto.toggle", one_time=True),
            )
        ],
        [
            Button(
                "⬅️ بازگشت به ظاهر و محتوا",
                await _token(repo, actor, "nav.section", "content"),
            )
        ],
    ]
    await _render(
        message,
        f"✨ استودیو ظاهر\n\n{count} Premium Emoji فعال دارید.\n"
        "برای هر دکمه یا گروه پیام فقط Emoji موردنظر را انتخاب کنید؛ "
        "نیازی به تایپ نام Registry داخل متن‌ها نیست.",
        rows,
    )


async def _render_group(
    message: Message,
    repo: ShopRepository,
    actor: int,
    group: str,
) -> None:
    targets = [item for item in ALL_TARGETS if item.group == group]
    theme = await _theme(repo)
    emojis = {item["name"]: item for item in await _active_emojis(repo)}
    rows: list[list[Button]] = []
    for target in targets:
        name = theme.get(group, {}).get(target.key)
        emoji = emojis.get(name) if name else None
        label = target.label if not name else f"{target.label}  •  {name}"
        rows.append(
            [
                Button(
                    label,
                    await _token(
                        repo,
                        actor,
                        "target.choose",
                        f"{group}:{target.key}",
                    ),
                    "success" if name else "default",
                    emoji["custom_emoji_id"] if emoji else None,
                )
            ]
        )
    rows.append(
        [
            Button(
                "🔎 جستجوی متن یا دکمه",
                await _token(repo, actor, "target.search"),
            )
        ]
    )
    rows.append([Button("⬅️ بازگشت", await _token(repo, actor, "home"))])
    await _render(
        message,
        f"{GROUP_LABELS[group]}\n\nموردی را انتخاب کنید و Emoji آن را عوض کنید.",
        rows,
    )


async def _render_chooser(
    message: Message,
    repo: ShopRepository,
    actor: int,
    group: str,
    key: str,
    *,
    query: str | None = None,
) -> None:
    target = TARGET_MAP[(group, key)]
    emojis = await _active_emojis(repo)
    if query:
        needle = _norm(query)
        emojis = [
            item
            for item in emojis
            if needle in _norm(item["name"]) or needle in _norm(item["fallback"])
        ]
    rows: list[list[Button]] = []
    for item in emojis[:24]:
        rows.append(
            [
                Button(
                    item["name"],
                    await _token(
                        repo,
                        actor,
                        "target.set",
                        f"{group}:{key}:{item['name']}",
                        one_time=True,
                    ),
                    "default",
                    item["custom_emoji_id"],
                )
            ]
        )
    if len(emojis) > 24:
        rows.append(
            [
                Button(
                    f"{len(emojis) - 24} مورد دیگر؛ از جستجو استفاده کنید",
                    await _token(
                        repo,
                        actor,
                        "emoji.search",
                        f"{group}:{key}",
                    ),
                )
            ]
        )
    rows.append(
        [
            Button(
                "🔎 جستجوی Emoji",
                await _token(repo, actor, "emoji.search", f"{group}:{key}"),
            )
        ]
    )
    rows.append(
        [
            Button(
                "بدون Emoji",
                await _token(
                    repo,
                    actor,
                    "target.set",
                    f"{group}:{key}:",
                    one_time=True,
                ),
                "danger",
            )
        ]
    )
    rows.append(
        [
            Button(
                "⬅️ بازگشت",
                await _token(repo, actor, "group", group),
            )
        ]
    )
    await _render(
        message,
        f"✨ انتخاب Emoji\n\nهدف: {target.label}\n"
        f"نمونه: {target.samples[0]}\n\n"
        "یکی از Emojiهای ثبت‌شده را انتخاب کنید.",
        rows,
    )


async def _render_library(
    message: Message,
    repo: ShopRepository,
    actor: int,
    query: str | None = None,
) -> None:
    rows_data = await _all_emojis(repo)
    if query:
        needle = _norm(query)
        rows_data = [
            row for row in rows_data if needle in _norm(row.name) or needle in _norm(row.fallback)
        ]
    rows: list[list[Button]] = []
    for emoji in rows_data[:24]:
        rows.append(
            [
                Button(
                    emoji.name,
                    await _token(repo, actor, "emoji.item", str(emoji.id)),
                    "success" if emoji.active else "danger",
                    emoji.custom_emoji_id if emoji.active else None,
                )
            ]
        )
    rows.append(
        [
            Button(
                "➕ ثبت سریع Premium Emoji",
                await _token(repo, actor, "emoji.register"),
                "primary",
            )
        ]
    )
    rows.append(
        [
            Button(
                "🔎 جستجو در Emojiها",
                await _token(repo, actor, "library.search"),
            )
        ]
    )
    rows.append([Button("⬅️ بازگشت", await _token(repo, actor, "home"))])
    await _render(
        message,
        "😀 کتابخانه Premium Emoji\n\nEmoji را بزنید تا وضعیت یا نماد جایگزین آن را تغییر دهید.",
        rows,
    )


async def _render_emoji_item(
    message: Message,
    repo: ShopRepository,
    actor: int,
    emoji_id: UUID,
) -> None:
    async with repo.sessions() as session:
        emoji = await session.get(EmojiRow, emoji_id)
    if not emoji:
        raise AccessDenied("EMOJI_NOT_FOUND")
    toggle = await _token(
        repo,
        actor,
        "emoji.toggle",
        f"{emoji.id}:{int(not emoji.active)}",
        one_time=True,
    )
    rows = [
        [
            Button(
                "غیرفعال کردن" if emoji.active else "فعال کردن",
                toggle,
                "danger" if emoji.active else "success",
            )
        ],
        [
            Button(
                "✏️ تنظیم نماد جایگزین",
                await _token(repo, actor, "emoji.fallback", str(emoji.id)),
            )
        ],
        [
            Button(
                "⬅️ بازگشت به کتابخانه",
                await _token(repo, actor, "library"),
            )
        ],
    ]
    await _render(
        message,
        f"{{emoji:{emoji.name}}} {emoji.name}\n\n"
        f"وضعیت: {'فعال' if emoji.active else 'غیرفعال'}\n"
        f"نماد جایگزین: {emoji.fallback}\n\n"
        "نماد جایگزین باعث می‌شود همان Emoji معمولی در متن‌های ربات "
        "به‌صورت Premium نمایش داده شود.",
        rows,
    )


async def _set_fsm(repo: ShopRepository, value: dict) -> None:
    await repo.coordinator.redis.set(
        FSM_KEY,
        json.dumps(value, ensure_ascii=False),
        ex=900,
    )


async def _prompt_search(
    message: Message,
    repo: ShopRepository,
    actor: int,
    kind: str,
    data: dict | None = None,
) -> None:
    state = {"kind": kind, **(data or {})}
    await _set_fsm(repo, state)
    back_action = "library" if kind == "library_search" else "home"
    if kind == "emoji_search":
        back_action = "target.choose"
        back_object = f"{state['group']}:{state['key']}"
    else:
        back_object = ""
    await _render(
        message,
        "🔎 عبارت موردنظر را تایپ کنید.",
        [
            [
                Button(
                    "⬅️ بازگشت",
                    await _token(
                        repo,
                        actor,
                        back_action,
                        back_object,
                    ),
                )
            ]
        ],
    )


def _custom_emoji_sample(message: Message) -> tuple[str, str, str] | None:
    for source in (message, message.reply_to_message):
        if not source:
            continue
        for attr, text_attr in (
            ("entities", "text"),
            ("caption_entities", "caption"),
        ):
            text = getattr(source, text_attr, None) or ""
            for entity in list(getattr(source, attr, None) or []):
                if str(getattr(entity, "type", "")) != "custom_emoji":
                    continue
                custom_id = str(getattr(entity, "custom_emoji_id", "") or "")
                if not custom_id.isdigit():
                    continue
                sample = (
                    _utf16_slice(
                        text,
                        int(entity.offset),
                        int(entity.length),
                    )
                    or "•"
                )
                current_text = message.text or message.caption or ""
                if source is message:
                    total = len(text.encode("utf-16-le")) // 2
                    current_text = _utf16_slice(text, 0, int(entity.offset)) + _utf16_slice(
                        text,
                        int(entity.offset) + int(entity.length),
                        total - int(entity.offset) - int(entity.length),
                    )
                return custom_id, sample[:16], current_text.strip()
    return None


async def _register_emoji(
    repo: ShopRepository,
    actor: int,
    name: str,
    custom_id: str,
    fallback: str,
) -> None:
    name = name.strip()
    if not name or len(name) > 40:
        raise ValueError("INVALID_EMOJI_NAME")
    row = await repo.register_emoji(actor, name, custom_id)
    async with repo.sessions.begin() as session:
        stored = await session.get(EmojiRow, row.id, with_for_update=True)
        if stored:
            stored.fallback = fallback[:16] or "•"
    await repo.coordinator.redis.delete(EMOJI_CACHE)


async def _handle_text(message: Message, repo: ShopRepository) -> None:
    actor = message.from_user.id
    raw = await repo.coordinator.redis.get(FSM_KEY)
    if not raw:
        return
    state = json.loads(raw)
    kind = state.get("kind")
    text = (message.text or message.caption or "").strip()
    if kind == "target_search":
        await repo.coordinator.redis.delete(FSM_KEY)
        results = _search_targets(text)
        rows = [
            [
                Button(
                    f"{GROUP_LABELS[item.group]} — {item.label}",
                    await _token(
                        repo,
                        actor,
                        "target.choose",
                        f"{item.group}:{item.key}",
                    ),
                )
            ]
            for item in results[:20]
        ]
        rows.append([Button("⬅️ بازگشت", await _token(repo, actor, "home"))])
        await _render(
            message,
            f"🔎 نتیجه جستجو برای «{text}»\n\n{len(results)} مورد پیدا شد.",
            rows,
        )
        return
    if kind == "library_search":
        await repo.coordinator.redis.delete(FSM_KEY)
        await _render_library(message, repo, actor, text)
        return
    if kind == "emoji_search":
        await repo.coordinator.redis.delete(FSM_KEY)
        await _render_chooser(
            message,
            repo,
            actor,
            state["group"],
            state["key"],
            query=text,
        )
        return
    if kind == "emoji_register":
        sample = _custom_emoji_sample(message)
        if not sample:
            await _render(
                message,
                "Premium Emoji پیدا نشد. خود Custom Emoji را ارسال کنید یا به پیامی که "
                "آن Emoji داخلش است Reply بزنید.",
                [[Button("⬅️ بازگشت", await _token(repo, actor, "library"))]],
            )
            return
        custom_id, fallback, suggested = sample
        if suggested and len(suggested) <= 40:
            try:
                await _register_emoji(
                    repo,
                    actor,
                    suggested,
                    custom_id,
                    fallback,
                )
            except (IntegrityError, ValueError):
                await message.answer("این نام یا Premium Emoji قبلاً ثبت شده است.")
                return
            await repo.coordinator.redis.delete(FSM_KEY)
            await _render_library(message, repo, actor)
            return
        await _set_fsm(
            repo,
            {
                "kind": "emoji_register_name",
                "custom_id": custom_id,
                "fallback": fallback,
            },
        )
        await _render(
            message,
            "یک نام کوتاه و قابل جستجو برای این Emoji بنویسید؛ مثلاً shop، orders یا فروشگاه.",
            [[Button("⬅️ بازگشت", await _token(repo, actor, "library"))]],
        )
        return
    if kind == "emoji_register_name":
        try:
            await _register_emoji(
                repo,
                actor,
                text,
                state["custom_id"],
                state["fallback"],
            )
        except (IntegrityError, ValueError):
            await message.answer("نام معتبر نیست یا قبلاً ثبت شده. یک نام کوتاه دیگر بفرستید.")
            return
        await repo.coordinator.redis.delete(FSM_KEY)
        await _render_library(message, repo, actor)
        return
    if kind == "emoji_fallback":
        value = text
        sample = _custom_emoji_sample(message)
        if sample:
            value = sample[1]
        if not value or len(value) > 16:
            await message.answer("یک Emoji معمولی کوتاه مثل 🛍 یا 📦 بفرستید.")
            return
        async with repo.sessions.begin() as session:
            emoji = await session.get(
                EmojiRow,
                UUID(state["emoji_id"]),
                with_for_update=True,
            )
            if not emoji:
                raise AccessDenied("EMOJI_NOT_FOUND")
            emoji.fallback = value
            await repo.audit(
                session,
                actor,
                "emoji.fallback",
                str(emoji.id),
                value,
            )
        await repo.coordinator.redis.delete(FSM_KEY, EMOJI_CACHE)
        await _render_emoji_item(
            message,
            repo,
            actor,
            UUID(state["emoji_id"]),
        )


def build_appearance_studio_router(repo: ShopRepository) -> Router:
    router = Router(name="appearance-studio-v2")

    @router.callback_query(_ContentSectionFilter(repo))
    async def content_section(query: CallbackQuery) -> None:
        try:
            await repo.coordinator.resolve_callback(
                query.data[2:],
                query.from_user.id,
            )
            await _render_content_section(
                query.message,
                repo,
                query.from_user.id,
            )
            await query.answer()
        except AccessDenied:
            await query.answer(
                "این دکمه منقضی شده است.",
                show_alert=True,
            )

    @router.callback_query(_LegacyAppearanceFilter(repo))
    async def legacy_entry(query: CallbackQuery) -> None:
        try:
            await repo.coordinator.resolve_callback(
                query.data,
                query.from_user.id,
            )
            await _set_nav(repo, "content")
            await _clear_admin_flow(repo, query.from_user.id)
            await _render_home(
                query.message,
                repo,
                query.from_user.id,
            )
            await query.answer()
        except AccessDenied:
            await query.answer(
                "این دکمه منقضی شده است.",
                show_alert=True,
            )

    @router.callback_query(F.data.startswith(CALLBACK_PREFIX))
    async def callback(query: CallbackQuery) -> None:
        if not query.message:
            return
        actor = query.from_user.id
        try:
            state = await _resolve_token(repo, query.data, actor)
            action = state["a"]
            obj = str(state.get("o") or "")
            if action not in {
                "target.search",
                "emoji.search",
                "library.search",
                "emoji.register",
                "emoji.fallback",
            }:
                await repo.coordinator.redis.delete(FSM_KEY)
            if action == "home":
                await _set_nav(repo, "content")
                await _clear_admin_flow(repo, actor)
                await _render_home(query.message, repo, actor)
            elif action == "nav.section":
                await _clear_admin_flow(repo, actor)
                from .admin_menu import render_admin_section

                await render_admin_section(
                    query.message,
                    repo,
                    actor,
                    obj,
                )
            elif action == "nav.home":
                await _clear_admin_flow(repo, actor)
                from .admin_menu import render_admin_home

                await _set_nav(repo, "home")
                await render_admin_home(
                    query.message,
                    repo,
                    actor,
                )
            elif action == "group":
                await _render_group(
                    query.message,
                    repo,
                    actor,
                    obj,
                )
            elif action == "target.choose":
                group, key = obj.split(":", 1)
                await _render_chooser(
                    query.message,
                    repo,
                    actor,
                    group,
                    key,
                )
            elif action == "target.set":
                group, key, name = obj.split(":", 2)
                await _set_theme_target(
                    repo,
                    actor,
                    group,
                    key,
                    name or None,
                )
                await _render_group(
                    query.message,
                    repo,
                    actor,
                    group,
                )
                await query.answer("ذخیره شد")
                return
            elif action == "target.search":
                await _prompt_search(
                    query.message,
                    repo,
                    actor,
                    "target_search",
                )
            elif action == "emoji.search":
                group, key = obj.split(":", 1)
                await _prompt_search(
                    query.message,
                    repo,
                    actor,
                    "emoji_search",
                    {"group": group, "key": key},
                )
            elif action == "library":
                await _clear_admin_flow(repo, actor)
                await _render_library(
                    query.message,
                    repo,
                    actor,
                )
            elif action == "library.search":
                await _prompt_search(
                    query.message,
                    repo,
                    actor,
                    "library_search",
                )
            elif action == "emoji.register":
                await _set_fsm(repo, {"kind": "emoji_register"})
                await _render(
                    query.message,
                    "➕ ثبت سریع Premium Emoji\n\n"
                    "خود Premium Custom Emoji را ارسال کنید. اگر کنار آن یک نام بنویسید "
                    "همان لحظه ثبت می‌شود؛ اگر فقط Emoji را بفرستید، مرحله بعد نامش را می‌پرسم.",
                    [[Button("⬅️ بازگشت", await _token(repo, actor, "library"))]],
                )
            elif action == "emoji.item":
                await _render_emoji_item(
                    query.message,
                    repo,
                    actor,
                    UUID(obj),
                )
            elif action == "emoji.toggle":
                emoji_id, active = obj.split(":", 1)
                await repo.set_emoji_active(
                    actor,
                    UUID(emoji_id),
                    bool(int(active)),
                )
                await repo.coordinator.redis.delete(EMOJI_CACHE)
                await _render_emoji_item(
                    query.message,
                    repo,
                    actor,
                    UUID(emoji_id),
                )
            elif action == "emoji.fallback":
                await _set_fsm(
                    repo,
                    {
                        "kind": "emoji_fallback",
                        "emoji_id": obj,
                    },
                )
                await _render(
                    query.message,
                    "Emoji معمولی متناظر را بفرستید؛ مثلاً 🛍 یا 📦.\n"
                    "از این مقدار برای تبدیل خودکار Emojiهای معمولی متن ربات به Premium استفاده می‌شود.",
                    [
                        [
                            Button(
                                "⬅️ بازگشت",
                                await _token(
                                    repo,
                                    actor,
                                    "emoji.item",
                                    obj,
                                ),
                            )
                        ]
                    ],
                )
            elif action == "auto.toggle":
                await _toggle_auto_unicode(repo, actor)
                await _render_home(
                    query.message,
                    repo,
                    actor,
                )
            else:
                raise AccessDenied("APPEARANCE_ACTION_INVALID")
            await query.answer()
        except (AccessDenied, ValueError, KeyError):
            await query.answer(
                "این گزینه منقضی شده یا قابل استفاده نیست.",
                show_alert=True,
            )

    @router.message(_AppearanceMessageFilter(repo))
    async def appearance_text(message: Message) -> None:
        try:
            await _handle_text(message, repo)
        except (AccessDenied, ValueError):
            await message.answer(
                "ورودی معتبر نیست. دوباره تلاش کنید یا از دکمه بازگشت استفاده کنید."
            )

    return router
