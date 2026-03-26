"""Tests for nanoclaw.db."""

from __future__ import annotations

from nanoclaw.core.types import NewMessage, RegisteredGroup, ScheduledTask
from nanoclaw.db.sqlite import (
    _init_test_database,
    create_task,
    delete_task,
    get_all_registered_groups,
    get_all_sessions,
    get_all_tasks,
    get_due_tasks,
    get_messages_since,
    get_new_messages,
    get_router_state,
    get_session,
    get_task_by_id,
    get_tasks_for_group,
    set_registered_group,
    set_router_state,
    set_session,
    store_chat_metadata,
    store_message,
    update_task,
)


def setup_function() -> None:
    _init_test_database()


def test_store_and_get_messages() -> None:
    store_chat_metadata("chat1", "2024-01-01T00:00:00Z", name="Test Chat")
    msg = NewMessage(
        id="m1",
        chat_jid="chat1",
        sender="user1",
        sender_name="Alice",
        content="Hello",
        timestamp="2024-01-01T00:00:01Z",
    )
    store_message(msg)

    messages, new_ts = get_new_messages(["chat1"], "2024-01-01T00:00:00Z", "Andy")
    assert len(messages) == 1
    assert messages[0].content == "Hello"
    assert new_ts > "2024-01-01T00:00:00Z"


def test_get_messages_since() -> None:
    store_chat_metadata("chat1", "2024-01-01T00:00:00Z")
    store_message(
        NewMessage(
            id="m1",
            chat_jid="chat1",
            sender="user1",
            sender_name="Alice",
            content="Hello",
            timestamp="2024-01-01T00:00:01Z",
        )
    )
    store_message(
        NewMessage(
            id="m2",
            chat_jid="chat1",
            sender="user1",
            sender_name="Alice",
            content="World",
            timestamp="2024-01-01T00:00:02Z",
        )
    )
    msgs = get_messages_since("chat1", "2024-01-01T00:00:00Z", "Andy")
    assert len(msgs) == 2


def test_router_state() -> None:
    set_router_state("test_key", "test_value")
    assert get_router_state("test_key") == "test_value"
    assert get_router_state("missing") is None


def test_sessions() -> None:
    set_session("group1", "sess-123")
    assert get_session("group1") == "sess-123"
    assert get_session("missing") is None

    sessions = get_all_sessions()
    assert sessions["group1"] == "sess-123"


def test_registered_groups() -> None:
    group = RegisteredGroup(
        name="Test Group",
        folder="testgroup",
        trigger="@Andy",
        added_at="2024-01-01T00:00:00Z",
    )
    set_registered_group("chat@jid", group)

    groups = get_all_registered_groups()
    assert "chat@jid" in groups
    assert groups["chat@jid"].name == "Test Group"
    assert groups["chat@jid"].folder == "testgroup"


def test_task_crud() -> None:
    task = ScheduledTask(
        id="t1",
        group_folder="testgroup",
        chat_jid="chat@jid",
        prompt="Do something",
        schedule_type="cron",
        schedule_value="0 9 * * *",
        context_mode="group",
        next_run="2024-01-02T09:00:00Z",
        status="active",
        created_at="2024-01-01T00:00:00Z",
    )
    create_task(task)

    retrieved = get_task_by_id("t1")
    assert retrieved is not None
    assert retrieved.prompt == "Do something"

    tasks = get_tasks_for_group("testgroup")
    assert len(tasks) == 1

    all_tasks = get_all_tasks()
    assert len(all_tasks) == 1

    update_task("t1", prompt="Updated prompt")
    updated = get_task_by_id("t1")
    assert updated is not None
    assert updated.prompt == "Updated prompt"

    delete_task("t1")
    assert get_task_by_id("t1") is None


def test_get_due_tasks() -> None:
    task = ScheduledTask(
        id="t2",
        group_folder="testgroup",
        chat_jid="chat@jid",
        prompt="Past task",
        schedule_type="once",
        schedule_value="2020-01-01T00:00:00Z",
        context_mode="isolated",
        next_run="2020-01-01T00:00:00Z",
        status="active",
        created_at="2020-01-01T00:00:00Z",
    )
    create_task(task)

    due = get_due_tasks()
    assert len(due) >= 1
    assert any(t.id == "t2" for t in due)


