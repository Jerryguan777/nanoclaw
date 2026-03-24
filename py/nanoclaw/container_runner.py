"""Container spawning and JSON I/O protocol.

Spawns agent execution in containers and handles streaming output parsing.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from nanoclaw.config import (
    CONTAINER_IMAGE,
    CONTAINER_MAX_OUTPUT_SIZE,
    CONTAINER_TIMEOUT,
    CREDENTIAL_PROXY_PORT,
    DATA_DIR,
    GROUPS_DIR,
    IDLE_TIMEOUT,
    PROJECT_ROOT,
    TIMEZONE,
)
from nanoclaw.container_runtime import (
    CONTAINER_HOST_GATEWAY,
    CONTAINER_RUNTIME_BIN,
    host_gateway_args,
    readonly_mount_args,
    stop_container,
)
from nanoclaw.credential_proxy import detect_auth_mode
from nanoclaw.group_folder import resolve_group_folder_path, resolve_group_ipc_path
from nanoclaw.logger import get_logger
from nanoclaw.mount_security import validate_additional_mounts

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from nanoclaw.types import RegisteredGroup

logger = get_logger()

# Sentinel markers for robust output parsing (must match agent-runner)
OUTPUT_START_MARKER: str = "---NANOCLAW_OUTPUT_START---"
OUTPUT_END_MARKER: str = "---NANOCLAW_OUTPUT_END---"


@dataclass
class ContainerInput:
    """Input payload sent to the container agent via stdin."""

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


def _serialize_container_input(inp: ContainerInput) -> dict[str, object]:
    """Serialize ContainerInput to a JSON-compatible dict using camelCase keys.

    Matches the TypeScript interface expected by the container agent-runner.
    """
    d: dict[str, object] = {
        "prompt": inp.prompt,
        "groupFolder": inp.group_folder,
        "chatJid": inp.chat_jid,
        "isMain": inp.is_main,
    }
    if inp.session_id is not None:
        d["sessionId"] = inp.session_id
    if inp.is_scheduled_task:
        d["isScheduledTask"] = inp.is_scheduled_task
    if inp.assistant_name is not None:
        d["assistantName"] = inp.assistant_name
    return d


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
        # (group folder, IPC, .claude/) are mounted separately below.
        # Read-only prevents the agent from modifying host application code
        # (src/, dist/, package.json, etc.) which would bypass the sandbox
        # entirely on next restart.
        mounts.append(
            VolumeMount(
                host_path=str(project_root),
                container_path="/workspace/project",
                readonly=True,
            )
        )

        # Shadow .env so the agent cannot read secrets from the mounted project root.
        # Credentials are injected by the credential proxy, never exposed to containers.
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
        # Only directory mounts are supported, not file mounts
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
    # Each group gets their own .claude/ to prevent cross-group session access
    group_sessions_dir = DATA_DIR / "sessions" / group.folder / ".claude"
    group_sessions_dir.mkdir(parents=True, exist_ok=True)
    settings_file = group_sessions_dir / "settings.json"
    if not settings_file.exists():
        settings_file.write_text(
            json.dumps(
                {
                    "env": {
                        # Enable agent swarms (subagent orchestration)
                        "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
                        # Load CLAUDE.md from additional mounted directories
                        "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD": "1",
                        # Enable Claude's memory feature
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

    # Per-group IPC namespace: each group gets its own IPC directory
    # This prevents cross-group privilege escalation via IPC
    group_ipc_dir = resolve_group_ipc_path(group.folder)
    (group_ipc_dir / "messages").mkdir(parents=True, exist_ok=True)
    (group_ipc_dir / "tasks").mkdir(parents=True, exist_ok=True)
    (group_ipc_dir / "input").mkdir(parents=True, exist_ok=True)
    mounts.append(
        VolumeMount(
            host_path=str(group_ipc_dir),
            container_path="/workspace/ipc",
            readonly=False,
        )
    )

    # Python agent-runner is baked into the container image (no per-group source mount needed).
    # TS version mounted source for per-group customization + runtime compilation;
    # Python runs directly from /app/agent_runner/ in the image.

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
) -> list[str]:
    """Build the CLI arguments for the container runtime.

    This is a pure computation function (synchronous).
    """
    args: list[str] = ["run", "-i", "--rm", "--name", container_name]

    # Pass host timezone so container's local time matches the user's
    args.extend(["-e", f"TZ={TIMEZONE}"])

    # Route API traffic through the credential proxy (containers never see real secrets)
    args.extend(
        [
            "-e",
            f"ANTHROPIC_BASE_URL=http://{CONTAINER_HOST_GATEWAY}:{CREDENTIAL_PROXY_PORT}",
        ]
    )

    # Mirror the host's auth method with a placeholder value.
    # API key mode: SDK sends x-api-key, proxy replaces with real key.
    # OAuth mode:   SDK exchanges placeholder token for temp API key,
    #               proxy injects real OAuth token on that exchange request.
    auth_mode = detect_auth_mode()
    if auth_mode == "api-key":
        args.extend(["-e", "ANTHROPIC_API_KEY=placeholder"])
    else:
        args.extend(["-e", "CLAUDE_CODE_OAUTH_TOKEN=placeholder"])

    # Runtime-specific args for host gateway resolution
    args.extend(host_gateway_args())

    # Run as host user so bind-mounted files are accessible.
    # Skip when running as root (uid 0), as the container's node user (uid 1000),
    # or when getuid is unavailable.
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
    on_process: Callable[[asyncio.subprocess.Process, str], None],
    on_output: Callable[[ContainerOutput], Awaitable[None]] | None = None,
) -> ContainerOutput:
    """Run the agent inside a container and return the parsed output.

    Streams stdout looking for OUTPUT_START_MARKER/OUTPUT_END_MARKER pairs.
    Resets the timeout on each streamed output marker.
    """
    start_time = time.monotonic()
    start_epoch_ms = int(time.time() * 1000)

    group_dir = resolve_group_folder_path(group.folder)
    group_dir.mkdir(parents=True, exist_ok=True)

    mounts = build_volume_mounts(group, inp.is_main)
    safe_name = re.sub(r"[^a-zA-Z0-9-]", "-", group.folder)
    container_name = f"nanoclaw-{safe_name}-{start_epoch_ms}"
    container_args = build_container_args(mounts, container_name)

    logger.debug(
        "Container mount configuration",
        group=group.name,
        container_name=container_name,
        mounts=[f"{m.host_path} -> {m.container_path}{' (ro)' if m.readonly else ''}" for m in mounts],
        container_args=" ".join(container_args),
    )

    logger.info(
        "Spawning container agent",
        group=group.name,
        container_name=container_name,
        mount_count=len(mounts),
        is_main=inp.is_main,
    )

    logs_dir = group_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    proc = await asyncio.create_subprocess_exec(
        CONTAINER_RUNTIME_BIN,
        *container_args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    on_process(proc, container_name)

    # Write input to container stdin
    input_payload = json.dumps(_serialize_container_input(inp)).encode("utf-8")
    assert proc.stdin is not None
    proc.stdin.write(input_payload)
    proc.stdin.close()

    stdout_buf = ""
    stderr_buf = ""
    stdout_truncated = False
    stderr_truncated = False

    # Streaming output state
    parse_buffer = ""
    new_session_id: str | None = None
    had_streaming_output = False
    timed_out = False

    config_timeout = (
        group.container_config.timeout if group.container_config else CONTAINER_TIMEOUT
    ) or CONTAINER_TIMEOUT
    # Grace period: hard timeout must be at least IDLE_TIMEOUT + 30s so the
    # graceful _close sentinel has time to trigger before the hard kill fires.
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
                # Attempt graceful stop
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

    async def _read_stdout() -> None:
        nonlocal stdout_buf, stdout_truncated, parse_buffer
        nonlocal new_session_id, had_streaming_output
        assert proc.stdout is not None
        while True:
            chunk_bytes = await proc.stdout.read(8192)
            if not chunk_bytes:
                break
            chunk = chunk_bytes.decode("utf-8", errors="replace")

            # Accumulate for logging
            if not stdout_truncated:
                remaining = CONTAINER_MAX_OUTPUT_SIZE - len(stdout_buf)
                if len(chunk) > remaining:
                    stdout_buf += chunk[:remaining]
                    stdout_truncated = True
                    logger.warning(
                        "Container stdout truncated due to size limit",
                        group=group.name,
                        size=len(stdout_buf),
                    )
                else:
                    stdout_buf += chunk

            # Stream-parse for output markers
            if on_output is not None:
                parse_buffer += chunk
                while True:
                    start_idx = parse_buffer.find(OUTPUT_START_MARKER)
                    if start_idx == -1:
                        break
                    end_idx = parse_buffer.find(OUTPUT_END_MARKER, start_idx)
                    if end_idx == -1:
                        break  # Incomplete pair, wait for more data

                    json_str = parse_buffer[start_idx + len(OUTPUT_START_MARKER) : end_idx].strip()
                    parse_buffer = parse_buffer[end_idx + len(OUTPUT_END_MARKER) :]

                    try:
                        raw = json.loads(json_str)
                        parsed = _parse_container_output(raw)
                        if parsed.new_session_id:
                            new_session_id = parsed.new_session_id
                        had_streaming_output = True
                        # Activity detected - reset the hard timeout
                        activity_event.set()
                        # Call on_output for all markers (including null results)
                        # so idle timers start even for "silent" query completions.
                        await on_output(parsed)
                    except (json.JSONDecodeError, KeyError, TypeError) as err:
                        logger.warning(
                            "Failed to parse streamed output chunk",
                            group=group.name,
                            error=str(err),
                        )

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
            # Don't reset timeout on stderr - SDK writes debug logs continuously.
            # Timeout only resets on actual output (OUTPUT_MARKER in stdout).
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

    # Run stdout/stderr readers concurrently
    await asyncio.gather(_read_stdout(), _read_stderr())

    # Wait for process to exit
    code = await proc.wait()

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
                    f"Duration: {duration_ms}ms",
                    f"Exit Code: {code}",
                    f"Had Streaming Output: {had_streaming_output}",
                ]
            ),
            encoding="utf-8",
        )

        # Timeout after output = idle cleanup, not failure.
        # The agent already sent its response; this is just the
        # container being reaped after the idle period expired.
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
        f"Duration: {duration_ms}ms",
        f"Exit Code: {code}",
        f"Stdout Truncated: {stdout_truncated}",
        f"Stderr Truncated: {stderr_truncated}",
        "",
    ]

    is_error = code != 0

    if is_verbose or is_error:
        # On error, log input metadata only - not the full prompt.
        # Full input is only included at verbose level to avoid
        # persisting user conversation content on every non-zero exit.
        if is_verbose:
            log_lines.extend(
                [
                    "=== Input ===",
                    json.dumps(_serialize_container_input(inp), indent=2),
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
                "",
                f"=== Stdout{' (TRUNCATED)' if stdout_truncated else ''} ===",
                stdout_buf,
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
            stdout=stdout_buf,
            log_file=str(log_file),
        )
        return ContainerOutput(
            status="error",
            result=None,
            error=f"Container exited with code {code}: {stderr_buf[-200:]}",
        )

    # Streaming mode: return completion marker
    if on_output is not None:
        logger.info(
            "Container completed (streaming mode)",
            group=group.name,
            duration=duration_ms,
            new_session_id=new_session_id,
        )
        return ContainerOutput(
            status="success",
            result=None,
            new_session_id=new_session_id,
        )

    # Legacy mode: parse the last output marker pair from accumulated stdout
    try:
        # Extract JSON between sentinel markers for robust parsing
        start_idx = stdout_buf.find(OUTPUT_START_MARKER)
        end_idx = stdout_buf.find(OUTPUT_END_MARKER)

        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            json_line = stdout_buf[start_idx + len(OUTPUT_START_MARKER) : end_idx].strip()
        else:
            # Fallback: last non-empty line (backwards compatibility)
            lines = stdout_buf.strip().split("\n")
            json_line = lines[-1]

        raw = json.loads(json_line)
        output = _parse_container_output(raw)

        logger.info(
            "Container completed",
            group=group.name,
            duration=duration_ms,
            status=output.status,
            has_result=output.result is not None,
        )
        return output

    except (json.JSONDecodeError, KeyError, TypeError, IndexError) as err:
        logger.error(
            "Failed to parse container output",
            group=group.name,
            stdout=stdout_buf,
            stderr=stderr_buf,
            error=str(err),
        )
        return ContainerOutput(
            status="error",
            result=None,
            error=f"Failed to parse container output: {err}",
        )


def write_tasks_snapshot(
    group_folder: str,
    is_main: bool,
    tasks: list[dict[str, object]],
) -> None:
    """Write filtered tasks to the group's IPC directory.

    Main sees all tasks, others only see their own.
    """
    group_ipc_dir = resolve_group_ipc_path(group_folder)
    group_ipc_dir.mkdir(parents=True, exist_ok=True)

    # Main sees all tasks, others only see their own
    filtered_tasks: list[dict[str, object]]
    filtered_tasks = tasks if is_main else [t for t in tasks if t.get("groupFolder") == group_folder]

    tasks_file = group_ipc_dir / "current_tasks.json"
    tasks_file.write_text(json.dumps(filtered_tasks, indent=2), encoding="utf-8")


def write_groups_snapshot(
    group_folder: str,
    is_main: bool,
    groups: list[AvailableGroup],
    _registered_jids: set[str],
) -> None:
    """Write available groups snapshot for the container to read.

    Only main group can see all available groups (for activation).
    Non-main groups only see their own registration status.
    """
    group_ipc_dir = resolve_group_ipc_path(group_folder)
    group_ipc_dir.mkdir(parents=True, exist_ok=True)

    # Main sees all groups; others see nothing (they can't activate groups)
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

    groups_file = group_ipc_dir / "available_groups.json"
    groups_file.write_text(
        json.dumps(
            {
                "groups": visible_groups,
                "lastSync": datetime.now(UTC).isoformat(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
