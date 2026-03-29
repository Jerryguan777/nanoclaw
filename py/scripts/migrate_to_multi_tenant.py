#!/usr/bin/env python3
"""Migrate existing single-tenant data to the new multi-tenant schema.

Steps:
  1. Create a default tenant
  2. Create a default role (general)
  3. For each registered_group -> coworker + channel_binding + conversation
  4. Migrate sessions to new format
  5. Move groups/{folder}/ -> data/tenants/{tid}/coworkers/{folder}/workspace/

Usage:
    python scripts/migrate_to_multi_tenant.py [--database-url URL] [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from pathlib import Path

# Ensure src is on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from nanoclaw.core.config import DATA_DIR, GROUPS_DIR
from nanoclaw.core.logger import get_logger
from nanoclaw.db import pg

logger = get_logger()


def _infer_channel_type(jid: str) -> str:
    """Infer channel type from JID prefix."""
    if jid.startswith("tg:"):
        return "telegram"
    if jid.startswith("slack:"):
        return "slack"
    return "unknown"


def _extract_chat_id(jid: str) -> str:
    """Extract numeric/string chat ID from prefixed JID."""
    if ":" in jid:
        return jid.split(":", 1)[1]
    return jid


async def migrate(database_url: str | None = None, dry_run: bool = False) -> None:
    """Run the migration."""
    await pg.init_database(database_url)
    logger.info("Database connected, starting migration")

    # 1. Create default tenant
    existing = await pg.get_tenant_by_slug("default")
    if existing:
        tenant = existing
        logger.info("Default tenant already exists", tenant_id=tenant.id)
    elif dry_run:
        logger.info("[DRY RUN] Would create default tenant")
        await pg.close_database()
        return
    else:
        tenant = await pg.create_tenant(slug="default", name="Default Tenant")
        logger.info("Created default tenant", tenant_id=tenant.id)

    # 2. Create default role
    roles = await pg.get_roles_for_tenant(tenant.id)
    role = next((r for r in roles if r.name == "general"), None)
    if role:
        logger.info("Default role already exists", role_id=role.id)
    elif dry_run:
        logger.info("[DRY RUN] Would create default role")
        await pg.close_database()
        return
    else:
        role = await pg.create_role(
            tenant_id=tenant.id,
            name="general",
            role_type="general",
        )
        logger.info("Created default role", role_id=role.id)

    # 3. Migrate registered groups
    groups = await pg.get_all_registered_groups_legacy()
    sessions = await pg.get_all_sessions()

    for jid, group in groups.items():
        channel_type = _infer_channel_type(jid)
        chat_id = _extract_chat_id(jid)

        # Check if coworker already exists for this folder
        existing_coworkers = await pg.get_coworkers_for_tenant(tenant.id)
        coworker = next((c for c in existing_coworkers if c.folder == group.folder), None)

        if coworker:
            logger.info("Coworker already exists for folder", folder=group.folder, coworker_id=coworker.id)
        elif dry_run:
            logger.info("[DRY RUN] Would create coworker", folder=group.folder, jid=jid)
            continue
        else:
            coworker = await pg.create_coworker(
                tenant_id=tenant.id,
                role_id=role.id,
                name=group.name,
                folder=group.folder,
                is_admin=group.is_main,
            )
            logger.info("Created coworker", name=group.name, folder=group.folder, coworker_id=coworker.id)

        # Channel binding
        bindings = await pg.get_channel_bindings_for_coworker(coworker.id)
        binding = next((b for b in bindings if b.channel_type == channel_type), None)

        if binding:
            logger.info("Channel binding already exists", channel_type=channel_type, binding_id=binding.id)
        elif dry_run:
            logger.info("[DRY RUN] Would create channel binding", channel_type=channel_type)
            continue
        else:
            binding = await pg.create_channel_binding(
                coworker_id=coworker.id,
                tenant_id=tenant.id,
                channel_type=channel_type,
                credentials={},
            )
            logger.info("Created channel binding", channel_type=channel_type, binding_id=binding.id)

        # Conversation
        existing_conv = await pg.get_conversation_by_binding_chat(binding.id, chat_id)
        if existing_conv:
            logger.info("Conversation already exists", chat_id=chat_id, conversation_id=existing_conv.id)
            conversation = existing_conv
        elif dry_run:
            logger.info("[DRY RUN] Would create conversation", chat_id=chat_id)
            continue
        else:
            conversation = await pg.create_conversation(
                tenant_id=tenant.id,
                coworker_id=coworker.id,
                channel_binding_id=binding.id,
                channel_chat_id=chat_id,
                name=group.name,
                trigger_pattern=group.trigger,
                requires_trigger=group.requires_trigger,
                is_main=group.is_main,
            )
            logger.info("Created conversation", chat_id=chat_id, conversation_id=conversation.id)

        # Migrate session
        old_session = sessions.get(group.folder)
        if old_session and not dry_run:
            await pg.set_session_new(conversation.id, tenant.id, coworker.id, old_session)
            logger.info("Migrated session", folder=group.folder, conversation_id=conversation.id)

    # 4. Move filesystem: groups/{folder}/ -> data/tenants/{tid}/coworkers/{folder}/workspace/
    if not dry_run and GROUPS_DIR.exists():
        for folder_dir in GROUPS_DIR.iterdir():
            if not folder_dir.is_dir() or folder_dir.name in ("global", "__pycache__"):
                continue

            target_dir = DATA_DIR / "tenants" / tenant.id / "coworkers" / folder_dir.name / "workspace"
            if target_dir.exists():
                logger.info("Target workspace already exists, skipping move", folder=folder_dir.name)
                continue

            target_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(str(folder_dir), str(target_dir))
            logger.info("Copied workspace", src=str(folder_dir), dst=str(target_dir))
    elif dry_run:
        logger.info("[DRY RUN] Would move group folders to tenant workspace directories")

    logger.info("Migration complete")
    await pg.close_database()


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate to multi-tenant schema")
    parser.add_argument("--database-url", help="PostgreSQL connection URL")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without making changes")
    args = parser.parse_args()

    asyncio.run(migrate(database_url=args.database_url, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
