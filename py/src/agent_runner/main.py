"""
NanoClaw Agent Runner (Python)

Runs inside a Docker container, receives config via stdin, outputs result to stdout.

Input protocol:
  Stdin: Full ContainerInput JSON (read until EOF)
  IPC:   Follow-up messages written as JSON files to /workspace/ipc/input/
         Files: {type:"message", text:"..."}.json -- polled and consumed
         Sentinel: /workspace/ipc/input/_close -- signals session end

Stdout protocol:
  Each result is wrapped in OUTPUT_START_MARKER / OUTPUT_END_MARKER pairs.
  Multiple results may be emitted (one per agent teams result).
  Final marker after loop ends signals completion.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import ClaudeAgentOptions, HookMatcher, query

from .ipc_mcp import create_nanoclaw_mcp_server

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass
class ContainerInput:
    prompt: str
    group_folder: str
    chat_jid: str
    is_main: bool
    session_id: str | None = None
    is_scheduled_task: bool = False
    assistant_name: str | None = None


@dataclass
class ContainerOutput:
    status: str  # "success" | "error"
    result: str | None
    new_session_id: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"status": self.status, "result": self.result}
        if self.new_session_id is not None:
            d["newSessionId"] = self.new_session_id
        if self.error is not None:
            d["error"] = self.error
        return d


@dataclass
class SessionEntry:
    session_id: str
    full_path: str
    summary: str
    first_prompt: str


@dataclass
class ParsedMessage:
    role: str  # "user" | "assistant"
    content: str


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IPC_INPUT_DIR = Path("/workspace/ipc/input")
IPC_INPUT_CLOSE_SENTINEL = IPC_INPUT_DIR / "_close"
IPC_POLL_SECONDS = 0.5

OUTPUT_START_MARKER = "---NANOCLAW_OUTPUT_START---"
OUTPUT_END_MARKER = "---NANOCLAW_OUTPUT_END---"


# ---------------------------------------------------------------------------
# MessageStream -- push-based async iterable for SDK user messages
# ---------------------------------------------------------------------------


class MessageStream:
    """
    Push-based async iterable for streaming user messages to the SDK.
    Keeps the iterable alive until end() is called, preventing isSingleUserTurn.
    """

    def __init__(self) -> None:
        self._queue: list[dict[str, Any]] = []
        self._event: asyncio.Event = asyncio.Event()
        self._done: bool = False

    def push(self, text: str) -> None:
        self._queue.append(
            {
                "type": "user",
                "message": {"role": "user", "content": text},
                "parent_tool_use_id": None,
                "session_id": "",
            }
        )
        self._event.set()

    def end(self) -> None:
        self._done = True
        self._event.set()

    async def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            while self._queue:
                yield self._queue.pop(0)
            if self._done:
                return
            self._event.clear()
            await self._event.wait()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def write_output(output: ContainerOutput) -> None:
    print(OUTPUT_START_MARKER)
    print(json.dumps(output.to_dict()))
    print(OUTPUT_END_MARKER)
    sys.stdout.flush()


def log(message: str) -> None:
    print(f"[agent-runner] {message}", file=sys.stderr, flush=True)


def get_session_summary(session_id: str, transcript_path: str) -> str | None:
    project_dir = Path(transcript_path).parent
    index_path = project_dir / "sessions-index.json"

    if not index_path.exists():
        log(f"Sessions index not found at {index_path}")
        return None

    try:
        index_data = json.loads(index_path.read_text())
        for entry in index_data.get("entries", []):
            if entry.get("sessionId") == session_id:
                summary = entry.get("summary")
                if summary:
                    return summary
    except (OSError, json.JSONDecodeError, KeyError, ValueError, RuntimeError) as exc:
        log(f"Failed to read sessions index: {exc}")

    return None


# ---------------------------------------------------------------------------
# Transcript archiving (PreCompact hook)
# ---------------------------------------------------------------------------


def _sanitize_filename(summary: str) -> str:
    import re

    name = summary.lower()
    name = re.sub(r"[^a-z0-9]+", "-", name)
    name = name.strip("-")
    return name[:50]


def _generate_fallback_name() -> str:
    now = datetime.now()
    return f"conversation-{now.hour:02d}{now.minute:02d}"


def parse_transcript(content: str) -> list[ParsedMessage]:
    messages: list[ParsedMessage] = []

    for line in content.split("\n"):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            if entry.get("type") == "user" and entry.get("message", {}).get("content"):
                msg_content = entry["message"]["content"]
                text = msg_content if isinstance(msg_content, str) else "".join(c.get("text", "") for c in msg_content)
                if text:
                    messages.append(ParsedMessage(role="user", content=text))
            elif entry.get("type") == "assistant" and entry.get("message", {}).get("content"):
                text_parts = [c.get("text", "") for c in entry["message"]["content"] if c.get("type") == "text"]
                text = "".join(text_parts)
                if text:
                    messages.append(ParsedMessage(role="assistant", content=text))
        except (json.JSONDecodeError, TypeError, KeyError):
            pass

    return messages


def format_transcript_markdown(
    messages: list[ParsedMessage],
    title: str | None = None,
    assistant_name: str | None = None,
) -> str:
    now = datetime.now()
    date_str = now.strftime("%b %-d, %-I:%M %p")

    lines: list[str] = [
        f"# {title or 'Conversation'}",
        "",
        f"Archived: {date_str}",
        "",
        "---",
        "",
    ]

    for msg in messages:
        sender = "User" if msg.role == "user" else (assistant_name or "Assistant")
        content = msg.content[:2000] + "..." if len(msg.content) > 2000 else msg.content
        lines.append(f"**{sender}**: {content}")
        lines.append("")

    return "\n".join(lines)


def create_pre_compact_hook(
    assistant_name: str | None = None,
) -> Any:
    """Return a PreCompact hook callback that archives transcripts."""

    async def hook(input_data: Any, _tool_use_id: Any, _context: Any) -> dict[str, Any]:
        transcript_path: str | None = getattr(input_data, "transcript_path", None)
        session_id: str | None = getattr(input_data, "session_id", None)

        if not transcript_path or not Path(transcript_path).exists():
            log("No transcript found for archiving")
            return {}

        try:
            content = Path(transcript_path).read_text()
            messages = parse_transcript(content)

            if not messages:
                log("No messages to archive")
                return {}

            summary = get_session_summary(session_id, transcript_path) if session_id else None
            name = _sanitize_filename(summary) if summary else _generate_fallback_name()

            conversations_dir = Path("/workspace/group/conversations")
            conversations_dir.mkdir(parents=True, exist_ok=True)

            date = datetime.now().strftime("%Y-%m-%d")
            filename = f"{date}-{name}.md"
            filepath = conversations_dir / filename

            markdown = format_transcript_markdown(messages, summary, assistant_name)
            filepath.write_text(markdown)

            log(f"Archived conversation to {filepath}")
        except (OSError, json.JSONDecodeError, KeyError, ValueError, RuntimeError) as exc:
            log(f"Failed to archive transcript: {exc}")

        return {}

    return hook


# ---------------------------------------------------------------------------
# IPC polling helpers
# ---------------------------------------------------------------------------


def should_close() -> bool:
    if IPC_INPUT_CLOSE_SENTINEL.exists():
        with contextlib.suppress(OSError):
            IPC_INPUT_CLOSE_SENTINEL.unlink()
        return True
    return False


def drain_ipc_input() -> list[str]:
    try:
        IPC_INPUT_DIR.mkdir(parents=True, exist_ok=True)
        files = sorted(f for f in IPC_INPUT_DIR.iterdir() if f.suffix == ".json")

        messages: list[str] = []
        for filepath in files:
            try:
                data = json.loads(filepath.read_text())
                filepath.unlink()
                if data.get("type") == "message" and data.get("text"):
                    messages.append(data["text"])
            except (OSError, json.JSONDecodeError, KeyError, ValueError, RuntimeError) as exc:
                log(f"Failed to process input file {filepath.name}: {exc}")
                with contextlib.suppress(OSError):
                    filepath.unlink()
        return messages
    except (OSError, json.JSONDecodeError, KeyError, ValueError, RuntimeError) as exc:
        log(f"IPC drain error: {exc}")
        return []


async def wait_for_ipc_message() -> str | None:
    """Wait for a new IPC message or _close sentinel."""
    while True:
        if should_close():
            return None
        messages = drain_ipc_input()
        if messages:
            return "\n".join(messages)
        await asyncio.sleep(IPC_POLL_SECONDS)


# ---------------------------------------------------------------------------
# Core query runner
# ---------------------------------------------------------------------------


@dataclass
class QueryResult:
    new_session_id: str | None = None
    last_assistant_uuid: str | None = None
    closed_during_query: bool = False


async def run_query(
    prompt: str,
    session_id: str | None,
    mcp_server: Any,
    container_input: ContainerInput,
    sdk_env: dict[str, str | None],
    resume_at: str | None = None,
) -> QueryResult:
    stream = MessageStream()
    stream.push(prompt)

    result = QueryResult()
    ipc_polling = True

    async def poll_ipc_during_query() -> None:
        nonlocal ipc_polling
        while ipc_polling:
            if should_close():
                log("Close sentinel detected during query, ending stream")
                result.closed_during_query = True
                stream.end()
                ipc_polling = False
                return
            messages = drain_ipc_input()
            for text in messages:
                log(f"Piping IPC message into active query ({len(text)} chars)")
                stream.push(text)
            await asyncio.sleep(IPC_POLL_SECONDS)

    # Load global CLAUDE.md as additional system context (shared across all groups)
    global_claude_md_path = Path("/workspace/global/CLAUDE.md")
    global_claude_md: str | None = None
    if not container_input.is_main and global_claude_md_path.exists():
        global_claude_md = global_claude_md_path.read_text()

    # Discover additional directories mounted at /workspace/extra/*
    extra_dirs: list[str] = []
    extra_base = Path("/workspace/extra")
    if extra_base.exists():
        for entry in extra_base.iterdir():
            if entry.is_dir():
                extra_dirs.append(str(entry))
    if extra_dirs:
        log(f"Additional directories: {', '.join(extra_dirs)}")

    # Build system prompt
    system_prompt: dict[str, Any] | None = None
    if global_claude_md:
        system_prompt = {
            "type": "preset",
            "preset": "claude_code",
            "append": global_claude_md,
        }

    # Build extra_args for resume-session-at
    extra_args: dict[str, str] | None = None
    if resume_at:
        extra_args = {"resume-session-at": resume_at}

    options = ClaudeAgentOptions(
        cwd="/workspace/group",
        add_dirs=extra_dirs if extra_dirs else None,
        resume=session_id,
        system_prompt=system_prompt,
        allowed_tools=[
            "Bash",
            "Read",
            "Write",
            "Edit",
            "Glob",
            "Grep",
            "WebSearch",
            "WebFetch",
            "Task",
            "TaskOutput",
            "TaskStop",
            "TeamCreate",
            "TeamDelete",
            "SendMessage",
            "TodoWrite",
            "ToolSearch",
            "Skill",
            "NotebookEdit",
            "mcp__nanoclaw__*",
        ],
        env=sdk_env,
        permission_mode="bypassPermissions",
        mcp_servers={"nanoclaw": mcp_server},
        hooks={
            "PreCompact": [HookMatcher(hooks=[create_pre_compact_hook(container_input.assistant_name)])],
        },
        setting_sources=["project", "user"],
    )

    if extra_args:
        options.extra_args = extra_args

    message_count = 0
    result_count = 0

    # Start IPC polling as a background task (not in the same task group as query)
    poll_task = asyncio.ensure_future(poll_ipc_during_query())

    try:
        async for message in query(prompt=stream, options=options):
            message_count += 1

            # Determine message type from the object class name
            cls_name = type(message).__name__
            log_type = cls_name

            if cls_name == "SystemMessage":
                subtype = getattr(message, "subtype", "")
                log_type = f"system/{subtype}"
                data = getattr(message, "data", {})

                if subtype == "init":
                    result.new_session_id = data.get("session_id") if isinstance(data, dict) else None
                    log(f"Session initialized: {result.new_session_id}")

                elif subtype == "task_notification":
                    log(
                        f"Task notification: task={data.get('task_id')} "
                        f"status={data.get('status')} summary={data.get('summary')}"
                        if isinstance(data, dict)
                        else f"Task notification: {data}"
                    )

            elif cls_name == "AssistantMessage":
                uuid = getattr(message, "uuid", None)
                if uuid:
                    result.last_assistant_uuid = uuid

            elif cls_name == "ResultMessage":
                result_count += 1
                text_result = getattr(message, "result", None)
                subtype = getattr(message, "subtype", "")
                session_id_from_result = getattr(message, "session_id", None)
                if session_id_from_result:
                    result.new_session_id = session_id_from_result
                preview = text_result[:200] if text_result else ""
                log(f"Result #{result_count}: subtype={subtype}{f' text={preview}' if text_result else ''}")
                write_output(
                    ContainerOutput(
                        status="success",
                        result=text_result or None,
                        new_session_id=result.new_session_id,
                    )
                )

            log(f"[msg #{message_count}] type={log_type}")
    finally:
        # Stop IPC polling once the query iterator ends
        ipc_polling = False
        poll_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poll_task

    log(
        f"Query done. Messages: {message_count}, results: {result_count}, "
        f"lastAssistantUuid: {result.last_assistant_uuid or 'none'}, "
        f"closedDuringQuery: {result.closed_during_query}"
    )
    return result


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def main() -> None:
    # Read ContainerInput JSON from stdin
    try:
        stdin_data = sys.stdin.read()
        raw = json.loads(stdin_data)
        container_input = ContainerInput(
            prompt=raw["prompt"],
            group_folder=raw["groupFolder"],
            chat_jid=raw["chatJid"],
            is_main=raw["isMain"],
            session_id=raw.get("sessionId"),
            is_scheduled_task=raw.get("isScheduledTask", False),
            assistant_name=raw.get("assistantName"),
        )
        with contextlib.suppress(OSError):
            Path("/tmp/input.json").unlink()
        log(f"Received input for group: {container_input.group_folder}")
    except (OSError, json.JSONDecodeError, KeyError, ValueError, RuntimeError) as exc:
        write_output(
            ContainerOutput(
                status="error",
                result=None,
                error=f"Failed to parse input: {exc}",
            )
        )
        sys.exit(1)

    # Credentials are injected by the host's credential proxy via ANTHROPIC_BASE_URL.
    # No real secrets exist in the container environment.
    import os

    sdk_env: dict[str, str | None] = dict(os.environ)

    # Create in-process MCP server
    mcp_server = create_nanoclaw_mcp_server(
        chat_jid=container_input.chat_jid,
        group_folder=container_input.group_folder,
        is_main=container_input.is_main,
    )

    session_id = container_input.session_id
    IPC_INPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Clean up stale _close sentinel from previous container runs
    with contextlib.suppress(OSError):
        IPC_INPUT_CLOSE_SENTINEL.unlink()

    # Build initial prompt (drain any pending IPC messages too)
    prompt = container_input.prompt
    if container_input.is_scheduled_task:
        prompt = (
            "[SCHEDULED TASK - The following message was sent automatically "
            "and is not coming directly from the user or group.]\n\n" + prompt
        )
    pending = drain_ipc_input()
    if pending:
        log(f"Draining {len(pending)} pending IPC messages into initial prompt")
        prompt += "\n" + "\n".join(pending)

    # Query loop: run query -> wait for IPC message -> run new query -> repeat
    resume_at: str | None = None
    try:
        while True:
            log(f"Starting query (session: {session_id or 'new'}, resumeAt: {resume_at or 'latest'})...")

            query_result = await run_query(
                prompt,
                session_id,
                mcp_server,
                container_input,
                sdk_env,
                resume_at,
            )
            if query_result.new_session_id:
                session_id = query_result.new_session_id
            if query_result.last_assistant_uuid:
                resume_at = query_result.last_assistant_uuid

            # If _close was consumed during the query, exit immediately.
            if query_result.closed_during_query:
                log("Close sentinel consumed during query, exiting")
                break

            # Emit session update so host can track it
            write_output(
                ContainerOutput(
                    status="success",
                    result=None,
                    new_session_id=session_id,
                )
            )

            log("Query ended, waiting for next IPC message...")

            # Wait for the next message or _close sentinel
            next_message = await wait_for_ipc_message()
            if next_message is None:
                log("Close sentinel received, exiting")
                break

            log(f"Got new message ({len(next_message)} chars), starting new query")
            prompt = next_message
    except (OSError, json.JSONDecodeError, KeyError, ValueError, RuntimeError) as exc:
        error_message = str(exc)
        log(f"Agent error: {error_message}")
        write_output(
            ContainerOutput(
                status="error",
                result=None,
                new_session_id=session_id,
                error=error_message,
            )
        )
        sys.exit(1)
