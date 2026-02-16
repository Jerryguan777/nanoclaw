"""CLI channel — interactive stdin/stdout interface.

Replaces WhatsApp for local use. Messages come from stdin, responses go to stdout.
Single "group" with JID "cli@local".
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import datetime, timezone
from typing import Callable, Awaitable

from nanoclaw.channels.base import BaseChannel, OnChatMetadata, OnInboundMessage
from nanoclaw.config import ASSISTANT_NAME
from nanoclaw.logger import logger
from nanoclaw.models import NewMessage


CLI_JID = "cli@local"


class CLIChannel(BaseChannel):
    name = "cli"
    prefix_assistant_name = True

    def __init__(
        self,
        on_message: OnInboundMessage,
        on_chat_metadata: OnChatMetadata,
    ) -> None:
        self._on_message = on_message
        self._on_chat_metadata = on_chat_metadata
        self._connected = False
        self._read_task: asyncio.Task | None = None

    async def connect(self) -> None:
        self._connected = True
        logger.info("CLI channel connected", jid=CLI_JID)

    async def start_reading(self) -> None:
        """Start reading from stdin in a background task."""
        self._read_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        loop = asyncio.get_event_loop()
        print(f"\nNanoClaw CLI (trigger: @{ASSISTANT_NAME})")
        print("Type your messages below. Press Ctrl+C to exit.\n")

        while self._connected:
            try:
                line = await loop.run_in_executor(None, self._read_line)
                if line is None:
                    break
                line = line.strip()
                if not line:
                    continue

                now = datetime.now(timezone.utc).isoformat()
                self._on_chat_metadata(CLI_JID, now)

                msg = NewMessage(
                    id=str(uuid.uuid4()),
                    chat_jid=CLI_JID,
                    sender="user@local",
                    sender_name="User",
                    content=line,
                    timestamp=now,
                    is_from_me=False,
                )
                self._on_message(CLI_JID, msg)
            except (EOFError, KeyboardInterrupt):
                break
            except Exception as e:
                logger.error("CLI read error", error=str(e))

    @staticmethod
    def _read_line() -> str | None:
        try:
            return input("> ")
        except EOFError:
            return None

    async def send_message(self, jid: str, text: str) -> None:
        # Print to stdout with formatting
        print(f"\n{text}\n")

    def is_connected(self) -> bool:
        return self._connected

    def owns_jid(self, jid: str) -> bool:
        return jid == CLI_JID

    async def disconnect(self) -> None:
        self._connected = False
        if self._read_task and not self._read_task.done():
            self._read_task.cancel()
