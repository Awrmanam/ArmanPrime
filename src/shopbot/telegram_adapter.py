from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import httpx

ButtonStyle = Literal["primary", "success", "danger", "default"]


@dataclass(frozen=True)
class Button:
    text: str
    callback_data: str
    style: ButtonStyle = "default"
    icon_custom_emoji_id: str | None = None

    def payload(self) -> dict[str, str]:
        result = {"text": self.text, "callback_data": self.callback_data}
        if self.style != "default":
            result["style"] = self.style
        if self.icon_custom_emoji_id:
            result["icon_custom_emoji_id"] = self.icon_custom_emoji_id
        return result


def utf16_offset(text: str, character_index: int) -> int:
    return len(text[:character_index].encode("utf-16-le")) // 2


def extract_custom_emoji(entities: list[dict]) -> list[str]:
    return [
        entity["custom_emoji_id"]
        for entity in entities
        if entity.get("type") == "custom_emoji" and entity.get("custom_emoji_id")
    ]


class TelegramAPI:
    """Small raw adapter for Bot API fields not yet exposed by aiogram."""

    def __init__(self, token: str, client: httpx.AsyncClient | None = None):
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.client = client or httpx.AsyncClient(timeout=15)

    async def send_message(self, chat_id: int, text: str, rows: list[list[Button]]) -> dict:
        keyboard = [[button.payload() for button in row] for row in rows]
        response = await self.client.post(
            f"{self.base_url}/sendMessage",
            json={"chat_id": chat_id, "text": text, "reply_markup": {"inline_keyboard": keyboard}},
        )
        response.raise_for_status()
        return response.json()
