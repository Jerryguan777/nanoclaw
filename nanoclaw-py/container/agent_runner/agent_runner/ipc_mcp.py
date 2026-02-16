"""Stdio MCP Server for NanoClaw — runs inside the container.

Port of container/agent-runner/src/ipc-mcp-stdio.ts.
Provides tools: send_message, schedule_task, list_tasks, pause_task, resume_task, cancel_task, register_group.
"""

from __future__ import annotations

import json
import os
import random
import string
import time
from datetime import datetime, timezone
from pathlib import Path

from croniter import croniter
from mcp.server import Server
from mcp.server.stdio import stdio_server

IPC_DIR = Path("/workspace/ipc")
MESSAGES_DIR = IPC_DIR / "messages"
TASKS_DIR = IPC_DIR / "tasks"

chat_jid = os.environ.get("NANOCLAW_CHAT_JID", "")
group_folder = os.environ.get("NANOCLAW_GROUP_FOLDER", "")
is_main = os.environ.get("NANOCLAW_IS_MAIN") == "1"

server = Server("nanoclaw")


def _write_ipc_file(directory: Path, data: dict) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    filename = f"{int(time.time() * 1000)}-{''.join(random.choices(string.ascii_lowercase, k=6))}.json"
    filepath = directory / filename
    tmp = filepath.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.rename(filepath)
    return filename


@server.tool()
async def send_message(text: str, sender: str = "") -> str:
    """Send a message to the user or group immediately while you're still running."""
    data = {
        "type": "message",
        "chatJid": chat_jid,
        "text": text,
        "groupFolder": group_folder,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if sender:
        data["sender"] = sender
    _write_ipc_file(MESSAGES_DIR, data)
    return "Message sent."


@server.tool()
async def schedule_task(
    prompt: str,
    schedule_type: str,
    schedule_value: str,
    context_mode: str = "group",
    target_group_jid: str = "",
) -> str:
    """Schedule a recurring or one-time task.

    schedule_type: 'cron', 'interval', or 'once'
    schedule_value: cron expression, milliseconds, or ISO timestamp
    context_mode: 'group' (with chat history) or 'isolated' (fresh session)
    """
    # Validate
    if schedule_type == "cron":
        try:
            croniter(schedule_value)
        except Exception:
            return f'Invalid cron: "{schedule_value}"'
    elif schedule_type == "interval":
        ms = int(schedule_value)
        if ms <= 0:
            return f'Invalid interval: "{schedule_value}"'
    elif schedule_type == "once":
        try:
            datetime.fromisoformat(schedule_value)
        except Exception:
            return f'Invalid timestamp: "{schedule_value}"'

    target_jid = (target_group_jid if is_main and target_group_jid else chat_jid)

    data = {
        "type": "schedule_task",
        "prompt": prompt,
        "schedule_type": schedule_type,
        "schedule_value": schedule_value,
        "context_mode": context_mode,
        "targetJid": target_jid,
        "createdBy": group_folder,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    filename = _write_ipc_file(TASKS_DIR, data)
    return f"Task scheduled ({filename}): {schedule_type} - {schedule_value}"


@server.tool()
async def list_tasks() -> str:
    """List all scheduled tasks."""
    tasks_file = IPC_DIR / "current_tasks.json"
    if not tasks_file.exists():
        return "No scheduled tasks found."

    try:
        all_tasks = json.loads(tasks_file.read_text())
        tasks = all_tasks if is_main else [t for t in all_tasks if t.get("groupFolder") == group_folder]
        if not tasks:
            return "No scheduled tasks found."

        lines = []
        for t in tasks:
            lines.append(
                f"- [{t['id']}] {t['prompt'][:50]}... "
                f"({t['schedule_type']}: {t['schedule_value']}) - "
                f"{t['status']}, next: {t.get('next_run', 'N/A')}"
            )
        return "Scheduled tasks:\n" + "\n".join(lines)
    except Exception as e:
        return f"Error reading tasks: {e}"


@server.tool()
async def pause_task(task_id: str) -> str:
    """Pause a scheduled task."""
    _write_ipc_file(TASKS_DIR, {
        "type": "pause_task",
        "taskId": task_id,
        "groupFolder": group_folder,
        "isMain": is_main,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    return f"Task {task_id} pause requested."


@server.tool()
async def resume_task(task_id: str) -> str:
    """Resume a paused task."""
    _write_ipc_file(TASKS_DIR, {
        "type": "resume_task",
        "taskId": task_id,
        "groupFolder": group_folder,
        "isMain": is_main,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    return f"Task {task_id} resume requested."


@server.tool()
async def cancel_task(task_id: str) -> str:
    """Cancel and delete a scheduled task."""
    _write_ipc_file(TASKS_DIR, {
        "type": "cancel_task",
        "taskId": task_id,
        "groupFolder": group_folder,
        "isMain": is_main,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    return f"Task {task_id} cancellation requested."


@server.tool()
async def register_group(jid: str, name: str, folder: str, trigger: str) -> str:
    """Register a new group so the agent can respond to messages there. Main group only."""
    if not is_main:
        return "Only the main group can register new groups."

    _write_ipc_file(TASKS_DIR, {
        "type": "register_group",
        "jid": jid,
        "name": name,
        "folder": folder,
        "trigger": trigger,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    return f'Group "{name}" registered.'


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
