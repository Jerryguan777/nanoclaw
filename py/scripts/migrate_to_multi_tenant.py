"""Migrate existing single-tenant data to multi-tenant schema.

Run after upgrading to Step 5. Converts:
- registered_groups -> coworkers + channel_bindings + conversations
- sessions (legacy) -> sessions (per-conversation)
- Filesystem: groups/{folder}/ -> data/tenants/{tid}/coworkers/{folder}/workspace/

Usage:
    python -m scripts.migrate_to_multi_tenant
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass

from nanoclaw.core.config import ASSISTANT_NAME, DATA_DIR, GROUPS_DIR
from nanoclaw.core.env import read_env_file
from nanoclaw.core.logger import get_logger
from nanoclaw.db.pg import (
    close_database,
    create_channel_binding,
    create_conversation,
    create_coworker,
    create_role,
    create_tenant,
    drop_legacy_tables,
    get_all_registered_groups,
    get_all_sessions_legacy,
    get_tenant_by_slug,
    init_database,
    set_session,
)

logger = get_logger()


def _infer_channel_type(jid: str) -> str:
    """Infer channel type from a JID string."""
    if jid.startswith("tg:"):
        return "telegram"
    if jid.startswith("slack:"):
        return "slack"
    if jid.startswith("discord:"):
        return "discord"
    return "telegram"


def _extract_chat_id(jid: str) -> str:
    """Extract the chat ID from a prefixed JID."""
    for prefix in ("tg:", "slack:", "discord:"):
        if jid.startswith(prefix):
            return jid[len(prefix) :]
    return jid


def _get_current_credentials(channel_type: str) -> dict[str, str]:
    """Get current credentials for a channel type from env."""
    import os

    env = read_env_file(
        [
            "TELEGRAM_BOT_TOKEN",
            "SLACK_BOT_TOKEN",
            "SLACK_APP_TOKEN",
        ]
    )

    if channel_type == "telegram":
        token = os.environ.get("TELEGRAM_BOT_TOKEN") or env.get("TELEGRAM_BOT_TOKEN", "")
        return {"bot_token": token} if token else {}
    elif channel_type == "slack":
        bot_token = os.environ.get("SLACK_BOT_TOKEN") or env.get("SLACK_BOT_TOKEN", "")
        app_token = os.environ.get("SLACK_APP_TOKEN") or env.get("SLACK_APP_TOKEN", "")
        return {"bot_token": bot_token, "app_token": app_token} if bot_token else {}
    return {}


@dataclass
class _PendingConversation:
    """Collected data for a group to be migrated, before legacy tables are dropped."""

    jid: str
    name: str
    folder: str
    is_main: bool
    requires_trigger: bool
    channel_type: str
    chat_id: str
    old_session: str | None
    container_config: object | None


async def migrate_to_multi_tenant() -> None:
    """Migrate existing single-tenant data to new multi-tenant schema.

    Flow:
    1. Read all data from legacy tables
    2. Create tenant + role (in new tables)
    3. Drop legacy tables → recreate as new-format tables
    4. Write migrated data into new tables
    5. Copy filesystem
    """
    await init_database()

    # ---- Phase 1: Read legacy data (while old tables still exist) ----

    registered_groups = await get_all_registered_groups()
    sessions = await get_all_sessions_legacy()

    if not registered_groups:
        logger.info("No registered groups to migrate")
        await close_database()
        return

    pending: list[_PendingConversation] = []
    for jid, group in registered_groups.items():
        channel_type = _infer_channel_type(jid)
        chat_id = _extract_chat_id(jid)
        pending.append(
            _PendingConversation(
                jid=jid,
                name=group.name,
                folder=group.folder,
                is_main=group.is_main,
                requires_trigger=group.requires_trigger,
                channel_type=channel_type,
                chat_id=chat_id,
                old_session=sessions.get(group.folder),
                container_config=group.container_config,
            )
        )

    logger.info("Collected legacy data", groups=len(pending), sessions=len(sessions))

    # ---- Phase 2: Create tenant + role (in new multi-tenant tables) ----

    tenant = await get_tenant_by_slug("default")
    if tenant is None:
        tenant = await create_tenant(slug="default", name="Default Tenant")
        logger.info("Created default tenant", tenant_id=tenant.id)
    else:
        logger.info("Using existing default tenant", tenant_id=tenant.id)

    role = await create_role(tenant_id=tenant.id, name="general")
    logger.info("Created default role", role_id=role.id)

    # ---- Phase 3: Drop legacy tables → new-format tables created ----

    await drop_legacy_tables()

    # ---- Phase 4: Write migrated data into new tables ----

    for p in pending:
        logger.info("Migrating group", jid=p.jid, name=p.name, folder=p.folder, channel_type=p.channel_type)

        # In single-tenant migration, all coworkers share the global ASSISTANT_NAME.
        # The group name is used as the conversation display name instead.
        coworker = await create_coworker(
            tenant_id=tenant.id,
            role_id=role.id,
            name=ASSISTANT_NAME,
            folder=p.folder,
            is_admin=p.is_main,
            container_config=p.container_config,  # type: ignore[arg-type]
        )
        logger.info("Created coworker", coworker_id=coworker.id, name=coworker.name)

        credentials = _get_current_credentials(p.channel_type)
        binding = await create_channel_binding(
            coworker_id=coworker.id,
            tenant_id=tenant.id,
            channel_type=p.channel_type,
            credentials=credentials,
        )
        logger.info("Created channel binding", binding_id=binding.id, channel_type=p.channel_type)

        conversation = await create_conversation(
            tenant_id=tenant.id,
            coworker_id=coworker.id,
            channel_binding_id=binding.id,
            channel_chat_id=p.chat_id,
            name=p.name,
            requires_trigger=p.requires_trigger,
        )
        logger.info("Created conversation", conversation_id=conversation.id, chat_id=p.chat_id)

        if p.old_session:
            await set_session(conversation.id, tenant.id, coworker.id, p.old_session)
            logger.info("Migrated session", folder=p.folder, session_id=p.old_session[:30])

        # ---- Phase 5: Copy filesystem ----
        coworker_base = DATA_DIR / "tenants" / tenant.id / "coworkers" / p.folder

        # 5a: groups/{folder}/ → coworkers/{folder}/workspace/
        old_dir = GROUPS_DIR / p.folder
        new_workspace = coworker_base / "workspace"
        if old_dir.exists() and not new_workspace.exists():
            new_workspace.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(str(old_dir), str(new_workspace), dirs_exist_ok=True)
            logger.info("Migrated workspace", old=str(old_dir), new=str(new_workspace))

        # 5b: data/sessions/{folder}/.claude/ → coworkers/{folder}/.claude/
        old_claude = DATA_DIR / "sessions" / p.folder / ".claude"
        new_claude = coworker_base / ".claude"
        if old_claude.exists():
            new_claude.mkdir(parents=True, exist_ok=True)
            shutil.copytree(str(old_claude), str(new_claude), dirs_exist_ok=True)
            logger.info("Migrated .claude session", old=str(old_claude), new=str(new_claude))

    logger.info("Migration complete", groups_migrated=len(pending), tenant_id=tenant.id)


def main() -> None:
    """CLI entry point."""
    asyncio.run(migrate_to_multi_tenant())


if __name__ == "__main__":
    main()
