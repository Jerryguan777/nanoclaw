"""Tests for IPC authorization — port of src/ipc-auth.test.ts."""

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock

from nanoclaw.db import init_test_database, create_task, get_task_by_id
from nanoclaw.ipc import _process_task_ipc, IpcDeps
from nanoclaw.models import RegisteredGroup, ScheduledTask


@pytest.fixture(autouse=True)
def fresh_db():
    init_test_database()


def _make_deps(**overrides) -> IpcDeps:
    return IpcDeps(
        send_message=overrides.get("send_message", AsyncMock()),
        registered_groups=overrides.get("registered_groups", lambda: {}),
        register_group=overrides.get("register_group", MagicMock()),
        get_available_groups=overrides.get("get_available_groups", lambda: []),
        write_groups_snapshot=overrides.get("write_groups_snapshot", MagicMock()),
    )


@pytest.mark.asyncio
async def test_main_can_register_group():
    register_fn = MagicMock()
    deps = _make_deps(register_group=register_fn)

    await _process_task_ipc(
        {"type": "register_group", "jid": "g1@local", "name": "Test",
         "folder": "test", "trigger": "@Andy"},
        source_group="main", is_main=True, deps=deps,
    )
    register_fn.assert_called_once()


@pytest.mark.asyncio
async def test_non_main_cannot_register_group():
    register_fn = MagicMock()
    deps = _make_deps(register_group=register_fn)

    await _process_task_ipc(
        {"type": "register_group", "jid": "g1@local", "name": "Test",
         "folder": "test", "trigger": "@Andy"},
        source_group="other", is_main=False, deps=deps,
    )
    register_fn.assert_not_called()


@pytest.mark.asyncio
async def test_non_main_cannot_pause_other_groups_task():
    create_task(ScheduledTask(
        id="task-1", group_folder="secret", chat_jid="secret@local",
        prompt="test", schedule_type="once", schedule_value="2026-06-01",
        status="active", created_at="2026-01-01",
    ))

    deps = _make_deps()
    await _process_task_ipc(
        {"type": "pause_task", "taskId": "task-1"},
        source_group="other", is_main=False, deps=deps,
    )

    task = get_task_by_id("task-1")
    assert task is not None
    assert task.status == "active"  # not paused


@pytest.mark.asyncio
async def test_main_can_pause_any_task():
    create_task(ScheduledTask(
        id="task-2", group_folder="other", chat_jid="other@local",
        prompt="test", schedule_type="once", schedule_value="2026-06-01",
        status="active", created_at="2026-01-01",
    ))

    deps = _make_deps()
    await _process_task_ipc(
        {"type": "pause_task", "taskId": "task-2"},
        source_group="main", is_main=True, deps=deps,
    )

    task = get_task_by_id("task-2")
    assert task is not None
    assert task.status == "paused"
