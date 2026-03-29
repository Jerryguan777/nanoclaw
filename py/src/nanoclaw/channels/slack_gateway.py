"""SlackGateway — manages multiple Slack apps (one per coworker)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from nanoclaw.core.logger import get_logger

if TYPE_CHECKING:
    from nanoclaw.channels.gateway import MessageCallback
    from nanoclaw.core.types import ChannelBinding

logger = get_logger()

_MAX_MESSAGE_LENGTH = 4000


class _SlackAppInstance:
    """A single Slack app managed by the gateway."""

    def __init__(self, binding: ChannelBinding, app: AsyncApp, handler: AsyncSocketModeHandler) -> None:
        self.binding = binding
        self.app = app
        self.handler = handler
        self.bot_user_id: str | None = None


class SlackGateway:
    """Manages multiple Slack apps (one per coworker)."""

    def __init__(self, on_message: MessageCallback) -> None:
        self._apps: dict[str, _SlackAppInstance] = {}  # binding_id -> app instance
        self._on_message = on_message

    @property
    def channel_type(self) -> str:
        return "slack"

    async def add_binding(self, binding: ChannelBinding) -> None:
        """Start a Slack app for a channel binding."""
        bot_token = binding.credentials.get("bot_token", "")
        app_token = binding.credentials.get("app_token", "")
        if not bot_token or not app_token:
            logger.warning("Slack binding missing tokens", binding_id=binding.id)
            return

        app = AsyncApp(token=bot_token)
        binding_id = binding.id
        on_message = self._on_message

        @app.event("message")
        async def _on_msg(event: dict[str, Any], say: Any) -> None:
            subtype = event.get("subtype")
            if subtype and subtype != "bot_message":
                return

            text = event.get("text")
            if not text:
                return

            channel_id = event.get("channel", "")
            sender = event.get("user") or event.get("bot_id", "")
            sender_name = sender  # basic; full resolution would need API call
            is_group = event.get("channel_type") != "im"

            await on_message(binding_id, channel_id, sender, text, sender_name, is_group)

        handler = AsyncSocketModeHandler(app, app_token)

        try:
            auth = await app.client.auth_test()
            bot_user_id = auth.get("user_id")
        except Exception:  # noqa: BLE001
            bot_user_id = None

        await handler.connect_async()  # type: ignore[no-untyped-call]

        instance = _SlackAppInstance(binding, app, handler)
        instance.bot_user_id = bot_user_id
        self._apps[binding.id] = instance

        logger.info("Slack app started for binding", binding_id=binding.id, bot_user_id=bot_user_id)

    async def remove_binding(self, binding_id: str) -> None:
        """Stop and remove a Slack app."""
        instance = self._apps.pop(binding_id, None)
        if instance:
            await instance.handler.close_async()  # type: ignore[no-untyped-call]
            logger.info("Slack app stopped", binding_id=binding_id)

    async def send_message(self, binding_id: str, chat_id: str, text: str) -> None:
        """Send a message via a specific Slack app."""
        instance = self._apps.get(binding_id)
        if instance is None:
            logger.warning("Slack binding not found for send", binding_id=binding_id)
            return
        try:
            if len(text) <= _MAX_MESSAGE_LENGTH:
                await instance.app.client.chat_postMessage(channel=chat_id, text=text)
            else:
                for i in range(0, len(text), _MAX_MESSAGE_LENGTH):
                    await instance.app.client.chat_postMessage(channel=chat_id, text=text[i : i + _MAX_MESSAGE_LENGTH])
        except Exception:
            logger.exception("Failed to send Slack message", binding_id=binding_id, chat_id=chat_id)

    async def set_typing(self, binding_id: str, chat_id: str, is_typing: bool) -> None:
        """Slack Bot API has no typing indicator endpoint — no-op."""

    async def shutdown(self) -> None:
        """Stop all apps."""
        for binding_id in list(self._apps.keys()):
            await self.remove_binding(binding_id)
