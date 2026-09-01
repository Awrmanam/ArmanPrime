from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from .repository import AccessDenied, ShopRepository
from .telegram_adapter import Button


ADMIN_SECTIONS: dict[str, tuple[str, str, tuple[tuple[str, str, str], ...]]] = {
    "catalog": (
        "🛍 فروشگاه و محصولات",
        "مدیریت دسته‌بندی‌ها، محصولات و گزینه‌های خرید.",
        (
            ("دسته‌بندی‌ها", "admin.category", "default"),
            ("محصولات و گزینه‌های خرید", "admin.product", "primary"),
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
    "⚙️ پنل مدیریت\n\n"
    "بخش موردنظر را انتخاب کنید. تنظیمات مرتبط داخل همان بخش قرار دارند."
)
LEGACY_ADMIN_PREFIX = "پنل مدیریت\n\nوضعیت آمادگی فروشگاه:"


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


async def admin_home_view(
    repo: ShopRepository, actor_id: int
) -> tuple[str, list[list[Button]]]:
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


async def render_admin_home(message: Message, repo: ShopRepository, actor_id: int) -> None:
    from . import runtime as runtime_module

    text, rows = await admin_home_view(repo, actor_id)
    await runtime_module.answer_keyboard(message, text, rows)


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
        rows.append([Button(label, await _legacy_token(repo, actor_id, action), style)])
    rows.append([Button("بازگشت به پنل مدیریت", await _home_token(repo, actor_id))])
    await runtime_module.answer_keyboard(message, f"{title}\n\n{description}", rows)


def build_admin_menu_router(repo: ShopRepository) -> Router:
    router = Router(name="admin-navigation")

    @router.message(Command("admin"))
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

    return router