def test_store_chat_metadata_with_channel() -> None:
    store_chat_metadata("tg:123", "2024-01-01T00:00:00Z", name="TG Chat", channel="telegram", is_group=True)
    from nanoclaw.db.sqlite import get_all_chats

    chats = get_all_chats()
    assert any(c.jid == "tg:123" for c in chats)


def test_store_chat_metadata_no_name() -> None:
    store_chat_metadata("chat2", "2024-01-01T00:00:00Z")
    from nanoclaw.db.sqlite import get_all_chats

    chats = get_all_chats()
    assert any(c.jid == "chat2" for c in chats)


def test_update_chat_name() -> None:
    from nanoclaw.db.sqlite import update_chat_name

    store_chat_metadata("chat3", "2024-01-01T00:00:00Z", name="Old Name")
    update_chat_name("chat3", "New Name")
    from nanoclaw.db.sqlite import get_all_chats

    chats = get_all_chats()
    chat = next(c for c in chats if c.jid == "chat3")
    assert chat.name == "New Name"


def test_group_sync() -> None:
    from nanoclaw.db.sqlite import get_last_group_sync, set_last_group_sync

    assert get_last_group_sync() is None
    set_last_group_sync()
    assert get_last_group_sync() is not None


def test_update_task_after_run() -> None:
    from nanoclaw.db.sqlite import log_task_run, update_task_after_run

    task = ScheduledTask(
        id="t3",
        group_folder="testgroup",
        chat_jid="chat@jid",
        prompt="Test",
        schedule_type="cron",
        schedule_value="0 9 * * *",
        context_mode="group",
        next_run="2024-01-02T09:00:00Z",
        status="active",
        created_at="2024-01-01T00:00:00Z",
    )
    create_task(task)
    update_task_after_run("t3", "2024-01-03T09:00:00Z", "Done")
    updated = get_task_by_id("t3")
    assert updated is not None
    assert updated.last_result == "Done"
    assert updated.next_run == "2024-01-03T09:00:00Z"

    from nanoclaw.core.types import TaskRunLog

    log_task_run(TaskRunLog(task_id="t3", run_at="2024-01-02T09:00:00Z", duration_ms=500, status="success"))


def test_update_task_multiple_fields() -> None:
    task = ScheduledTask(
        id="t4",
        group_folder="testgroup",
        chat_jid="chat@jid",
        prompt="Original",
        schedule_type="cron",
        schedule_value="0 9 * * *",
        context_mode="group",
        next_run="2024-01-02T09:00:00Z",
        status="active",
        created_at="2024-01-01T00:00:00Z",
    )
    create_task(task)
    update_task("t4", prompt="New prompt", status="paused", schedule_value="0 10 * * *")
    updated = get_task_by_id("t4")
    assert updated is not None
    assert updated.prompt == "New prompt"
    assert updated.status == "paused"


def test_update_task_no_fields() -> None:
    task = ScheduledTask(
        id="t5",
        group_folder="testgroup",
        chat_jid="chat@jid",
        prompt="Test",
        schedule_type="once",
        schedule_value="2024-01-01T00:00:00Z",
        context_mode="isolated",
        status="active",
        created_at="2024-01-01T00:00:00Z",
    )
    create_task(task)
    update_task("t5")  # No fields — should be a no-op


def test_store_message_direct() -> None:
    from nanoclaw.db.sqlite import store_message_direct

    store_chat_metadata("chat5", "2024-01-01T00:00:00Z")
    store_message_direct(
        id="md1",
        chat_jid="chat5",
        sender="user1",
        sender_name="Alice",
        content="Direct message",
        timestamp="2024-01-01T00:00:01Z",
        is_from_me=False,
    )
    msgs = get_messages_since("chat5", "2024-01-01T00:00:00Z", "Andy")
    assert len(msgs) == 1


def test_registered_group_with_config() -> None:
    from nanoclaw.core.types import ContainerConfig

    group = RegisteredGroup(
        name="Config Group",
        folder="configgroup",
        trigger="@Andy",
        added_at="2024-01-01T00:00:00Z",
        container_config=ContainerConfig(timeout=600000),
        is_main=True,
    )
    set_registered_group("config@jid", group)
    groups = get_all_registered_groups()
    assert "config@jid" in groups
    assert groups["config@jid"].is_main is True
