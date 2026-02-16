"""HTTP API channel — REST interface via FastAPI.

Provides endpoints for sending messages, reading responses, managing groups/tasks.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Callable, Awaitable

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel as PydanticBaseModel

from nanoclaw.channels.base import BaseChannel, OnChatMetadata, OnInboundMessage
from nanoclaw.config import ASSISTANT_NAME, HTTP_HOST, HTTP_PORT
from nanoclaw.logger import logger
from nanoclaw.models import NewMessage

HTTP_JID_PREFIX = "http-"


# --- Request/response models ---

class MessageRequest(PydanticBaseModel):
    text: str
    group: str = "main"
    sender_name: str = "User"


class MessageResponse(PydanticBaseModel):
    status: str
    message_id: str


class GroupRequest(PydanticBaseModel):
    jid: str
    name: str
    folder: str
    trigger: str


# --- Channel ---


class HTTPChannel(BaseChannel):
    name = "http"
    prefix_assistant_name = False  # API returns clean text

    def __init__(
        self,
        on_message: OnInboundMessage,
        on_chat_metadata: OnChatMetadata,
    ) -> None:
        self._on_message = on_message
        self._on_chat_metadata = on_chat_metadata
        self._connected = False
        self._app = FastAPI(title="NanoClaw API")
        self._response_queues: dict[str, asyncio.Queue[str | None]] = {}
        self._setup_routes()

    def _setup_routes(self) -> None:
        app = self._app

        @app.post("/messages", response_model=MessageResponse)
        async def send_message(req: MessageRequest):
            """Send a message to a group. Returns immediately."""
            jid = f"{HTTP_JID_PREFIX}{req.group}@http"
            now = datetime.now(timezone.utc).isoformat()
            msg_id = str(uuid.uuid4())

            self._on_chat_metadata(jid, now)
            msg = NewMessage(
                id=msg_id,
                chat_jid=jid,
                sender="http-user",
                sender_name=req.sender_name,
                content=req.text,
                timestamp=now,
            )
            self._on_message(jid, msg)
            return MessageResponse(status="queued", message_id=msg_id)

        @app.post("/messages/stream")
        async def send_message_stream(req: MessageRequest):
            """Send a message and stream the response via SSE."""
            jid = f"{HTTP_JID_PREFIX}{req.group}@http"
            now = datetime.now(timezone.utc).isoformat()
            msg_id = str(uuid.uuid4())

            # Create response queue for this request
            queue: asyncio.Queue[str | None] = asyncio.Queue()
            self._response_queues[msg_id] = queue

            self._on_chat_metadata(jid, now)
            msg = NewMessage(
                id=msg_id,
                chat_jid=jid,
                sender="http-user",
                sender_name=req.sender_name,
                content=req.text,
                timestamp=now,
            )
            self._on_message(jid, msg)

            async def event_stream():
                try:
                    while True:
                        text = await asyncio.wait_for(queue.get(), timeout=300)
                        if text is None:
                            break
                        yield f"data: {text}\n\n"
                except asyncio.TimeoutError:
                    yield "data: [timeout]\n\n"
                finally:
                    self._response_queues.pop(msg_id, None)

            return StreamingResponse(event_stream(), media_type="text/event-stream")

        @app.get("/health")
        async def health():
            return {"status": "ok", "assistant": ASSISTANT_NAME}

    @property
    def app(self) -> FastAPI:
        return self._app

    async def connect(self) -> None:
        self._connected = True
        logger.info("HTTP channel ready", host=HTTP_HOST, port=HTTP_PORT)

    async def start_server(self) -> None:
        """Start the uvicorn server. Call in a background task."""
        import uvicorn

        config = uvicorn.Config(
            self._app,
            host=HTTP_HOST,
            port=HTTP_PORT,
            log_level="warning",
        )
        server = uvicorn.Server(config)
        await server.serve()

    async def send_message(self, jid: str, text: str) -> None:
        # Push to any waiting SSE streams for this group
        for queue in self._response_queues.values():
            await queue.put(text)

    def is_connected(self) -> bool:
        return self._connected

    def owns_jid(self, jid: str) -> bool:
        return jid.endswith("@http")

    async def disconnect(self) -> None:
        self._connected = False
        for queue in self._response_queues.values():
            await queue.put(None)
        self._response_queues.clear()
