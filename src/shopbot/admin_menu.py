from __future__ import annotations

import json
import secrets
from uuid import UUID

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from .repository import AccessDenied, InvalidState, ShopRepository
from .rich_text import PLACEHOLDER, render_rich_text
from .telegram_adapter import Button
from .variant_store import VariantStore

ADMIN_SECTIONS: dict[str, tuple[str, str, tuple[tuple[str, str, str], ...]]] = {
    "catalog": (
        "🛍 فروشگاه و محصولات",
        "محصولات، پلن‌ها و دسته‌بندی‌های فروشگاه.",
        (
            ("مدیریت محصولات", "admin.product", "primary"),
            ("دسته‌بندی‌ها", "admin.category", "default"),
        ),
    ),
    "operations": (
        "📦 سفارش‌ها و بررسی‌ها",
        "کارهای روزانه فروشگاه و صف‌های بررسی.",
        (
            ("سفارش‌ها", "admin.orders", "success"),
            ("احراز هویت کاربران", "admin.kyc", "default"),
            ("کارت‌های بانکی مشتریان", "admin.cards", "default"),
        ),
    ),
    "finance": (
        "💳 مالی و پرداخت",
        "نرخ ارز، قیمت‌گذاری و کارت‌های دریافت وجه.",
        (
            ("نرخ ارزها", "admin.rate", "default"),
            ("فرمول قیمت‌گذاری", "admin.pricing", "default"),
            ("کارت‌های مقصد", "admin.merchant", "default"),
        ),
    ),
    "content": (
        "🎨 ظاهر و محتوا",
        "متن‌ها، صفحات و ظاهر فروشگاه.",
        (
            ("قوانین فروشگاه", "admin.terms", "default"),
            ("صفحات سفارشی", "admin.page", "default"),
            ("صفحه احراز هویت", "admin.kyc_page", "default"),
            ("Premium Emoji", "admin.emoji", "default"),
            ("ظاهر پنل", "admin.appearance", "primary"),
        ),
    ),
    "system": (
        "⚙️ سیستم و گزارش‌ها",
        "اتصال مرکز مدیریت و مشاهده رویدادهای مدیریتی.",
        (
            ("مرکز بررسی و اعلان‌ها", "admin.management", "primary"),
            ("گزارش فعالیت‌ها", "admin.audit", "default"),
        ),
    ),
}

ADMIN_HOME_TEXT = (
    "⚙️ پنل مدیریت\n\nبخش موردنظر را انتخاب کنید. تنظیمات مرتبط داخل همان بخش قرار دارند."
)
LEGACY_ADMIN_PREFIX = "پنل مدیریت"

_MESSAGE_BRIDGE_INSTALLED = False
_ORIGINAL_MESSAGE_ANSWER = Message.answer
_ORIGINAL_MESSAGE_EDIT_TEXT = Message.edit_text


async def _menu_token(repo: ShopRepository, actor_id: int, section: str) -> str:
    token = await repo.coordinator.issue_callback(
        "admin.menu.section", actor_id, section, one_time=True
    )
    value = f"m:{token}"
    if len(value.encode()) > 64:
        raise AssertionError("admin menu callback exceeds Telegram limit")
    return value


async def _home_token(repo: ShopRepository, actor_id: int) -> str:
    token = await repo.coordinator.issue_callback("admin.menu.home", actor_id, one_time=True)
    value = f"m:{token}"
    if len(value.encode()) > 64:
        raise AssertionError("admin menu callback exceeds Telegram limit")
    return value


async def _legacy_token(repo: ShopRepository, actor_id: int, action: str) -> str:
    return await repo.coordinator.issue_callback(action, actor_id, one_time=False)


async def _ux_token(
    repo: ShopRepository,
    actor_id: int,
    action: str,
    object_id: str = "",
    *,
    one_time: bool = False,
) -> str:
    opaque = secrets.token_urlsafe(12)
    state = json.dumps(
        {"a": action, "u": actor_id, "o": object_id, "once": one_time},
        separators=(",", ":"),
    )
    await repo.coordinator.redis.set(f"adminux:{opaque}", state, ex=1800)
    value = f"u1.{opaque}"
    if len(value.encode()) > 64:
        raise AssertionError("admin UX callback exceeds Telegram limit")
    return value


