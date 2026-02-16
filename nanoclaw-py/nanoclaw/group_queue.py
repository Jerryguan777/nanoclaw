"""Group queue — manages per-group concurrency and message/task ordering.

Port of src/group-queue.ts. Uses asyncio primitives instead of Node callbacks.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Awaitable, Callable

from nanoclaw.config import DATA_DIR, MAX_CONCURRENT_CONTAINERS
from nanoclaw.logger import logger

MAX_RETRIES = 5
BASE_RETRY_S = 5.0


class _QueuedTask:
    __slots__ = ("id", "group_jid", "fn")

    def __init__(self, task_id: str, group_jid: str, fn: Callable[[], Awaitable[None]]):
        self.id = task_id
        self.group_jid = group_jid
        self.fn = fn


class _GroupState:
    __slots__ = (
        "active", "pending_messages", "pending_tasks",
        "process", "container_name", "group_folder", "retry_count",
    )

    def __init__(self) -> None:
        self.active = False
        self.pending_messages = False
        self.pending_tasks: list[_QueuedTask] = []
        self.process: asyncio.subprocess.Process | None = None
        self.container_name: str | None = None
        self.group_folder: str | None = None
        self.retry_count = 0


class GroupQueue:
    def __init__(self) -> None:
        self._groups: dict[str, _GroupState] = {}
        self._active_count = 0
        self._waiting: list[str] = []
        self._process_messages_fn: Callable[[str], Awaitable[bool]] | None = None
        self._shutting_down = False

    def _get(self, jid: str) -> _GroupState:
        if jid not in self._groups:
            self._groups[jid] = _GroupState()
        return self._groups[jid]

    def set_process_messages_fn(self, fn: Callable[[str], Awaitable[bool]]) -> None:
        self._process_messages_fn = fn

    def enqueue_message_check(self, group_jid: str) -> None:
        if self._shutting_down:
            return
        state = self._get(group_jid)
        if state.active:
            state.pending_messages = True
            return
        if self._active_count >= MAX_CONCURRENT_CONTAINERS:
            state.pending_messages = True
            if group_jid not in self._waiting:
                self._waiting.append(group_jid)
            return
        asyncio.ensure_future(self._run_for_group(group_jid, "messages"))

    def enqueue_task(self, group_jid: str, task_id: str, fn: Callable[[], Awaitable[None]]) -> None:
        if self._shutting_down:
            return
        state = self._get(group_jid)
        if any(t.id == task_id for t in state.pending_tasks):
            return
        qt = _QueuedTask(task_id, group_jid, fn)
        if state.active:
            state.pending_tasks.append(qt)
            return
        if self._active_count >= MAX_CONCURRENT_CONTAINERS:
            state.pending_tasks.append(qt)
            if group_jid not in self._waiting:
                self._waiting.append(group_jid)
            return
        asyncio.ensure_future(self._run_task(group_jid, qt))

    def register_process(
        self, group_jid: str, proc: asyncio.subprocess.Process,
        container_name: str, group_folder: str | None = None,
    ) -> None:
        state = self._get(group_jid)
        state.process = proc
        state.container_name = container_name
        if group_folder:
            state.group_folder = group_folder

    def send_message(self, group_jid: str, text: str) -> bool:
        state = self._get(group_jid)
        if not state.active or not state.group_folder:
            return False
        input_dir = DATA_DIR / "ipc" / state.group_folder / "input"
        try:
            input_dir.mkdir(parents=True, exist_ok=True)
            filename = f"{int(time.time() * 1000)}-{id(text) % 10000:04d}.json"
            filepath = input_dir / filename
            tmp = filepath.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({"type": "message", "text": text}))
            tmp.rename(filepath)
            return True
        except Exception:
            return False

    def close_stdin(self, group_jid: str) -> None:
        state = self._get(group_jid)
        if not state.active or not state.group_folder:
            return
        input_dir = DATA_DIR / "ipc" / state.group_folder / "input"
        try:
            input_dir.mkdir(parents=True, exist_ok=True)
            (input_dir / "_close").write_text("")
        except Exception:
            pass

    async def _run_for_group(self, group_jid: str, reason: str) -> None:
        state = self._get(group_jid)
        state.active = True
        state.pending_messages = False
        self._active_count += 1

        logger.debug("Starting container for group", group_jid=group_jid, reason=reason)

        try:
            if self._process_messages_fn:
                success = await self._process_messages_fn(group_jid)
                if success:
                    state.retry_count = 0
                else:
                    self._schedule_retry(group_jid, state)
        except Exception as e:
            logger.error("Error processing messages", group_jid=group_jid, error=str(e))
            self._schedule_retry(group_jid, state)
        finally:
            state.active = False
            state.process = None
            state.container_name = None
            state.group_folder = None
            self._active_count -= 1
            self._drain_group(group_jid)

    async def _run_task(self, group_jid: str, task: _QueuedTask) -> None:
        state = self._get(group_jid)
        state.active = True
        self._active_count += 1

        try:
            await task.fn()
        except Exception as e:
            logger.error("Error running task", group_jid=group_jid, task_id=task.id, error=str(e))
        finally:
            state.active = False
            state.process = None
            state.container_name = None
            state.group_folder = None
            self._active_count -= 1
            self._drain_group(group_jid)

    def _schedule_retry(self, group_jid: str, state: _GroupState) -> None:
        state.retry_count += 1
        if state.retry_count > MAX_RETRIES:
            logger.error("Max retries exceeded", group_jid=group_jid)
            state.retry_count = 0
            return
        delay = BASE_RETRY_S * (2 ** (state.retry_count - 1))
        logger.info("Scheduling retry", group_jid=group_jid, retry=state.retry_count, delay_s=delay)

        async def _retry():
            await asyncio.sleep(delay)
            if not self._shutting_down:
                self.enqueue_message_check(group_jid)

        asyncio.ensure_future(_retry())

    def _drain_group(self, group_jid: str) -> None:
        if self._shutting_down:
            return
        state = self._get(group_jid)
        if state.pending_tasks:
            task = state.pending_tasks.pop(0)
            asyncio.ensure_future(self._run_task(group_jid, task))
            return
        if state.pending_messages:
            asyncio.ensure_future(self._run_for_group(group_jid, "drain"))
            return
        self._drain_waiting()

    def _drain_waiting(self) -> None:
        while self._waiting and self._active_count < MAX_CONCURRENT_CONTAINERS:
            jid = self._waiting.pop(0)
            state = self._get(jid)
            if state.pending_tasks:
                task = state.pending_tasks.pop(0)
                asyncio.ensure_future(self._run_task(jid, task))
            elif state.pending_messages:
                asyncio.ensure_future(self._run_for_group(jid, "drain"))

    async def shutdown(self, grace_period_ms: int = 10_000) -> None:
        self._shutting_down = True
        active = [
            s.container_name
            for s in self._groups.values()
            if s.process and s.container_name
        ]
        logger.info("GroupQueue shutting down", active_count=self._active_count, detached=active)
