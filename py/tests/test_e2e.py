"""End-to-end tests for the NanoClaw Python runtime engine.

Strategy: Mock Docker (run_container_agent), test real logic.
Uses in-memory SQLite, real GroupQueue, real message routing.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

# ---------------------------------------------------------------------------
# Mock Channel
# ---------------------------------------------------------------------------


@dataclass
class MockChannel:
    """Channel implementation that records all interactions."""

    name: str = "mock"
    owned_jids: list[str] = field(default_factory=list)
    sent: list[tuple[str, str]] = field(default_factory=list)
    typing_events: list[tuple[str, bool]] = field(default_factory=list)
    _connected: bool = True

    async def connect(self) -> None:
        pass

    async def send_message(self, jid: str, text: str) -> None:
        self.sent.append((jid, text))

    def is_connected(self) -> bool:
        return self._connected

    def owns_jid(self, jid: str) -> bool:
        return jid in self.owned_jids

    async def disconnect(self) -> None:
        self._connected = False

    async def set_typing(self, jid: str, is_typing: bool) -> None:
        self.typing_events.append((jid, is_typing))


# ---------------------------------------------------------------------------
# Environment fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def e2e_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Set up isolated E2E environment with tmp dirs and in-memory DB."""
    data_dir = tmp_path / "data"
    groups_dir = tmp_path / "groups"
    store_dir = tmp_path / "store"
    data_dir.mkdir()
    groups_dir.mkdir()
    store_dir.mkdir()

    monkeypatch.setattr("nanoclaw.config.DATA_DIR", data_dir)
    monkeypatch.setattr("nanoclaw.config.GROUPS_DIR", groups_dir)
    monkeypatch.setattr("nanoclaw.config.STORE_DIR", store_dir)
    monkeypatch.setattr("nanoclaw.config.POLL_INTERVAL", 0.05)
    monkeypatch.setattr("nanoclaw.config.IPC_POLL_INTERVAL", 0.05)
    monkeypatch.setattr("nanoclaw.config.SCHEDULER_POLL_INTERVAL", 0.05)
    monkeypatch.setattr("nanoclaw.config.IDLE_TIMEOUT", 1000)
    monkeypatch.setattr("nanoclaw.config.TIMEZONE", "UTC")
    monkeypatch.setattr("nanoclaw.config.ASSISTANT_NAME", "Andy")
    monkeypatch.setattr("nanoclaw.config.TRIGGER_PATTERN", re.compile(r"^@Andy\b", re.IGNORECASE))

    # Also patch in modules that import these at module-load time
    monkeypatch.setattr("nanoclaw.group_folder.DATA_DIR", data_dir)
    monkeypatch.setattr("nanoclaw.group_folder.GROUPS_DIR", groups_dir)
    monkeypatch.setattr("nanoclaw.group_queue.DATA_DIR", data_dir)
    monkeypatch.setattr("nanoclaw.main.TIMEZONE", "UTC")
    monkeypatch.setattr("nanoclaw.main.ASSISTANT_NAME", "Andy")
    monkeypatch.setattr("nanoclaw.main.TRIGGER_PATTERN", re.compile(r"^@Andy\b", re.IGNORECASE))

    from nanoclaw.db import _init_test_database

    _init_test_database()

    return tmp_path


# ---------------------------------------------------------------------------
# Container agent mock
# ---------------------------------------------------------------------------


def make_container_mock(
    response_text: str = "Hello from the agent!",
    new_session_id: str | None = "sess-001",
    status: str = "success",
    error: str | None = None,
) -> Callable[..., Awaitable[object]]:
    """Create a mock for run_container_agent that simulates container execution."""
    from nanoclaw.container_runner import ContainerOutput

    captured_inputs: list[object] = []

    async def mock_run_container_agent(
        group: object,
        inp: object,
        on_process: Callable[..., None],
        on_output: Callable[..., Awaitable[None]] | None = None,
    ) -> ContainerOutput:
        captured_inputs.append(inp)
        on_process(object(), "test-container-mock")

        output = ContainerOutput(
            status=status,  # type: ignore[arg-type]
            result=response_text if status == "success" else None,
            new_session_id=new_session_id,
            error=error,
        )

        if on_output is not None:
            await on_output(output)

        return ContainerOutput(status="success", result=None, new_session_id=new_session_id)

    mock_run_container_agent.captured_inputs = captured_inputs  # type: ignore[attr-defined]
    return mock_run_container_agent  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Helper: set up main module state
