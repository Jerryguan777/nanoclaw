"""Scheduled task execution (cron, interval, once).

Runs a polling loop that picks up due tasks and enqueues them for
container execution via the GroupQueue.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

from croniter import croniter

from nanoclaw.container.runner import (
    ContainerInput,
    ContainerOutput,
    run_container_agent,
    write_tasks_snapshot,
)
from nanoclaw.core.config import ASSISTANT_NAME, SCHEDULER_POLL_INTERVAL
from nanoclaw.core.group_folder import resolve_group_folder_path
from nanoclaw.core.logger import get_logger
from nanoclaw.core.types import RegisteredGroup, ScheduledTask, TaskRunLog
from nanoclaw.db.sqlite import (
    get_all_tasks,
    get_due_tasks,
    get_task_by_id,
    log_task_run,
    update_task,
    update_task_after_run,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from nanoclaw.container.scheduler import GroupQueue

logger = get_logger()


def compute_next_run(task: ScheduledTask) -> str | None:
    """Compute the next run time for a recurring task.

    Anchors intervals to the task's scheduled time rather than now
    to prevent cumulative drift.
    """
    if task.schedule_type == "once":
        return None

    now = time.time()

    if task.schedule_type == "cron":
        cron = croniter(task.schedule_value, datetime.now(UTC))
        return cron.get_next(datetime).isoformat()

    if task.schedule_type == "interval":
        try:
            ms = int(task.schedule_value)
        except (ValueError, TypeError):
            ms = 0

        if ms <= 0:
            # Guard against malformed interval that would cause an infinite loop
            logger.warning("Invalid interval value", task_id=task.id, value=task.schedule_value)
            return datetime.fromtimestamp(now + 60.0, tz=UTC).isoformat()

        # Anchor to the scheduled time, not now, to prevent drift.
        # Skip past any missed intervals so we always land in the future.
        assert task.next_run is not None
        next_ts = datetime.fromisoformat(task.next_run).timestamp() + ms / 1000.0
        while next_ts <= now:
            next_ts += ms / 1000.0
        return datetime.fromtimestamp(next_ts, tz=UTC).isoformat()

    return None


class SchedulerDependencies(Protocol):
    """Dependencies injected into the scheduler loop."""

    def registered_groups(self) -> dict[str, RegisteredGroup]: ...
    def get_sessions(self) -> dict[str, str]: ...
    @property
    def queue(self) -> GroupQueue: ...
    def on_process(
        self,
        group_jid: str,
        proc: object,
        container_name: str,
        group_folder: str,
    ) -> None: ...
    def send_message(self, jid: str, text: str) -> Awaitable[None]: ...


_TASK_CLOSE_DELAY_S: float = 10.0


async def _run_task(
    task: ScheduledTask,
    deps: SchedulerDependencies,
) -> None:
    """Run a single scheduled task in a container."""
    start_time = time.monotonic()
    time.time()

    try:
        group_dir = resolve_group_folder_path(task.group_folder)
    except ValueError as exc:
        err_msg = str(exc)
        # Stop retry churn for malformed legacy rows.
        update_task(task.id, status="paused")
        logger.error("Task has invalid group folder", task_id=task.id, group_folder=task.group_folder, error=err_msg)
        log_task_run(
            TaskRunLog(
                task_id=task.id,
                run_at=datetime.now(UTC).isoformat(),
                duration_ms=int((time.monotonic() - start_time) * 1000),
                status="error",
                result=None,
                error=err_msg,
            )
        )
        return

    group_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Running scheduled task", task_id=task.id, group=task.group_folder)

    groups = deps.registered_groups()
    group = next((g for g in groups.values() if g.folder == task.group_folder), None)

    if group is None:
        logger.error("Group not found for task", task_id=task.id, group_folder=task.group_folder)
        log_task_run(
            TaskRunLog(
                task_id=task.id,
                run_at=datetime.now(UTC).isoformat(),
                duration_ms=int((time.monotonic() - start_time) * 1000),
                status="error",
                result=None,
                error=f"Group not found: {task.group_folder}",
            )
        )
        return

    # Update tasks snapshot for container to read (filtered by group)
    is_main = group.is_main
    all_tasks = get_all_tasks()
    write_tasks_snapshot(
        task.group_folder,
        is_main,
        [
            {
                "id": t.id,
                "groupFolder": t.group_folder,
                "prompt": t.prompt,
                "schedule_type": t.schedule_type,
                "schedule_value": t.schedule_value,
                "status": t.status,
                "next_run": t.next_run,
            }
            for t in all_tasks
        ],
    )

    result: str | None = None
    error: str | None = None

    # For group context mode, use the group's current session
    sessions = deps.get_sessions()
    session_id = sessions.get(task.group_folder) if task.context_mode == "group" else None

    # After the task produces a result, close the container promptly.
    close_handle: asyncio.TimerHandle | None = None

    def _schedule_close() -> None:
        nonlocal close_handle
        if close_handle is not None:
            return  # already scheduled
        loop = asyncio.get_running_loop()
        close_handle = loop.call_later(
            _TASK_CLOSE_DELAY_S,
            lambda: deps.queue.close_stdin(task.chat_jid),
        )

    try:

        async def _on_output(streamed_output: ContainerOutput) -> None:
            nonlocal result, error
            if streamed_output.result:
                result = streamed_output.result
                await deps.send_message(task.chat_jid, streamed_output.result)
                _schedule_close()
            if streamed_output.status == "success":
                deps.queue.notify_idle(task.chat_jid)
                _schedule_close()
            if streamed_output.status == "error":
                error = streamed_output.error or "Unknown error"

        output = await run_container_agent(
            group,
            ContainerInput(
                prompt=task.prompt,
                session_id=session_id,
                group_folder=task.group_folder,
                chat_jid=task.chat_jid,
                is_main=is_main,
                is_scheduled_task=True,
                assistant_name=ASSISTANT_NAME,
            ),
            lambda proc, container_name: deps.on_process(task.chat_jid, proc, container_name, task.group_folder),
            _on_output,
        )

        if close_handle is not None:
            close_handle.cancel()

        if output.status == "error":
            error = output.error or "Unknown error"
        elif output.result:
            result = output.result

        logger.info("Task completed", task_id=task.id, duration_ms=int((time.monotonic() - start_time) * 1000))
    except (OSError, RuntimeError, ValueError) as exc:
        if close_handle is not None:
            close_handle.cancel()
        error = str(exc)
        logger.error("Task failed", task_id=task.id, error=error)

    duration_ms = int((time.monotonic() - start_time) * 1000)

    log_task_run(
        TaskRunLog(
            task_id=task.id,
            run_at=datetime.now(UTC).isoformat(),
            duration_ms=duration_ms,
            status="error" if error else "success",
            result=result,
            error=error,
        )
    )

    next_run = compute_next_run(task)
    result_summary: str
    if error:
        result_summary = f"Error: {error}"
    elif result:
        result_summary = result[:200]
    else:
        result_summary = "Completed"
    update_task_after_run(task.id, next_run, result_summary)


_scheduler_running: bool = False


def start_scheduler_loop(deps: SchedulerDependencies) -> asyncio.Task[None]:
    """Launch an asyncio task that polls for due scheduled tasks."""
    global _scheduler_running
    if _scheduler_running:
        logger.debug("Scheduler loop already running, skipping duplicate start")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[None] = loop.create_future()
        fut.set_result(None)
        return asyncio.ensure_future(fut)
    _scheduler_running = True
    logger.info("Scheduler loop started")

    async def _loop() -> None:
        while True:
            try:
                due_tasks = get_due_tasks()
                if due_tasks:
                    logger.info("Found due tasks", count=len(due_tasks))

                for task in due_tasks:
                    # Re-check task status in case it was paused/cancelled
                    current_task = get_task_by_id(task.id)
                    if not current_task or current_task.status != "active":
                        continue

                    def _make_fn(t: ScheduledTask) -> Callable[[], Awaitable[None]]:
                        async def _task_fn() -> None:
                            await _run_task(t, deps)

                        return _task_fn

                    deps.queue.enqueue_task(
                        current_task.chat_jid,
                        current_task.id,
                        _make_fn(current_task),
                    )
            except (OSError, RuntimeError, ValueError):
                logger.exception("Error in scheduler loop")

            await asyncio.sleep(SCHEDULER_POLL_INTERVAL)

    return asyncio.create_task(_loop())


def _reset_scheduler_loop_for_tests() -> None:
    """Reset module state for tests."""
    global _scheduler_running
    _scheduler_running = False
