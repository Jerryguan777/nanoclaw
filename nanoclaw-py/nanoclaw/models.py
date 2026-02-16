"""Pydantic models — ports of src/types.ts."""

from __future__ import annotations

from pydantic import BaseModel, Field


class AdditionalMount(BaseModel):
    host_path: str
    container_path: str | None = None
    readonly: bool = True


class ContainerConfig(BaseModel):
    additional_mounts: list[AdditionalMount] | None = None
    timeout: int | None = None  # ms, default 300_000


class RegisteredGroup(BaseModel):
    name: str
    folder: str
    trigger: str
    added_at: str
    container_config: ContainerConfig | None = None
    requires_trigger: bool = True


class NewMessage(BaseModel):
    id: str
    chat_jid: str
    sender: str
    sender_name: str
    content: str
    timestamp: str
    is_from_me: bool = False


class ScheduledTask(BaseModel):
    id: str
    group_folder: str
    chat_jid: str
    prompt: str
    schedule_type: str  # 'cron' | 'interval' | 'once'
    schedule_value: str
    context_mode: str = "isolated"  # 'group' | 'isolated'
    next_run: str | None = None
    last_run: str | None = None
    last_result: str | None = None
    status: str = "active"  # 'active' | 'paused' | 'completed'
    created_at: str = ""


class TaskRunLog(BaseModel):
    task_id: str
    run_at: str
    duration_ms: int
    status: str  # 'success' | 'error'
    result: str | None = None
    error: str | None = None


class ContainerInput(BaseModel):
    prompt: str
    session_id: str | None = None
    group_folder: str
    chat_jid: str
    is_main: bool
    is_scheduled_task: bool = False


class ContainerOutput(BaseModel):
    status: str  # 'success' | 'error'
    result: str | None = None
    new_session_id: str | None = None
    error: str | None = None


class AvailableGroup(BaseModel):
    jid: str
    name: str
    last_activity: str
    is_registered: bool


# --- Channel abstraction ---

class Channel:
    """Base class for message channels."""

    name: str = ""
    prefix_assistant_name: bool = True

    async def connect(self) -> None:
        raise NotImplementedError

    async def send_message(self, jid: str, text: str) -> None:
        raise NotImplementedError

    def is_connected(self) -> bool:
        return False

    def owns_jid(self, jid: str) -> bool:
        return False

    async def disconnect(self) -> None:
        pass

    async def set_typing(self, jid: str, is_typing: bool) -> None:
        pass