async def _resolve_ux_token(repo: ShopRepository, token: str, actor_id: int) -> dict:
    if not token.startswith("u1.") or len(token.encode()) > 64:
        raise AccessDenied("ADMIN_UX_CALLBACK_INVALID")
    key = f"adminux:{token[3:]}"
    raw = await repo.coordinator.redis.get(key)
    if not raw:
        raise AccessDenied("ADMIN_UX_CALLBACK_EXPIRED")
    state = json.loads(raw)
    if int(state["u"]) != actor_id:
        raise AccessDenied("ADMIN_UX_CALLBACK_OWNER_REQUIRED")
    if state.get("once") and await repo.coordinator.redis.delete(key) != 1:
        raise AccessDenied("ADMIN_UX_CALLBACK_REPLAYED")
    return state


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


async def admin_home_view(repo: ShopRepository, actor_id: int) -> tuple[str, list[list[Button]]]:
    repo.owner(actor_id)
    rows: list[list[Button]] = []
    section_styles = {
        "catalog": "primary",
        "operations": "success",
        "finance": "default",
        "content": "default",
        "system": "default",
    }
    for key, (title, _description, _items) in ADMIN_SECTIONS.items():
        rows.append([Button(title, await _menu_token(repo, actor_id, key), section_styles[key])])
    rows.append(
        [
            Button(
                "بازگشت به منوی اصلی",
                await _legacy_token(repo, actor_id, "nav.home"),
                "danger",
            )
        ]
    )
    return ADMIN_HOME_TEXT, rows


async def store_admin_home_view(
    repo: ShopRepository, actor_id: int
) -> tuple[str, list[list[Button]]]:
    repo.owner(actor_id)
    store = VariantStore(repo)
    products = await _ux_token(repo, actor_id, "store.products")
    create = await store.issue_callback("admin.family.new", actor_id, one_time=True)
    orders = await store.issue_callback("admin.orders", actor_id, one_time=False)
    return (
        "🛍 مدیریت فروشگاه\n\n"
        "محصولات، پلن‌ها و سفارش‌های فروشگاه را از اینجا مدیریت کنید.",
        [
            [Button("📦 محصولات من", products, "primary")],
            [Button("➕ افزودن محصول جدید", create, "success")],
            [Button("🧾 سفارش‌ها", orders)],
            [Button("⬅️ بازگشت به پنل مدیریت", await _home_token(repo, actor_id))],
        ],
    )


async def products_view(
    repo: ShopRepository, actor_id: int
) -> tuple[str, list[list[Button]]]:
    repo.owner(actor_id)
    store = VariantStore(repo)
    families = await store.owner_families()
    rows: list[list[Button]] = []
    active_count = 0
    for family in families:
        plans = await store.family_variants(family["id"], owner=True)
        if family["active"]:
            active_count += 1
        token = await _ux_token(repo, actor_id, "store.product", str(family["id"]))
        label = f"{family['title']}  •  {len(plans)} پلن"
        rows.append(
            [
                await _rich_button(
                    repo,
                    label,
                    token,
                    "success" if family["active"] else "default",
                    family.get("button_emoji_key"),
                )
            ]
        )
    create = await store.issue_callback("admin.family.new", actor_id, one_time=True)
    rows.append([Button("➕ افزودن محصول", create, "primary")])
    rows.append([Button("⬅️ بازگشت", await _ux_token(repo, actor_id, "store.home"))])
    if not families:
        text = "📦 محصولات من\n\nهنوز محصولی نساخته‌اید."
    else:
        text = (
            "📦 محصولات من\n\n"
            f"{len(families)} محصول ثبت شده • {active_count} محصول فعال"
        )
    return text, rows


async def _category_title(repo: ShopRepository, actor_id: int, category_id: UUID) -> str:
    for category in await repo.owner_categories(actor_id):
        if category.id == category_id:
            return category.title
    return "نامشخص"