# ---------------------------------------------------------------------------


def setup_main_state(
    channel: MockChannel,
    group_jid: str = "chat@test",
    group_folder: str = "testgroup",
    is_main: bool = True,
    groups_dir: Path | None = None,
) -> None:
    """Wire up main.py module-level state for testing."""
    import nanoclaw.main as m
    from nanoclaw.db import set_registered_group, store_chat_metadata
    from nanoclaw.types import RegisteredGroup

    group = RegisteredGroup(
        name="Test Group",
        folder=group_folder,
        trigger="@Andy",
        added_at="2024-01-01T00:00:00Z",
        is_main=is_main,
        requires_trigger=not is_main,
    )

    m._registered_groups = {group_jid: group}
    m._channels = [channel]
    m._last_timestamp = ""
    m._last_agent_timestamp = {}
    m._sessions = {}
    m._queue = m.GroupQueue()
    m._queue.set_process_messages_fn(m._process_group_messages)

    set_registered_group(group_jid, group)
    store_chat_metadata(group_jid, "2024-01-01T00:00:00Z", name="Test Group")

    # Create group folder on disk
    if groups_dir:
        gdir = groups_dir / group_folder
        gdir.mkdir(parents=True, exist_ok=True)
        (gdir / "logs").mkdir(exist_ok=True)


def inject_message(
    group_jid: str,
    content: str,
    sender: str = "user@test",
    sender_name: str = "TestUser",
    msg_id: str | None = None,
    timestamp: str = "2024-06-01T12:00:01Z",
) -> None:
    """Store a message directly in the DB as if a channel delivered it."""
    from nanoclaw.db import store_message
    from nanoclaw.types import NewMessage

    store_message(
        NewMessage(
            id=msg_id or f"msg-{id(content)}",
            chat_jid=group_jid,
            sender=sender,
            sender_name=sender_name,
            content=content,
            timestamp=timestamp,
        )
    )


# ===========================================================================
# E2E Test Cases
# ===========================================================================


async def test_message_inbound_to_response(e2e_env: Path) -> None:
    """Full flow: message stored → process_group_messages → container mock → channel sends response."""
    channel = MockChannel(owned_jids=["chat@test"])
    groups_dir = e2e_env / "groups"
    setup_main_state(channel, groups_dir=groups_dir)

    inject_message("chat@test", "Hello Andy, what's the weather?")

    mock_agent = make_container_mock(response_text="It's sunny today!")

    with patch("nanoclaw.main.run_container_agent", mock_agent):
        import nanoclaw.main as m

        result = await m._process_group_messages("chat@test")

    assert result is True
    assert len(channel.sent) == 1
    assert channel.sent[0][0] == "chat@test"
    assert "sunny" in channel.sent[0][1]


async def test_trigger_pattern_required(e2e_env: Path) -> None:
    """Non-main groups require trigger word to activate."""
    channel = MockChannel(owned_jids=["group@test"])
    groups_dir = e2e_env / "groups"
    setup_main_state(channel, group_jid="group@test", group_folder="nonmain", is_main=False, groups_dir=groups_dir)

    # Message without trigger → should not invoke container
    inject_message("group@test", "Just a regular message")

    mock_agent = make_container_mock()

    with patch("nanoclaw.main.run_container_agent", mock_agent):
        import nanoclaw.main as m

        result = await m._process_group_messages("group@test")

    assert result is True
    assert len(channel.sent) == 0  # No response sent
    assert len(mock_agent.captured_inputs) == 0  # type: ignore[attr-defined]


