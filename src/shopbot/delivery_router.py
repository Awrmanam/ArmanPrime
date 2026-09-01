from __future__ import annotations

import contextlib
import json
import secrets
from uuid import UUID

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from .db import OrderRow
from .repository import AccessDenied, InvalidState, ShopRepository
from .rich_text import render_rich_text
from .telegram_adapter import Button
from .variant_store import ACTIVATION_LABELS, VariantStore

_CALLBACK_PREFIX = "d1."
_STATE_PREFIX = "delivery2:"
_DRAFT_PREFIX = "delivery-draft-v2:"


def _markup(rows: list[list[Button]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup.model_validate(
        {"inline_keyboard": [[button.payload() for button in row] for row in rows]}
    )


async def _send(
    repo: ShopRepository,
    message: Message,
    text_value: str,
    rows: list[list[Button]] | None = None,
) -> Message:
    rendered = await render_rich_text(text_value, repo.resolve_rich_emoji)
    keyboard = _markup(rows) if rows else None
    try:
        return await message.answer(rendered.html, parse_mode="HTML", reply_markup=keyboard)
    except TelegramBadRequest as exc:
        detail = str(exc).lower()
        if "custom emoji" not in detail and "tg-emoji" not in detail and "style" not in detail:
            raise
        plain_rows = None
        if rows:
            plain_rows = [
                [Button(button.text, button.callback_data) for button in row]
                for row in rows
            ]
        return await message.answer(
            rendered.fallback,
            reply_markup=_markup(plain_rows) if plain_rows else None,
        )


def _title(context: dict) -> str:
    return f"{context['family_title']} — {context['variant_title']}"


def _warranty(context: dict) -> str:
    return context.get("warranty_text") or "طبق شرایط محصول"


def build_delivery_payload(
    context: dict,
    data: dict,
    *,
    preview: bool = False,
) -> tuple[str, str | None]:
    """Build the customer-facing delivery body from the variant fulfillment type."""

    title = _title(context)
    warranty = _warranty(context)
    kind = context.get("fulfillment_type") or "custom"
    note = (data.get("note") or "").strip()
    extra = f"\n\nتوضیحات:\n{note}" if note else ""

    if kind == "activation_code":
        code = data.get("code", "")
        return (
            f"{title}\n\n🎁 کد فعال‌سازی:\n{code}\n\n🛡 گارانتی: {warranty}{extra}",
            None,
        )
    if kind == "activation_link":
        return (
            f"{title}\n\n🔗 لینک فعال‌سازی:\n\n🛡 گارانتی: {warranty}{extra}",
            data.get("link") or None,
        )
    if kind in {"account_no_login", "payment_link", "account_login"}:
        return (
            f"{title}\n\n✅ فعال‌سازی روی حساب شما با موفقیت انجام شد."
            f"\n\n🛡 گارانتی: {warranty}{extra}",
            None,
        )
    if kind == "account_credentials":
        identifier = data.get("identifier", "")
        password = "••••••••" if preview else data.get("password", "")
        return (
            f"{title}\n\n👤 اطلاعات ورود:\n"
            f"ایمیل / نام کاربری: {identifier}\n"
            f"رمز عبور: {password}\n\n"
            f"🛡 گارانتی: {warranty}{extra}",
            None,
        )

    content = (data.get("content") or "").strip()
    return (
        f"{title}\n\n{content}\n\n🛡 گارانتی: {warranty}{extra}",
        data.get("link") or None,
    )


class DeliveryFlow:
    """Variant-aware manual fulfillment with actor-bound, one-time callbacks."""

    def __init__(self, repo: ShopRepository, store: VariantStore):
        self.repo = repo
        self.store = store
        self.router = Router(name="variant-delivery")
        self._install_handlers()

    def _draft_key(self, actor: int, order_id: UUID) -> str:
        return f"{_DRAFT_PREFIX}{actor}:{order_id}"

    async def issue(
        self,
        action: str,
        actor: int,
        object_id: str,
        *,
        ttl: int = 1800,
    ) -> str:
        token = secrets.token_urlsafe(12)
        payload = json.dumps(
            {"a": action, "u": actor, "o": object_id},
            separators=(",", ":"),
        )
        await self.repo.coordinator.redis.set(f"delivery-callback:{token}", payload, ex=ttl)
        return f"{_CALLBACK_PREFIX}{token}"

    async def _resolve(self, callback_data: str, actor: int) -> dict:
        if not callback_data.startswith(_CALLBACK_PREFIX):
            raise AccessDenied("DELIVERY_CALLBACK_INVALID")
        token = callback_data[len(_CALLBACK_PREFIX) :]
        raw = await self.repo.coordinator.redis.getdel(f"delivery-callback:{token}")
        if not raw:
            raise AccessDenied("DELIVERY_CALLBACK_EXPIRED")
        state = json.loads(raw)
        if int(state.get("u", -1)) != actor:
            raise AccessDenied("DELIVERY_CALLBACK_ACTOR_MISMATCH")
        return state

    async def _load_draft(self, actor: int, order_id: UUID) -> dict:
        raw = await self.repo.coordinator.redis.get(self._draft_key(actor, order_id))
        if not raw:
            return {}
        return json.loads(self.repo.vault.decrypt(raw))

    async def _save_draft(self, actor: int, order_id: UUID, data: dict) -> None:
        encrypted = self.repo.vault.encrypt(json.dumps(data, ensure_ascii=False))
        await self.repo.coordinator.redis.set(
            self._draft_key(actor, order_id), encrypted, ex=1800
        )

    async def _clear(self, actor: int, order_id: UUID) -> None:
        await self.repo.coordinator.redis.delete(
            self._draft_key(actor, order_id),
            f"fsm:{actor}",
        )

    async def _assert_claim(self, actor: int, order_id: UUID) -> None:
        self.repo.owner(actor)
        async with self.repo.sessions() as session:
            order = await session.get(OrderRow, order_id)
            if (
                not order
                or order.status != "PROCESSING"
                or order.assigned_admin_id != actor
            ):
                raise AccessDenied("CLAIMING_ADMIN_REQUIRED")

    async def _context(self, actor: int, order_id: UUID) -> dict:
        await self._assert_claim(actor, order_id)
        context = await self.store.order_context(order_id)
        if not context:
            raise InvalidState("VARIANT_ORDER_NOT_FOUND")
        return context

    async def _set_state(self, actor: int, order_id: UUID, step: str) -> None:
        await self.repo.coordinator.redis.set(
            f"fsm:{actor}", f"{_STATE_PREFIX}{order_id}:{step}", ex=1800
        )

    async def _show_start(self, message: Message, actor: int, order_id: UUID) -> None:
        context = await self._context(actor, order_id)
        kind = context.get("fulfillment_type") or "custom"
        method = ACTIVATION_LABELS.get(kind, context.get("activation_method") or "سفارشی")
        cancel = await self.issue("cancel", actor, str(order_id))
        await self._save_draft(actor, order_id, {})

        if kind == "activation_code":
            await self._set_state(actor, order_id, "code")
            prompt = "کد فعال‌سازی را ارسال کنید."
        elif kind == "activation_link":
            await self._set_state(actor, order_id, "link")
            prompt = "لینک فعال‌سازی / Gift را ارسال کنید."
        elif kind in {"account_no_login", "payment_link", "account_login"}:
            complete = await self.issue("complete", actor, str(order_id))
            note = await self.issue("note", actor, str(order_id))
            await _send(
                self.repo,
                message,
                f"ثبت تحویل\n\n{_title(context)}\nروش انجام: {method}\n\n"
                "اگر فعال‌سازی انجام شده، می‌توانید مستقیم تحویل را ثبت کنید یا برای مشتری توضیح اضافه کنید.",
                [
                    [Button("فعال‌سازی انجام شد", complete, "success")],
                    [Button("افزودن توضیح", note, "primary")],
                    [Button("لغو", cancel, "danger")],
                ],
            )
            return
        elif kind == "account_credentials":
            await self._set_state(actor, order_id, "identifier")
            prompt = "ایمیل / نام کاربری اکانت آماده را ارسال کنید."
        else:
            await self._set_state(actor, order_id, "content")
            prompt = "متن تحویل را ارسال کنید."

        await _send(
            self.repo,
            message,
            f"ثبت تحویل\n\n{_title(context)}\nروش انجام: {method}\n\n{prompt}",
            [[Button("لغو", cancel, "danger")]],
        )

    async def _preview(self, message: Message, actor: int, order_id: UUID) -> None:
        context = await self._context(actor, order_id)
        data = await self._load_draft(actor, order_id)
        body, link = build_delivery_payload(context, data, preview=True)
        if link:
            body += f"\n{link}"
        confirm = await self.issue("confirm", actor, str(order_id), ttl=1800)
        restart = await self.issue("restart", actor, str(order_id), ttl=1800)
        cancel = await self.issue("cancel", actor, str(order_id), ttl=1800)
        await self.repo.coordinator.redis.delete(f"fsm:{actor}")
        await _send(
            self.repo,
            message,
            "پیش‌نمایش تحویل برای مشتری\n\n" + body,
            [
                [Button("تأیید و ارسال", confirm, "success")],
                [Button("ویرایش / شروع دوباره", restart, "primary")],
                [Button("لغو", cancel, "danger")],
            ],
        )

    def _install_handlers(self) -> None:
        @self.router.callback_query(F.data.startswith(_CALLBACK_PREFIX))
        async def delivery_callback(query: CallbackQuery) -> None:
            try:
                state = await self._resolve(query.data, query.from_user.id)
                actor = query.from_user.id
                order_id = UUID(state["o"])
                action = state["a"]

                if action in {"start", "restart"}:
                    await self._show_start(query.message, actor, order_id)
                elif action == "note":
                    await self._context(actor, order_id)
                    await self._set_state(actor, order_id, "note")
                    await _send(
                        self.repo,
                        query.message,
                        "توضیحی که باید همراه تحویل برای مشتری نمایش داده شود را ارسال کنید.\n"
                        "برای بدون توضیح «-» بفرستید.",
                    )
                elif action == "complete":
                    await self._context(actor, order_id)
                    await self._save_draft(actor, order_id, {})
                    await self._preview(query.message, actor, order_id)
                elif action == "custom_link":
                    await self._context(actor, order_id)
                    await self._set_state(actor, order_id, "custom_link")
                    await _send(self.repo, query.message, "لینک تحویل را ارسال کنید.")
                elif action == "preview":
                    await self._preview(query.message, actor, order_id)
                elif action == "confirm":
                    context = await self._context(actor, order_id)
                    data = await self._load_draft(actor, order_id)
                    content, activation_link = build_delivery_payload(context, data)
                    await self.repo.deliver(actor, order_id, content, activation_link)
                    await self.store.purge_sensitive(order_id)
                    await self._clear(actor, order_id)
                    await _send(
                        self.repo,
                        query.message,
                        "✅ تحویل ثبت شد و برای مشتری در صف ارسال قرار گرفت.\n"
                        "اطلاعات حساس ورودی سفارش نیز پاک شد.",
                    )
                elif action == "cancel":
                    await self._clear(actor, order_id)
                    await _send(
                        self.repo,
                        query.message,
                        "فرایند تحویل لغو شد؛ سفارش همچنان در حال انجام است.",
                    )
                else:
                    raise AccessDenied("DELIVERY_CALLBACK_ACTION_INVALID")
                await query.answer()
            except (AccessDenied, InvalidState, ValueError) as exc:
                with contextlib.suppress(Exception):
                    await query.answer(f"درخواست قابل انجام نیست: {exc}", show_alert=True)

        @self.router.message(F.text)
        async def delivery_text(message: Message) -> None:
            state = await self.repo.coordinator.redis.get(f"fsm:{message.from_user.id}")
            if not state or not state.startswith(_STATE_PREFIX):
                raise SkipHandler

            actor = message.from_user.id
            try:
                _, order_raw, step = state.split(":", 2)
                order_id = UUID(order_raw)
                context = await self._context(actor, order_id)
                data = await self._load_draft(actor, order_id)
                value = message.text.strip()
                if not value:
                    raise InvalidState("DELIVERY_VALUE_REQUIRED")

                if step == "code":
                    data["code"] = value
                    await self._save_draft(actor, order_id, data)
                    await self._preview(message, actor, order_id)
                elif step == "link":
                    if not value.startswith(("https://", "http://")):
                        raise InvalidState("DELIVERY_LINK_INVALID")
                    data["link"] = value
                    await self._save_draft(actor, order_id, data)
                    await self._preview(message, actor, order_id)
                elif step == "identifier":
                    data["identifier"] = value
                    await self._save_draft(actor, order_id, data)
                    await self._set_state(actor, order_id, "password")
                    await _send(
                        self.repo,
                        message,
                        "رمز عبور اکانت آماده را ارسال کنید.\n"
                        "پیام شما بعد از پردازش حذف می‌شود.",
                    )
                elif step == "password":
                    data["password"] = value
                    await self._save_draft(actor, order_id, data)
                    with contextlib.suppress(Exception):
                        await message.delete()
                    await self._preview(message, actor, order_id)
                elif step == "note":
                    data["note"] = "" if value == "-" else value
                    await self._save_draft(actor, order_id, data)
                    await self._preview(message, actor, order_id)
                elif step == "content":
                    data["content"] = value
                    await self._save_draft(actor, order_id, data)
                    add_link = await self.issue("custom_link", actor, str(order_id))
                    preview = await self.issue("preview", actor, str(order_id))
                    await self.repo.coordinator.redis.delete(f"fsm:{actor}")
                    await _send(
                        self.repo,
                        message,
                        "آیا همراه این تحویل لینک هم لازم است؟",
                        [
                            [Button("افزودن لینک", add_link, "primary")],
                            [Button("بدون لینک", preview, "success")],
                        ],
                    )
                elif step == "custom_link":
                    if not value.startswith(("https://", "http://")):
                        raise InvalidState("DELIVERY_LINK_INVALID")
                    data["link"] = value
                    await self._save_draft(actor, order_id, data)
                    await self._preview(message, actor, order_id)
                else:
                    raise InvalidState("DELIVERY_STEP_INVALID")
            except (AccessDenied, InvalidState, ValueError):
                await message.answer(
                    "مقدار معتبر نیست؛ همان اطلاعات خواسته‌شده را دوباره ارسال کنید."
                )
