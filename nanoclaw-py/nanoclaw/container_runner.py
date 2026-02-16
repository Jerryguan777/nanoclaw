"""Container runner — spawns agent containers and parses streamed output.

Port of src/container-runner.ts. The most complex module due to async subprocess
stream management and marker-delimited JSON parsing.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from pathlib import Path
from typing import Awaitable, Callable

from nanoclaw.config import (
    CONTAINER_IMAGE,
    CONTAINER_MAX_OUTPUT_SIZE,
    CONTAINER_RUNTIME,
    CONTAINER_TIMEOUT,
    DATA_DIR,
    GROUPS_DIR,
    IDLE_TIMEOUT,
)
from nanoclaw.logger import logger
from nanoclaw.models import (
    AvailableGroup,
    ContainerInput,
    ContainerOutput,
    RegisteredGroup,
)

OUTPUT_START_MARKER = "---NANOCLAW_OUTPUT_START---"
OUTPUT_END_MARKER = "---NANOCLAW_OUTPUT_END---"


def _build_volume_mounts(
    group: RegisteredGroup, is_main: bool
) -> list[dict]:
    """Build the list of volume mounts for a container invocation."""
    mounts: list[dict] = []
    project_root = Path.cwd()

    if is_main:
        mounts.append({
            "host": str(project_root),
            "container": "/workspace/project",
            "readonly": False,
        })
        mounts.append({
            "host": str(GROUPS_DIR / group.folder),
            "container": "/workspace/group",
            "readonly": False,
        })
    else:
        mounts.append({
            "host": str(GROUPS_DIR / group.folder),
            "container": "/workspace/group",
            "readonly": False,
        })
        global_dir = GROUPS_DIR / "global"
        if global_dir.exists():
            mounts.append({
                "host": str(global_dir),
                "container": "/workspace/global",
                "readonly": True,
            })

    # Per-group Claude sessions
    group_sessions_dir = DATA_DIR / "sessions" / group.folder / ".claude"
    group_sessions_dir.mkdir(parents=True, exist_ok=True)
    settings_file = group_sessions_dir / "settings.json"
    if not settings_file.exists():
        settings_file.write_text(json.dumps({
            "env": {
                "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
                "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD": "1",
                "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "0",
            },
        }, indent=2) + "\n")

    # Sync skills
    skills_src = project_root / "container" / "skills"
    skills_dst = group_sessions_dir / "skills"
    if skills_src.exists():
        for skill_dir in skills_src.iterdir():
            if not skill_dir.is_dir():
                continue
            dst_dir = skills_dst / skill_dir.name
            dst_dir.mkdir(parents=True, exist_ok=True)
            for f in skill_dir.iterdir():
                shutil.copy2(f, dst_dir / f.name)

    mounts.append({
        "host": str(group_sessions_dir),
        "container": "/home/node/.claude",
        "readonly": False,
    })

    # Per-group IPC namespace
    group_ipc_dir = DATA_DIR / "ipc" / group.folder
    for sub in ("messages", "tasks", "input"):
        (group_ipc_dir / sub).mkdir(parents=True, exist_ok=True)
    mounts.append({
        "host": str(group_ipc_dir),
        "container": "/workspace/ipc",
        "readonly": False,
    })

    # Environment file
    env_dir = DATA_DIR / "env"
    env_dir.mkdir(parents=True, exist_ok=True)
    env_file = project_root / ".env"
    if env_file.exists():
        allowed_vars = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")
        lines = [
            line
            for line in env_file.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
            and any(line.strip().startswith(f"{v}=") for v in allowed_vars)
        ]
        if lines:
            (env_dir / "env").write_text("\n".join(lines) + "\n")
            mounts.append({
                "host": str(env_dir),
                "container": "/workspace/env-dir",
                "readonly": True,
            })

    # Agent runner source (hot-reload bypass)
    agent_runner_src = project_root / "container" / "agent-runner" / "src"
    if agent_runner_src.exists():
        mounts.append({
            "host": str(agent_runner_src),
            "container": "/app/src",
            "readonly": True,
        })

    return mounts


def _build_container_args(
    mounts: list[dict], container_name: str
) -> list[str]:
    """Build CLI arguments for docker/container run."""
    args = ["run", "-i", "--rm", "--name", container_name]

    for m in mounts:
        if m["readonly"]:
            args.extend([
                "--mount",
                f"type=bind,source={m['host']},target={m['container']},readonly",
            ])
        else:
            args.extend(["-v", f"{m['host']}:{m['container']}"])

    args.append(CONTAINER_IMAGE)
    return args


async def run_container_agent(
    group: RegisteredGroup,
    inp: ContainerInput,
    on_process: Callable[[asyncio.subprocess.Process, str], None],
    on_output: Callable[[ContainerOutput], Awaitable[None]] | None = None,
) -> ContainerOutput:
    """Spawn a container, feed it input, parse streamed output."""
    start_time = time.monotonic()
    group_dir = GROUPS_DIR / group.folder
    group_dir.mkdir(parents=True, exist_ok=True)

    mounts = _build_volume_mounts(group, inp.is_main)
    safe_name = group.folder.replace("/", "-").replace(" ", "-")
    container_name = f"nanoclaw-{safe_name}-{int(time.time() * 1000)}"
    container_args = _build_container_args(mounts, container_name)

    logger.info(
        "Spawning container agent",
        group=group.name,
        container=container_name,
        mount_count=len(mounts),
        is_main=inp.is_main,
    )

    logs_dir = GROUPS_DIR / group.folder / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    proc = await asyncio.create_subprocess_exec(
        CONTAINER_RUNTIME,
        *container_args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    on_process(proc, container_name)

    # Write input and close stdin
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(inp.model_dump()).encode())
    proc.stdin.close()

    # State for streaming parse
    stdout_buf = ""
    stderr_buf = ""
    stdout_truncated = False
    stderr_truncated = False
    new_session_id: str | None = None
    had_streaming_output = False
    timed_out = False
    output_queue: asyncio.Queue[ContainerOutput] = asyncio.Queue()

    config_timeout = (group.container_config.timeout if group.container_config and group.container_config.timeout else CONTAINER_TIMEOUT)
    timeout_s = max(config_timeout, IDLE_TIMEOUT + 30_000) / 1000.0

    # Timeout handle
    timeout_handle: asyncio.TimerHandle | None = None
    loop = asyncio.get_event_loop()

    def _kill_on_timeout() -> None:
        nonlocal timed_out
        timed_out = True
        logger.error("Container timeout, killing", group=group.name, container=container_name)
        try:
            proc.kill()
        except ProcessLookupError:
            pass

    def _reset_timeout() -> None:
        nonlocal timeout_handle
        if timeout_handle:
            timeout_handle.cancel()
        timeout_handle = loop.call_later(timeout_s, _kill_on_timeout)

    _reset_timeout()

    async def _read_stdout() -> None:
        nonlocal stdout_buf, stdout_truncated, new_session_id, had_streaming_output
        assert proc.stdout is not None
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                break
            text = chunk.decode(errors="replace")

            if not stdout_truncated:
                remaining = CONTAINER_MAX_OUTPUT_SIZE - len(stdout_buf)
                if len(text) > remaining:
                    stdout_buf += text[:remaining]
                    stdout_truncated = True
                else:
                    stdout_buf += text

            if on_output:
                # Parse buffer not the full stdout — use a local attribute
                _read_stdout._parse_buffer += text  # type: ignore[attr-defined]
                buf: str = _read_stdout._parse_buffer  # type: ignore[attr-defined]
                while True:
                    start_idx = buf.find(OUTPUT_START_MARKER)
                    if start_idx == -1:
                        break
                    end_idx = buf.find(OUTPUT_END_MARKER, start_idx)
                    if end_idx == -1:
                        break
                    json_str = buf[start_idx + len(OUTPUT_START_MARKER):end_idx].strip()
                    buf = buf[end_idx + len(OUTPUT_END_MARKER):]
                    _read_stdout._parse_buffer = buf  # type: ignore[attr-defined]
                    try:
                        parsed = ContainerOutput.model_validate_json(json_str)
                        if parsed.new_session_id:
                            new_session_id = parsed.new_session_id
                        had_streaming_output = True
                        _reset_timeout()
                        await output_queue.put(parsed)
                    except Exception as e:
                        logger.warning("Failed to parse streamed output", error=str(e))

    _read_stdout._parse_buffer = ""  # type: ignore[attr-defined]

    async def _read_stderr() -> None:
        nonlocal stderr_buf, stderr_truncated
        assert proc.stderr is not None
        while True:
            chunk = await proc.stderr.read(65536)
            if not chunk:
                break
            text = chunk.decode(errors="replace")
            for line in text.strip().splitlines():
                if line:
                    logger.debug(line, container=group.folder)
            if not stderr_truncated:
                remaining = CONTAINER_MAX_OUTPUT_SIZE - len(stderr_buf)
                if len(text) > remaining:
                    stderr_buf += text[:remaining]
                    stderr_truncated = True
                else:
                    stderr_buf += text

    async def _output_consumer() -> None:
        """Consume output_queue and call on_output sequentially."""
        while True:
            item = await output_queue.get()
            if item is None:  # sentinel
                break
            if on_output:
                await on_output(item)

    # Launch concurrent readers + consumer
    consumer_task = asyncio.create_task(_output_consumer())
    stdout_task = asyncio.create_task(_read_stdout())
    stderr_task = asyncio.create_task(_read_stderr())

    # Wait for process to finish
    await asyncio.gather(stdout_task, stderr_task)
    code = await proc.wait()

    # Signal consumer to finish
    await output_queue.put(None)  # type: ignore[arg-type]
    await consumer_task

    if timeout_handle:
        timeout_handle.cancel()

    duration_ms = int((time.monotonic() - start_time) * 1000)

    # Write log file
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    log_file = logs_dir / f"container-{ts}.log"
    log_lines = [
        f"=== Container Run Log {'(TIMEOUT)' if timed_out else ''} ===",
        f"Group: {group.name}",
        f"Container: {container_name}",
        f"Duration: {duration_ms}ms",
        f"Exit Code: {code}",
    ]
    if code != 0 or os.environ.get("LOG_LEVEL") in ("debug", "trace"):
        log_lines.extend([
            f"\n=== Stderr ===\n{stderr_buf}",
            f"\n=== Stdout ===\n{stdout_buf}",
        ])
    log_file.write_text("\n".join(log_lines))

    if timed_out:
        if had_streaming_output:
            logger.info("Container timed out after output (idle cleanup)",
                        group=group.name, duration=duration_ms)
            return ContainerOutput(status="success", result=None, new_session_id=new_session_id)
        logger.error("Container timed out with no output", group=group.name, duration=duration_ms)
        return ContainerOutput(status="error", result=None, error=f"Container timed out after {config_timeout}ms")

    if code != 0:
        logger.error("Container exited with error", group=group.name, code=code, duration=duration_ms)
        return ContainerOutput(
            status="error", result=None,
            error=f"Container exited with code {code}: {stderr_buf[-200:]}",
        )

    if on_output:
        logger.info("Container completed (streaming mode)", group=group.name, duration=duration_ms)
        return ContainerOutput(status="success", result=None, new_session_id=new_session_id)

    # Legacy: parse last marker pair from stdout
    try:
        start_idx = stdout_buf.find(OUTPUT_START_MARKER)
        end_idx = stdout_buf.find(OUTPUT_END_MARKER)
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            json_str = stdout_buf[start_idx + len(OUTPUT_START_MARKER):end_idx].strip()
        else:
            json_str = stdout_buf.strip().rsplit("\n", 1)[-1]
        return ContainerOutput.model_validate_json(json_str)
    except Exception as e:
        logger.error("Failed to parse container output", group=group.name, error=str(e))
        return ContainerOutput(status="error", result=None, error=f"Parse error: {e}")


def write_tasks_snapshot(
    group_folder: str, is_main: bool, tasks: list[dict]
) -> None:
    group_ipc_dir = DATA_DIR / "ipc" / group_folder
    group_ipc_dir.mkdir(parents=True, exist_ok=True)
    filtered = tasks if is_main else [t for t in tasks if t.get("groupFolder") == group_folder]
    (group_ipc_dir / "current_tasks.json").write_text(json.dumps(filtered, indent=2))


def write_groups_snapshot(
    group_folder: str,
    is_main: bool,
    groups: list[AvailableGroup],
    registered_jids: set[str],
) -> None:
    group_ipc_dir = DATA_DIR / "ipc" / group_folder
    group_ipc_dir.mkdir(parents=True, exist_ok=True)
    visible = [g.model_dump() for g in groups] if is_main else []
    from datetime import datetime, timezone
    (group_ipc_dir / "available_groups.json").write_text(
        json.dumps({"groups": visible, "lastSync": datetime.now(timezone.utc).isoformat()}, indent=2)
    )
