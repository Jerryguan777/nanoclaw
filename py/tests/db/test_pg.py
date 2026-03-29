"""Tests for nanoclaw.db (PostgreSQL)."""

from __future__ import annotations

import pytest

from nanoclaw.core.types import NewMessage, RegisteredGroup, ScheduledTask
from nanoclaw.db.pg import (
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

pytestmark = pytest.mark.usefixtures("test_db")


async def test_store_and_get_messages() -> None:
    await store_chat_metadata("chat1", "2024-01-01T00:00:00Z", name="Test Chat")
    msg = NewMessage(
        id="m1",
        chat_jid="chat1",
        sender="user1",
        sender_name="Alice",
        content="Hello",
        timestamp="2024-01-01T00:00:01Z",
    )
    await store_message(msg)

    messages, new_ts = await get_new_messages(["chat1"], "2024-01-01T00:00:00Z", "Andy")
    assert len(messages) == 1
    assert messages[0].content == "Hello"
    assert new_ts > "2024-01-01T00:00:00Z"


async def test_get_messages_since() -> None:
    await store_chat_metadata("chat1", "2024-01-01T00:00:00Z")
    await store_message(
        NewMessage(
            id="m1",
            chat_jid="chat1",
            sender="user1",
            sender_name="Alice",
            content="Hello",
            timestamp="2024-01-01T00:00:01Z",
        )
    )
    await store_message(
        NewMessage(
            id="m2",
            chat_jid="chat1",
            sender="user1",
            sender_name="Alice",
            content="World",
            timestamp="2024-01-01T00:00:02Z",
        )
    )
    msgs = await get_messages_since("chat1", "2024-01-01T00:00:00Z", "Andy")
    assert len(msgs) == 2


async def test_router_state() -> None:
    await set_router_state("test_key", "test_value")
    assert await get_router_state("test_key") == "test_value"
    assert await get_router_state("missing") is None


async def test_sessions() -> None:
    await set_session("group1", "sess-123")
    assert await get_session("group1") == "sess-123"
    assert await get_session("missing") is None

    sessions = await get_all_sessions()
    assert sessions["group1"] == "sess-123"


async def test_registered_groups() -> None:
    group = RegisteredGroup(
        name="Test Group",
        folder="testgroup",
        trigger="@Andy",
        added_at="2024-01-01T00:00:00Z",
    )
    await set_registered_group("chat@jid", group)

    groups = await get_all_registered_groups()
    assert "chat@jid" in groups
    assert groups["chat@jid"].name == "Test Group"
    assert groups["chat@jid"].folder == "testgroup"


async def test_task_crud() -> None:
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
    await create_task(task)

    retrieved = await get_task_by_id("t1")
    assert retrieved is not None
    assert retrieved.prompt == "Do something"

    tasks = await get_tasks_for_group("testgroup")
    assert len(tasks) == 1

    all_tasks = await get_all_tasks()
    assert len(all_tasks) == 1

    await update_task("t1", prompt="Updated prompt")
    updated = await get_task_by_id("t1")
    assert updated is not None
    assert updated.prompt == "Updated prompt"

    await delete_task("t1")
    assert await get_task_by_id("t1") is None


async def test_get_due_tasks() -> None:
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
    await create_task(task)

    due = await get_due_tasks()
    assert len(due) >= 1
    assert any(t.id == "t2" for t in due)


async def test_store_chat_metadata_with_channel() -> None:
    await store_chat_metadata("tg:123", "2024-01-01T00:00:00Z", name="TG Chat", channel="telegram", is_group=True)
    from nanoclaw.db.pg import get_all_chats

    chats = await get_all_chats()
    assert any(c.jid == "tg:123" for c in chats)


async def test_store_chat_metadata_no_name() -> None:
    await store_chat_metadata("chat2", "2024-01-01T00:00:00Z")
    from nanoclaw.db.pg import get_all_chats

    chats = await get_all_chats()
    assert any(c.jid == "chat2" for c in chats)


async def test_update_chat_name() -> None:
    from nanoclaw.db.pg import update_chat_name

    await store_chat_metadata("chat3", "2024-01-01T00:00:00Z", name="Old Name")
    await update_chat_name("chat3", "New Name")
    from nanoclaw.db.pg import get_all_chats

    chats = await get_all_chats()
    chat = next(c for c in chats if c.jid == "chat3")
    assert chat.name == "New Name"


async def test_group_sync() -> None:
    from nanoclaw.db.pg import get_last_group_sync, set_last_group_sync

    assert await get_last_group_sync() is None
    await set_last_group_sync()
    assert await get_last_group_sync() is not None


async def test_update_task_after_run() -> None:
    from nanoclaw.db.pg import log_task_run, update_task_after_run

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
    await create_task(task)
    await update_task_after_run("t3", "2024-01-03T09:00:00Z", "Done")
    updated = await get_task_by_id("t3")
    assert updated is not None
    assert updated.last_result == "Done"
    assert updated.next_run == "2024-01-03T09:00:00Z"

    from nanoclaw.core.types import TaskRunLog

    await log_task_run(TaskRunLog(task_id="t3", run_at="2024-01-02T09:00:00Z", duration_ms=500, status="success"))


async def test_update_task_multiple_fields() -> None:
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
    await create_task(task)
    await update_task("t4", prompt="New prompt", status="paused", schedule_value="0 10 * * *")
    updated = await get_task_by_id("t4")
    assert updated is not None
    assert updated.prompt == "New prompt"
    assert updated.status == "paused"


async def test_update_task_no_fields() -> None:
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
    await create_task(task)
    await update_task("t5")  # No fields — should be a no-op


async def test_store_message_direct() -> None:
    from nanoclaw.db.pg import store_message_direct

    await store_chat_metadata("chat5", "2024-01-01T00:00:00Z")
    await store_message_direct(
        id="md1",
        chat_jid="chat5",
        sender="user1",
        sender_name="Alice",
        content="Direct message",
        timestamp="2024-01-01T00:00:01Z",
        is_from_me=False,
    )
    msgs = await get_messages_since("chat5", "2024-01-01T00:00:00Z", "Andy")
    assert len(msgs) == 1


async def test_multi_tenant_crud() -> None:
    """Test tenant, role, coworker, channel binding, and conversation CRUD."""
    from nanoclaw.db.pg import (
        create_channel_binding,
        create_conversation,
        create_coworker,
        create_role,
        create_tenant,
        create_user,
        get_all_channel_bindings,
        get_all_conversations,
        get_all_coworkers,
        get_all_tenants,
        get_channel_binding,
        get_channel_bindings_for_coworker,
        get_conversation,
        get_conversation_by_binding_chat,
        get_conversations_for_coworker,
        get_coworker,
        get_coworkers_for_tenant,
        get_role,
        get_roles_for_tenant,
        get_tenant,
        get_tenant_by_slug,
        get_users_for_tenant,
    )

    # Tenant
    tenant = await create_tenant(slug="acme", name="Acme Corp")
    assert tenant.slug == "acme"
    assert tenant.plan == "starter"

    t = await get_tenant(tenant.id)
    assert t is not None
    assert t.name == "Acme Corp"

    t_by_slug = await get_tenant_by_slug("acme")
    assert t_by_slug is not None
    assert t_by_slug.id == tenant.id

    all_tenants = await get_all_tenants()
    assert any(t.id == tenant.id for t in all_tenants)

    # User
    user = await create_user(tenant_id=tenant.id, name="Alice", email="alice@acme.com", role="admin")
    assert user.name == "Alice"
    assert user.role == "admin"

    users = await get_users_for_tenant(tenant.id)
    assert len(users) == 1

    # Role
    role = await create_role(
        tenant_id=tenant.id,
        name="General",
        role_type="general",
        system_prompt="Be helpful",
        tools=["browser"],
    )
    assert role.name == "General"
    assert role.tools == ["browser"]

    r = await get_role(role.id)
    assert r is not None
    assert r.system_prompt == "Be helpful"

    roles = await get_roles_for_tenant(tenant.id)
    assert len(roles) == 1

    # Coworker
    coworker = await create_coworker(
        tenant_id=tenant.id,
        role_id=role.id,
        name="Ops AI",
        folder="ops-ai",
        is_admin=True,
    )
    assert coworker.name == "Ops AI"
    assert coworker.is_admin is True

    cw = await get_coworker(coworker.id)
    assert cw is not None
    assert cw.folder == "ops-ai"

    cws = await get_coworkers_for_tenant(tenant.id)
    assert len(cws) == 1

    all_cws = await get_all_coworkers()
    assert any(c.id == coworker.id for c in all_cws)

    # Channel Binding
    binding = await create_channel_binding(
        coworker_id=coworker.id,
        tenant_id=tenant.id,
        channel_type="telegram",
        credentials={"bot_token": "test-token"},
    )
    assert binding.channel_type == "telegram"
    assert binding.credentials["bot_token"] == "test-token"

    b = await get_channel_binding(binding.id)
    assert b is not None

    bs = await get_channel_bindings_for_coworker(coworker.id)
    assert len(bs) == 1

    all_bs = await get_all_channel_bindings(tenant.id)
    assert len(all_bs) == 1

    # Conversation
    conv = await create_conversation(
        tenant_id=tenant.id,
        coworker_id=coworker.id,
        channel_binding_id=binding.id,
        channel_chat_id="12345",
        name="Test Group",
        trigger_pattern="@Ops",
        is_main=True,
    )
    assert conv.channel_chat_id == "12345"
    assert conv.is_main is True

    c = await get_conversation(conv.id)
    assert c is not None
    assert c.name == "Test Group"

    cs = await get_conversations_for_coworker(coworker.id)
    assert len(cs) == 1

    c_by_bc = await get_conversation_by_binding_chat(binding.id, "12345")
    assert c_by_bc is not None
    assert c_by_bc.id == conv.id

    all_convs = await get_all_conversations(tenant.id)
    assert len(all_convs) == 1


async def test_session_new_format() -> None:
    """Test set_session_new for multi-tenant sessions."""
    from nanoclaw.db.pg import (
        create_channel_binding,
        create_conversation,
        create_coworker,
        create_role,
        create_tenant,
        set_session_new,
    )

    tenant = await create_tenant(slug="sesstest", name="Sess Test")
    role = await create_role(tenant_id=tenant.id, name="general", role_type="general")
    coworker = await create_coworker(
        tenant_id=tenant.id, role_id=role.id, name="Test", folder="sesstest"
    )
    binding = await create_channel_binding(
        coworker_id=coworker.id, tenant_id=tenant.id, channel_type="telegram"
    )
    conv = await create_conversation(
        tenant_id=tenant.id,
        coworker_id=coworker.id,
        channel_binding_id=binding.id,
        channel_chat_id="99999",
    )

    await set_session_new(conv.id, tenant.id, coworker.id, "session-xyz")
    # Verify it was stored (using the session table)
    from nanoclaw.db.pg import _get_pool

    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT session_id FROM sessions WHERE conversation_id = $1::uuid", conv.id
        )
    assert row is not None
    assert row["session_id"] == "session-xyz"


async def test_registered_group_with_config() -> None:
    from nanoclaw.core.types import ContainerConfig

    group = RegisteredGroup(
        name="Config Group",
        folder="configgroup",
        trigger="@Andy",
        added_at="2024-01-01T00:00:00Z",
        container_config=ContainerConfig(timeout=600000),
        is_main=True,
    )
    await set_registered_group("config@jid", group)
    groups = await get_all_registered_groups()
    assert "config@jid" in groups
    assert groups["config@jid"].is_main is True
