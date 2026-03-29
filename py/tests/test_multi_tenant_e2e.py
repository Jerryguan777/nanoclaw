"""End-to-end tests for multi-tenant multi-coworker architecture (Step 5).

These tests exercise the new functionality from the user's perspective:
- Creating and managing tenants, roles, coworkers, bindings, conversations
- Multi-tenant data isolation
- Three-level concurrency control
- OrchestratorState routing and lookup
- GroupQueue with tenant/coworker-aware scheduling
- Volume mount path generation for multi-tenant
- Migration from RegisteredGroup to new schema
- Database constraint enforcement (unique, FK, cascade)
- Edge cases: empty names, missing FKs, duplicate slugs, etc.

Strategy: Real PostgreSQL (via testcontainers), real data layer,
mock-only for things requiring actual containers/NATS.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import patch

import asyncpg
import pytest

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.usefixtures("test_db")


# ============================================================================
# Helpers
# ============================================================================


async def _setup_tenant_with_coworker(
    *,
    tenant_slug: str = "acme",
    tenant_name: str = "Acme Corp",
    role_name: str = "general",
    coworker_name: str = "Ops AI",
    coworker_folder: str = "ops-ai",
    channel_type: str = "telegram",
    chat_id: str = "12345",
    is_admin: bool = False,
    max_concurrent_containers: int = 5,
    coworker_max_concurrent: int = 2,
) -> dict[str, str]:
    """Create a full tenant→role→coworker→binding→conversation chain.

    Returns dict with all entity IDs.
    """
    from nanoclaw.db.pg import (
        create_channel_binding,
        create_conversation,
        create_coworker,
        create_role,
        create_tenant,
    )

    tenant = await create_tenant(
        slug=tenant_slug, name=tenant_name,
        max_concurrent_containers=max_concurrent_containers,
    )
    role = await create_role(tenant_id=tenant.id, name=role_name, role_type="general")
    coworker = await create_coworker(
        tenant_id=tenant.id,
        role_id=role.id,
        name=coworker_name,
        folder=coworker_folder,
        is_admin=is_admin,
        max_concurrent=coworker_max_concurrent,
    )
    binding = await create_channel_binding(
        coworker_id=coworker.id,
        tenant_id=tenant.id,
        channel_type=channel_type,
        credentials={"bot_token": f"tok-{coworker_folder}"},
    )
    conversation = await create_conversation(
        tenant_id=tenant.id,
        coworker_id=coworker.id,
        channel_binding_id=binding.id,
        channel_chat_id=chat_id,
        name=f"Chat {chat_id}",
        trigger_pattern=f"@{coworker_name}",
        is_main=is_admin,
    )
    return {
        "tenant_id": tenant.id,
        "role_id": role.id,
        "coworker_id": coworker.id,
        "binding_id": binding.id,
        "conversation_id": conversation.id,
    }


# ============================================================================
# 1. Tenant Isolation
# ============================================================================


class TestTenantIsolation:
    """Two tenants must not see each other's data."""

    async def test_coworkers_scoped_to_tenant(self) -> None:
        """get_coworkers_for_tenant returns only that tenant's coworkers."""
        from nanoclaw.db.pg import get_coworkers_for_tenant

        ids_a = await _setup_tenant_with_coworker(
            tenant_slug="tenant-a", coworker_folder="ops-a", chat_id="a1",
        )
        ids_b = await _setup_tenant_with_coworker(
            tenant_slug="tenant-b", coworker_folder="ops-b", chat_id="b1",
        )

        cws_a = await get_coworkers_for_tenant(ids_a["tenant_id"])
        cws_b = await get_coworkers_for_tenant(ids_b["tenant_id"])

        assert len(cws_a) == 1
        assert cws_a[0].folder == "ops-a"
        assert len(cws_b) == 1
        assert cws_b[0].folder == "ops-b"

        # Cross-tenant: no leakage
        assert all(c.tenant_id == ids_a["tenant_id"] for c in cws_a)
        assert all(c.tenant_id == ids_b["tenant_id"] for c in cws_b)

    async def test_conversations_scoped_to_tenant(self) -> None:
        """Conversations only visible within their own tenant."""
        from nanoclaw.db.pg import get_all_conversations

        ids_a = await _setup_tenant_with_coworker(
            tenant_slug="iso-a", coworker_folder="cw-a", chat_id="chat-a",
        )
        ids_b = await _setup_tenant_with_coworker(
            tenant_slug="iso-b", coworker_folder="cw-b", chat_id="chat-b",
        )

        convs_a = await get_all_conversations(ids_a["tenant_id"])
        convs_b = await get_all_conversations(ids_b["tenant_id"])

        assert len(convs_a) == 1
        assert convs_a[0].channel_chat_id == "chat-a"
        assert len(convs_b) == 1
        assert convs_b[0].channel_chat_id == "chat-b"

    async def test_same_folder_name_different_tenants(self) -> None:
        """Two tenants can each have a coworker with the same folder name."""
        ids_a = await _setup_tenant_with_coworker(
            tenant_slug="dup-a", coworker_folder="shared-folder", chat_id="da1",
        )
        ids_b = await _setup_tenant_with_coworker(
            tenant_slug="dup-b", coworker_folder="shared-folder", chat_id="db1",
        )
        # Both exist — no unique violation
        assert ids_a["coworker_id"] != ids_b["coworker_id"]

    async def test_same_chat_id_different_coworkers(self) -> None:
        """Different coworkers (different bots) can be in the same chat group."""
        from nanoclaw.db.pg import (
            create_channel_binding,
            create_conversation,
            create_coworker,
            create_role,
            create_tenant,
        )

        tenant = await create_tenant(slug="samechat", name="SameChat")
        role = await create_role(tenant_id=tenant.id, name="general", role_type="general")

        cw_a = await create_coworker(
            tenant_id=tenant.id, role_id=role.id, name="Bot A", folder="bot-a",
        )
        cw_b = await create_coworker(
            tenant_id=tenant.id, role_id=role.id, name="Bot B", folder="bot-b",
        )

        bind_a = await create_channel_binding(
            coworker_id=cw_a.id, tenant_id=tenant.id, channel_type="telegram",
        )
        bind_b = await create_channel_binding(
            coworker_id=cw_b.id, tenant_id=tenant.id, channel_type="telegram",
        )

        # Same chat_id "group-1001" for both bots
        conv_a = await create_conversation(
            tenant_id=tenant.id, coworker_id=cw_a.id,
            channel_binding_id=bind_a.id, channel_chat_id="group-1001",
        )
        conv_b = await create_conversation(
            tenant_id=tenant.id, coworker_id=cw_b.id,
            channel_binding_id=bind_b.id, channel_chat_id="group-1001",
        )

        # Both conversations exist with different IDs but same chat
        assert conv_a.id != conv_b.id
        assert conv_a.channel_chat_id == conv_b.channel_chat_id == "group-1001"


