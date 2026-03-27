"""IPC message types for NATS-based communication between Orchestrator and Agent."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class IpcEnvelope:
    """Wrapper for all IPC messages sent over NATS JetStream."""

    type: str
    group_folder: str
    timestamp: str
    payload: dict[str, object]

    def serialize(self) -> bytes:
        return json.dumps(asdict(self)).encode()

    @classmethod
    def deserialize(cls, data: bytes) -> IpcEnvelope:
        raw = json.loads(data)
        return cls(
            type=raw["type"],
            group_folder=raw["group_folder"],
            timestamp=raw["timestamp"],
            payload=raw["payload"],
        )


@dataclass(frozen=True)
class AgentInitData:
    """Channel 1: initial input written to KV before container starts."""

    prompt: str
    group_folder: str
    chat_jid: str
    is_main: bool
    session_id: str | None = None
    is_scheduled_task: bool = False
    assistant_name: str | None = None

    def serialize(self) -> bytes:
        return json.dumps(asdict(self)).encode()

    @classmethod
    def deserialize(cls, data: bytes) -> AgentInitData:
        raw = json.loads(data)
        return cls(
            prompt=raw["prompt"],
            group_folder=raw["group_folder"],
            chat_jid=raw["chat_jid"],
            is_main=raw["is_main"],
            session_id=raw.get("session_id"),
            is_scheduled_task=raw.get("is_scheduled_task", False),
            assistant_name=raw.get("assistant_name"),
        )
