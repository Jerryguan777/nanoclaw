"""Per-group concurrent queue with global concurrency limit.

Manages container processes across groups, handling message queueing,
task prioritization, retry with exponential backoff, and graceful shutdown.
"""

from __future__ import annotations

import asyncio
import json
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from nanoclaw.config import DATA_DIR, MAX_CONCURRENT_CONTAINERS
from nanoclaw.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = get_logger()

_MAX_RETRIES = 5
_BASE_RETRY_MS = 5000


@dataclass
class _QueuedTask:
    id: str
    group_jid: str
    fn: Callable[[], Awaitable[None]]


@dataclass
class _GroupState:
    active: bool = False
    idle_waiting: bool = False
    is_task_container: bool = False
    running_task_id: str | None = None
    pending_messages: bool = False
    pending_tasks: list[_QueuedTask] = field(default_factory=list)
    process: object | None = None  # asyncio.subprocess.Process
    container_name: str | None = None
    group_folder: str | None = None
    retry_count: int = 0


class GroupQueue:
    """Manages per-group container concurrency and task/message queuing."""

    def __init__(self) -> None:
        self._groups: dict[str, _GroupState] = {}
        self._active_count: int = 0
        self._waiting_groups: list[str] = []
        self._process_messages_fn: Callable[[str], Awaitable[bool]] | None = None
        self._shutting_down: bool = False
        self._background_tasks: set[asyncio.Task[None]] = set()

    def _spawn(self, coro: Awaitable[None]) -> None:
        """Launch a background task and track it to prevent GC."""
        task = asyncio.ensure_future(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _get_group(self, group_jid: str) -> _GroupState:
        state = self._groups.get(group_jid)
        if state is None:
            state = _GroupState()
            self._groups[group_jid] = state
        return state

    def set_process_messages_fn(self, fn: Callable[[str], Awaitable[bool]]) -> None:
        """Set the callback for processing messages for a group."""
        self._process_messages_fn = fn

    def enqueue_message_check(self, group_jid: str) -> None:
        """Queue a message check for a group."""
        if self._shutting_down:
            return

        state = self._get_group(group_jid)

        if state.active:
            state.pending_messages = True
            logger.debug("Container active, message queued", group_jid=group_jid)
            return

        if self._active_count >= MAX_CONCURRENT_CONTAINERS:
            state.pending_messages = True
            if group_jid not in self._waiting_groups:
                self._waiting_groups.append(group_jid)
            logger.debug(
                "At concurrency limit, message queued",
                group_jid=group_jid,
                active_count=self._active_count,
            )
            return

        self._spawn(self._run_for_group(group_jid, "messages"))

    def enqueue_task(self, group_jid: str, task_id: str, fn: Callable[[], Awaitable[None]]) -> None:
        """Queue a task for execution."""
        if self._shutting_down:
            return

        state = self._get_group(group_jid)

        # Prevent double-queuing
        if state.running_task_id == task_id:
            logger.debug("Task already running, skipping", group_jid=group_jid, task_id=task_id)
            return
        if any(t.id == task_id for t in state.pending_tasks):
            logger.debug("Task already queued, skipping", group_jid=group_jid, task_id=task_id)
            return

        if state.active:
            state.pending_tasks.append(_QueuedTask(id=task_id, group_jid=group_jid, fn=fn))
            if state.idle_waiting:
                self.close_stdin(group_jid)
            logger.debug("Container active, task queued", group_jid=group_jid, task_id=task_id)
            return

        if self._active_count >= MAX_CONCURRENT_CONTAINERS:
            state.pending_tasks.append(_QueuedTask(id=task_id, group_jid=group_jid, fn=fn))
            if group_jid not in self._waiting_groups:
                self._waiting_groups.append(group_jid)
            logger.debug(
                "At concurrency limit, task queued",
                group_jid=group_jid,
                task_id=task_id,
                active_count=self._active_count,
            )
            return

        # Run immediately
        task = _QueuedTask(id=task_id, group_jid=group_jid, fn=fn)
        self._spawn(self._run_task(group_jid, task))

    def register_process(
        self,
        group_jid: str,
        proc: object,
        container_name: str,
        group_folder: str | None = None,
    ) -> None:
        """Track an active container process for a group."""
        state = self._get_group(group_jid)
        state.process = proc
        state.container_name = container_name
        if group_folder:
            state.group_folder = group_folder

    def notify_idle(self, group_jid: str) -> None:
        """Mark container as idle-waiting. Preempt if tasks pending."""
        state = self._get_group(group_jid)
        state.idle_waiting = True
        if state.pending_tasks:
            self.close_stdin(group_jid)

    def send_message(self, group_jid: str, text: str) -> bool:
        """Send a follow-up message to the active container via IPC file."""
        state = self._get_group(group_jid)
        if not state.active or not state.group_folder or state.is_task_container:
            return False
        state.idle_waiting = False

        input_dir = DATA_DIR / "ipc" / state.group_folder / "input"
        try:
            input_dir.mkdir(parents=True, exist_ok=True)
            import time

            filename = f"{int(time.time() * 1000)}-{id(text) % 10000:04d}.json"
            filepath = input_dir / filename
            temp_path = filepath.with_suffix(".json.tmp")
            temp_path.write_text(json.dumps({"type": "message", "text": text}))
            temp_path.rename(filepath)
            return True
        except OSError:
            return False

    def close_stdin(self, group_jid: str) -> None:
        """Signal the active container to wind down."""
        state = self._get_group(group_jid)
        if not state.active or not state.group_folder:
            return

        input_dir = DATA_DIR / "ipc" / state.group_folder / "input"
        try:
            input_dir.mkdir(parents=True, exist_ok=True)
            (input_dir / "_close").write_text("")
        except OSError:
            pass

    async def _run_for_group(self, group_jid: str, reason: str) -> None:
        state = self._get_group(group_jid)
        state.active = True
        state.idle_waiting = False
        state.is_task_container = False
        state.pending_messages = False
        self._active_count += 1

        logger.debug(
            "Starting container for group",
            group_jid=group_jid,
            reason=reason,
            active_count=self._active_count,
        )

        try:
            if self._process_messages_fn:
                success = await self._process_messages_fn(group_jid)
                if success:
                    state.retry_count = 0
                else:
                    self._schedule_retry(group_jid, state)
        except Exception:
            logger.exception("Error processing messages for group", group_jid=group_jid)
            self._schedule_retry(group_jid, state)
        finally:
            state.active = False
            state.process = None
            state.container_name = None
            state.group_folder = None
            self._active_count -= 1
            self._drain_group(group_jid)

    async def _run_task(self, group_jid: str, task: _QueuedTask) -> None:
        state = self._get_group(group_jid)
        state.active = True
        state.idle_waiting = False
        state.is_task_container = True
        state.running_task_id = task.id
        self._active_count += 1

        logger.debug(
            "Running queued task",
            group_jid=group_jid,
            task_id=task.id,
            active_count=self._active_count,
        )

        try:
            await task.fn()
        except Exception:
            logger.exception("Error running task", group_jid=group_jid, task_id=task.id)
        finally:
            state.active = False
            state.is_task_container = False
            state.running_task_id = None
            state.process = None
            state.container_name = None
            state.group_folder = None
            self._active_count -= 1
            self._drain_group(group_jid)

    def _schedule_retry(self, group_jid: str, state: _GroupState) -> None:
        state.retry_count += 1
        if state.retry_count > _MAX_RETRIES:
            logger.error(
                "Max retries exceeded, dropping messages",
                group_jid=group_jid,
                retry_count=state.retry_count,
            )
            state.retry_count = 0
            return

        delay_s = (_BASE_RETRY_MS * math.pow(2, state.retry_count - 1)) / 1000.0
        logger.info(
            "Scheduling retry with backoff",
            group_jid=group_jid,
            retry_count=state.retry_count,
            delay_s=delay_s,
        )

        async def _retry() -> None:
            await asyncio.sleep(delay_s)
            if not self._shutting_down:
                self.enqueue_message_check(group_jid)

        self._spawn(_retry())

    def _drain_group(self, group_jid: str) -> None:
        if self._shutting_down:
            return

        state = self._get_group(group_jid)

        # Tasks first (they won't be re-discovered from SQLite like messages)
        if state.pending_tasks:
            task = state.pending_tasks.pop(0)
            self._spawn(self._run_task(group_jid, task))
            return

        # Then pending messages
        if state.pending_messages:
            self._spawn(self._run_for_group(group_jid, "drain"))
            return

        # Nothing pending; check if other groups are waiting for a slot
        self._drain_waiting()

    def _drain_waiting(self) -> None:
        while self._waiting_groups and self._active_count < MAX_CONCURRENT_CONTAINERS:
            next_jid = self._waiting_groups.pop(0)
            state = self._get_group(next_jid)

            if state.pending_tasks:
                task = state.pending_tasks.pop(0)
                self._spawn(self._run_task(next_jid, task))
            elif state.pending_messages:
                self._spawn(self._run_for_group(next_jid, "drain"))

    async def shutdown(self, _grace_period_ms: int = 0) -> None:
        """Graceful shutdown — detach containers, don't kill them."""
        self._shutting_down = True

        active_containers: list[str] = []
        for state in self._groups.values():
            if state.process and state.container_name:
                active_containers.append(state.container_name)

        logger.info(
            "GroupQueue shutting down (containers detached, not killed)",
            active_count=self._active_count,
            detached_containers=active_containers,
        )