async def test_trigger_pattern_activates(e2e_env: Path) -> None:
    """Non-main groups respond when trigger word is present."""
    channel = MockChannel(owned_jids=["group@test"])
    groups_dir = e2e_env / "groups"
    setup_main_state(channel, group_jid="group@test", group_folder="triggergrp", is_main=False, groups_dir=groups_dir)

    inject_message("group@test", "@Andy what time is it?")

    mock_agent = make_container_mock(response_text="It's 3pm")

    with patch("nanoclaw.main.run_container_agent", mock_agent):
        import nanoclaw.main as m

        result = await m._process_group_messages("group@test")

    assert result is True
    assert len(channel.sent) == 1
    assert "3pm" in channel.sent[0][1]


async def test_session_persistence(e2e_env: Path) -> None:
    """Container returns new_session_id → persisted for next call."""
    channel = MockChannel(owned_jids=["chat@test"])
    groups_dir = e2e_env / "groups"
    setup_main_state(channel, groups_dir=groups_dir)

    inject_message("chat@test", "First message")

    mock_agent = make_container_mock(response_text="Got it", new_session_id="sess-abc-123")

    with patch("nanoclaw.main.run_container_agent", mock_agent):
        import nanoclaw.main as m

        await m._process_group_messages("chat@test")

    # Session should be persisted
    assert m._sessions.get("testgroup") == "sess-abc-123"

    from nanoclaw.db import get_session

    assert get_session("testgroup") == "sess-abc-123"


async def test_error_rollback(e2e_env: Path) -> None:
    """Container error → cursor rolled back for retry."""
    channel = MockChannel(owned_jids=["chat@test"])
    groups_dir = e2e_env / "groups"
    setup_main_state(channel, groups_dir=groups_dir)

    inject_message("chat@test", "This will fail")

    mock_agent = make_container_mock(status="error", error="Container crashed", response_text="")

    with patch("nanoclaw.main.run_container_agent", mock_agent):
        import nanoclaw.main as m

        result = await m._process_group_messages("chat@test")

    assert result is False  # Signal retry
    # Cursor should be rolled back (empty or previous value)
    assert m._last_agent_timestamp.get("chat@test", "") == ""


async def test_internal_tags_stripped(e2e_env: Path) -> None:
    """<internal> tags in agent response are stripped before sending."""
    channel = MockChannel(owned_jids=["chat@test"])
    groups_dir = e2e_env / "groups"
    setup_main_state(channel, groups_dir=groups_dir)

    inject_message("chat@test", "Tell me something")

    mock_agent = make_container_mock(response_text="Visible text <internal>hidden reasoning</internal> more visible")

    with patch("nanoclaw.main.run_container_agent", mock_agent):
        import nanoclaw.main as m

        await m._process_group_messages("chat@test")

    assert len(channel.sent) == 1
    assert "hidden reasoning" not in channel.sent[0][1]
    assert "Visible text" in channel.sent[0][1]
    assert "more visible" in channel.sent[0][1]


async def test_typing_indicator(e2e_env: Path) -> None:
    """Typing indicators are sent before and after processing."""
    channel = MockChannel(owned_jids=["chat@test"])
    groups_dir = e2e_env / "groups"
    setup_main_state(channel, groups_dir=groups_dir)

    inject_message("chat@test", "Hello")

    mock_agent = make_container_mock(response_text="Hi there")

    with patch("nanoclaw.main.run_container_agent", mock_agent):
        import nanoclaw.main as m

        await m._process_group_messages("chat@test")

    # Should have typing=True then typing=False
    assert ("chat@test", True) in channel.typing_events
    assert ("chat@test", False) in channel.typing_events


async def test_format_messages_xml(e2e_env: Path) -> None:
    """Messages are formatted as XML before being passed to container."""
    channel = MockChannel(owned_jids=["chat@test"])
    groups_dir = e2e_env / "groups"
    setup_main_state(channel, groups_dir=groups_dir)

    inject_message("chat@test", "Hello world", sender_name="Alice")

    captured_prompts: list[str] = []

    from nanoclaw.container_runner import ContainerInput, ContainerOutput

    async def capturing_mock(
        group: object,
        inp: ContainerInput,
        on_process: Callable[..., None],
        on_output: Callable[..., Awaitable[None]] | None = None,
    ) -> ContainerOutput:
        captured_prompts.append(inp.prompt)
        on_process(object(), "test-container")
        if on_output:
            await on_output(ContainerOutput(status="success", result="OK"))
        return ContainerOutput(status="success", result=None)

    with patch("nanoclaw.main.run_container_agent", capturing_mock):
        import nanoclaw.main as m

        await m._process_group_messages("chat@test")

    assert len(captured_prompts) == 1
    prompt = captured_prompts[0]
    assert "<messages>" in prompt
    assert 'sender="Alice"' in prompt
    assert "Hello world" in prompt
    assert "<context timezone=" in prompt


