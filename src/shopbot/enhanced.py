from __future__ import annotations

import contextlib
import types
from uuid import UUID

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest

from . import runtime as runtime_module
from .rich_text import render_rich_text
from .telegram_adapter import Button
from .variant_store import VariantStore

_BOT_REPOS: dict[int, object] = {}
_TRANSPORT_PATCHED = False


def _button_feature_rejected(exc: TelegramBadRequest) -> bool:
    detail = str(exc).lower()
    return (
        "style" in detail
        or "icon_custom_emoji_id" in detail
        or "custom emoji" in detail
        or "tg-emoji" in detail
    )


def _install_transport_patch() -> None:
    global _TRANSPORT_PATCHED
    if _TRANSPORT_PATCHED:
        return

    original_send_message = Bot.send_message
    original_send_photo = Bot.send_photo
    original_send_document = Bot.send_document

    async def send_message(self, chat_id, text, *args, **kwargs):
        repo = _BOT_REPOS.get(id(self))
        generic_delivery = "یک رویداد جدید فروشگاه ثبت شد.\n\n"
        if isinstance(text, str) and text.startswith(generic_delivery):
            text = "✅ سفارش شما آماده شد\n\n" + text[len(generic_delivery) :]
        if repo and isinstance(text, str) and "{emoji:" in text and "parse_mode" not in kwargs:
            rendered = await render_rich_text(text, repo.resolve_rich_emoji)
            try:
                return await original_send_message(
                    self,
                    chat_id,
                    rendered.html,
                    *args,
                    parse_mode="HTML",
                    **kwargs,
                )
            except TelegramBadRequest as exc:
                if not _button_feature_rejected(exc):
                    raise
                return await original_send_message(
                    self, chat_id, rendered.fallback, *args, **kwargs
                )
        return await original_send_message(self, chat_id, text, *args, **kwargs)

    async def _media_call(original, self, chat_id, media, *args, **kwargs):
        repo = _BOT_REPOS.get(id(self))
        caption = kwargs.get("caption")
        if (
            repo
            and isinstance(caption, str)
            and "{emoji:" in caption
            and "parse_mode" not in kwargs
        ):
            rendered = await render_rich_text(caption, repo.resolve_rich_emoji)
            rich_kwargs = dict(kwargs)
            rich_kwargs["caption"] = rendered.html
            rich_kwargs["parse_mode"] = "HTML"
            try:
                return await original(self, chat_id, media, *args, **rich_kwargs)
            except TelegramBadRequest as exc:
                if not _button_feature_rejected(exc):
                    raise
                fallback_kwargs = dict(kwargs)
                fallback_kwargs["caption"] = rendered.fallback
                return await original(self, chat_id, media, *args, **fallback_kwargs)
        return await original(self, chat_id, media, *args, **kwargs)

    async def send_photo(self, chat_id, photo, *args, **kwargs):
        return await _media_call(
            original_send_photo, self, chat_id, photo, *args, **kwargs
        )

    async def send_document(self, chat_id, document, *args, **kwargs):
        return await _media_call(
            original_send_document, self, chat_id, document, *args, **kwargs
        )

    Bot.send_message = send_message
    Bot.send_photo = send_photo
    Bot.send_document = send_document
    _TRANSPORT_PATCHED = True


