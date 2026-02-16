"""NanoClaw main entry point — orchestrates all subsystems.

Port of src/index.ts. Replaces WhatsApp with CLI + HTTP channels.
"""

from __future__ import annotations

import asyncio
import json
import signal
import subprocess
import sys
from pathlib import Path

from nanoclaw.config import (
    ASSISTANT_NAME,
    CONTAINER_RUNTIME,
    DATA_DIR,
    GROUPS_DIR,
    IDLE_TIMEOUT,
    MAIN_GROUP_FOLDER,
    POLL_INTERVAL,
    TRIGGER_PATTERN,
)
from nanoclaw.container_runner import (
    ContainerOutput,
    run_container_agent,
    write_groups_snapshot,
    write_tasks_snapshot,
)
from nanoclaw.db import (
    get_all_chats,
    get_all_registered_groups,
    get_all_sessions,
    get_all_tasks,
    get_messages_since,
    get_new_messages,
    get_router_state,
    init_database,
    set_registered_group,
    set_router_state,
    set_session,
    store_chat_metadata,
    store_message,
)
from nanoclaw.group_queue import GroupQueue
from nanoclaw.ipc import IpcDeps, start_ipc_watcher
from nanoclaw.logger import logger
from nanoclaw.models import (
    AvailableGroup,
    ContainerInput,
    NewMessage,
    RegisteredGroup,
)
from nanoclaw.router import format_messages, format_outbound
from nanoclaw.task_scheduler import SchedulerDeps, start_scheduler_loop

# --- Global state ---

_last_timestamp = ""
_sessions: dict[str, str] = {}
_registered_groups: dict[str, RegisteredGroup] = {}
_last_agent_timestamp: dict[str, str] = {}
_queue = GroupQueue()
_channels: list = []


def _load_state() -> None:
    global _last_timestamp, _sessions, _registered_groups, _last_agent_timestamp
    _last_timestamp = get_router_state("last_timestamp") or ""
    agent_ts = get_router_state("last_agent_timestamp")
    try:
        _last_agent_timestamp = json.loads(agent_ts) if agent_ts else {}
    except Exception:
        logger.warning("Corrupted last_agent_timestamp, resetting")
        _last_agent_timestamp = {}
    _sessions = get_all_sessions()
    _registered_groups = get_all_registered_groups()
    logger.info("State loaded", group_count=len(_registered_groups))


def _save_state() -> None:
    set_router_state("last_timestamp", _last_timestamp)
    set_router_state("last_agent_timestamp", json.dumps(_last_agent_timestamp))


def _register_group(jid: str, group: RegisteredGroup) -> None:
    _registered_groups[jid] = group
    set_registered_group(jid, group)
    group_dir = GROUPS_DIR / group.folder
    (group_dir / "logs").mkdir(parents=True, exist_ok=True)
    logger.info("Group registered", jid=jid, name=group.name, folder=group.folder)


def _get_available_groups() -> list[AvailableGroup]:
    chats = get_all_chats()
    registered_jids = set(_registered_groups.keys())
    # For CLI/HTTP mode, all registered JIDs count as available
    return [
        AvailableGroup(
            jid=jid,
            name=group.name,
            last_activity=group.added_at,
            is_registered=True,
        )
        for jid, group in _registered_groups.items()
    ]


# --- Message processing ---


