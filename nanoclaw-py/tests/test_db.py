"""Tests for database operations — port of src/db.test.ts."""

import pytest
from nanoclaw.db import (
    init_test_database,
    store_message,
    get_new_messages,
    get_messages_since,
    set_registered_group,
    get_all_registered_groups,
    get_registered_group,
    set_router_state,
    get_router_state,
    set_session,
    get_all_sessions,
    create_task,
    get_task_by_id,
    get_all_tasks,
    update_task,
    delete_task,
    store_chat_metadata,
    get_all_chats,
)
from nanoclaw.models import NewMessage, RegisteredGroup, ScheduledTask


@pytest.fixture(autouse=True)
def fresh_db():
    init_test_database()


# --- Messages ---

def test_store_and_retrieve_messages():
    store_message(NewMessage(
        id="m1", chat_jid="g1@local", sender="u1",
        sender_name="Alice", content="Hello", timestamp="2026-01-01T00:00:01Z",
    ))
    store_message(NewMessage(
        id="m2", chat_jid="g1@local", sender="u2",
        sender_name="Bob", content="Hi there", timestamp="2026-01-01T00:00:02Z",
    ))

    msgs, new_ts = get_new_messages(["g1@local"], "2026-01-01T00:00:00Z", "Andy")
    assert len(msgs) == 2
    assert new_ts == "2026-01-01T00:00:02Z"


def test_filter_bot_messages():
    store_message(NewMessage(
        id="m1", chat_jid="g1@local", sender="u1",
        sender_name="Alice", content="Hello", timestamp="t1",
    ))
    store_message(NewMessage(
        id="m2", chat_jid="g1@local", sender="bot",
        sender_name="Bot", content="Andy: response", timestamp="t2",
    ))

    msgs, _ = get_new_messages(["g1@local"], "", "Andy")
    assert len(msgs) == 1
    assert msgs[0].content == "Hello"


def test_get_messages_since():
    store_message(NewMessage(
        id="m1", chat_jid="g1@local", sender="u1",
        sender_name="A", content="old", timestamp="t1",
    ))
    store_message(NewMessage(
        id="m2", chat_jid="g1@local", sender="u1",
        sender_name="A", content="new", timestamp="t2",
    ))

    msgs = get_messages_since("g1@local", "t1", "Andy")
    assert len(msgs) == 1
    assert msgs[0].content == "new"


# --- Registered groups ---

def test_registered_groups():
    group = RegisteredGroup(
        name="Test Group", folder="test", trigger="@Andy",
        added_at="2026-01-01", requires_trigger=True,
    )
    set_registered_group("test@local", group)

    all_groups = get_all_registered_groups()
    assert "test@local" in all_groups
    assert all_groups["test@local"].name == "Test Group"
    assert all_groups["test@local"].requires_trigger is True

    single = get_registered_group("test@local")
    assert single is not None
    assert single.folder == "test"


def test_registered_group_not_found():
    assert get_registered_group("nonexistent") is None


# --- Router state ---

def test_router_state():
    set_router_state("last_timestamp", "2026-01-01T00:00:00Z")
    assert get_router_state("last_timestamp") == "2026-01-01T00:00:00Z"
    assert get_router_state("nonexistent") is None


# --- Sessions ---

def test_sessions():
    set_session("main", "sess-123")
    set_session("other", "sess-456")
    sessions = get_all_sessions()
    assert sessions["main"] == "sess-123"
    assert sessions["other"] == "sess-456"


# --- Scheduled tasks ---

def test_create_and_get_task():
    task = ScheduledTask(
        id="task-1", group_folder="main", chat_jid="cli@local",
        prompt="Do something", schedule_type="once",
        schedule_value="2026-06-01T12:00:00Z",
        context_mode="isolated", next_run="2026-06-01T12:00:00Z",
        status="active", created_at="2026-01-01T00:00:00Z",
    )
    create_task(task)

    retrieved = get_task_by_id("task-1")
    assert retrieved is not None
    assert retrieved.prompt == "Do something"

    all_tasks = get_all_tasks()
    assert len(all_tasks) == 1


def test_update_and_delete_task():
    task = ScheduledTask(
        id="task-2", group_folder="main", chat_jid="cli@local",
        prompt="Test", schedule_type="interval", schedule_value="60000",
        status="active", created_at="2026-01-01T00:00:00Z",
    )
    create_task(task)

    update_task("task-2", status="paused")
    t = get_task_by_id("task-2")
    assert t is not None
    assert t.status == "paused"

    delete_task("task-2")
    assert get_task_by_id("task-2") is None


# --- Chat metadata ---

def test_chat_metadata():
    store_chat_metadata("g1@local", "2026-01-01T00:00:00Z", name="Group One")
    chats = get_all_chats()
    assert len(chats) == 1
    assert chats[0]["name"] == "Group One"