async def test_sender_allowlist_drop_mode(e2e_env: Path) -> None:
    """Drop mode: messages from non-allowed senders are not stored."""
    from nanoclaw.db import get_messages_since
    from nanoclaw.sender_allowlist import ChatAllowlistEntry, SenderAllowlistConfig
    from nanoclaw.types import NewMessage

    channel = MockChannel(owned_jids=["chat@test"])
    setup_main_state(channel)

    # Set up drop-mode allowlist
    drop_config = SenderAllowlistConfig(
        default=ChatAllowlistEntry(allow=["allowed_user"], mode="drop"),
    )

    import nanoclaw.main as m

    with patch("nanoclaw.main.load_sender_allowlist", return_value=drop_config):
        # Simulate on_message callback for non-allowed sender
        msg = NewMessage(
            id="drop-1",
            chat_jid="chat@test",
            sender="blocked_user",
            sender_name="Blocked",
            content="This should be dropped",
            timestamp="2024-06-01T12:00:01Z",
        )

        # Simulate the on_message handler from main.py
        if not msg.is_from_me and not msg.is_bot_message and msg.chat_jid in m._registered_groups:
            from nanoclaw.sender_allowlist import is_sender_allowed, should_drop_message

            if should_drop_message(msg.chat_jid, drop_config) and not is_sender_allowed(
                msg.chat_jid, msg.sender, drop_config
            ):
                pass  # Message dropped
            else:
                from nanoclaw.db import store_message

                store_message(msg)

    # Message should NOT be in DB
    msgs = get_messages_since("chat@test", "", "Andy")
    assert len(msgs) == 0


async def test_ipc_task_scheduling(e2e_env: Path) -> None:
    """IPC task file → creates scheduled task in DB."""
    from nanoclaw.db import get_task_by_id
    from nanoclaw.ipc import process_task_ipc
    from nanoclaw.types import RegisteredGroup

    registered = {
        "chat@test": RegisteredGroup(
            name="Test",
            folder="testgroup",
            trigger="@Andy",
            added_at="2024-01-01T00:00:00Z",
        )
    }

    tasks_changed = []

    class FakeDeps:
        async def send_message(self, jid: str, text: str) -> None:
            pass

        def registered_groups(self) -> dict[str, RegisteredGroup]:
            return registered

        def register_group(self, jid: str, group: RegisteredGroup) -> None:
            pass

        async def sync_groups(self, force: bool) -> None:
            pass

        def get_available_groups(self) -> list[object]:
            return []

        def write_groups_snapshot(self, gf: str, im: bool, ag: list[object], rj: set[str]) -> None:
            pass

        def on_tasks_changed(self) -> None:
            tasks_changed.append(True)

    deps = FakeDeps()

    await process_task_ipc(
        {
            "type": "schedule_task",
            "prompt": "Check the weather",
            "schedule_type": "cron",
            "schedule_value": "0 9 * * *",
            "targetJid": "chat@test",
            "taskId": "test-task-001",
        },
        "testgroup",
        True,  # is_main
        deps,  # type: ignore[arg-type]
    )

    task = get_task_by_id("test-task-001")
    assert task is not None
    assert task.prompt == "Check the weather"
    assert task.schedule_type == "cron"
    assert task.schedule_value == "0 9 * * *"
    assert task.group_folder == "testgroup"
    assert len(tasks_changed) == 1


