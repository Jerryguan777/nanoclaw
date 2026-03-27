"""Container spawning with NATS-based IPC.

Spawns agent execution in containers. All communication between the
Orchestrator and Agent happens over NATS (KV + JetStream + request-reply).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from nanoclaw.container.runtime import (
    CONTAINER_HOST_GATEWAY,
    CONTAINER_RUNTIME_BIN,
    host_gateway_args,
    readonly_mount_args,
    stop_container,
)
from nanoclaw.core.config import (
    CONTAINER_IMAGE,
    CONTAINER_MAX_OUTPUT_SIZE,
    CONTAINER_TIMEOUT,
    CREDENTIAL_PROXY_PORT,
    DATA_DIR,
    GROUPS_DIR,
    IDLE_TIMEOUT,
    NATS_URL,
    PROJECT_ROOT,
    TIMEZONE,
)
from nanoclaw.core.group_folder import resolve_group_folder_path
from nanoclaw.core.logger import get_logger
from nanoclaw.ipc.protocol import AgentInitData
from nanoclaw.security.credential_proxy import detect_auth_mode
from nanoclaw.security.mount_security import validate_additional_mounts

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from nanoclaw.core.types import RegisteredGroup
    from nanoclaw.ipc.nats_transport import NatsTransport

logger = get_logger()


@dataclass
class ContainerInput:
    """Input payload for the container agent."""

    prompt: str
    group_folder: str
    chat_jid: str
    is_main: bool
    session_id: str | None = None
    is_scheduled_task: bool = False
    assistant_name: str | None = None


@dataclass
class ContainerOutput:
    """Parsed output from the container agent."""

    status: Literal["success", "error"]
    result: str | None
    new_session_id: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class VolumeMount:
    """A bind mount specification for container execution."""

    host_path: str
    container_path: str
    readonly: bool


@dataclass(frozen=True)
class AvailableGroup:
    """A group visible to containers for activation."""

    jid: str
    name: str
    last_activity: str
    is_registered: bool


def _parse_container_output(raw: dict[str, object]) -> ContainerOutput:
    """Parse a raw JSON dict into a ContainerOutput, handling camelCase keys."""
    result_val = raw.get("result")
    new_sid = raw.get("newSessionId")
    err_val = raw.get("error")
    return ContainerOutput(
        status=str(raw.get("status", "error")),  # type: ignore[arg-type]
        result=str(result_val) if result_val is not None else None,
        new_session_id=str(new_sid) if new_sid is not None else None,
        error=str(err_val) if err_val is not None else None,
    )


def build_volume_mounts(
    group: RegisteredGroup,
    is_main: bool,
) -> list[VolumeMount]:
    """Build the list of volume mounts for a container invocation.

    This is a pure computation function (synchronous).
    """
    mounts: list[VolumeMount] = []
    project_root = PROJECT_ROOT
    group_dir = resolve_group_folder_path(group.folder)

    if is_main:
        # Main gets the project root read-only. Writable paths the agent needs
        # (group folder, .claude/) are mounted separately below.
        mounts.append(
            VolumeMount(
                host_path=str(project_root),
                container_path="/workspace/project",
                readonly=True,
            )
        )

        # Shadow .env so the agent cannot read secrets from the mounted project root.
        env_file = project_root / ".env"
        if env_file.exists():
            mounts.append(
                VolumeMount(
                    host_path="/dev/null",
                    container_path="/workspace/project/.env",
                    readonly=True,
                )
            )

        # Main also gets its group folder as the working directory
        mounts.append(
            VolumeMount(
                host_path=str(group_dir),
                container_path="/workspace/group",
                readonly=False,
            )
        )
    else:
        # Other groups only get their own folder
        mounts.append(
            VolumeMount(
                host_path=str(group_dir),
                container_path="/workspace/group",
                readonly=False,
            )
        )

        # Global memory directory (read-only for non-main)
        global_dir = GROUPS_DIR / "global"
        if global_dir.exists():
            mounts.append(
                VolumeMount(
                    host_path=str(global_dir),
                    container_path="/workspace/global",
                    readonly=True,
                )
            )

    # Per-group Claude sessions directory (isolated from other groups)
    group_sessions_dir = DATA_DIR / "sessions" / group.folder / ".claude"
    group_sessions_dir.mkdir(parents=True, exist_ok=True)
    settings_file = group_sessions_dir / "settings.json"
    if not settings_file.exists():
        settings_file.write_text(
            json.dumps(
                {
                    "env": {
                        "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
                        "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD": "1",
                        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "0",
                    },
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    # Sync skills from container/skills/ into each group's .claude/skills/
    skills_src = project_root / "container" / "skills"
    skills_dst = group_sessions_dir / "skills"
    if skills_src.exists():
        for skill_dir in skills_src.iterdir():
            if not skill_dir.is_dir():
                continue
            dst_dir = skills_dst / skill_dir.name
            shutil.copytree(str(skill_dir), str(dst_dir), dirs_exist_ok=True)

    mounts.append(
        VolumeMount(
            host_path=str(group_sessions_dir),
            container_path="/home/agent/.claude",
            readonly=False,
        )
    )

    # No IPC directory mount needed — all communication goes through NATS.

    # Additional mounts validated against external allowlist (tamper-proof from containers)
    if group.container_config and group.container_config.additional_mounts:
        validated_mounts = validate_additional_mounts(
            group.container_config.additional_mounts,
            group.name,
            is_main,
        )
        for m in validated_mounts:
            mounts.append(
                VolumeMount(
                    host_path=str(m["host_path"]),
                    container_path=str(m["container_path"]),
                    readonly=bool(m["readonly"]),
                )
            )

    return mounts


def build_container_args(
    mounts: list[VolumeMount],
    container_name: str,
    job_id: str,
) -> list[str]:
    """Build the CLI arguments for the container runtime.

    This is a pure computation function (synchronous).
    """
    args: list[str] = ["run", "-i", "--rm", "--name", container_name]

    # Pass host timezone so container's local time matches the user's
    args.extend(["-e", f"TZ={TIMEZONE}"])

    # NATS connection for IPC
    nats_url = NATS_URL.replace("localhost", CONTAINER_HOST_GATEWAY)
    args.extend(["-e", f"NATS_URL={nats_url}"])
    args.extend(["-e", f"JOB_ID={job_id}"])

    # Route API traffic through the credential proxy (containers never see real secrets)
    args.extend(
        [
            "-e",
            f"ANTHROPIC_BASE_URL=http://{CONTAINER_HOST_GATEWAY}:{CREDENTIAL_PROXY_PORT}",
        ]
    )

    # Mirror the host's auth method with a placeholder value.
    auth_mode = detect_auth_mode()
    if auth_mode == "api-key":
        args.extend(["-e", "ANTHROPIC_API_KEY=placeholder"])
    else:
        args.extend(["-e", "CLAUDE_CODE_OAUTH_TOKEN=placeholder"])

    # Runtime-specific args for host gateway resolution
    args.extend(host_gateway_args())

    # Run as host user so bind-mounted files are accessible.
    host_uid: int | None = None
    host_gid: int | None = None
    if hasattr(os, "getuid"):
        host_uid = os.getuid()
    if hasattr(os, "getgid"):
        host_gid = os.getgid()
    if host_uid is not None and host_uid != 0 and host_uid != 1000:
        args.extend(["--user", f"{host_uid}:{host_gid}"])
        args.extend(["-e", "HOME=/home/agent"])

    for mount in mounts:
        if mount.readonly:
            args.extend(readonly_mount_args(mount.host_path, mount.container_path))
        else:
            args.extend(["-v", f"{mount.host_path}:{mount.container_path}"])

    args.append(CONTAINER_IMAGE)

    return args


async def run_container_agent(
    group: RegisteredGroup,
    inp: ContainerInput,
    on_process: Callable[[asyncio.subprocess.Process, str, str], None],
    on_output: Callable[[ContainerOutput], Awaitable[None]] | None = None,
    transport: NatsTransport | None = None,
) -> ContainerOutput:
    """Run the agent inside a container with NATS-based IPC.

    Channel 1: Writes initial input to KV before starting container.
    Channel 2: Subscribes to JetStream for streaming results.
    Channel 6: Writes snapshots to KV before starting container.
    Stderr is still read for logging purposes.
    """
    start_time = time.monotonic()
    start_epoch_ms = int(time.time() * 1000)

    group_dir = resolve_group_folder_path(group.folder)
    group_dir.mkdir(parents=True, exist_ok=True)

    job_id = f"{group.folder}-{uuid.uuid4().hex[:12]}"

    mounts = build_volume_mounts(group, inp.is_main)
    safe_name = re.sub(r"[^a-zA-Z0-9-]", "-", group.folder)
    container_name = f"nanoclaw-{safe_name}-{start_epoch_ms}"
    container_args = build_container_args(mounts, container_name, job_id)

    logger.debug(
        "Container mount configuration",
        group=group.name,
        container_name=container_name,
        job_id=job_id,
        mounts=[f"{m.host_path} -> {m.container_path}{' (ro)' if m.readonly else ''}" for m in mounts],
        container_args=" ".join(container_args),
    )

    logger.info(
        "Spawning container agent",
        group=group.name,
        container_name=container_name,
        job_id=job_id,
        mount_count=len(mounts),
        is_main=inp.is_main,
    )

    logs_dir = group_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    # Channel 1: Write initial input to KV before starting container
    if transport is not None:
        kv_init = await transport.js.key_value("agent-init")
        agent_init = AgentInitData(
            prompt=inp.prompt,
            group_folder=inp.group_folder,
            chat_jid=inp.chat_jid,
            is_main=inp.is_main,
            session_id=inp.session_id,
            is_scheduled_task=inp.is_scheduled_task,
            assistant_name=inp.assistant_name,
        )
        await kv_init.put(job_id, agent_init.serialize())

    proc = await asyncio.create_subprocess_exec(
        CONTAINER_RUNTIME_BIN,
        *container_args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )

    on_process(proc, container_name, job_id)

    stderr_buf = ""
    stderr_truncated = False

    # Streaming output state
    new_session_id: str | None = None
    had_streaming_output = False
    timed_out = False

    config_timeout = (
        group.container_config.timeout if group.container_config else CONTAINER_TIMEOUT
    ) or CONTAINER_TIMEOUT
    timeout_ms = max(config_timeout, IDLE_TIMEOUT + 30_000)
    timeout_s = timeout_ms / 1000.0

    # Timeout management via an asyncio.Event and a watcher task
    activity_event = asyncio.Event()
    timeout_fired = asyncio.Event()

    async def _timeout_watcher() -> None:
        nonlocal timed_out
        while True:
            activity_event.clear()
            try:
                await asyncio.wait_for(activity_event.wait(), timeout=timeout_s)
            except TimeoutError:
                timed_out = True
                timeout_fired.set()
                logger.error(
                    "Container timeout, stopping gracefully",
                    group=group.name,
                    container_name=container_name,
                )
                try:
                    stop_proc = await asyncio.create_subprocess_exec(
                        *stop_container(container_name).split(),
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    try:
                        await asyncio.wait_for(stop_proc.wait(), timeout=15.0)
                    except TimeoutError:
                        logger.warning(
                            "Graceful stop failed, force killing",
                            group=group.name,
                            container_name=container_name,
                        )
                        proc.kill()
                except OSError:
                    proc.kill()
                return

    timeout_task = asyncio.create_task(_timeout_watcher())

    # Channel 2: Subscribe to JetStream for streaming results
    results_sub = None
    if transport is not None and on_output is not None:
        results_sub = await transport.js.subscribe(f"agent.{job_id}.results")

    async def _read_results() -> None:
        nonlocal new_session_id, had_streaming_output
        if results_sub is None:
            return
        async for msg in results_sub.messages:
            try:
                raw = json.loads(msg.data)
                parsed = _parse_container_output(raw)
                if parsed.new_session_id:
                    new_session_id = parsed.new_session_id
                had_streaming_output = True
                activity_event.set()
                if on_output is not None:
                    await on_output(parsed)
                await msg.ack()
            except (json.JSONDecodeError, KeyError, TypeError) as err:
                logger.warning(
                    "Failed to parse streamed output",
                    group=group.name,
                    error=str(err),
                )
                await msg.ack()

    async def _read_stderr() -> None:
        nonlocal stderr_buf, stderr_truncated
        assert proc.stderr is not None
        while True:
            chunk_bytes = await proc.stderr.read(8192)
            if not chunk_bytes:
                break
            chunk = chunk_bytes.decode("utf-8", errors="replace")
            lines = chunk.strip().split("\n")
            for line in lines:
                if line:
                    logger.debug(line, container=group.folder)
            if stderr_truncated:
                continue
            remaining = CONTAINER_MAX_OUTPUT_SIZE - len(stderr_buf)
            if len(chunk) > remaining:
                stderr_buf += chunk[:remaining]
                stderr_truncated = True
                logger.warning(
                    "Container stderr truncated due to size limit",
                    group=group.name,
                    size=len(stderr_buf),
                )
            else:
                stderr_buf += chunk

    # Run readers concurrently
    tasks: list[asyncio.Task[None]] = [asyncio.create_task(_read_stderr())]
    if results_sub is not None:
        tasks.append(asyncio.create_task(_read_results()))

    # Wait for process to exit
    code = await proc.wait()

    # Cancel the results subscription and readers
    if results_sub is not None:
        await results_sub.unsubscribe()
    for t in tasks:
        t.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await t

    # Cancel the timeout watcher
    timeout_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await timeout_task

    duration_ms = int((time.monotonic() - start_time) * 1000)

    if timed_out:
        ts = datetime.now(UTC).isoformat().replace(":", "-").replace(".", "-")
        timeout_log = logs_dir / f"container-{ts}.log"
        timeout_log.write_text(
            "\n".join(
                [
                    "=== Container Run Log (TIMEOUT) ===",
                    f"Timestamp: {datetime.now(UTC).isoformat()}",
                    f"Group: {group.name}",
                    f"Container: {container_name}",
                    f"Job ID: {job_id}",
                    f"Duration: {duration_ms}ms",
                    f"Exit Code: {code}",
                    f"Had Streaming Output: {had_streaming_output}",
                ]
            ),
            encoding="utf-8",
        )

        if had_streaming_output:
            logger.info(
                "Container timed out after output (idle cleanup)",
                group=group.name,
                container_name=container_name,
                duration=duration_ms,
                code=code,
            )
            return ContainerOutput(
                status="success",
                result=None,
                new_session_id=new_session_id,
            )

        logger.error(
            "Container timed out with no output",
            group=group.name,
            container_name=container_name,
            duration=duration_ms,
            code=code,
        )
        return ContainerOutput(
            status="error",
            result=None,
            error=f"Container timed out after {config_timeout}ms",
        )

    timestamp = datetime.now(UTC).isoformat().replace(":", "-").replace(".", "-")
    log_file = logs_dir / f"container-{timestamp}.log"
    is_verbose = os.environ.get("LOG_LEVEL") in ("debug", "trace")

    log_lines: list[str] = [
        "=== Container Run Log ===",
        f"Timestamp: {datetime.now(UTC).isoformat()}",
        f"Group: {group.name}",
        f"IsMain: {inp.is_main}",
        f"Job ID: {job_id}",
        f"Duration: {duration_ms}ms",
        f"Exit Code: {code}",
        f"Stderr Truncated: {stderr_truncated}",
        "",
    ]

    is_error = code != 0

    if is_verbose or is_error:
        if is_verbose:
            log_lines.extend(
                [
                    "=== Input Summary ===",
                    f"Prompt length: {len(inp.prompt)} chars",
                    f"Session ID: {inp.session_id or 'new'}",
                    f"Job ID: {job_id}",
                    "",
                ]
            )
        else:
            log_lines.extend(
                [
                    "=== Input Summary ===",
                    f"Prompt length: {len(inp.prompt)} chars",
                    f"Session ID: {inp.session_id or 'new'}",
                    "",
                ]
            )
        log_lines.extend(
            [
                "=== Container Args ===",
                " ".join(container_args),
                "",
                "=== Mounts ===",
                "\n".join(f"{m.host_path} -> {m.container_path}{' (ro)' if m.readonly else ''}" for m in mounts),
                "",
                f"=== Stderr{' (TRUNCATED)' if stderr_truncated else ''} ===",
                stderr_buf,
            ]
        )
    else:
        log_lines.extend(
            [
                "=== Input Summary ===",
                f"Prompt length: {len(inp.prompt)} chars",
                f"Session ID: {inp.session_id or 'new'}",
                "",
                "=== Mounts ===",
                "\n".join(f"{m.container_path}{' (ro)' if m.readonly else ''}" for m in mounts),
                "",
            ]
        )

    log_file.write_text("\n".join(log_lines), encoding="utf-8")
    logger.debug("Container log written", log_file=str(log_file), verbose=is_verbose)

    if code != 0:
        logger.error(
            "Container exited with error",
            group=group.name,
            code=code,
            duration=duration_ms,
            stderr=stderr_buf,
            log_file=str(log_file),
        )
        return ContainerOutput(
            status="error",
            result=None,
            error=f"Container exited with code {code}: {stderr_buf[-200:]}",
        )

    # Streaming mode: return completion marker
    logger.info(
        "Container completed",
        group=group.name,
        duration=duration_ms,
        new_session_id=new_session_id,
    )
    return ContainerOutput(
        status="success",
        result=None,
        new_session_id=new_session_id,
    )


async def write_tasks_snapshot(
    transport: NatsTransport,
    group_folder: str,
    is_main: bool,
    tasks: list[dict[str, object]],
) -> None:
    """Write filtered tasks to NATS KV for the agent to read.

    Main sees all tasks, others only see their own.
    """
    filtered_tasks: list[dict[str, object]]
    filtered_tasks = tasks if is_main else [t for t in tasks if t.get("groupFolder") == group_folder]

    kv = await transport.js.key_value("snapshots")
    await kv.put(f"{group_folder}.tasks", json.dumps(filtered_tasks).encode())


async def write_groups_snapshot(
    transport: NatsTransport,
    group_folder: str,
    is_main: bool,
    groups: list[AvailableGroup],
    _registered_jids: set[str],
) -> None:
    """Write available groups snapshot to NATS KV for the agent to read.

    Only main group can see all available groups (for activation).
    """
    visible_groups: list[dict[str, object]]
    if is_main:
        visible_groups = [
            {
                "jid": g.jid,
                "name": g.name,
                "lastActivity": g.last_activity,
                "isRegistered": g.is_registered,
            }
            for g in groups
        ]
    else:
        visible_groups = []

    kv = await transport.js.key_value("snapshots")
    await kv.put(
        f"{group_folder}.groups",
        json.dumps(
            {
                "groups": visible_groups,
                "lastSync": datetime.now(UTC).isoformat(),
            }
        ).encode(),
    )