async def product_view(
    repo: ShopRepository, actor_id: int, family_id: UUID
) -> tuple[str, list[list[Button]]]:
    repo.owner(actor_id)
    store = VariantStore(repo)
    family = await store.family(family_id)
    if not family:
        raise InvalidState("PRODUCT_FAMILY_NOT_FOUND")
    plans = await store.family_variants(family_id, owner=True)
    category = await _category_title(repo, actor_id, family["category_id"])
    active_plans = sum(1 for item in plans if item["active"])
    plans_token = await _ux_token(repo, actor_id, "store.plans", str(family_id))
    create_plan = await store.issue_callback(
        "admin.variant.new", actor_id, str(family_id), one_time=True
    )
    toggle = await _ux_token(
        repo,
        actor_id,
        "store.product.toggle",
        str(family_id),
        one_time=True,
    )
    status = "فعال ✅" if family["active"] else "غیرفعال ⏸"
    toggle_label = "⏸ غیرفعال کردن محصول" if family["active"] else "▶️ فعال کردن محصول"
    text = (
        f"📦 {family['title']}\n\n"
        f"وضعیت: {status}\n"
        f"دسته‌بندی: {category}\n"
        f"پلن‌ها: {len(plans)} پلن • {active_plans} فعال"
    )
    if family.get("description"):
        text += f"\n\n{family['description']}"
    return (
        text,
        [
            [Button("💳 پلن‌های فروش", plans_token, "primary")],
            [Button("➕ افزودن پلن", create_plan, "success")],
            [Button(toggle_label, toggle, "danger" if family["active"] else "success")],
            [Button("⬅️ محصولات من", await _ux_token(repo, actor_id, "store.products"))],
        ],
    )


async def plans_view(
    repo: ShopRepository, actor_id: int, family_id: UUID
) -> tuple[str, list[list[Button]]]:
    repo.owner(actor_id)
    store = VariantStore(repo)
    family = await store.family(family_id)
    if not family:
        raise InvalidState("PRODUCT_FAMILY_NOT_FOUND")
    plans = await store.family_variants(family_id, owner=True)
    rows: list[list[Button]] = []
    for plan in plans:
        token = await _ux_token(repo, actor_id, "store.plan", str(plan["id"]))
        prefix = "✅" if plan["active"] else "⏸"
        rows.append(
            [
                await _rich_button(
                    repo,
                    f"{prefix} {plan['title']}",
                    token,
                    "success" if plan["active"] else "default",
                    plan.get("button_emoji_key"),
                )
            ]
        )
    create = await store.issue_callback(
        "admin.variant.new", actor_id, str(family_id), one_time=True
    )
    rows.append([Button("➕ افزودن پلن جدید", create, "primary")])
    rows.append(
        [Button("⬅️ بازگشت به محصول", await _ux_token(repo, actor_id, "store.product", str(family_id)))]
    )
    text = f"💳 پلن‌های {family['title']}\n\n"
    text += f"{len(plans)} پلن ثبت شده" if plans else "هنوز پلنی برای این محصول نساخته‌اید."
    return text, rows