async def _process_group_messages(chat_jid: str) -> bool:
    """Process pending messages for a group. Returns True on success."""
    global _last_agent_timestamp

    group = _registered_groups.get(chat_jid)
    if not group:
        return True

    is_main = group.folder == MAIN_GROUP_FOLDER
    since = _last_agent_timestamp.get(chat_jid, "")
    missed = get_messages_since(chat_jid, since, ASSISTANT_NAME)

    if not missed:
        return True

    # Trigger check for non-main groups
    if not is_main and group.requires_trigger:
        has_trigger = any(TRIGGER_PATTERN.search(m.content.strip()) for m in missed)
        if not has_trigger:
            return True

    prompt = format_messages(missed)
    prev_cursor = _last_agent_timestamp.get(chat_jid, "")
    _last_agent_timestamp[chat_jid] = missed[-1].timestamp
    _save_state()

    logger.info("Processing messages", group=group.name, count=len(missed))

    # Idle timer
    idle_handle: asyncio.TimerHandle | None = None

    def reset_idle():
        nonlocal idle_handle
        if idle_handle:
            idle_handle.cancel()
        idle_handle = asyncio.get_event_loop().call_later(
            IDLE_TIMEOUT / 1000,
            lambda: _queue.close_stdin(chat_jid),
        )

    had_error = False
    output_sent = False

    async def on_output(result: ContainerOutput) -> None:
        nonlocal had_error, output_sent
        if result.result:
            raw = result.result
            import re
            text = re.sub(r"<internal>[\s\S]*?</internal>", "", raw).strip()
            if text:
                for ch in _channels:
                    if ch.owns_jid(chat_jid):
                        outbound = format_outbound(ch, raw)
                        if outbound:
                            await ch.send_message(chat_jid, outbound)
                            output_sent = True
            reset_idle()
        if result.status == "error":
            had_error = True

    output = await _run_agent(group, prompt, chat_jid, on_output)

    if idle_handle:
        idle_handle.cancel()

    if output == "error" or had_error:
        if output_sent:
            logger.warning("Agent error after output sent, skipping cursor rollback", group=group.name)
            return True
        _last_agent_timestamp[chat_jid] = prev_cursor
        _save_state()
        logger.warning("Agent error, rolled back cursor", group=group.name)
        return False

    return True


async def _run_agent(
    group: RegisteredGroup,
    prompt: str,
    chat_jid: str,
    on_output: ...,
) -> str:
    is_main = group.folder == MAIN_GROUP_FOLDER
    session_id = _sessions.get(group.folder)

    # Write snapshots
    tasks = get_all_tasks()
    write_tasks_snapshot(
        group.folder, is_main,
        [{"id": t.id, "groupFolder": t.group_folder, "prompt": t.prompt,
          "schedule_type": t.schedule_type, "schedule_value": t.schedule_value,
          "status": t.status, "next_run": t.next_run} for t in tasks],
    )
    available = _get_available_groups()
    write_groups_snapshot(group.folder, is_main, available, set(_registered_groups.keys()))

    async def wrapped_on_output(output: ContainerOutput) -> None:
        if output.new_session_id:
            _sessions[group.folder] = output.new_session_id
            set_session(group.folder, output.new_session_id)
        if on_output:
            await on_output(output)

    try:
        output = await run_container_agent(
            group,
            ContainerInput(
                prompt=prompt,
                session_id=session_id,
                group_folder=group.folder,
                chat_jid=chat_jid,
                is_main=is_main,
            ),
            lambda proc, name: _queue.register_process(chat_jid, proc, name, group.folder),
            wrapped_on_output,
        )
        if output.new_session_id:
            _sessions[group.folder] = output.new_session_id
            set_session(group.folder, output.new_session_id)
        if output.status == "error":
            logger.error("Container agent error", group=group.name, error=output.error)
            return "error"
        return "success"
    except Exception as e:
        logger.error("Agent error", group=group.name, error=str(e))
        return "error"


# --- Message loop ---


async def _message_loop() -> None:
    global _last_timestamp
    logger.info(f"NanoClaw running (trigger: @{ASSISTANT_NAME})")

    while True:
        try:
            jids = list(_registered_groups.keys())
            messages, new_ts = get_new_messages(jids, _last_timestamp, ASSISTANT_NAME)

            if messages:
                logger.info("New messages", count=len(messages))
                _last_timestamp = new_ts
                _save_state()

                # Group by chat
                by_group: dict[str, list[NewMessage]] = {}
                for msg in messages:
                    by_group.setdefault(msg.chat_jid, []).append(msg)

                for chat_jid, group_msgs in by_group.items():
                    group = _registered_groups.get(chat_jid)
                    if not group:
                        continue

                    is_main = group.folder == MAIN_GROUP_FOLDER
                    needs_trigger = not is_main and group.requires_trigger

                    if needs_trigger:
                        has_trigger = any(TRIGGER_PATTERN.search(m.content.strip()) for m in group_msgs)
                        if not has_trigger:
                            continue

                    all_pending = get_messages_since(
                        chat_jid,
                        _last_agent_timestamp.get(chat_jid, ""),
                        ASSISTANT_NAME,
                    )
                    to_send = all_pending if all_pending else group_msgs
                    formatted = format_messages(to_send)

                    if _queue.send_message(chat_jid, formatted):
                        _last_agent_timestamp[chat_jid] = to_send[-1].timestamp
                        _save_state()
                    else:
                        _queue.enqueue_message_check(chat_jid)

        except Exception as e:
            logger.error("Error in message loop", error=str(e))

        await asyncio.sleep(POLL_INTERVAL)


