import pytest

from shopbot.rich_text import render_rich_text


@pytest.mark.asyncio
async def test_rich_text_resolves_multiple_registry_emojis_and_escapes_text():
    values = {"shield": ("123456", "🛡️"), "shop": ("987654", "🛍️")}

    async def resolve(key):
        return values.get(key)

    rendered = await render_rich_text("<کاربر> {emoji:shield} فروشگاه {emoji:shop}", resolve)
    assert "&lt;کاربر&gt;" in rendered.html
    assert rendered.html.count("<tg-emoji") == 2
    assert 'emoji-id="123456"' in rendered.html
    assert rendered.fallback == "<کاربر> 🛡️ فروشگاه 🛍️"


@pytest.mark.asyncio
async def test_unknown_or_inactive_rich_emoji_renders_clean_text():
    async def missing(_key):
        return None

    rendered = await render_rich_text("شروع {emoji:missing} پایان", missing)
    assert rendered.html == "شروع  پایان"
    assert rendered.fallback == "شروع  پایان"
    assert "missing" not in rendered.html
