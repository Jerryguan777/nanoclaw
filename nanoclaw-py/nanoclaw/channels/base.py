"""Abstract channel base class."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Awaitable

from nanoclaw.models import NewMessage


# Callback types
OnInboundMessage = Callable[[str, NewMessage], None]
OnChatMetadata = Callable[[str, str], None]  # (jid, timestamp)


class BaseChannel(ABC):
    """All input/output channels implement this interface."""

    name: str = ""
    prefix_assistant_name: bool = True

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def send_message(self, jid: str, text: str) -> None: ...

    @abstractmethod
    def is_connected(self) -> bool: ...

    @abstractmethod
    def owns_jid(self, jid: str) -> bool: ...

    async def disconnect(self) -> None:
        pass

    async def set_typing(self, jid: str, is_typing: bool) -> None:
        pass
