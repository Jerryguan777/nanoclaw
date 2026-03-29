"""Telegram gateway — manages multiple Telegram bots (one per coworker)."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from telegram.constants import ChatAction, ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, filters

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
    """A single Telegram bot instance for one channel binding."""

    def __init__(self, binding: ChannelBinding, on_message: MessageCallback) -> None:
        self.binding = binding
        self._on_message = on_message
        self._app: Application | None = None  # type: ignore[type-arg]
        self._bot_username: str | None = None

    async def start(self) -> None:
        """Initialize and start polling."""
        token = self.binding.credentials.get("bot_token", "")
        if not token:
            logger.warning("Telegram bot has no token", binding_id=self.binding.id)
            return

        self._app = Application.builder().token(token).build()
        app = self._app
        binding_id = self.binding.id

        async def _on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            msg = update.effective_message
            chat = update.effective_chat
            user = update.effective_user
            if msg is None or chat is None or msg.text is None:
                return
            if msg.text.startswith("/"):
                cmd = msg.text.lstrip("/").split()[0].split("@")[0].lower()
                if cmd in ("chatid", "ping"):
                    return

            chat_id = str(chat.id)
            content = msg.text
            timestamp = msg.date.isoformat() if msg.date else ""
            sender_name = user.first_name if user else "Unknown"
            sender = str(user.id) if user else ""
            msg_id = str(msg.message_id)
            is_group = chat.type in ("group", "supergroup")

            # Translate @bot_username mentions
            if self._bot_username and msg.entities:
                for entity in msg.entities:
                    if entity.type == "mention":
                        mention_text = content[entity.offset : entity.offset + entity.length].lower()
                        if mention_text == f"@{self._bot_username.lower()}":
                            bot_name = self.binding.bot_display_name or self._bot_username
                            content = f"@{bot_name} {content}"
                            break

            await self._on_message(binding_id, chat_id, sender, sender_name, content, timestamp, msg_id, is_group)

        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_text))

        # Media handlers
        for filt, ph in [
            (filters.PHOTO, "[Photo]"),
            (filters.VIDEO, "[Video]"),
            (filters.VOICE, "[Voice message]"),
            (filters.AUDIO, "[Audio]"),
            (filters.Document.ALL, "[Document]"),
            (filters.Sticker.ALL, "[Sticker]"),
            (filters.LOCATION, "[Location]"),
            (filters.CONTACT, "[Contact]"),
        ]:

            async def _media_handler(
                update: Update,
                context: ContextTypes.DEFAULT_TYPE,
                _ph: str = ph,
            ) -> None:
                msg = update.effective_message
                chat = update.effective_chat
                user = update.effective_user
                if msg is None or chat is None:
                    return
                chat_id = str(chat.id)
                timestamp = msg.date.isoformat() if msg.date else ""
                sender_name = user.first_name if user else "Unknown"
                sender = str(user.id) if user else ""
                caption = f" {msg.caption}" if msg.caption else ""
                is_group = chat.type in ("group", "supergroup")
                await self._on_message(
                    binding_id,
                    chat_id,
                    sender,
                    sender_name,
                    f"{_ph}{caption}",
                    timestamp,
                    str(msg.message_id),
                    is_group,
                )

            app.add_handler(MessageHandler(filt, _media_handler))

        # Commands
        async def _cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if update.effective_chat is None:
                return
            chat = update.effective_chat
            chat_name = chat.title or (update.effective_user.first_name if update.effective_user else "Private")
            await chat.send_message(
                f"Chat ID: `{chat.id}`\nName: {chat_name}\nType: {chat.type}",
                parse_mode=ParseMode.MARKDOWN,
            )

        async def _cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if update.effective_chat:
                name = self.binding.bot_display_name or self._bot_username or "Bot"
                await update.effective_chat.send_message(f"{name} is online.")

        app.add_handler(CommandHandler("chatid", _cmd_chatid))
        app.add_handler(CommandHandler("ping", _cmd_ping))

        async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
            logger.error("Telegram bot error", binding_id=binding_id, error=str(context.error))

        app.add_error_handler(_on_error)

        await app.initialize()
        me = await app.bot.get_me()
        self._bot_username = me.username
        logger.info("Telegram bot connected", username=me.username, bot_id=me.id, binding_id=binding_id)

        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)  # type: ignore[union-attr]

    async def stop(self) -> None:
        """Stop the bot."""
        if self._app:
            await self._app.updater.stop()  # type: ignore[union-attr]
            await self._app.stop()
            await self._app.shutdown()
            self._app = None

    async def send_message(self, chat_id: str, text: str) -> None:
        """Send a message via this bot."""
        if self._app is None:
            return
        try:
            if len(text) <= _MAX_LENGTH:
                await _send_telegram_message(self._app.bot, chat_id, text)
            else:
                for i in range(0, len(text), _MAX_LENGTH):
                    await _send_telegram_message(self._app.bot, chat_id, text[i : i + _MAX_LENGTH])
        except Exception:
            logger.exception("Failed to send Telegram message", chat_id=chat_id, binding_id=self.binding.id)

    async def set_typing(self, chat_id: str, is_typing: bool) -> None:
        """Send typing indicator."""
        if not self._app or not is_typing:
            return
        with contextlib.suppress(Exception):
            await self._app.bot.send_chat_action(chat_id, ChatAction.TYPING)


class TelegramGateway:
    """Manages multiple Telegram bots (one per coworker)."""

    def __init__(self, on_message: MessageCallback) -> None:
        self._on_message = on_message
        self._bots: dict[str, _BotInstance] = {}

    @property
    def channel_type(self) -> str:
        return "telegram"

    async def add_binding(self, binding: ChannelBinding) -> None:
        """Start a new bot for this binding."""
        if binding.id in self._bots:
            return
        bot = _BotInstance(binding, self._on_message)
        await bot.start()
        self._bots[binding.id] = bot

    async def remove_binding(self, binding_id: str) -> None:
        """Stop and remove a bot."""
        bot = self._bots.pop(binding_id, None)
        if bot:
            await bot.stop()

    async def send_message(self, binding_id: str, chat_id: str, text: str) -> None:
        """Send a message via the specified bot."""
        bot = self._bots.get(binding_id)
        if bot:
            await bot.send_message(chat_id, text)
        else:
            logger.warning("No bot for binding", binding_id=binding_id)

    async def set_typing(self, binding_id: str, chat_id: str, is_typing: bool) -> None:
        """Send typing indicator via the specified bot."""
        bot = self._bots.get(binding_id)
        if bot:
            await bot.set_typing(chat_id, is_typing)

    async def shutdown(self) -> None:
        """Stop all bots."""
        for bot in list(self._bots.values()):
            await bot.stop()
        self._bots.clear()
