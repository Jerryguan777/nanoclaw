"""Message formatting and outbound routing — port of src/router.ts."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from nanoclaw.config import ASSISTANT_NAME

if TYPE_CHECKING:
    from nanoclaw.models import Channel, NewMessage


def escape_xml(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def format_messages(messages: list[NewMessage]) -> str:
    lines = [
        f'<message sender="{escape_xml(m.sender_name)}" time="{m.timestamp}">'
        f"{escape_xml(m.content)}</message>"
        for m in messages
    ]
    return f"<messages>\n" + "\n".join(lines) + "\n</messages>"


def strip_internal_tags(text: str) -> str:
    return re.sub(r"<internal>[\s\S]*?</internal>", "", text).strip()


def format_outbound(channel: Channel, raw_text: str) -> str:
    text = strip_internal_tags(raw_text)
    if not text:
        return ""
    prefix = f"{ASSISTANT_NAME}: " if channel.prefix_assistant_name else ""
    return f"{prefix}{text}"


def route_outbound(channels: list[Channel], jid: str, text: str) -> None:
    for ch in channels:
        if ch.owns_jid(jid) and ch.is_connected():
            import asyncio
            asyncio.ensure_future(ch.send_message(jid, text))
            return
    raise ValueError(f"No channel for JID: {jid}")
