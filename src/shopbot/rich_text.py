from __future__ import annotations

import html
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

PLACEHOLDER = re.compile(r"\{emoji:([A-Za-z0-9_.-]{1,64})\}")


@dataclass(frozen=True)
class RichText:
    html: str
    fallback: str


async def render_rich_text(
    value: str,
    resolver: Callable[[str], Awaitable[tuple[str, str] | None]],
) -> RichText:
    rich_parts: list[str] = []
    fallback_parts: list[str] = []
    cursor = 0
    for match in PLACEHOLDER.finditer(value):
        plain = value[cursor : match.start()]
        rich_parts.append(html.escape(plain))
        fallback_parts.append(plain)
        resolved = await resolver(match.group(1))
        if resolved:
            custom_id, fallback = resolved
            rich_parts.append(
                f'<tg-emoji emoji-id="{html.escape(custom_id, quote=True)}">'
                f"{html.escape(fallback)}</tg-emoji>"
            )
            fallback_parts.append(fallback)
        cursor = match.end()
    tail = value[cursor:]
    rich_parts.append(html.escape(tail))
    fallback_parts.append(tail)
    return RichText("".join(rich_parts), "".join(fallback_parts))
