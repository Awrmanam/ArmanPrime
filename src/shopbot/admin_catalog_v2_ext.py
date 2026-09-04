from __future__ import annotations

from uuid import UUID

from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from .admin_catalog_v2 import CatalogAdminV2
from .repository import InvalidState, ShopRepository
from .telegram_adapter import Button
from .variant_store import VariantStore, checkout_values


class CatalogAdminV2Extended(CatalogAdminV2):
    async def home(self, message: Message, actor: int) -> None:
        self._owner(actor)
        from .admin_menu import _home_token

        await self._render(
            message,
            "🛍 مدیریت فروشگاه\n\nمحصولات و پلن‌های فروش را از اینجا مدیریت کنید.",
            [
                [Button("📦 محصولات من", await self._token(actor, "products"), "primary")],
                [Button("➕ افزودن محصول جدید", await self._token(actor, "product.new"), "success")],
                [
                    Button(
                        "🧾 سفارش‌ها",
                        await self.repo.coordinator.issue_callback("admin.orders", actor),
                    )
                ],
                [Button("⬅️ بازگشت به پنل مدیریت", await _home_token(self.repo, actor))],
            ],
        )

    async def _callback(self, query: CallbackQuery, state: dict) -> None:
        actor = query.from_user.id
        action = state["a"]
        obj = str(state.get("o") or "")

        if action == "product.new.description.skip":
            data = await self._draft(actor)
            if data.get("kind") != "product":
                raise InvalidState("PRODUCT_DRAFT_EXPIRED")
            family_id = await self.store.create_family(
                actor,
                UUID(data["category_id"]),
                data["title"],
                "",
                button_emoji_key=None,
            )
            await self._clear_draft(actor)
            await self.product_page(query.message, actor, family_id)
            return

        if action == "field.custom.type":
            data = await self._draft(actor)
            data["field_type"] = obj
            await self._save_draft(actor, data)
            await self._render(
                query.message,
                "پر کردن این فیلد برای مشتری اجباری باشد؟",
                [
                    [
                        Button(
                            "بله، اجباری",
                            await self._token(actor, "field.custom.required", "1", once=True),
                            "primary",
                        )
                    ],
                    [
                        Button(
                            "خیر، اختیاری",
                            await self._token(actor, "field.custom.required", "0", once=True),
                        )
                    ],
                ],
            )
            return

        if action == "field.custom.required":
            data = await self._draft(actor)
            data["required"] = obj == "1"
            await self._save_draft(actor, data)
            await self._render(
                query.message,
                "این اطلاعات حساس است؟\nمثل رمز، Session و لینک خصوصی باید حساس باشد.",
                [
                    [
                        Button(
                            "بله، حساس",
                            await self._token(actor, "field.custom.sensitive", "1", once=True),
                            "danger",
                        )
                    ],
                    [
                        Button(
                            "خیر، عادی",
                            await self._token(actor, "field.custom.sensitive", "0", once=True),
                        )
                    ],
                ],
            )
            return

        if action == "field.custom.sensitive":
            data = await self._draft(actor)
            data["sensitive"] = obj == "1"
            data["delete_after_fulfillment"] = data["sensitive"]
            variant_id = UUID(data["variant_id"])
            await self.store.add_field(actor, variant_id, data)
            await self._clear_draft(actor)
            await self._plan_fields(query.message, actor, variant_id)
            return

        if action == "plan.edit.delivery.unit":
            variant_raw, unit = obj.split(":", 1)
            data = await self._draft(actor)
            low = int(data["delivery_min"])
            high = int(data["delivery_max"])
            multiplier = {"minute": 1, "hour": 60, "day": 1440}[unit]
            await self._update_plan(
                actor,
                UUID(variant_raw),
                {
                    "delivery_type": "range",
                    "delivery_min": low,
                    "delivery_max": high,
                    "delivery_unit": unit,
                    "delivery_text": None,
                },
                {"delivery_minutes": high * multiplier},
            )
            await self._clear_draft(actor)
            await self._plan_delivery(query.message, actor, UUID(variant_raw))
            return

        if action == "field.delete":
            field_id = UUID(obj)
            async with self.repo.sessions() as session:
                used = await session.scalar(
                    select(func.count())
                    .select_from(checkout_values)
                    .where(checkout_values.c.field_id == field_id)
                )
            if int(used or 0):
                raise InvalidState("FIELD_HAS_HISTORY")

        await super()._callback(query, state)

    async def _message(self, message: Message, actor: int, state: str, value: str) -> None:
        if state == "plan.edit.delivery_min":
            data = await self._draft(actor)
            amount = int(value)
            if amount < 0:
                raise InvalidState("INVALID_DELIVERY_RANGE")
            data["delivery_min"] = amount
            await self._save_draft(actor, data)
            await self._set_fsm(actor, "plan.edit.delivery_max")
            await self._render(message, "حداکثر زمان تحویل را به‌صورت عدد وارد کنید.", [])
            return

        if state == "plan.edit.delivery_max":
            data = await self._draft(actor)
            amount = int(value)
            if amount < int(data.get("delivery_min", 0)):
                raise InvalidState("INVALID_DELIVERY_RANGE")
            data["delivery_max"] = amount
            await self._save_draft(actor, data)
            variant_id = str(data["variant_id"])
            await self._render(
                message,
                "واحد زمان را انتخاب کنید.",
                [
                    [
                        Button(
                            "دقیقه",
                            await self._token(
                                actor,
                                "plan.edit.delivery.unit",
                                f"{variant_id}:minute",
                                once=True,
                            ),
                        )
                    ],
                    [
                        Button(
                            "ساعت",
                            await self._token(
                                actor,
                                "plan.edit.delivery.unit",
                                f"{variant_id}:hour",
                                once=True,
                            ),
                        )
                    ],
                    [
                        Button(
                            "روز",
                            await self._token(
                                actor,
                                "plan.edit.delivery.unit",
                                f"{variant_id}:day",
                                once=True,
                            ),
                        )
                    ],
                ],
            )
            return

        await super()._message(message, actor, state, value)


def build_admin_catalog_v2_router(repo: ShopRepository, store: VariantStore):
    return CatalogAdminV2Extended(repo, store).router