async def plan_view(
    repo: ShopRepository, actor_id: int, variant_id: UUID
) -> tuple[str, list[list[Button]]]:
    repo.owner(actor_id)
    store = VariantStore(repo)
    item = await store.variant_with_family(variant_id)
    if not item:
        raise InvalidState("VARIANT_NOT_FOUND")
    fields = await store.variant_fields(variant_id)
    offers = await store.offers(variant_id)
    try:
        price_text = f"{await store.estimate_price(variant_id):,} تومان"
    except InvalidState:
        price_text = "هنوز قابل محاسبه نیست"
    status = "فعال ✅" if item["active"] else "غیرفعال ⏸"
    payment = "کارت‌به‌کارت" if item["payment_method"] == "card_to_card" else item["payment_method"]
    text = (
        f"💳 {item['family_title']} • {item['title']}\n\n"
        f"وضعیت: {status}\n"
        f"💰 قیمت فروش: {price_text}\n"
        f"⚡ تحویل: {store.delivery_label(item)}\n"
        f"🛡 گارانتی: {store.warranty_label(item)}\n"
        f"💵 روش پرداخت: {payment}\n"
        f"🪪 احراز هویت: {'لازم' if item['requires_kyc'] else 'لازم نیست'}\n"
        f"💳 کارت تأییدشده: {'لازم' if item['requires_verified_source_card'] else 'لازم نیست'}\n"
        f"👤 اطلاعات مشتری: {len(fields)} مورد\n"
        f"🏪 تأمین‌کننده: {len(offers)} مورد"
    )
    fields_token = await _ux_token(repo, actor_id, "store.plan.fields", str(variant_id))
    offers_token = await _ux_token(repo, actor_id, "store.plan.offers", str(variant_id))
    toggle = await _ux_token(
        repo,
        actor_id,
        "store.plan.toggle",
        str(variant_id),
        one_time=True,
    )
    toggle_label = "⏸ غیرفعال کردن پلن" if item["active"] else "▶️ فعال کردن پلن"
    return (
        text,
        [
            [Button("👤 اطلاعات موردنیاز مشتری", fields_token, "primary")],
            [Button("🏪 تأمین‌کننده‌ها", offers_token)],
            [Button(toggle_label, toggle, "danger" if item["active"] else "success")],
            [
                Button(
                    "⬅️ پلن‌های محصول",
                    await _ux_token(repo, actor_id, "store.plans", str(item["family_id"])),
                )
            ],
        ],
    )


async def plan_fields_view(
    repo: ShopRepository, actor_id: int, variant_id: UUID
) -> tuple[str, list[list[Button]]]:
    repo.owner(actor_id)
    store = VariantStore(repo)
    item = await store.variant_with_family(variant_id)
    if not item:
        raise InvalidState("VARIANT_NOT_FOUND")
    fields = await store.variant_fields(variant_id)
    if fields:
        lines = []
        for field in fields:
            flags = []
            if field["required"]:
                flags.append("اجباری")
            if field["sensitive"]:
                flags.append("حساس")
            suffix = f" ({'، '.join(flags)})" if flags else ""
            lines.append(f"• {field['label']}{suffix}")
        body = "\n".join(lines)
    else:
        body = "برای این پلن اطلاعاتی از مشتری دریافت نمی‌شود."
    add = await store.issue_callback("admin.field.new", actor_id, str(variant_id), one_time=True)
    return (
        f"👤 اطلاعات موردنیاز مشتری\n\n{item['family_title']} • {item['title']}\n\n{body}",
        [
            [Button("➕ افزودن فیلد", add, "primary")],
            [Button("⬅️ بازگشت به پلن", await _ux_token(repo, actor_id, "store.plan", str(variant_id)))],
        ],
    )


async def plan_offers_view(
    repo: ShopRepository, actor_id: int, variant_id: UUID
) -> tuple[str, list[list[Button]]]:
    repo.owner(actor_id)
    store = VariantStore(repo)
    item = await store.variant_with_family(variant_id)
    if not item:
        raise InvalidState("VARIANT_NOT_FOUND")
    offers = await store.offers(variant_id)
    if offers:
        body = "\n".join(
            f"• {offer['supplier_name']} — {offer['cost_amount']} {offer['cost_currency']}"
            + (f" — {offer['marketplace']}" if offer.get("marketplace") else "")
            for offer in offers
        )
    else:
        body = "هنوز تأمین‌کننده‌ای برای این پلن ثبت نشده است."
    add = await store.issue_callback("admin.offer.new", actor_id, str(variant_id), one_time=True)
    return (
        f"🏪 تأمین‌کننده‌ها\n\n{item['family_title']} • {item['title']}\n\n{body}",
        [
            [Button("➕ افزودن تأمین‌کننده", add, "primary")],
            [Button("⬅️ بازگشت به پلن", await _ux_token(repo, actor_id, "store.plan", str(variant_id)))],
        ],
    )