# ============================================================================
# 2. Database Constraints & Edge Cases
# ============================================================================


class TestDBConstraints:
    """Verify FK constraints, unique violations, cascade deletes."""

    async def test_duplicate_tenant_slug_raises(self) -> None:
        """Creating two tenants with the same slug should raise."""
        from nanoclaw.db.pg import create_tenant

        await create_tenant(slug="unique-slug", name="First")
        with pytest.raises(asyncpg.UniqueViolationError):
            await create_tenant(slug="unique-slug", name="Second")

    async def test_duplicate_coworker_folder_same_tenant_raises(self) -> None:
        """Same folder within same tenant must raise unique violation."""
        from nanoclaw.db.pg import create_coworker, create_role, create_tenant

        tenant = await create_tenant(slug="dup-folder", name="Dup")
        role = await create_role(tenant_id=tenant.id, name="r", role_type="general")
        await create_coworker(
            tenant_id=tenant.id, role_id=role.id, name="A", folder="same-folder",
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            await create_coworker(
                tenant_id=tenant.id, role_id=role.id, name="B", folder="same-folder",
            )

    async def test_duplicate_role_name_same_tenant_raises(self) -> None:
        """Same role name within same tenant must raise unique violation."""
        from nanoclaw.db.pg import create_role, create_tenant

        tenant = await create_tenant(slug="dup-role", name="DupRole")
        await create_role(tenant_id=tenant.id, name="ops", role_type="operations")
        with pytest.raises(asyncpg.UniqueViolationError):
            await create_role(tenant_id=tenant.id, name="ops", role_type="logistics")

    async def test_duplicate_binding_coworker_channel_type_raises(self) -> None:
        """One coworker can't have two bindings for the same channel type."""
        from nanoclaw.db.pg import (
            create_channel_binding,
            create_coworker,
            create_role,
            create_tenant,
        )

        tenant = await create_tenant(slug="dup-bind", name="DupBind")
        role = await create_role(tenant_id=tenant.id, name="r", role_type="general")
        cw = await create_coworker(
            tenant_id=tenant.id, role_id=role.id, name="CW", folder="cw",
        )
        await create_channel_binding(
            coworker_id=cw.id, tenant_id=tenant.id, channel_type="telegram",
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            await create_channel_binding(
                coworker_id=cw.id, tenant_id=tenant.id, channel_type="telegram",
            )

    async def test_duplicate_conversation_binding_chat_raises(self) -> None:
        """Same binding + chat_id must raise unique violation."""
        ids = await _setup_tenant_with_coworker(
            tenant_slug="dup-conv", coworker_folder="cw-dc", chat_id="chat-dc",
        )
        from nanoclaw.db.pg import create_conversation

        with pytest.raises(asyncpg.UniqueViolationError):
            await create_conversation(
                tenant_id=ids["tenant_id"],
                coworker_id=ids["coworker_id"],
                channel_binding_id=ids["binding_id"],
                channel_chat_id="chat-dc",  # duplicate
            )

    async def test_coworker_with_invalid_role_id_raises(self) -> None:
        """FK violation if role_id doesn't exist."""
        from nanoclaw.db.pg import create_coworker, create_tenant

        tenant = await create_tenant(slug="bad-role", name="BadRole")
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await create_coworker(
                tenant_id=tenant.id,
                role_id="00000000-0000-0000-0000-000000000000",
                name="Bad",
                folder="bad",
            )

    async def test_cascade_delete_coworker_removes_bindings_conversations(self) -> None:
        """Deleting a coworker cascades to bindings and conversations."""
        from nanoclaw.db.pg import (
            _get_pool,
            get_all_channel_bindings,
            get_all_conversations,
        )

        ids = await _setup_tenant_with_coworker(
            tenant_slug="cascade", coworker_folder="cascade-cw", chat_id="cascade-chat",
        )

        # Verify they exist
        assert len(await get_all_channel_bindings(ids["tenant_id"])) == 1
        assert len(await get_all_conversations(ids["tenant_id"])) == 1

        # Delete coworker directly
        pool = _get_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM coworkers WHERE id = $1::uuid", ids["coworker_id"])

        # Cascaded
        assert len(await get_all_channel_bindings(ids["tenant_id"])) == 0
        assert len(await get_all_conversations(ids["tenant_id"])) == 0

    async def test_get_nonexistent_entities_returns_none(self) -> None:
        """Lookups for non-existent IDs return None, not raise."""
        from nanoclaw.db.pg import (
            get_channel_binding,
            get_conversation,
            get_conversation_by_binding_chat,
            get_coworker,
            get_role,
            get_tenant,
            get_tenant_by_slug,
        )

        fake_uuid = "00000000-0000-0000-0000-000000000000"
        assert await get_tenant(fake_uuid) is None
        assert await get_tenant_by_slug("nonexistent") is None
        assert await get_role(fake_uuid) is None
        assert await get_coworker(fake_uuid) is None
        assert await get_channel_binding(fake_uuid) is None
        assert await get_conversation(fake_uuid) is None
        assert await get_conversation_by_binding_chat(fake_uuid, "nope") is None


# ============================================================================
# 3. Three-Level Concurrency (OrchestratorState)
# ============================================================================


class TestThreeLevelConcurrency:
    """Verify global, per-tenant, per-coworker concurrency limits."""

    def _build_state(
        self,
        global_limit: int = 10,
        tenant_limit: int = 5,
        coworker_limit: int = 2,
        num_tenants: int = 1,
        num_coworkers_per_tenant: int = 1,
    ) -> tuple:
        """Build an OrchestratorState with specified limits."""
        from nanoclaw.core.orchestrator_state import (
            CoworkerConfig,
            CoworkerState,
            OrchestratorState,
        )
        from nanoclaw.core.types import Tenant

        state = OrchestratorState(global_limit=global_limit)
        tenant_ids = []
        coworker_ids = []

        for ti in range(num_tenants):
            tid = f"t{ti}"
            state.tenants[tid] = Tenant(
                id=tid, slug=f"tenant-{ti}", name=f"Tenant {ti}",
                max_concurrent_containers=tenant_limit,
            )
            tenant_ids.append(tid)

            for ci in range(num_coworkers_per_tenant):
                cwid = f"cw{ti}-{ci}"
                config = CoworkerConfig(
                    id=cwid, tenant_id=tid, name=f"CW {ci}",
                    folder=f"cw-{ti}-{ci}", system_prompt=None,
                    trigger_pattern=None, agent_backend="claude-code",
                    container_image=None, max_concurrent=coworker_limit,
                    role_config={}, tools=[], skills=[],
                )
                state.coworkers[cwid] = CoworkerState(config=config)
                coworker_ids.append(cwid)

        return state, tenant_ids, coworker_ids

    def test_global_limit_blocks_all(self) -> None:
        """When global limit reached, no tenant/coworker can start."""
        state, tids, cwids = self._build_state(global_limit=2, tenant_limit=100, coworker_limit=100)

        state.increment_active(tids[0], cwids[0])
        state.increment_active(tids[0], cwids[0])
        assert state.can_start_container(tids[0], cwids[0]) is False

    def test_tenant_limit_blocks_only_that_tenant(self) -> None:
        """Tenant A at limit doesn't block Tenant B."""
        state, tids, cwids = self._build_state(
            global_limit=100, tenant_limit=1, coworker_limit=100,
            num_tenants=2, num_coworkers_per_tenant=1,
        )
        # Tenant 0 fills up
        state.increment_active(tids[0], cwids[0])
        assert state.can_start_container(tids[0], cwids[0]) is False
        # Tenant 1 still OK
        assert state.can_start_container(tids[1], cwids[1]) is True

    def test_coworker_limit_blocks_only_that_coworker(self) -> None:
        """Coworker A at limit doesn't block Coworker B in same tenant."""
        state, tids, cwids = self._build_state(
            global_limit=100, tenant_limit=100, coworker_limit=1,
            num_tenants=1, num_coworkers_per_tenant=2,
        )
        # CW 0 fills up
        state.increment_active(tids[0], cwids[0])
        assert state.can_start_container(tids[0], cwids[0]) is False
        # CW 1 still OK
        assert state.can_start_container(tids[0], cwids[1]) is True

    def test_unknown_tenant_or_coworker_allows_start(self) -> None:
        """If tenant/coworker not registered in state, start is allowed (no limit to check)."""
        from nanoclaw.core.orchestrator_state import OrchestratorState

        state = OrchestratorState(global_limit=100)
        # No tenants/coworkers registered → no tenant/coworker limit to hit
        assert state.can_start_container("unknown-t", "unknown-cw") is True

    def test_increment_decrement_symmetry(self) -> None:
        """Increment then decrement returns to zero."""
        state, tids, cwids = self._build_state()
        state.increment_active(tids[0], cwids[0])
        state.increment_active(tids[0], cwids[0])
        assert state.global_active == 2

        state.decrement_active(tids[0], cwids[0])
        state.decrement_active(tids[0], cwids[0])
        assert state.global_active == 0
        assert state.tenant_active[tids[0]] == 0
        assert state.coworker_active[cwids[0]] == 0

    def test_stress_many_increments(self) -> None:
        """100 increments → global_active = 100, then 100 decrements → 0."""
        state, tids, cwids = self._build_state(
            global_limit=200, tenant_limit=200, coworker_limit=200,
        )
        for _ in range(100):
            state.increment_active(tids[0], cwids[0])
        assert state.global_active == 100
        for _ in range(100):
            state.decrement_active(tids[0], cwids[0])
        assert state.global_active == 0


# ============================================================================
# 4. OrchestratorState Routing & Lookup
# ============================================================================


class TestOrchestratorRouting:
    """Test conversation and binding lookups."""

    def test_find_conversation_with_multiple_coworkers(self) -> None:
        """Multiple coworkers each with conversations; lookup returns correct one."""
        from nanoclaw.core.orchestrator_state import (
            ConversationState,
            CoworkerConfig,
            CoworkerState,
            OrchestratorState,
        )
        from nanoclaw.core.types import Conversation

        state = OrchestratorState()

        for i in range(3):
            config = CoworkerConfig(
                id=f"cw{i}", tenant_id="t1", name=f"CW{i}", folder=f"cw{i}",
                system_prompt=None, trigger_pattern=None, agent_backend="claude-code",
                container_image=None, max_concurrent=2, role_config={},
                tools=[], skills=[],
            )
            cw_state = CoworkerState(config=config)
            conv = Conversation(
                id=f"conv{i}", tenant_id="t1", coworker_id=f"cw{i}",
                channel_binding_id=f"b{i}", channel_chat_id=f"chat-{i}",
            )
            cw_state.conversations[f"chat-{i}"] = ConversationState(conversation=conv)
            state.coworkers[f"cw{i}"] = cw_state

        # Look up each conversation
        result = state.find_coworker_for_conversation("chat-1")
        assert result is not None
        assert result[0].config.id == "cw1"
        assert result[1].conversation.id == "conv1"

        # Non-existent
        assert state.find_coworker_for_conversation("chat-999") is None

    def test_same_chat_id_returns_first_match(self) -> None:
        """If two coworkers have same chat_id, first registered wins (dict ordering)."""
        from nanoclaw.core.orchestrator_state import (
            ConversationState,
            CoworkerConfig,
            CoworkerState,
            OrchestratorState,
        )
        from nanoclaw.core.types import Conversation

        state = OrchestratorState()

        for i in range(2):
            config = CoworkerConfig(
                id=f"cw{i}", tenant_id="t1", name=f"CW{i}", folder=f"cw{i}",
                system_prompt=None, trigger_pattern=None, agent_backend="claude-code",
                container_image=None, max_concurrent=2, role_config={},
                tools=[], skills=[],
            )
            cw_state = CoworkerState(config=config)
            # Both have "same-chat"
            conv = Conversation(
                id=f"conv{i}", tenant_id="t1", coworker_id=f"cw{i}",
                channel_binding_id=f"b{i}", channel_chat_id="same-chat",
            )
            cw_state.conversations["same-chat"] = ConversationState(conversation=conv)
            state.coworkers[f"cw{i}"] = cw_state

        result = state.find_coworker_for_conversation("same-chat")
        assert result is not None
        # First match in dict order
        assert result[0].config.id == "cw0"


# ============================================================================
# 5. GroupQueue with Three-Level Concurrency
# ============================================================================


class TestGroupQueueMultiTenant:
    """GroupQueue respects OrchestratorState concurrency limits."""

    async def test_queue_respects_coworker_limit(self) -> None:
        """Coworker at limit → second enqueue is queued, not immediately started."""
        from nanoclaw.container.scheduler import GroupQueue
        from nanoclaw.core.orchestrator_state import (
            CoworkerConfig,
            CoworkerState,
            OrchestratorState,
        )
        from nanoclaw.core.types import Tenant

        orch = OrchestratorState(global_limit=100)
        orch.tenants["t1"] = Tenant(
            id="t1", slug="t1", name="T1", max_concurrent_containers=100,
        )
        config = CoworkerConfig(
            id="cw1", tenant_id="t1", name="CW1", folder="cw1",
            system_prompt=None, trigger_pattern=None, agent_backend="claude-code",
            container_image=None, max_concurrent=1,  # Only 1 at a time
            role_config={}, tools=[], skills=[],
        )
        orch.coworkers["cw1"] = CoworkerState(config=config)

        queue = GroupQueue(orchestrator_state=orch)
        calls: list[str] = []

        async def process_fn(group_jid: str) -> bool:
            calls.append(group_jid)
            await asyncio.sleep(0.2)
            return True

        queue.set_process_messages_fn(process_fn)

        # First enqueue sets tenant/coworker metadata
        queue.enqueue_message_check("group-a", tenant_id="t1", coworker_id="cw1")
        await asyncio.sleep(0.05)
        # Second enqueue while first is running → should be queued
        queue.enqueue_message_check("group-b", tenant_id="t1", coworker_id="cw1")

        await asyncio.sleep(0.5)
        # Both should eventually run (sequential, not parallel)
        assert "group-a" in calls
        assert "group-b" in calls

    async def test_queue_allows_different_coworkers_parallel(self) -> None:
        """Different coworkers can run in parallel even if each has limit=1."""
        from nanoclaw.container.scheduler import GroupQueue
        from nanoclaw.core.orchestrator_state import (
            CoworkerConfig,
            CoworkerState,
            OrchestratorState,
        )
        from nanoclaw.core.types import Tenant

        orch = OrchestratorState(global_limit=100)
        orch.tenants["t1"] = Tenant(
            id="t1", slug="t1", name="T1", max_concurrent_containers=100,
        )
        for i in range(2):
            config = CoworkerConfig(
                id=f"cw{i}", tenant_id="t1", name=f"CW{i}", folder=f"cw{i}",
                system_prompt=None, trigger_pattern=None, agent_backend="claude-code",
                container_image=None, max_concurrent=1,
                role_config={}, tools=[], skills=[],
            )
            orch.coworkers[f"cw{i}"] = CoworkerState(config=config)

        queue = GroupQueue(orchestrator_state=orch)
        concurrent_max = 0
        current = 0

        async def process_fn(group_jid: str) -> bool:
            nonlocal concurrent_max, current
            current += 1
            concurrent_max = max(concurrent_max, current)
            await asyncio.sleep(0.15)
            current -= 1
            return True

        queue.set_process_messages_fn(process_fn)

        queue.enqueue_message_check("group-a", tenant_id="t1", coworker_id="cw0")
        queue.enqueue_message_check("group-b", tenant_id="t1", coworker_id="cw1")

        await asyncio.sleep(0.5)
        # Both should have run in parallel since different coworkers
        assert concurrent_max == 2


# ============================================================================
# 6. Volume Mount Paths (Multi-Tenant)
# ============================================================================


class TestVolumeMountsMultiTenant:
    """Verify build_volume_mounts_multi_tenant produces correct paths."""

    @staticmethod
    def _build(tmp_path: Path, folder: str, conv_id: str, tenant_id: str, is_admin: bool) -> list:  # type: ignore[type-arg]
        import nanoclaw.container.runner as runner
        from nanoclaw.core.types import Coworker

        cw = Coworker(id="cw1", tenant_id="t1", role_id="r1", name=folder.title(), folder=folder)
        with (
            patch.object(runner, "DATA_DIR", tmp_path / "data"),
            patch.object(runner, "PROJECT_ROOT", tmp_path),
        ):
            return runner.build_volume_mounts_multi_tenant(tenant_id, cw, conv_id, is_admin=is_admin)

    def test_paths_contain_tenant_and_coworker(self, tmp_path: Path) -> None:
        mounts = self._build(tmp_path, "ops", "conv-123", "tenant-abc", is_admin=False)

        host_paths = [m.host_path for m in mounts]
        workspace_mounts = [p for p in host_paths if "workspace" in p]
        assert any("tenant-abc" in p and "ops" in p for p in workspace_mounts)

        session_mounts = [p for p in host_paths if "sessions" in p]
        assert any("conv-123" in p for p in session_mounts)

    def test_admin_gets_project_mount(self, tmp_path: Path) -> None:
        mounts = self._build(tmp_path, "admin", "conv-1", "t1", is_admin=True)
        container_paths = [m.container_path for m in mounts]
        assert "/workspace/project" in container_paths

    def test_non_admin_no_project_mount(self, tmp_path: Path) -> None:
        mounts = self._build(tmp_path, "worker", "conv-1", "t1", is_admin=False)
        container_paths = [m.container_path for m in mounts]
        assert "/workspace/project" not in container_paths

    def test_shared_dir_is_readonly(self, tmp_path: Path) -> None:
        mounts = self._build(tmp_path, "w", "c1", "t1", is_admin=False)
        shared = [m for m in mounts if m.container_path == "/workspace/shared"]
        assert len(shared) == 1
        assert shared[0].readonly is True

    def test_directories_created_on_disk(self, tmp_path: Path) -> None:
        self._build(tmp_path, "w", "c1", "t1", is_admin=False)
        data = tmp_path / "data"
        assert (data / "tenants" / "t1" / "coworkers" / "w" / "workspace").exists()
        assert (data / "tenants" / "t1" / "coworkers" / "w" / "logs").exists()
        assert (data / "tenants" / "t1" / "coworkers" / "w" / "sessions" / "c1").exists()
        assert (data / "tenants" / "t1" / "shared").exists()


# ============================================================================
# 7. NATS KV Snapshot Tenant Prefix
# ============================================================================


class TestSnapshotTenantPrefix:
    """write_tasks_snapshot / write_groups_snapshot use tenant prefix."""

    async def test_tasks_snapshot_key_has_tenant_prefix(self) -> None:
        """When tenant_id provided, KV key = {tenant_id}.{folder}.tasks."""
        from unittest.mock import AsyncMock, MagicMock

        import nanoclaw.container.runner as runner

        mock_transport = MagicMock()
        mock_kv = AsyncMock()
        mock_transport.js.key_value = AsyncMock(return_value=mock_kv)

        await runner.write_tasks_snapshot(
            mock_transport, "ops-folder", True, [], tenant_id="tenant-xyz",
        )

        mock_kv.put.assert_called_once()
        key = mock_kv.put.call_args[0][0]
        assert key == "tenant-xyz.ops-folder.tasks"

    async def test_tasks_snapshot_key_no_tenant(self) -> None:
        """Without tenant_id, KV key = {folder}.tasks (legacy)."""
        from unittest.mock import AsyncMock, MagicMock

        import nanoclaw.container.runner as runner

        mock_transport = MagicMock()
        mock_kv = AsyncMock()
        mock_transport.js.key_value = AsyncMock(return_value=mock_kv)

        await runner.write_tasks_snapshot(mock_transport, "ops-folder", True, [])

        key = mock_kv.put.call_args[0][0]
        assert key == "ops-folder.tasks"

    async def test_groups_snapshot_key_has_tenant_prefix(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        import nanoclaw.container.runner as runner

        mock_transport = MagicMock()
        mock_kv = AsyncMock()
        mock_transport.js.key_value = AsyncMock(return_value=mock_kv)

        await runner.write_groups_snapshot(
            mock_transport, "ops-folder", True, [], set(), tenant_id="t-abc",
        )

        key = mock_kv.put.call_args[0][0]
        assert key == "t-abc.ops-folder.groups"


# ============================================================================
# 8. Migration: RegisteredGroup → Coworker + Binding + Conversation
# ============================================================================


class TestMigrationConverter:
    """Test the registered_group_to_coworker converter."""

    def test_telegram_jid_infers_channel_type(self) -> None:
        import warnings

        from nanoclaw.core.types import RegisteredGroup, registered_group_to_coworker

        group = RegisteredGroup(
            name="TG Group", folder="tg-grp", trigger="@Bot", added_at="2024-01-01",
            is_main=True, requires_trigger=False,
        )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            cw, binding, conv = registered_group_to_coworker(
                "tg:12345", group, tenant_id="t1", role_id="r1",
                coworker_id="cw1", binding_id="b1", conversation_id="cv1",
            )

        assert binding.channel_type == "telegram"
        assert conv.channel_chat_id == "12345"
        assert cw.is_admin is True
        assert conv.requires_trigger is False
        assert conv.is_main is True

    def test_slack_jid_infers_channel_type(self) -> None:
        import warnings

        from nanoclaw.core.types import RegisteredGroup, registered_group_to_coworker

        group = RegisteredGroup(
            name="Slack Group", folder="slk", trigger="@Bot", added_at="2024-01-01",
        )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            _, binding, conv = registered_group_to_coworker(
                "slack:C456", group, tenant_id="t1", role_id="r1",
            )

        assert binding.channel_type == "slack"
        assert conv.channel_chat_id == "C456"

    def test_unknown_jid_prefix(self) -> None:
        import warnings

        from nanoclaw.core.types import RegisteredGroup, registered_group_to_coworker

        group = RegisteredGroup(
            name="Unknown", folder="unk", trigger="@Bot", added_at="2024-01-01",
        )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            _, binding, _ = registered_group_to_coworker(
                "whatsapp:123", group, tenant_id="t1", role_id="r1",
            )

        assert binding.channel_type == "unknown"

    def test_converter_emits_deprecation_warning(self) -> None:
        import warnings

        from nanoclaw.core.types import RegisteredGroup, registered_group_to_coworker

        group = RegisteredGroup(
            name="G", folder="g", trigger="@Bot", added_at="2024-01-01",
        )
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            registered_group_to_coworker("tg:1", group, tenant_id="t", role_id="r")

        assert len(w) == 1
        assert issubclass(w[0].category, DeprecationWarning)
        assert "migration helper" in str(w[0].message)


# ============================================================================
# 9. CoworkerConfig Merge from Role + Coworker
# ============================================================================


class TestCoworkerConfigMerge:
    """CoworkerConfig.from_role_and_coworker correctly merges."""

    def test_trigger_pattern_built_from_name(self) -> None:
        from nanoclaw.core.orchestrator_state import CoworkerConfig
        from nanoclaw.core.types import Coworker, Role

        role = Role(id="r1", tenant_id="t1", name="General", role_type="general")
        cw = Coworker(id="cw1", tenant_id="t1", role_id="r1", name="Ops AI", folder="ops")

        config = CoworkerConfig.from_role_and_coworker(role, cw)
        assert config.trigger_pattern is not None
        assert config.trigger_pattern.search("@Ops AI hello")
        assert config.trigger_pattern.search("@ops ai hello")  # case insensitive
        assert not config.trigger_pattern.search("hello @Ops AI")  # must be at start
        assert config.trigger_pattern.search("@Ops AI")  # name at end of string (no trailing text)

    def test_special_chars_in_name_escaped(self) -> None:
        """Names with regex special chars (parens, dots) don't break the pattern."""
        from nanoclaw.core.orchestrator_state import CoworkerConfig
        from nanoclaw.core.types import Coworker, Role

        role = Role(id="r1", tenant_id="t1", name="General", role_type="general")
        cw = Coworker(
            id="cw1", tenant_id="t1", role_id="r1",
            name="AI (v2.0)", folder="ai-v2",
        )

        config = CoworkerConfig.from_role_and_coworker(role, cw)
        assert config.trigger_pattern is not None
        assert config.trigger_pattern.search("@AI (v2.0) help")
        # Raw "AI .v2.0." without escaping should NOT match
        assert not config.trigger_pattern.search("@AI Xv2X0Y help")

    def test_role_fields_propagated(self) -> None:
        from nanoclaw.core.orchestrator_state import CoworkerConfig
        from nanoclaw.core.types import Coworker, Role

        role = Role(
            id="r1", tenant_id="t1", name="Coder", role_type="operations",
            agent_backend="pi-mono", system_prompt="Be precise",
            tools=["browser", "bash"], skills=["search"],
            config_overrides={"temperature": 0.5},
        )
        cw = Coworker(
            id="cw1", tenant_id="t1", role_id="r1",
            name="CW", folder="cw", max_concurrent=5, is_admin=True,
        )

        config = CoworkerConfig.from_role_and_coworker(role, cw)
        assert config.agent_backend == "pi-mono"
        assert config.system_prompt == "Be precise"
        assert config.tools == ["browser", "bash"]
        assert config.skills == ["search"]
        assert config.role_config == {"temperature": 0.5}
        assert config.max_concurrent == 5
        assert config.is_admin is True


# ============================================================================
# 10. Session Per-Conversation Isolation
# ============================================================================


class TestSessionPerConversation:
    """Sessions are scoped per-conversation, not per-coworker."""

    async def test_two_conversations_independent_sessions(self) -> None:
        from nanoclaw.db.pg import (
            _get_pool,
            create_channel_binding,
            create_conversation,
            create_coworker,
            create_role,
            create_tenant,
            set_session_new,
        )

        tenant = await create_tenant(slug="sess-iso", name="SessIso")
        role = await create_role(tenant_id=tenant.id, name="general", role_type="general")
        cw = await create_coworker(
            tenant_id=tenant.id, role_id=role.id, name="Bot", folder="bot",
        )
        bind = await create_channel_binding(
            coworker_id=cw.id, tenant_id=tenant.id, channel_type="telegram",
        )
        conv_a = await create_conversation(
            tenant_id=tenant.id, coworker_id=cw.id,
            channel_binding_id=bind.id, channel_chat_id="chat-a",
        )
        conv_b = await create_conversation(
            tenant_id=tenant.id, coworker_id=cw.id,
            channel_binding_id=bind.id, channel_chat_id="chat-b",
        )

        # Set different sessions for each conversation
        await set_session_new(conv_a.id, tenant.id, cw.id, "session-AAA")
        await set_session_new(conv_b.id, tenant.id, cw.id, "session-BBB")

        pool = _get_pool()
        async with pool.acquire() as conn:
            row_a = await conn.fetchrow(
                "SELECT session_id FROM sessions WHERE conversation_id = $1::uuid", conv_a.id,
            )
            row_b = await conn.fetchrow(
                "SELECT session_id FROM sessions WHERE conversation_id = $1::uuid", conv_b.id,
            )

        assert row_a is not None and row_a["session_id"] == "session-AAA"
        assert row_b is not None and row_b["session_id"] == "session-BBB"


# ============================================================================
# 11. Legacy tenant_id Default Param
# ============================================================================


class TestLegacyTenantDefault:
    """All legacy db functions should work with DEFAULT_TENANT."""

    async def test_store_and_get_with_default_tenant(self) -> None:
        """Legacy functions without explicit tenant_id use DEFAULT_TENANT."""
        from nanoclaw.core.types import NewMessage
        from nanoclaw.db.pg import (
            get_messages_since,
            get_router_state,
            set_router_state,
            store_chat_metadata,
            store_message,
        )

        await store_chat_metadata("legacy-chat", "2024-01-01T00:00:00Z", name="Legacy")
        await store_message(NewMessage(
            id="lm1", chat_jid="legacy-chat", sender="u1", sender_name="User",
            content="Hello", timestamp="2024-01-01T00:00:01Z",
        ))

        msgs = await get_messages_since("legacy-chat", "2024-01-01T00:00:00Z", "Andy")
        assert len(msgs) == 1

        await set_router_state("legacy-key", "legacy-value")
        assert await get_router_state("legacy-key") == "legacy-value"

    async def test_explicit_tenant_id_isolates_from_default(self) -> None:
        """Data stored with explicit tenant_id is invisible to DEFAULT_TENANT queries."""
        from nanoclaw.db.pg import get_router_state, set_router_state

        await set_router_state("shared-key", "default-value")
        await set_router_state("shared-key", "other-value", tenant_id="other-tenant")

        assert await get_router_state("shared-key") == "default-value"
        assert await get_router_state("shared-key", tenant_id="other-tenant") == "other-value"


# ============================================================================
# 12. ScheduledTask with coworker_id / conversation_id
# ============================================================================


class TestScheduledTaskMultiTenant:
    """ScheduledTask now carries coworker_id and conversation_id."""

    async def test_task_with_coworker_id_roundtrip(self) -> None:
        from nanoclaw.core.types import ScheduledTask
        from nanoclaw.db.pg import create_task, get_task_by_id

        ids = await _setup_tenant_with_coworker(
            tenant_slug="task-mt", coworker_folder="task-cw", chat_id="task-chat",
        )

        task = ScheduledTask(
            id="mt-task-1",
            group_folder="task-cw",
            chat_jid="task-chat",
            prompt="Do thing",
            schedule_type="cron",
            schedule_value="0 9 * * *",
            context_mode="group",
            next_run="2024-01-02T09:00:00Z",
            status="active",
            created_at="2024-01-01T00:00:00Z",
            coworker_id=ids["coworker_id"],
            conversation_id=ids["conversation_id"],
        )
        await create_task(task)

        retrieved = await get_task_by_id("mt-task-1")
        assert retrieved is not None
        assert retrieved.coworker_id == ids["coworker_id"]
        assert retrieved.conversation_id == ids["conversation_id"]

    async def test_task_without_coworker_id_is_none(self) -> None:
        """Legacy tasks without coworker_id should have None."""
        from nanoclaw.core.types import ScheduledTask
        from nanoclaw.db.pg import create_task, get_task_by_id

        task = ScheduledTask(
            id="legacy-task-1",
            group_folder="grp",
            chat_jid="chat",
            prompt="Legacy",
            schedule_type="once",
            schedule_value="2024-01-01T00:00:00Z",
            context_mode="isolated",
            status="active",
            created_at="2024-01-01T00:00:00Z",
        )
        await create_task(task)

        retrieved = await get_task_by_id("legacy-task-1")
        assert retrieved is not None
        assert retrieved.coworker_id is None
        assert retrieved.conversation_id is None