async def test_ipc_message_routing(e2e_env: Path) -> None:
    """IPC message file in group dir → routed to channel.send_message."""
    data_dir = e2e_env / "data"
    ipc_dir = data_dir / "ipc" / "testgroup" / "messages"
    ipc_dir.mkdir(parents=True)

    from nanoclaw.types import RegisteredGroup

    registered = {
        "chat@test": RegisteredGroup(
            name="Test",
            folder="testgroup",
            trigger="@Andy",
            added_at="2024-01-01T00:00:00Z",
            is_main=True,
        )
    }

    sent_messages: list[tuple[str, str]] = []

    class FakeDeps:
        async def send_message(self, jid: str, text: str) -> None:
            sent_messages.append((jid, text))

        def registered_groups(self) -> dict[str, RegisteredGroup]:
            return registered

        def register_group(self, jid: str, group: RegisteredGroup) -> None:
            pass

        async def sync_groups(self, force: bool) -> None:
            pass

        def get_available_groups(self) -> list[object]:
            return []

        def write_groups_snapshot(self, gf: str, im: bool, ag: list[object], rj: set[str]) -> None:
            pass

        def on_tasks_changed(self) -> None:
            pass

    # Write an IPC message file
    msg_file = ipc_dir / "001.json"
    msg_file.write_text(json.dumps({"type": "message", "chatJid": "chat@test", "text": "Hello from IPC!"}))

    from nanoclaw.ipc import _process_ipc_files

    deps = FakeDeps()
    ipc_base = data_dir / "ipc"
    await _process_ipc_files(ipc_base, deps)  # type: ignore[arg-type]

    assert len(sent_messages) == 1
    assert sent_messages[0] == ("chat@test", "Hello from IPC!")
    assert not msg_file.exists()  # File consumed


async def test_queue_concurrency(e2e_env: Path) -> None:
    """Multiple groups enqueue → respects concurrency limit."""
    from nanoclaw.group_queue import GroupQueue

    queue = GroupQueue()
    processing_order: list[str] = []
    active_count: list[int] = []

    async def process_fn(group_jid: str) -> bool:
        active_count.append(sum(1 for s in queue._groups.values() if s.active))
        processing_order.append(group_jid)
        await asyncio.sleep(0.05)
        return True

    queue.set_process_messages_fn(process_fn)

    # Enqueue 3 groups
    queue.enqueue_message_check("group1")
    queue.enqueue_message_check("group2")
    queue.enqueue_message_check("group3")

    await asyncio.sleep(0.5)

    # All should have been processed
    assert "group1" in processing_order
    assert "group2" in processing_order
    assert "group3" in processing_order

    # Concurrency never exceeded MAX_CONCURRENT_CONTAINERS (5)
    for count in active_count:
        assert count <= 5


async def test_scheduled_task_compute_next_run(e2e_env: Path) -> None:
    """compute_next_run correctly calculates next execution time."""
    from nanoclaw.task_scheduler import compute_next_run
    from nanoclaw.types import ScheduledTask

    # once → None
    task_once = ScheduledTask(
        id="t1",
        group_folder="test",
        chat_jid="chat@test",
        prompt="once",
        schedule_type="once",
        schedule_value="2024-01-01T00:00:00",
        context_mode="isolated",
        next_run="2024-01-01T00:00:00Z",
        status="active",
        created_at="2024-01-01T00:00:00Z",
    )
    assert compute_next_run(task_once) is None

    # cron → future date
    task_cron = ScheduledTask(
        id="t2",
        group_folder="test",
        chat_jid="chat@test",
        prompt="cron",
        schedule_type="cron",
        schedule_value="0 9 * * *",
        context_mode="isolated",
        next_run="2024-01-01T09:00:00Z",
        status="active",
        created_at="2024-01-01T00:00:00Z",
    )
    next_cron = compute_next_run(task_cron)
    assert next_cron is not None
    assert "T" in next_cron

    # interval → anchored to next_run + interval
    task_interval = ScheduledTask(
        id="t3",
        group_folder="test",
        chat_jid="chat@test",
        prompt="interval",
        schedule_type="interval",
        schedule_value="3600000",
        context_mode="isolated",
        next_run="2020-01-01T00:00:00Z",
        status="active",
        created_at="2020-01-01T00:00:00Z",
    )
    next_interval = compute_next_run(task_interval)
    assert next_interval is not None
    # Must be in the future
    assert next_interval > "2024-01-01T00:00:00Z"
