"""IPC watcher — polls file-based IPC directories for messages and tasks.

Port of src/ipc.ts. Each group gets an isolated IPC namespace under data/ipc/{folder}/.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from croniter import croniter

from nanoclaw.config import (
    ASSISTANT_NAME,
    DATA_DIR,
    IPC_POLL_INTERVAL,
    MAIN_GROUP_FOLDER,
    TIMEZONE,
)
from nanoclaw.db import create_task, delete_task, get_task_by_id, update_task
from nanoclaw.logger import logger
from nanoclaw.models import AvailableGroup, RegisteredGroup, ScheduledTask


class IpcDeps:
    """Dependencies injected into the IPC watcher."""

    def __init__(
        self,
        send_message: Callable[[str, str], Awaitable[None]],
        registered_groups: Callable[[], dict[str, RegisteredGroup]],
        register_group: Callable[[str, RegisteredGroup], None],
        get_available_groups: Callable[[], list[AvailableGroup]],
        write_groups_snapshot: Callable[[str, bool, list[AvailableGroup], set[str]], None],
    ):
        self.send_message = send_message
        self.registered_groups = registered_groups
        self.register_group = register_group
        self.get_available_groups = get_available_groups
        self.write_groups_snapshot = write_groups_snapshot


async def start_ipc_watcher(deps: IpcDeps) -> None:
    """Main IPC polling loop — runs forever."""
    ipc_base = DATA_DIR / "ipc"
    ipc_base.mkdir(parents=True, exist_ok=True)

    logger.info("IPC watcher started (per-group namespaces)")

    while True:
        try:
            group_folders = [
                d.name
                for d in ipc_base.iterdir()
                if d.is_dir() and d.name != "errors"
            ]
        except Exception as e:
            logger.error("Error reading IPC base directory", error=str(e))
            await asyncio.sleep(IPC_POLL_INTERVAL)
            continue

        groups = deps.registered_groups()

        for source_group in group_folders:
            is_main = source_group == MAIN_GROUP_FOLDER
            messages_dir = ipc_base / source_group / "messages"
            tasks_dir = ipc_base / source_group / "tasks"

            # Process messages
            if messages_dir.exists():
                for f in sorted(messages_dir.glob("*.json")):
                    try:
                        data = json.loads(f.read_text())
                        if data.get("type") == "message" and data.get("chatJid") and data.get("text"):
                            target = groups.get(data["chatJid"])
                            if is_main or (target and target.folder == source_group):
                                await deps.send_message(
                                    data["chatJid"],
                                    f"{ASSISTANT_NAME}: {data['text']}",
                                )
                                logger.info("IPC message sent", chat_jid=data["chatJid"])
                            else:
                                logger.warning("Unauthorized IPC message blocked", source=source_group)
                        f.unlink()
                    except Exception as e:
                        logger.error("Error processing IPC message", file=f.name, error=str(e))
                        err_dir = ipc_base / "errors"
                        err_dir.mkdir(exist_ok=True)
                        f.rename(err_dir / f"{source_group}-{f.name}")

            # Process tasks
            if tasks_dir.exists():
                for f in sorted(tasks_dir.glob("*.json")):
                    try:
                        data = json.loads(f.read_text())
                        await _process_task_ipc(data, source_group, is_main, deps)
                        f.unlink()
                    except Exception as e:
                        logger.error("Error processing IPC task", file=f.name, error=str(e))
                        err_dir = ipc_base / "errors"
                        err_dir.mkdir(exist_ok=True)
                        f.rename(err_dir / f"{source_group}-{f.name}")

        await asyncio.sleep(IPC_POLL_INTERVAL)


async def _process_task_ipc(
    data: dict,
    source_group: str,
    is_main: bool,
    deps: IpcDeps,
) -> None:
    groups = deps.registered_groups()
    task_type = data.get("type", "")

    if task_type == "schedule_task":
        prompt = data.get("prompt")
        schedule_type = data.get("schedule_type")
        schedule_value = data.get("schedule_value")
        target_jid = data.get("targetJid")

        if not all((prompt, schedule_type, schedule_value, target_jid)):
            return

        target_entry = groups.get(target_jid)
        if not target_entry:
            logger.warning("Cannot schedule task: target not registered", target_jid=target_jid)
            return

        if not is_main and target_entry.folder != source_group:
            logger.warning("Unauthorized schedule_task blocked", source=source_group)
            return

        next_run: str | None = None
        if schedule_type == "cron":
            try:
                it = croniter(schedule_value)
                next_run = datetime.fromtimestamp(it.get_next(), tz=timezone.utc).isoformat()
            except Exception:
                logger.warning("Invalid cron expression", value=schedule_value)
                return
        elif schedule_type == "interval":
            ms = int(schedule_value)
            if ms <= 0:
                return
            next_run = datetime.fromtimestamp(
                datetime.now(timezone.utc).timestamp() + ms / 1000,
                tz=timezone.utc,
            ).isoformat()
        elif schedule_type == "once":
            dt = datetime.fromisoformat(schedule_value)
            next_run = dt.isoformat()

        import time, random, string
        task_id = f"task-{int(time.time() * 1000)}-{''.join(random.choices(string.ascii_lowercase, k=6))}"
        context_mode = data.get("context_mode", "isolated")
        if context_mode not in ("group", "isolated"):
            context_mode = "isolated"

        create_task(ScheduledTask(
            id=task_id,
            group_folder=target_entry.folder,
            chat_jid=target_jid,
            prompt=prompt,
            schedule_type=schedule_type,
            schedule_value=schedule_value,
            context_mode=context_mode,
            next_run=next_run,
            status="active",
            created_at=datetime.now(timezone.utc).isoformat(),
        ))
        logger.info("Task created via IPC", task_id=task_id, source=source_group)

    elif task_type in ("pause_task", "resume_task", "cancel_task"):
        task_id = data.get("taskId")
        if not task_id:
            return
        task = get_task_by_id(task_id)
        if not task:
            return
        if not is_main and task.group_folder != source_group:
            logger.warning(f"Unauthorized {task_type} blocked", source=source_group)
            return

        if task_type == "pause_task":
            update_task(task_id, status="paused")
        elif task_type == "resume_task":
            update_task(task_id, status="active")
        elif task_type == "cancel_task":
            delete_task(task_id)
        logger.info(f"Task {task_type} via IPC", task_id=task_id)

    elif task_type == "register_group":
        if not is_main:
            logger.warning("Unauthorized register_group blocked", source=source_group)
            return
        jid = data.get("jid")
        name = data.get("name")
        folder = data.get("folder")
        trigger = data.get("trigger")
        if not all((jid, name, folder, trigger)):
            logger.warning("Invalid register_group: missing fields")
            return
        deps.register_group(jid, RegisteredGroup(
            name=name,
            folder=folder,
            trigger=trigger,
            added_at=datetime.now(timezone.utc).isoformat(),
        ))

    elif task_type == "refresh_groups":
        if is_main:
            available = deps.get_available_groups()
            deps.write_groups_snapshot(
                source_group, True, available, set(groups.keys())
            )
        else:
            logger.warning("Unauthorized refresh_groups blocked", source=source_group)

    else:
        logger.warning("Unknown IPC task type", type=task_type)
