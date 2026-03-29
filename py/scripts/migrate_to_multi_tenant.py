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

from nanoclaw.core.config import DATA_DIR, GROUPS_DIR
from nanoclaw.core.env import read_env_file
from nanoclaw.core.logger import get_logger
from nanoclaw.db.pg import (
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
    env = read_env_file(
        [
            "TELEGRAM_BOT_TOKEN",
            "SLACK_BOT_TOKEN",
            "SLACK_APP_TOKEN",
        ]
    )

    if channel_type == "telegram":
        import os

        token = os.environ.get("TELEGRAM_BOT_TOKEN") or env.get("TELEGRAM_BOT_TOKEN", "")
        return {"bot_token": token} if token else {}
    elif channel_type == "slack":
        import os

        bot_token = os.environ.get("SLACK_BOT_TOKEN") or env.get("SLACK_BOT_TOKEN", "")
        app_token = os.environ.get("SLACK_APP_TOKEN") or env.get("SLACK_APP_TOKEN", "")
        return {"bot_token": bot_token, "app_token": app_token} if bot_token else {}
    return {}


async def migrate_to_multi_tenant() -> None:
    """Migrate existing single-tenant data to new multi-tenant schema."""
    await init_database()

    # 1. Create default tenant (or find existing)
    tenant = await get_tenant_by_slug("default")
    if tenant is None:
        tenant = await create_tenant(slug="default", name="Default Tenant")
        logger.info("Created default tenant", tenant_id=tenant.id)
    else:
        logger.info("Using existing default tenant", tenant_id=tenant.id)

    # 2. Create default role
    role = await create_role(
        tenant_id=tenant.id,
        name="general",
    )
    logger.info("Created default role", role_id=role.id)

    # 3. For each registered_group -> coworker + channel_binding + conversation
    registered_groups = await get_all_registered_groups()
    sessions = await get_all_sessions_legacy()

    # Group bindings by channel_type (share credentials across coworkers of same channel)

    for jid, group in registered_groups.items():
        channel_type = _infer_channel_type(jid)
        chat_id = _extract_chat_id(jid)

        logger.info(
            "Migrating group",
            jid=jid,
            name=group.name,
            folder=group.folder,
            channel_type=channel_type,
        )

        # Create coworker
        coworker = await create_coworker(
            tenant_id=tenant.id,
            role_id=role.id,
            name=group.name,
            folder=group.folder,
            is_admin=group.is_main,
            container_config=group.container_config,
        )
        logger.info("Created coworker", coworker_id=coworker.id, name=coworker.name)

        # Create channel binding
        credentials = _get_current_credentials(channel_type)
        binding = await create_channel_binding(
            coworker_id=coworker.id,
            tenant_id=tenant.id,
            channel_type=channel_type,
            credentials=credentials,
        )
        logger.info("Created channel binding", binding_id=binding.id, channel_type=channel_type)

        # Create conversation
        conversation = await create_conversation(
            tenant_id=tenant.id,
            coworker_id=coworker.id,
            channel_binding_id=binding.id,
            channel_chat_id=chat_id,
            name=group.name,
            requires_trigger=group.requires_trigger,
        )
        logger.info("Created conversation", conversation_id=conversation.id, chat_id=chat_id)

        # Migrate session
        old_session = sessions.get(group.folder)
        if old_session:
            await set_session(conversation.id, tenant.id, coworker.id, old_session)
            logger.info("Migrated session", folder=group.folder, session_id=old_session[:20])

        # 4. Move filesystem: groups/{folder}/ -> data/tenants/{tid}/coworkers/{folder}/workspace/
        old_dir = GROUPS_DIR / group.folder
        new_workspace = DATA_DIR / "tenants" / tenant.id / "coworkers" / group.folder / "workspace"

        if old_dir.exists() and not new_workspace.exists():
            new_workspace.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(str(old_dir), str(new_workspace), dirs_exist_ok=True)
            logger.info(
                "Migrated filesystem",
                old=str(old_dir),
                new=str(new_workspace),
            )
        elif new_workspace.exists():
            logger.info("Workspace already exists, skipping filesystem migration", folder=group.folder)

    # 5. Drop legacy tables
    await drop_legacy_tables()

    logger.info(
        "Migration complete",
        groups_migrated=len(registered_groups),
        tenant_id=tenant.id,
    )


def main() -> None:
    """CLI entry point."""
    asyncio.run(migrate_to_multi_tenant())


if __name__ == "__main__":
    main()
