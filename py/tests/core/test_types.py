"""Tests for nanoclaw.types."""

import warnings

from nanoclaw.core.types import (
    AdditionalMount,
    ChannelBinding,
    ContainerConfig,
    Conversation,
    Coworker,
    NewMessage,
    RegisteredGroup,
    Role,
    ScheduledTask,
    TaskRunLog,
    Tenant,
    User,
    registered_group_to_coworker,
)


def test_additional_mount_defaults() -> None:
    mount = AdditionalMount(host_path="/tmp/test")
    assert mount.host_path == "/tmp/test"
    assert mount.container_path is None
    assert mount.readonly is True


def test_container_config_defaults() -> None:
    cfg = ContainerConfig()
    assert cfg.additional_mounts == []
    assert cfg.timeout == 300_000


def test_registered_group() -> None:
    group = RegisteredGroup(name="test", folder="test", trigger="@Andy", added_at="2024-01-01")
    assert group.requires_trigger is True
    assert group.is_main is False
    assert group.container_config is None


def test_new_message() -> None:
    msg = NewMessage(
        id="1",
        chat_jid="chat@jid",
        sender="user@jid",
        sender_name="User",
        content="Hello",
        timestamp="2024-01-01T00:00:00Z",
    )
    assert msg.is_from_me is False
    assert msg.is_bot_message is False


def test_scheduled_task_defaults() -> None:
    task = ScheduledTask(
        id="t1",
        group_folder="test",
        chat_jid="chat@jid",
        prompt="Do something",
        schedule_type="cron",
        schedule_value="0 9 * * *",
        context_mode="group",
    )
    assert task.status == "active"
    assert task.next_run is None
    assert task.coworker_id is None
    assert task.conversation_id is None


def test_task_run_log() -> None:
    log = TaskRunLog(task_id="t1", run_at="2024-01-01T00:00:00Z", duration_ms=1000, status="success")
    assert log.result is None
    assert log.error is None


# --- Multi-tenant types ---


def test_tenant() -> None:
    t = Tenant(id="t1", slug="acme", name="Acme Corp")
    assert t.plan == "starter"
    assert t.max_concurrent_containers == 5
    assert t.config == {}


def test_user() -> None:
    u = User(id="u1", tenant_id="t1", name="Alice")
    assert u.role == "member"
    assert u.email is None
    assert u.channel_ids == {}


def test_role() -> None:
    r = Role(id="r1", tenant_id="t1", name="Ops AI", role_type="operations")
    assert r.agent_backend == "claude-code"
    assert r.system_prompt is None
    assert r.tools == []
    assert r.skills == []


def test_coworker() -> None:
    cw = Coworker(id="cw1", tenant_id="t1", role_id="r1", name="Ops", folder="ops")
    assert cw.is_admin is False
    assert cw.max_concurrent == 2
    assert cw.status == "active"


def test_channel_binding() -> None:
    cb = ChannelBinding(id="cb1", coworker_id="cw1", tenant_id="t1", channel_type="telegram")
    assert cb.credentials == {}
    assert cb.status == "active"


def test_conversation() -> None:
    conv = Conversation(
        id="cv1",
        tenant_id="t1",
        coworker_id="cw1",
        channel_binding_id="cb1",
        channel_chat_id="12345",
    )
    assert conv.requires_trigger is True
    assert conv.is_main is False
    assert conv.trigger_pattern is None


def test_registered_group_to_coworker_telegram() -> None:
    group = RegisteredGroup(
        name="Test Group",
        folder="testgroup",
        trigger="@Andy",
        added_at="2024-01-01",
        is_main=True,
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        cw, binding, conv = registered_group_to_coworker(
            "tg:12345",
            group,
            tenant_id="t1",
            role_id="r1",
            coworker_id="cw1",
            binding_id="b1",
            conversation_id="cv1",
        )
    assert cw.folder == "testgroup"
    assert cw.is_admin is True
    assert binding.channel_type == "telegram"
    assert conv.channel_chat_id == "12345"
    assert conv.trigger_pattern == "@Andy"


def test_registered_group_to_coworker_slack() -> None:
    group = RegisteredGroup(
        name="Slack Group",
        folder="slackgroup",
        trigger="@Andy",
        added_at="2024-01-01",
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        _cw, binding, conv = registered_group_to_coworker(
            "slack:C123",
            group,
            tenant_id="t1",
            role_id="r1",
        )
    assert binding.channel_type == "slack"
    assert conv.channel_chat_id == "C123"
