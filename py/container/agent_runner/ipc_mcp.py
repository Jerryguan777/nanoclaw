"""
NanoClaw in-process MCP server.

Defines MCP tools that write IPC files for the host process to consume.
Uses create_sdk_mcp_server / @tool for in-process registration (no stdio).
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from croniter import croniter

IPC_DIR = Path("/workspace/ipc")
MESSAGES_DIR = IPC_DIR / "messages"
TASKS_DIR = IPC_DIR / "tasks"


def _write_ipc_file(directory: Path, data: dict[str, Any]) -> str:
    """Atomically write a JSON IPC file and return the filename."""
    directory.mkdir(parents=True, exist_ok=True)

    rand_suffix = f"{time.time_ns() % 10**8:08x}"
    filename = f"{int(time.time() * 1000)}-{rand_suffix}.json"
    filepath = directory / filename
    temp_path = filepath.with_suffix(".json.tmp")

    temp_path.write_text(json.dumps(data, indent=2))
    temp_path.rename(filepath)

    return filename


def _text_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    """Return an MCP tool result dict."""
    result: dict[str, Any] = {
        "content": [{"type": "text", "text": text}],
    }
    if is_error:
        result["isError"] = True
    return result


def create_nanoclaw_mcp_server(
    chat_jid: str,
    group_folder: str,
    is_main: bool,
) -> Any:
    """Create and return an in-process MCP server with all NanoClaw tools."""

    @tool(
        name="send_message",
        description=(
            "Send a message to the user or group immediately while you're still "
            "running. Use this for progress updates or to send multiple messages. "
            "You can call this multiple times."
        ),
    )
    async def send_message(
        text: str,
        sender: str | None = None,
    ) -> dict[str, Any]:
        """
        Args:
            text: The message text to send.
            sender: Your role/identity name (e.g. "Researcher"). When set,
                    messages appear from a dedicated bot in Telegram.
        """
        data: dict[str, Any] = {
            "type": "message",
            "chatJid": chat_jid,
            "text": text,
            "groupFolder": group_folder,
            "timestamp": datetime.now().isoformat(),
        }
        if sender:
            data["sender"] = sender

        _write_ipc_file(MESSAGES_DIR, data)
        return _text_result("Message sent.")

    @tool(
        name="schedule_task",
        description=(
            "Schedule a recurring or one-time task. The task will run as a full "
            "agent with access to all tools. Returns the task ID for future "
            "reference. To modify an existing task, use update_task instead.\n\n"
            "CONTEXT MODE - Choose based on task type:\n"
            '\u2022 "group": Task runs in the group\'s conversation context, with '
            "access to chat history. Use for tasks that need context about ongoing "
            "discussions, user preferences, or recent interactions.\n"
            '\u2022 "isolated": Task runs in a fresh session with no conversation '
            "history. Use for independent tasks that don't need prior context. When "
            "using isolated mode, include all necessary context in the prompt itself.\n\n"
            "If unsure which mode to use, you can ask the user. Examples:\n"
            '- "Remind me about our discussion" \u2192 group (needs conversation context)\n'
            '- "Check the weather every morning" \u2192 isolated (self-contained task)\n'
            '- "Follow up on my request" \u2192 group (needs to know what was requested)\n'
            '- "Generate a daily report" \u2192 isolated (just needs instructions in prompt)\n\n'
            "MESSAGING BEHAVIOR - The task agent's output is sent to the user or "
            "group. It can also use send_message for immediate delivery, or wrap "
            "output in <internal> tags to suppress it. Include guidance in the prompt "
            "about whether the agent should:\n"
            "\u2022 Always send a message (e.g., reminders, daily briefings)\n"
            "\u2022 Only send a message when there's something to report (e.g., "
            '"notify me if...")\n'
            "\u2022 Never send a message (background maintenance tasks)\n\n"
            "SCHEDULE VALUE FORMAT (all times are LOCAL timezone):\n"
            '\u2022 cron: Standard cron expression (e.g., "*/5 * * * *" for every '
            '5 minutes, "0 9 * * *" for daily at 9am LOCAL time)\n'
            '\u2022 interval: Milliseconds between runs (e.g., "300000" for 5 '
            'minutes, "3600000" for 1 hour)\n'
            '\u2022 once: Local time WITHOUT "Z" suffix (e.g., '
            '"2026-02-01T15:30:00"). Do NOT use UTC/Z suffix.'
        ),
    )
    async def schedule_task(
        prompt: str,
        schedule_type: str,
        schedule_value: str,
        context_mode: str = "group",
        target_group_jid: str | None = None,
    ) -> dict[str, Any]:
        """
        Args:
            prompt: What the agent should do when the task runs. For isolated mode,
                    include all necessary context here.
            schedule_type: cron=recurring at specific times, interval=recurring every
                          N ms, once=run once at specific time.
            schedule_value: cron: "*/5 * * * *" | interval: milliseconds like "300000"
                           | once: local timestamp like "2026-02-01T15:30:00" (no Z suffix!)
            context_mode: group=runs with chat history and memory, isolated=fresh session
                         (include context in prompt).
            target_group_jid: (Main group only) JID of the group to schedule the task
                             for. Defaults to the current group.
        """
        # Validate schedule_type
        if schedule_type not in ("cron", "interval", "once"):
            return _text_result(
                f'Invalid schedule_type: "{schedule_type}". Must be cron, interval, or once.',
                is_error=True,
            )

        # Validate schedule_value
        if schedule_type == "cron":
            try:
                croniter(schedule_value)
            except (ValueError, KeyError):
                return _text_result(
                    f'Invalid cron: "{schedule_value}". Use format like '
                    '"0 9 * * *" (daily 9am) or "*/5 * * * *" (every 5 min).',
                    is_error=True,
                )
        elif schedule_type == "interval":
            try:
                ms = int(schedule_value)
                if ms <= 0:
                    raise ValueError
            except (ValueError, TypeError):
                return _text_result(
                    f'Invalid interval: "{schedule_value}". Must be positive milliseconds (e.g., "300000" for 5 min).',
                    is_error=True,
                )
        elif schedule_type == "once":
            if re.search(r"[Zz]$", schedule_value) or re.search(r"[+-]\d{2}:\d{2}$", schedule_value):
                return _text_result(
                    f"Timestamp must be local time without timezone suffix. "
                    f'Got "{schedule_value}" \u2014 use format like '
                    '"2026-02-01T15:30:00".',
                    is_error=True,
                )
            try:
                datetime.fromisoformat(schedule_value)
            except ValueError:
                return _text_result(
                    f'Invalid timestamp: "{schedule_value}". Use local time format like "2026-02-01T15:30:00".',
                    is_error=True,
                )

        # Non-main groups can only schedule for themselves
        target_jid = target_group_jid if is_main and target_group_jid else chat_jid

        rand_suffix = f"{time.time_ns() % 10**8:08x}"
        task_id = f"task-{int(time.time() * 1000)}-{rand_suffix}"

        data = {
            "type": "schedule_task",
            "taskId": task_id,
            "prompt": prompt,
            "schedule_type": schedule_type,
            "schedule_value": schedule_value,
            "context_mode": context_mode or "group",
            "targetJid": target_jid,
            "createdBy": group_folder,
            "timestamp": datetime.now().isoformat(),
        }

        _write_ipc_file(TASKS_DIR, data)
        return _text_result(f"Task {task_id} scheduled: {schedule_type} - {schedule_value}")

    @tool(
        name="list_tasks",
        description=(
            "List all scheduled tasks. From main: shows all tasks. From other groups: shows only that group's tasks."
        ),
    )
    async def list_tasks() -> dict[str, Any]:
        tasks_file = IPC_DIR / "current_tasks.json"

        try:
            if not tasks_file.exists():
                return _text_result("No scheduled tasks found.")

            all_tasks: list[dict[str, Any]] = json.loads(tasks_file.read_text())

            tasks = all_tasks if is_main else [t for t in all_tasks if t.get("groupFolder") == group_folder]

            if not tasks:
                return _text_result("No scheduled tasks found.")

            lines: list[str] = []
            for t in tasks:
                prompt_preview = t.get("prompt", "")[:50]
                stype = t.get("schedule_type", "?")
                sval = t.get("schedule_value", "?")
                status = t.get("status", "?")
                next_run = t.get("next_run", "N/A")
                lines.append(
                    f"- [{t.get('id', '?')}] {prompt_preview}... ({stype}: {sval}) - {status}, next: {next_run}"
                )

            return _text_result("Scheduled tasks:\n" + "\n".join(lines))
        except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
            return _text_result(f"Error reading tasks: {exc}")

    @tool(
        name="pause_task",
        description="Pause a scheduled task. It will not run until resumed.",
    )
    async def pause_task(task_id: str) -> dict[str, Any]:
        """
        Args:
            task_id: The task ID to pause.
        """
        data = {
            "type": "pause_task",
            "taskId": task_id,
            "groupFolder": group_folder,
            "isMain": is_main,
            "timestamp": datetime.now().isoformat(),
        }
        _write_ipc_file(TASKS_DIR, data)
        return _text_result(f"Task {task_id} pause requested.")

    @tool(
        name="resume_task",
        description="Resume a paused task.",
    )
    async def resume_task(task_id: str) -> dict[str, Any]:
        """
        Args:
            task_id: The task ID to resume.
        """
        data = {
            "type": "resume_task",
            "taskId": task_id,
            "groupFolder": group_folder,
            "isMain": is_main,
            "timestamp": datetime.now().isoformat(),
        }
        _write_ipc_file(TASKS_DIR, data)
        return _text_result(f"Task {task_id} resume requested.")

    @tool(
        name="cancel_task",
        description="Cancel and delete a scheduled task.",
    )
    async def cancel_task(task_id: str) -> dict[str, Any]:
        """
        Args:
            task_id: The task ID to cancel.
        """
        data = {
            "type": "cancel_task",
            "taskId": task_id,
            "groupFolder": group_folder,
            "isMain": is_main,
            "timestamp": datetime.now().isoformat(),
        }
        _write_ipc_file(TASKS_DIR, data)
        return _text_result(f"Task {task_id} cancellation requested.")

    @tool(
        name="update_task",
        description=(
            "Update an existing scheduled task. Only provided fields are changed; omitted fields stay the same."
        ),
    )
    async def update_task(
        task_id: str,
        prompt: str | None = None,
        schedule_type: str | None = None,
        schedule_value: str | None = None,
    ) -> dict[str, Any]:
        """
        Args:
            task_id: The task ID to update.
            prompt: New prompt for the task.
            schedule_type: New schedule type (cron, interval, or once).
            schedule_value: New schedule value (see schedule_task for format).
        """
        # Validate schedule_value if provided
        if (schedule_type == "cron" or (not schedule_type and schedule_value)) and schedule_value:
            try:
                croniter(schedule_value)
            except (ValueError, KeyError):
                return _text_result(f'Invalid cron: "{schedule_value}".', is_error=True)
        if schedule_type == "interval" and schedule_value:
            try:
                ms = int(schedule_value)
                if ms <= 0:
                    raise ValueError
            except (ValueError, TypeError):
                return _text_result(f'Invalid interval: "{schedule_value}".', is_error=True)

        data: dict[str, Any] = {
            "type": "update_task",
            "taskId": task_id,
            "groupFolder": group_folder,
            "isMain": str(is_main),
            "timestamp": datetime.now().isoformat(),
        }
        if prompt is not None:
            data["prompt"] = prompt
        if schedule_type is not None:
            data["schedule_type"] = schedule_type
        if schedule_value is not None:
            data["schedule_value"] = schedule_value

        _write_ipc_file(TASKS_DIR, data)
        return _text_result(f"Task {task_id} update requested.")

    @tool(
        name="register_group",
        description=(
            "Register a new chat/group so the agent can respond to messages there. "
            "Main group only.\n\n"
            "Use available_groups.json to find the JID for a group. The folder name "
            'must be channel-prefixed: "{channel}_{group-name}" (e.g., '
            '"whatsapp_family-chat", "telegram_dev-team", "discord_general"). '
            "Use lowercase with hyphens for the group name part."
        ),
    )
    async def register_group(
        jid: str,
        name: str,
        folder: str,
        trigger: str,
    ) -> dict[str, Any]:
        """
        Args:
            jid: The chat JID (e.g., "120363336345536173@g.us",
                 "tg:-1001234567890", "dc:1234567890123456").
            name: Display name for the group.
            folder: Channel-prefixed folder name (e.g., "whatsapp_family-chat",
                    "telegram_dev-team").
            trigger: Trigger word (e.g., "@Andy").
        """
        if not is_main:
            return _text_result("Only the main group can register new groups.", is_error=True)

        data = {
            "type": "register_group",
            "jid": jid,
            "name": name,
            "folder": folder,
            "trigger": trigger,
            "timestamp": datetime.now().isoformat(),
        }
        _write_ipc_file(TASKS_DIR, data)
        return _text_result(f'Group "{name}" registered. It will start receiving messages immediately.')

    # Collect all tool functions and create the server
    tools = [
        send_message,
        schedule_task,
        list_tasks,
        pause_task,
        resume_task,
        cancel_task,
        update_task,
        register_group,
    ]

    return create_sdk_mcp_server("nanoclaw", tools)