def _recover_pending() -> None:
    for jid, group in _registered_groups.items():
        since = _last_agent_timestamp.get(jid, "")
        pending = get_messages_since(jid, since, ASSISTANT_NAME)
        if pending:
            logger.info("Recovery: found unprocessed messages", group=group.name, count=len(pending))
            _queue.enqueue_message_check(jid)


def _ensure_container_runtime() -> None:
    """Check that Docker/container CLI is available."""
    try:
        subprocess.run(
            [CONTAINER_RUNTIME, "info" if CONTAINER_RUNTIME == "docker" else "system", "status"],
            capture_output=True, check=True, timeout=10,
        )
        logger.debug("Container runtime available", runtime=CONTAINER_RUNTIME)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logger.error("Container runtime not available", runtime=CONTAINER_RUNTIME, error=str(e))
        print(f"\nFATAL: Container runtime '{CONTAINER_RUNTIME}' is not available.")
        print(f"Install Docker or set CONTAINER_RUNTIME env var.\n")
        sys.exit(1)


# --- Entry points ---


async def _async_main(mode: str = "cli") -> None:
    _ensure_container_runtime()
    init_database()
    logger.info("Database initialized")
    _load_state()

    # If no groups registered, register default CLI group
    if not _registered_groups:
        from datetime import datetime, timezone
        from nanoclaw.channels.cli import CLI_JID
        _register_group(CLI_JID, RegisteredGroup(
            name="CLI",
            folder="main",
            trigger=f"@{ASSISTANT_NAME}",
            added_at=datetime.now(timezone.utc).isoformat(),
            requires_trigger=False,
        ))

    # Graceful shutdown
    shutdown_event = asyncio.Event()

    def _signal_handler():
        logger.info("Shutdown signal received")
        shutdown_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler)

    # Build channel(s)
    from nanoclaw.channels.cli import CLIChannel

    def on_message(jid: str, msg: NewMessage) -> None:
        store_message(msg)

    def on_metadata(jid: str, ts: str) -> None:
        store_chat_metadata(jid, ts)

    cli = CLIChannel(on_message=on_message, on_chat_metadata=on_metadata)
    await cli.connect()
    _channels.append(cli)

    # Build deps
    async def send_msg(jid: str, text: str) -> None:
        for ch in _channels:
            if ch.owns_jid(jid):
                await ch.send_message(jid, text)
                return

    ipc_deps = IpcDeps(
        send_message=send_msg,
        registered_groups=lambda: _registered_groups,
        register_group=_register_group,
        get_available_groups=_get_available_groups,
        write_groups_snapshot=write_groups_snapshot,
    )

    scheduler_deps = SchedulerDeps(
        registered_groups=lambda: _registered_groups,
        get_sessions=lambda: _sessions,
        queue=_queue,
        on_process=lambda jid, proc, name, folder: _queue.register_process(jid, proc, name, folder),
        send_message=send_msg,
    )

    _queue.set_process_messages_fn(_process_group_messages)
    _recover_pending()

    # Start all subsystems
    tasks = [
        asyncio.create_task(cli.start_reading()),
        asyncio.create_task(start_ipc_watcher(ipc_deps)),
        asyncio.create_task(start_scheduler_loop(scheduler_deps)),
        asyncio.create_task(_message_loop()),
    ]

    if mode == "http" or mode == "both":
        from nanoclaw.channels.http import HTTPChannel
        http = HTTPChannel(on_message=on_message, on_chat_metadata=on_metadata)
        await http.connect()
        _channels.append(http)
        tasks.append(asyncio.create_task(http.start_server()))

    # Wait for shutdown signal
    await shutdown_event.wait()
    logger.info("Shutting down...")
    await _queue.shutdown()
    for ch in _channels:
        await ch.disconnect()
    for t in tasks:
        t.cancel()


def cli_entry() -> None:
    """Entry point for `nanoclaw` CLI command."""
    import argparse
    parser = argparse.ArgumentParser(description="NanoClaw — Personal Claude Agent")
    parser.add_argument(
        "--mode", choices=["cli", "http", "both"], default="cli",
        help="Input channel mode (default: cli)",
    )
    args = parser.parse_args()
    asyncio.run(_async_main(mode=args.mode))


if __name__ == "__main__":
    cli_entry()