async def _render_view(
    message: Message, repo: ShopRepository, view: tuple[str, list[list[Button]]]
) -> None:
    from . import runtime as runtime_module

    text, rows = view
    await runtime_module.answer_keyboard(message, text, rows)


def _repo_for_message(message: Message) -> ShopRepository | None:
    """Resolve only the repository bound to this exact enhanced bot instance."""
    try:
        from .enhanced import _BOT_REPOS

        bot = message.bot
    except (AttributeError, LookupError, RuntimeError):
        return None
    return _BOT_REPOS.get(id(bot))


async def _peek_variant_callback(repo: ShopRepository, callback_data: str | None) -> dict | None:
    if not callback_data or not callback_data.startswith("v1."):
        return None
    raw = await repo.coordinator.redis.get(f"vcallback:{callback_data[3:]}")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


async def _object_from_markup(repo: ShopRepository, markup: object, action: str) -> str | None:
    keyboard = getattr(markup, "inline_keyboard", None)
    if not keyboard:
        return None
    for row in keyboard:
        for button in row:
            state = await _peek_variant_callback(repo, getattr(button, "callback_data", None))
            if state and state.get("a") == action:
                return str(state.get("o") or "") or None
    return None


async def _rewrite_legacy_admin_message(
    repo: ShopRepository | None,
    text: object,
    kwargs: dict,
    actor_id: int | None = None,
) -> tuple[object, dict]:
    if repo is None or not isinstance(text, str):
        return text, kwargs

    resolved_actor = actor_id
    if resolved_actor is None:
        resolved_actor = getattr(repo, "owner_id", None)
    if resolved_actor is None:
        return text, kwargs
    actor = int(resolved_actor)

    view: tuple[str, list[list[Button]]] | None = None
    if text.startswith(LEGACY_ADMIN_PREFIX):
        view = await admin_home_view(repo, actor)
    elif text.startswith("محصولات و گزینه‌های خرید") or "ساختار جدید: محصول ← گزینه خرید" in text:
        view = await store_admin_home_view(repo, actor)
    elif "هر گزینه خرید قیمت، روش فعال‌سازی" in text:
        family_raw = await _object_from_markup(repo, kwargs.get("reply_markup"), "admin.variant.new")
        if family_raw:
            view = await product_view(repo, actor, UUID(family_raw))
    elif "اطلاعات مشتری:" in text and "تأمین‌کننده‌ها:" in text:
        variant_raw = await _object_from_markup(repo, kwargs.get("reply_markup"), "admin.field.new")
        if variant_raw:
            view = await plan_view(repo, actor, UUID(variant_raw))

    if view is None:
        return text, kwargs

    from . import runtime as runtime_module

    value, rows = view
    rewritten = dict(kwargs)
    rewritten["reply_markup"] = runtime_module.markup(rows)
    rewritten.pop("parse_mode", None)
    return value, rewritten


def _install_message_admin_bridge() -> None:
    global _MESSAGE_BRIDGE_INSTALLED
    if _MESSAGE_BRIDGE_INSTALLED:
        return

    async def bridged_answer(self: Message, text: str, *args, **kwargs):
        repo = _repo_for_message(self)
        source = getattr(self, "from_user", None)
        actor_id = None if getattr(source, "is_bot", False) else getattr(source, "id", None)
        text, kwargs = await _rewrite_legacy_admin_message(repo, text, kwargs, actor_id)
        return await _ORIGINAL_MESSAGE_ANSWER(self, text, *args, **kwargs)

    async def bridged_edit_text(self: Message, text: str, *args, **kwargs):
        repo = _repo_for_message(self)
        source = getattr(self, "from_user", None)
        actor_id = None if getattr(source, "is_bot", False) else getattr(source, "id", None)
        text, kwargs = await _rewrite_legacy_admin_message(repo, text, kwargs, actor_id)
        return await _ORIGINAL_MESSAGE_EDIT_TEXT(self, text, *args, **kwargs)

    Message.answer = bridged_answer
    Message.edit_text = bridged_edit_text
    _MESSAGE_BRIDGE_INSTALLED = True