def create_app(settings):
    app = runtime_module.create_app(settings)
    runtime = app.state.runtime
    repo = runtime.repo
    store = VariantStore(repo)

    from .delivery_router import DeliveryFlow

    delivery_flow = DeliveryFlow(repo, store)
    legacy_issue_callback = repo.coordinator.issue_callback

    async def variant_legacy_issue_callback(
        action: str,
        actor_id: int,
        object_id: str = "",
        version: int = 1,
        *,
        one_time: bool = False,
        ttl: int = 1800,
    ) -> str:
        # Variant checkout should open the card center first. This prevents a
        # second registration flow when the customer already has a pending or
        # verified card, while the legacy storefront keeps its original behavior.
        if action == "begin_card":
            return await legacy_issue_callback(
                "customer.cards",
                actor_id,
                object_id,
                version,
                one_time=one_time,
                ttl=ttl,
            )
        if action == "admin.order.deliver" and object_id:
            with contextlib.suppress(ValueError):
                if await store.order_context(UUID(object_id)):
                    return await delivery_flow.issue(
                        "start", actor_id, object_id, ttl=ttl
                    )
        return await legacy_issue_callback(
            action,
            actor_id,
            object_id,
            version,
            one_time=one_time,
            ttl=ttl,
        )

    setattr(repo, "_legacy_issue_callback", variant_legacy_issue_callback)
    setattr(repo, "variant_store", store)

    original_mark_waiting_gate = store.mark_waiting_gate

    async def bridged_mark_waiting_gate(self, checkout_id: UUID) -> None:
        await original_mark_waiting_gate(checkout_id)
        checkout = await self.checkout(checkout_id)
        if checkout and checkout.get("telegram_id") is not None:
            actor_id = int(checkout["telegram_id"])
            await repo.coordinator.redis.set(
                f"pending-variant-checkout:{actor_id}",
                str(checkout_id),
                ex=86400,
            )

    store.mark_waiting_gate = types.MethodType(bridged_mark_waiting_gate, store)

    original_create_quote = store.create_quote

    async def bridged_create_quote(
        self,
        checkout_id: UUID,
        telegram_id: int,
        card_id: UUID | None,
    ):
        quote = await original_create_quote(checkout_id, telegram_id, card_id)
        await repo.coordinator.redis.delete(
            f"pending-variant-checkout:{telegram_id}",
            f"pending-checkout:{telegram_id}",
        )
        return quote

    store.create_quote = types.MethodType(bridged_create_quote, store)

    async def routed_issue_callback(
        self,
        action: str,
        actor_id: int,
        object_id: str = "",
        version: int = 1,
        *,
        one_time: bool = False,
        ttl: int = 1800,
    ) -> str:
        if action == "catalog":
            return await store.issue_callback(
                "catalog", actor_id, object_id, one_time=one_time, ttl=ttl
            )
        if action == "my_orders":
            return await store.issue_callback(
                "orders", actor_id, object_id, one_time=one_time, ttl=ttl
            )
        if action == "admin.product":
            return await store.issue_callback(
                "admin.home", actor_id, object_id, one_time=one_time, ttl=ttl
            )
        if action == "admin.orders":
            return await store.issue_callback(
                "admin.orders", actor_id, object_id, one_time=one_time, ttl=ttl
            )
        if action == "admin.delivery.confirm":
            return await store.issue_callback(
                "admin.delivery.confirm",
                actor_id,
                object_id,
                one_time=one_time,
                ttl=ttl,
            )
        if action == "resume_checkout" and object_id:
            pending_variant = await repo.coordinator.redis.get(
                f"pending-variant-checkout:{actor_id}"
            )
            if pending_variant:
                with contextlib.suppress(ValueError):
                    checkout_id = UUID(pending_variant)
                    checkout = await store.checkout(checkout_id, actor_id)
                    if checkout and checkout["status"] in {
                        "INPUT",
                        "READY",
                        "WAITING_GATE",
                        "QUOTED",
                    }:
                        return await store.issue_callback(
                            "resume",
                            actor_id,
                            str(checkout_id),
                            one_time=one_time,
                            ttl=ttl,
                        )
            with contextlib.suppress(ValueError):
                pending = await store.pending_for_legacy_product(
                    actor_id, UUID(object_id)
                )
                if pending:
                    return await store.issue_callback(
                        "resume",
                        actor_id,
                        str(pending["id"]),
                        one_time=one_time,
                        ttl=ttl,
                    )
        return await legacy_issue_callback(
            action,
            actor_id,
            object_id,
            version,
            one_time=one_time,
            ttl=ttl,
        )

    repo.coordinator.issue_callback = types.MethodType(
        routed_issue_callback, repo.coordinator
    )

    async def rich_answer_keyboard(message, text_value: str, rows: list[list[Button]]):
        rendered = await render_rich_text(text_value, repo.resolve_rich_emoji)

        async def send(reply_markup, *, rich: bool):
            value = rendered.html if rich else rendered.fallback
            kwargs = {"reply_markup": reply_markup}
            if rich:
                kwargs["parse_mode"] = "HTML"
            if getattr(getattr(message, "from_user", None), "is_bot", False):
                return await message.edit_text(value, **kwargs)
            return await message.answer(value, **kwargs)

        try:
            return await send(runtime_module.markup(rows), rich=True)
        except TelegramBadRequest as exc:
            if not _button_feature_rejected(exc):
                raise
            plain_rows = [
                [Button(button.text, button.callback_data) for button in row]
                for row in rows
            ]
            return await send(runtime_module.markup(plain_rows), rich=False)

    runtime_module.answer_keyboard = rich_answer_keyboard

    from .admin_menu import build_admin_menu_router
    from .variant_router import build_variant_router

    dispatcher = Dispatcher()
    dispatcher.include_router(delivery_flow.router)
    dispatcher.include_router(build_admin_menu_router(repo))
    dispatcher.include_router(build_variant_router(repo, store))
    dispatcher.include_router(runtime_module.persistent_router(repo))
    runtime.dispatcher = dispatcher

    _install_transport_patch()
    _BOT_REPOS[id(runtime.bot)] = repo
    return app