"""NanoClaw Agent Runner — runs inside a container.

Port of container/agent-runner/src/index.ts.

Input:  JSON on stdin (ContainerInput)
Output: Marker-delimited JSON on stdout (ContainerOutput)
IPC:    Follow-up messages via /workspace/ipc/input/ files
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from claude_code_sdk import ClaudeCodeOptions, Message, query

IPC_INPUT_DIR = Path("/workspace/ipc/input")
IPC_INPUT_CLOSE_SENTINEL = IPC_INPUT_DIR / "_close"
IPC_POLL_S = 0.5

OUTPUT_START_MARKER = "---NANOCLAW_OUTPUT_START---"
OUTPUT_END_MARKER = "---NANOCLAW_OUTPUT_END---"


def write_output(output: dict) -> None:
    print(OUTPUT_START_MARKER, flush=True)
    print(json.dumps(output), flush=True)
    print(OUTPUT_END_MARKER, flush=True)


def log(message: str) -> None:
    print(f"[agent-runner] {message}", file=sys.stderr, flush=True)


# --- IPC input handling ---


def should_close() -> bool:
    if IPC_INPUT_CLOSE_SENTINEL.exists():
        try:
            IPC_INPUT_CLOSE_SENTINEL.unlink()
        except OSError:
            pass
        return True
    return False


def drain_ipc_input() -> list[str]:
    try:
        IPC_INPUT_DIR.mkdir(parents=True, exist_ok=True)
        files = sorted(f for f in IPC_INPUT_DIR.iterdir() if f.suffix == ".json")
        messages = []
        for f in files:
            try:
                data = json.loads(f.read_text())
                f.unlink()
                if data.get("type") == "message" and data.get("text"):
                    messages.append(data["text"])
            except Exception as e:
                log(f"Failed to process input file {f.name}: {e}")
                try:
                    f.unlink()
                except OSError:
                    pass
        return messages
    except Exception as e:
        log(f"IPC drain error: {e}")
        return []


async def wait_for_ipc_message() -> str | None:
    """Wait for a new IPC message or _close sentinel."""
    while True:
        if should_close():
            return None
        messages = drain_ipc_input()
        if messages:
            return "\n".join(messages)
        await asyncio.sleep(IPC_POLL_S)


# --- Conversation archiving ---


def _sanitize_filename(s: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:50]


# --- Main query loop ---


async def run_query(
    prompt: str,
    session_id: str | None,
    container_input: dict,
) -> dict:
    """Run a single Claude query and stream results."""
    new_session_id: str | None = None
    last_assistant_uuid: str | None = None
    result_count = 0
    closed_during_query = False

    # Poll IPC during query
    ipc_polling = True

    # Build global system context
    global_claude_md = None
    global_path = Path("/workspace/global/CLAUDE.md")
    if not container_input.get("isMain") and global_path.exists():
        global_claude_md = global_path.read_text()

    options = ClaudeCodeOptions(
        cwd="/workspace/group",
        allowed_tools=[
            "Bash",
            "Read", "Write", "Edit", "Glob", "Grep",
            "WebSearch", "WebFetch",
            "Task",
            "TodoWrite",
            "NotebookEdit",
            "mcp__nanoclaw__*",
        ],
        permission_mode="bypassPermissions",
    )

    if session_id:
        options.resume = session_id

    # NOTE: The Python claude-code-sdk query() API may differ slightly from JS.
    # Adapt parameters based on the actual SDK version.
    try:
        async for message in query(prompt=prompt, options=options):
            # Extract session ID from init messages
            if hasattr(message, "type"):
                if message.type == "system" and hasattr(message, "session_id"):
                    new_session_id = message.session_id
                    log(f"Session initialized: {new_session_id}")

                if message.type == "assistant" and hasattr(message, "uuid"):
                    last_assistant_uuid = message.uuid

                if message.type == "result":
                    result_count += 1
                    text_result = getattr(message, "result", None)
                    log(f"Result #{result_count}: {str(text_result)[:200] if text_result else '(none)'}")
                    write_output({
                        "status": "success",
                        "result": text_result,
                        "newSessionId": new_session_id,
                    })

            # Check for IPC close during query
            if ipc_polling and should_close():
                closed_during_query = True
                ipc_polling = False
                break

    except Exception as e:
        log(f"Query error: {e}")
        raise

    log(f"Query done. Results: {result_count}, closedDuringQuery: {closed_during_query}")
    return {
        "newSessionId": new_session_id,
        "lastAssistantUuid": last_assistant_uuid,
        "closedDuringQuery": closed_during_query,
    }


async def main() -> None:
    # Read input from stdin
    stdin_data = sys.stdin.read()
    try:
        container_input = json.loads(stdin_data)
        log(f"Received input for group: {container_input.get('groupFolder', 'unknown')}")
    except Exception as e:
        write_output({"status": "error", "result": None, "error": f"Failed to parse input: {e}"})
        sys.exit(1)

    session_id = container_input.get("sessionId")
    IPC_INPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Clean stale close sentinel
    try:
        IPC_INPUT_CLOSE_SENTINEL.unlink()
    except FileNotFoundError:
        pass

    # Build initial prompt
    prompt = container_input["prompt"]
    if container_input.get("isScheduledTask"):
        prompt = f"[SCHEDULED TASK - The following message was sent automatically and is not coming directly from the user or group.]\n\n{prompt}"

    pending = drain_ipc_input()
    if pending:
        log(f"Draining {len(pending)} pending IPC messages into initial prompt")
        prompt += "\n" + "\n".join(pending)

    # Query loop
    try:
        while True:
            log(f"Starting query (session: {session_id or 'new'})...")

            result = await run_query(prompt, session_id, container_input)

            if result.get("newSessionId"):
                session_id = result["newSessionId"]

            if result.get("closedDuringQuery"):
                log("Close sentinel consumed during query, exiting")
                break

            # Emit session update
            write_output({"status": "success", "result": None, "newSessionId": session_id})

            log("Query ended, waiting for next IPC message...")
            next_message = await wait_for_ipc_message()
            if next_message is None:
                log("Close sentinel received, exiting")
                break

            log(f"Got new message ({len(next_message)} chars), starting new query")
            prompt = next_message

    except Exception as e:
        error_msg = str(e)
        log(f"Agent error: {error_msg}")
        write_output({
            "status": "error",
            "result": None,
            "newSessionId": session_id,
            "error": error_msg,
        })
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