async def render_admin_home(message: Message, repo: ShopRepository, actor_id: int) -> None:
    await _render_view(message, repo, await admin_home_view(repo, actor_id))


async def render_admin_section(
    message: Message, repo: ShopRepository, actor_id: int, section: str
) -> None:
    from . import runtime as runtime_module

    repo.owner(actor_id)
    definition = ADMIN_SECTIONS.get(section)
    if not definition:
        raise AccessDenied("ADMIN_SECTION_INVALID")
    title, description, items = definition
    rows: list[list[Button]] = []
    for label, action, style in items:
        if action == "admin.product":
            token = await _ux_token(repo, actor_id, "store.home")
        else:
            token = await _legacy_token(repo, actor_id, action)
        rows.append([Button(label, token, style)])
    rows.append([Button("بازگشت به پنل مدیریت", await _home_token(repo, actor_id))])
    await runtime_module.answer_keyboard(message, f"{title}\n\n{description}", rows)


def build_admin_menu_router(repo: ShopRepository) -> Router:
    _install_message_admin_bridge()
    router = Router(name="admin-navigation")

    @router.message(Command("admin"))
    @router.message(Command("setup"))
    async def admin_home(message: Message) -> None:
        if not message.from_user:
            return
        try:
            await render_admin_home(message, repo, message.from_user.id)
        except AccessDenied:
            await message.answer("دسترسی مدیر لازم است.")

    @router.callback_query(F.data.startswith("m:"))
    async def admin_menu_callback(query: CallbackQuery) -> None:
        if not query.from_user or not query.data or not query.message:
            return
        try:
            state = await repo.coordinator.resolve_callback(query.data[2:], query.from_user.id)
            if state["a"] == "admin.menu.home":
                await render_admin_home(query.message, repo, query.from_user.id)
            elif state["a"] == "admin.menu.section":
                await render_admin_section(
                    query.message, repo, query.from_user.id, str(state.get("o", ""))
                )
            else:
                raise AccessDenied("ADMIN_MENU_ACTION_INVALID")
            await query.answer()
        except AccessDenied:
            await query.answer("این دکمه منقضی شده است.", show_alert=True)

    @router.callback_query(F.data.startswith("u1."))
    async def catalog_admin_callback(query: CallbackQuery) -> None:
        if not query.from_user or not query.data or not query.message:
            return
        actor = query.from_user.id
        try:
            state = await _resolve_ux_token(repo, query.data, actor)
            action = state["a"]
            object_id = str(state.get("o") or "")
            store = VariantStore(repo)
            if action == "store.home":
                view = await store_admin_home_view(repo, actor)
            elif action == "store.products":
                view = await products_view(repo, actor)
            elif action == "store.product":
                view = await product_view(repo, actor, UUID(object_id))
            elif action == "store.plans":
                view = await plans_view(repo, actor, UUID(object_id))
            elif action == "store.plan":
                view = await plan_view(repo, actor, UUID(object_id))
            elif action == "store.plan.fields":
                view = await plan_fields_view(repo, actor, UUID(object_id))
            elif action == "store.plan.offers":
                view = await plan_offers_view(repo, actor, UUID(object_id))
            elif action == "store.product.toggle":
                family = await store.family(UUID(object_id))
                if not family:
                    raise InvalidState("PRODUCT_FAMILY_NOT_FOUND")
                await store.set_family_active(actor, UUID(object_id), not family["active"])
                view = await product_view(repo, actor, UUID(object_id))
            elif action == "store.plan.toggle":
                plan = await store.variant(UUID(object_id))
                if not plan:
                    raise InvalidState("VARIANT_NOT_FOUND")
                await store.set_variant_active(actor, UUID(object_id), not plan["active"])
                view = await plan_view(repo, actor, UUID(object_id))
            else:
                raise AccessDenied("ADMIN_UX_ACTION_INVALID")
            await _render_view(query.message, repo, view)
            await query.answer()
        except (AccessDenied, InvalidState, ValueError):
            await query.answer("این دکمه منقضی شده یا اطلاعات قابل نمایش نیست.", show_alert=True)

    return router
