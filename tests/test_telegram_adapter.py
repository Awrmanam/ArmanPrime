import httpx
import pytest

from shopbot.telegram_adapter import Button, TelegramAPI, extract_custom_emoji, utf16_offset


def test_styles_custom_emoji_and_utf16():
    for style in ("primary", "success", "danger"):
        payload = Button("Buy", "x", style, "custom-id").payload()
        assert payload["style"] == style
        assert payload["icon_custom_emoji_id"] == "custom-id"
    plain = Button("Plain", "x").payload()
    assert "icon_custom_emoji_id" not in plain and "style" not in plain
    assert utf16_offset("A\U0001f600B", 2) == 3
    assert utf16_offset("A\U0001f600B\U0001f642C", 4) == 6
    assert extract_custom_emoji(
        [{"type": "custom_emoji", "custom_emoji_id": "1"}, {"type": "bold"}]
    ) == ["1"]


@pytest.mark.asyncio
async def test_raw_adapter_payload():
    async def handler(request):
        body = __import__("json").loads(request.content)
        assert body["reply_markup"]["inline_keyboard"][0][0]["style"] == "primary"
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await TelegramAPI("secret", client).send_message(
        1, "text", [[Button("B", "c", "primary")]]
    )
    assert result["ok"]
    await client.aclose()
