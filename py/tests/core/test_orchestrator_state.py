"""Tests for nanoclaw.core.orchestrator_state."""

from nanoclaw.core.orchestrator_state import (
    ConversationState,
    CoworkerConfig,
    CoworkerState,
    OrchestratorState,
)
from nanoclaw.core.types import (
    ChannelBinding,
    Conversation,
    Coworker,
    Role,
    Tenant,
)


def _make_role() -> Role:
    return Role(
        id="r1",
        tenant_id="t1",
        name="General",
        role_type="general",
        system_prompt="Be helpful",
        tools=["browser"],
        skills=["search"],
    )


def _make_coworker(name: str = "Ops AI", folder: str = "ops") -> Coworker:
    return Coworker(
        id="cw1",
        tenant_id="t1",
        role_id="r1",
        name=name,
        folder=folder,
        is_admin=False,
        max_concurrent=3,
    )


def test_coworker_config_from_role_and_coworker() -> None:
    role = _make_role()
    cw = _make_coworker()
    config = CoworkerConfig.from_role_and_coworker(role, cw)
    assert config.id == "cw1"
    assert config.tenant_id == "t1"
    assert config.name == "Ops AI"
    assert config.system_prompt == "Be helpful"
    assert config.tools == ["browser"]
    assert config.skills == ["search"]
    assert config.max_concurrent == 3
    assert config.trigger_pattern is not None
    assert config.trigger_pattern.search("@Ops AI hello")


def test_orchestrator_state_three_level_concurrency() -> None:
    state = OrchestratorState(global_limit=2)
    state.tenants["t1"] = Tenant(id="t1", slug="acme", name="Acme", max_concurrent_containers=3)

    role = _make_role()
    cw = _make_coworker()
    config = CoworkerConfig.from_role_and_coworker(role, cw)
    state.coworkers["cw1"] = CoworkerState(config=config)

    assert state.can_start_container("t1", "cw1") is True

    # Hit global limit
    state.increment_active("t1", "cw1")
    state.increment_active("t1", "cw1")
    assert state.can_start_container("t1", "cw1") is False  # global_limit=2

    state.decrement_active("t1", "cw1")
    assert state.can_start_container("t1", "cw1") is True


def test_orchestrator_state_per_coworker_limit() -> None:
    state = OrchestratorState(global_limit=100)
    state.tenants["t1"] = Tenant(id="t1", slug="acme", name="Acme", max_concurrent_containers=100)

    role = _make_role()
    cw = _make_coworker()
    cw_config = CoworkerConfig.from_role_and_coworker(role, cw)
    state.coworkers["cw1"] = CoworkerState(config=cw_config)

    for _ in range(3):
        state.increment_active("t1", "cw1")
    # cw.max_concurrent=3, so 4th should be blocked
    assert state.can_start_container("t1", "cw1") is False


def test_find_coworker_for_conversation() -> None:
    state = OrchestratorState()
    role = _make_role()
    cw = _make_coworker()
    config = CoworkerConfig.from_role_and_coworker(role, cw)
    cw_state = CoworkerState(config=config)
    conv = Conversation(
        id="cv1",
        tenant_id="t1",
        coworker_id="cw1",
        channel_binding_id="b1",
        channel_chat_id="12345",
    )
    cw_state.conversations["12345"] = ConversationState(conversation=conv)
    state.coworkers["cw1"] = cw_state

    result = state.find_coworker_for_conversation("12345")
    assert result is not None
    assert result[0].config.name == "Ops AI"
    assert result[1].conversation.id == "cv1"

    assert state.find_coworker_for_conversation("99999") is None


def test_find_coworker_by_binding() -> None:
    state = OrchestratorState()
    role = _make_role()
    cw = _make_coworker()
    config = CoworkerConfig.from_role_and_coworker(role, cw)
    cw_state = CoworkerState(config=config)
    binding = ChannelBinding(id="b1", coworker_id="cw1", tenant_id="t1", channel_type="telegram")
    cw_state.channel_bindings["telegram"] = binding
    state.coworkers["cw1"] = cw_state

    result = state.find_coworker_by_binding("b1")
    assert result is not None
    assert result.config.id == "cw1"

    assert state.find_coworker_by_binding("nonexistent") is None


def test_decrement_never_goes_negative() -> None:
    state = OrchestratorState()
    state.decrement_active("t1", "cw1")
    assert state.global_active == 0
    assert state.tenant_active.get("t1", 0) == 0
    assert state.coworker_active.get("cw1", 0) == 0
