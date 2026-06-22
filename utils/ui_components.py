"""Shared Discord Components V2 helpers for the multifunction bot.

Place this file at ``utils/ui_components.py``.
Requires discord.py >= 2.7.1.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Optional

import discord

WHITE = 0xFFFFFF
MAX_V2_TEXT = 4000


def clip(text: object, limit: int = 1000, *, fallback: str = "—") -> str:
    value = str(text).strip() if text is not None else ""
    if not value:
        return fallback
    if len(value) <= limit:
        return value
    return value[: max(1, limit - 1)].rstrip() + "…"


def fields_markdown(fields: Iterable[tuple[str, object]]) -> str:
    parts: list[str] = []
    for name, value in fields:
        parts.append(f"**{clip(name, 120)}**\n{clip(value, 900)}")
    return "\n\n".join(parts)


def _content(title: str, description: str = "", fields: Iterable[tuple[str, object]] = ()) -> str:
    chunks = [f"## {clip(title, 250)}"]
    if description:
        chunks.append(clip(description, 2500, fallback=""))
    field_text = fields_markdown(fields)
    if field_text:
        chunks.append(field_text)
    return clip("\n\n".join(c for c in chunks if c), 3600, fallback="")


def card(
    title: str,
    description: str = "",
    *,
    fields: Iterable[tuple[str, object]] = (),
    thumbnail: Optional[str] = None,
    image: Optional[str] = None,
    file: Optional[discord.File] = None,
    footer: Optional[str] = None,
    accent: int | discord.Colour = WHITE,
    rows: Sequence[discord.ui.ActionRow] = (),
    timeout: Optional[float] = 180,
) -> discord.ui.LayoutView:
    """Build a polished Components V2 card.

    The total displayed text is clipped below Discord's 4,000-character
    LayoutView limit. Interactive rows can be inserted inside the container.
    """
    view = discord.ui.LayoutView(timeout=timeout)
    container = discord.ui.Container(accent_color=accent)
    text = _content(title, description, fields)

    if thumbnail:
        container.add_item(
            discord.ui.Section(
                discord.ui.TextDisplay(text),
                accessory=discord.ui.Thumbnail(thumbnail),
            )
        )
    else:
        container.add_item(discord.ui.TextDisplay(text))

    if image:
        container.add_item(
            discord.ui.MediaGallery(
                discord.MediaGalleryItem(media=image, description=clip(title, 120))
            )
        )

    if file:
        container.add_item(discord.ui.File(file))

    for row in rows:
        container.add_item(row)

    if footer:
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(f"-# {clip(footer, 280)}"))

    view.add_item(container)
    if view.content_length() > MAX_V2_TEXT:
        raise ValueError("Components V2 card exceeds Discord's text limit")
    return view


def text_card(
    title: str,
    description: str = "",
    *,
    accent: int | discord.Colour = WHITE,
    footer: Optional[str] = None,
    timeout: Optional[float] = 180,
) -> discord.ui.LayoutView:
    return card(title, description, accent=accent, footer=footer, timeout=timeout)


def button_row(*buttons: discord.ui.Button) -> discord.ui.ActionRow:
    return discord.ui.ActionRow(*buttons)


def select_row(select: discord.ui.Select) -> discord.ui.ActionRow:
    return discord.ui.ActionRow(select)
