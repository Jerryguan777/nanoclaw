"""TelegramGateway — manages multiple Telegram bots (one per coworker)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from telegram.constants import ChatAction, ParseMode
from telegram.ext import Application, MessageHandler, filters

from nanoclaw.core.logger import get_logger

if TYPE_CHECKING:
    from telegram import Bot, Update
    from telegram.ext import ContextTypes

    from nanoclaw.channels.gateway import MessageCallback
    from nanoclaw.core.types import ChannelBinding

logger = get_logger()

_MAX_LENGTH = 4096


async def _send_telegram_message(bot: Bot, chat_id: str | int, text: str) -> None:
    """Send with Markdown, falling back to plain text."""
    try:
        await bot.send_message(chat_id, text, parse_mode=ParseMode.MARKDOWN)
    except Exception:  # noqa: BLE001
        await bot.send_message(chat_id, text)


class _BotInstance:
    """A single Telegram bot managed by the gateway."""

    def __init__(self, binding: ChannelBinding, app: Application) -> None:  # type: ignore[type-arg]
        self.binding = binding
        self.app = app
        self.bot_username: str | None = None


class TelegramGateway:
    """Manages multiple Telegram bots (one per coworker)."""

    def __init__(self, on_message: MessageCallback) -> None:
        self._bots: dict[str, _BotInstance] = {}  # binding_id -> bot instance
        self._on_message = on_message

    @property
    def channel_type(self) -> str:
        return "telegram"

    async def add_binding(self, binding: ChannelBinding) -> None:
        """Start a Telegram bot for a channel binding."""
        token = binding.credentials.get("bot_token", "")
        if not token:
            logger.warning("Telegram binding missing bot_token", binding_id=binding.id)
            return

        app: Application[Any, Any, Any, Any, Any, Any] = Application.builder().token(token).build()

        binding_id = binding.id
        on_message = self._on_message

        async def _on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            msg = update.effective_message
            chat = update.effective_chat
            user = update.effective_user
            if msg is None or chat is None or msg.text is None:
                return

            if msg.text.startswith("/"):
                return

            chat_id = str(chat.id)
            content = msg.text
            sender_name = user.first_name if user else "Unknown"
            sender = str(user.id) if user else ""
            is_group = chat.type in ("group", "supergroup")

            await on_message(binding_id, chat_id, sender, content, sender_name, is_group)

        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_text))

        async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
            logger.error("Telegram bot error", binding_id=binding_id, error=str(context.error))

        app.add_error_handler(_on_error)

        await app.initialize()
        me = await app.bot.get_me()
        instance = _BotInstance(binding, app)
        instance.bot_username = me.username
        self._bots[binding.id] = instance

        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)  # type: ignore[union-attr]
        logger.info(
            "Telegram bot started for binding",
            binding_id=binding.id,
            username=me.username,
        )

    async def remove_binding(self, binding_id: str) -> None:
        """Stop and remove a Telegram bot."""
        instance = self._bots.pop(binding_id, None)
        if instance and instance.app:
            await instance.app.updater.stop()  # type: ignore[union-attr]
            await instance.app.stop()
            await instance.app.shutdown()
            logger.info("Telegram bot stopped", binding_id=binding_id)

    async def send_message(self, binding_id: str, chat_id: str, text: str) -> None:
        """Send a message via a specific bot."""
        instance = self._bots.get(binding_id)
        if instance is None:
            logger.warning("Telegram binding not found for send", binding_id=binding_id)
            return
        try:
            if len(text) <= _MAX_LENGTH:
                await _send_telegram_message(instance.app.bot, chat_id, text)
            else:
                for i in range(0, len(text), _MAX_LENGTH):
                    await _send_telegram_message(instance.app.bot, chat_id, text[i : i + _MAX_LENGTH])
        except Exception:
            logger.exception("Failed to send Telegram message", binding_id=binding_id, chat_id=chat_id)

    async def set_typing(self, binding_id: str, chat_id: str, is_typing: bool) -> None:
        """Send typing indicator."""
        if not is_typing:
            return
        instance = self._bots.get(binding_id)
        if instance is None:
            return
        try:
            await instance.app.bot.send_chat_action(chat_id, ChatAction.TYPING)
        except Exception:  # noqa: BLE001
            logger.debug("Failed to send Telegram typing indicator", binding_id=binding_id)

    async def shutdown(self) -> None:
        """Stop all bots."""
        for binding_id in list(self._bots.keys()):
            await self.remove_binding(binding_id)
