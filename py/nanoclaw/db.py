"""SQLite database operations."""

from __future__ import annotations

import contextlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nanoclaw.config import ASSISTANT_NAME, DATA_DIR, STORE_DIR
from nanoclaw.group_folder import is_valid_group_folder
from nanoclaw.logger import get_logger
from nanoclaw.types import (
    ContainerConfig,
    NewMessage,
    RegisteredGroup,
    ScheduledTask,
    TaskRunLog,
)

logger = get_logger()

_db: sqlite3.Connection | None = None


@dataclass(frozen=True)
class ChatInfo:
    """Chat metadata record."""

    jid: str
    name: str
    last_message_time: str
    channel: str | None
    is_group: int


def _get_db() -> sqlite3.Connection:
    """Return the module-level database connection, asserting it is initialized."""
    assert _db is not None, "Database not initialized. Call init_database() first."
    return _db


def _create_schema(database: sqlite3.Connection) -> None:
    """Create tables and run migrations."""
    database.executescript("""
        CREATE TABLE IF NOT EXISTS chats (
            jid TEXT PRIMARY KEY,
            name TEXT,
            last_message_time TEXT,
            channel TEXT,
            is_group INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT,
            chat_jid TEXT,
            sender TEXT,
            sender_name TEXT,
            content TEXT,
            timestamp TEXT,
            is_from_me INTEGER,
            is_bot_message INTEGER DEFAULT 0,
            PRIMARY KEY (id, chat_jid),
            FOREIGN KEY (chat_jid) REFERENCES chats(jid)
        );
        CREATE INDEX IF NOT EXISTS idx_timestamp ON messages(timestamp);

        CREATE TABLE IF NOT EXISTS scheduled_tasks (
            id TEXT PRIMARY KEY,
            group_folder TEXT NOT NULL,
            chat_jid TEXT NOT NULL,
            prompt TEXT NOT NULL,
            schedule_type TEXT NOT NULL,
            schedule_value TEXT NOT NULL,
            next_run TEXT,
            last_run TEXT,
            last_result TEXT,
            status TEXT DEFAULT 'active',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_next_run ON scheduled_tasks(next_run);
        CREATE INDEX IF NOT EXISTS idx_status ON scheduled_tasks(status);

        CREATE TABLE IF NOT EXISTS task_run_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            run_at TEXT NOT NULL,
            duration_ms INTEGER NOT NULL,
            status TEXT NOT NULL,
            result TEXT,
            error TEXT,
            FOREIGN KEY (task_id) REFERENCES scheduled_tasks(id)
        );
        CREATE INDEX IF NOT EXISTS idx_task_run_logs ON task_run_logs(task_id, run_at);

        CREATE TABLE IF NOT EXISTS router_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            group_folder TEXT PRIMARY KEY,
            session_id TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS registered_groups (
            jid TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            folder TEXT NOT NULL UNIQUE,
            trigger_pattern TEXT NOT NULL,
            added_at TEXT NOT NULL,
            container_config TEXT,
            requires_trigger INTEGER DEFAULT 1
        );
    """)

    # Add context_mode column if it doesn't exist (migration for existing DBs)
    with contextlib.suppress(sqlite3.OperationalError):
        database.execute("ALTER TABLE scheduled_tasks ADD COLUMN context_mode TEXT DEFAULT 'isolated'")

    # Add is_bot_message column if it doesn't exist (migration for existing DBs)
    try:
        database.execute("ALTER TABLE messages ADD COLUMN is_bot_message INTEGER DEFAULT 0")
        # Backfill: mark existing bot messages that used the content prefix pattern
        database.execute(
            "UPDATE messages SET is_bot_message = 1 WHERE content LIKE ?",
            (f"{ASSISTANT_NAME}:%",),
        )
    except sqlite3.OperationalError:
        pass  # column already exists

    # Add is_main column if it doesn't exist (migration for existing DBs)
    try:
        database.execute("ALTER TABLE registered_groups ADD COLUMN is_main INTEGER DEFAULT 0")
        # Backfill: existing rows with folder = 'main' are the main group
        database.execute("UPDATE registered_groups SET is_main = 1 WHERE folder = 'main'")
    except sqlite3.OperationalError:
        pass  # column already exists

    # Add channel and is_group columns if they don't exist (migration for existing DBs)
    try:
        database.execute("ALTER TABLE chats ADD COLUMN channel TEXT")
        database.execute("ALTER TABLE chats ADD COLUMN is_group INTEGER DEFAULT 0")
        # Backfill from JID patterns
        database.execute("UPDATE chats SET channel = 'whatsapp', is_group = 1 WHERE jid LIKE '%@g.us'")
        database.execute("UPDATE chats SET channel = 'whatsapp', is_group = 0 WHERE jid LIKE '%@s.whatsapp.net'")
        database.execute("UPDATE chats SET channel = 'discord', is_group = 1 WHERE jid LIKE 'dc:%'")
        database.execute("UPDATE chats SET channel = 'telegram', is_group = 1 WHERE jid LIKE 'tg:%'")
    except sqlite3.OperationalError:
        pass  # columns already exist

    database.commit()


def init_database() -> None:
    """Open (or create) the SQLite database and run schema migrations."""
    global _db
    db_path = STORE_DIR / "messages.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    _db = sqlite3.connect(str(db_path))
    _db.row_factory = sqlite3.Row
    _create_schema(_db)

    # Migrate from JSON files if they exist
    _migrate_json_state()


def _init_test_database() -> None:
    """Create a fresh in-memory database for tests."""
    global _db
    _db = sqlite3.connect(":memory:")
    _db.row_factory = sqlite3.Row
    _create_schema(_db)


# ---------------------------------------------------------------------------
# Chat metadata
# ---------------------------------------------------------------------------


def store_chat_metadata(
    chat_jid: str,
    timestamp: str,
    name: str | None = None,
    channel: str | None = None,
    is_group: bool | None = None,
) -> None:
    """Store chat metadata only (no message content).

    Used for all chats to enable group discovery without storing sensitive content.
    """
    db = _get_db()
    ch = channel
    group: int | None = None if is_group is None else (1 if is_group else 0)

    if name:
        db.execute(
            """
            INSERT INTO chats (jid, name, last_message_time, channel, is_group) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(jid) DO UPDATE SET
                name = excluded.name,
                last_message_time = MAX(last_message_time, excluded.last_message_time),
                channel = COALESCE(excluded.channel, channel),
                is_group = COALESCE(excluded.is_group, is_group)
            """,
            (chat_jid, name, timestamp, ch, group),
        )
    else:
        db.execute(
            """
            INSERT INTO chats (jid, name, last_message_time, channel, is_group) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(jid) DO UPDATE SET
                last_message_time = MAX(last_message_time, excluded.last_message_time),
                channel = COALESCE(excluded.channel, channel),
                is_group = COALESCE(excluded.is_group, is_group)
            """,
            (chat_jid, chat_jid, timestamp, ch, group),
        )
    db.commit()


def update_chat_name(chat_jid: str, name: str) -> None:
    """Update chat name without changing timestamp for existing chats.

    New chats get the current time as their initial timestamp.
    Used during group metadata sync.
    """
    db = _get_db()
    db.execute(
        """
        INSERT INTO chats (jid, name, last_message_time) VALUES (?, ?, ?)
        ON CONFLICT(jid) DO UPDATE SET name = excluded.name
        """,
        (chat_jid, name, datetime.now(UTC).isoformat()),
    )
    db.commit()


def get_all_chats() -> list[ChatInfo]:
    """Get all known chats, ordered by most recent activity."""
    db = _get_db()
    rows = db.execute(
        """
        SELECT jid, name, last_message_time, channel, is_group
        FROM chats
        ORDER BY last_message_time DESC
        """
    ).fetchall()
    return [
        ChatInfo(
            jid=row["jid"],
            name=row["name"],
            last_message_time=row["last_message_time"],
            channel=row["channel"],
            is_group=row["is_group"],
        )
        for row in rows
    ]


def get_last_group_sync() -> str | None:
    """Get timestamp of last group metadata sync."""
    db = _get_db()
    row = db.execute("SELECT last_message_time FROM chats WHERE jid = '__group_sync__'").fetchone()
    if row is None:
        return None
    return row["last_message_time"] or None


def set_last_group_sync() -> None:
    """Record that group metadata was synced."""
    db = _get_db()
    now = datetime.now(UTC).isoformat()
    db.execute(
        "INSERT OR REPLACE INTO chats (jid, name, last_message_time) VALUES ('__group_sync__', '__group_sync__', ?)",
        (now,),
    )
    db.commit()


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def store_message(msg: NewMessage) -> None:
    """Store a message with full content.

    Only call this for registered groups where message history is needed.
    """
    db = _get_db()
    db.execute(
        "INSERT OR REPLACE INTO messages (id, chat_jid, sender, sender_name, content, timestamp, is_from_me, is_bot_message) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            msg.id,
            msg.chat_jid,
            msg.sender,
            msg.sender_name,
            msg.content,
            msg.timestamp,
            1 if msg.is_from_me else 0,
            1 if msg.is_bot_message else 0,
        ),
    )
    db.commit()


def store_message_direct(
    *,
    id: str,
    chat_jid: str,
    sender: str,
    sender_name: str,
    content: str,
    timestamp: str,
    is_from_me: bool,
    is_bot_message: bool = False,
) -> None:
    """Store a message directly."""
    db = _get_db()
    db.execute(
        "INSERT OR REPLACE INTO messages (id, chat_jid, sender, sender_name, content, timestamp, is_from_me, is_bot_message) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            id,
            chat_jid,
            sender,
            sender_name,
            content,
            timestamp,
            1 if is_from_me else 0,
            1 if is_bot_message else 0,
        ),
    )
    db.commit()


def _row_to_new_message(row: sqlite3.Row) -> NewMessage:
    """Convert a sqlite3.Row to a NewMessage dataclass."""
    return NewMessage(
        id=row["id"],
        chat_jid=row["chat_jid"],
        sender=row["sender"],
        sender_name=row["sender_name"],
        content=row["content"],
        timestamp=row["timestamp"],
        is_from_me=bool(row["is_from_me"]),
        is_bot_message=bool(row.get("is_bot_message", 0)) if hasattr(row, "get") else False,
    )


def get_new_messages(
    jids: list[str],
    last_timestamp: str,
    bot_prefix: str,
    limit: int = 200,
) -> tuple[list[NewMessage], str]:
    """Get new messages since last_timestamp for the given JIDs.

    Returns (messages, new_timestamp).
    """
    if not jids:
        return [], last_timestamp

    db = _get_db()
    placeholders = ",".join("?" for _ in jids)
    sql = f"""
        SELECT * FROM (
            SELECT id, chat_jid, sender, sender_name, content, timestamp, is_from_me
            FROM messages
            WHERE timestamp > ? AND chat_jid IN ({placeholders})
                AND is_bot_message = 0 AND content NOT LIKE ?
                AND content != '' AND content IS NOT NULL
            ORDER BY timestamp DESC
            LIMIT ?
        ) ORDER BY timestamp
    """
    params: list[Any] = [last_timestamp, *jids, f"{bot_prefix}:%", limit]
    rows = db.execute(sql, params).fetchall()

    messages = [
        NewMessage(
            id=row["id"],
            chat_jid=row["chat_jid"],
            sender=row["sender"],
            sender_name=row["sender_name"],
            content=row["content"],
            timestamp=row["timestamp"],
            is_from_me=bool(row["is_from_me"]),
        )
        for row in rows
    ]

    new_timestamp = last_timestamp
    for msg in messages:
        if msg.timestamp > new_timestamp:
            new_timestamp = msg.timestamp

    return messages, new_timestamp


def get_messages_since(
    chat_jid: str,
    since_timestamp: str,
    bot_prefix: str,
    limit: int = 200,
) -> list[NewMessage]:
    """Get messages since a timestamp for a specific chat.

    Filters bot messages using both the is_bot_message flag AND the content
    prefix as a backstop for messages written before the migration ran.
    Subquery takes the N most recent, outer query re-sorts chronologically.
    """
    db = _get_db()
    sql = """
        SELECT * FROM (
            SELECT id, chat_jid, sender, sender_name, content, timestamp, is_from_me
            FROM messages
            WHERE chat_jid = ? AND timestamp > ?
                AND is_bot_message = 0 AND content NOT LIKE ?
                AND content != '' AND content IS NOT NULL
            ORDER BY timestamp DESC
            LIMIT ?
        ) ORDER BY timestamp
    """
    rows = db.execute(sql, (chat_jid, since_timestamp, f"{bot_prefix}:%", limit)).fetchall()
    return [
        NewMessage(
            id=row["id"],
            chat_jid=row["chat_jid"],
            sender=row["sender"],
            sender_name=row["sender_name"],
            content=row["content"],
            timestamp=row["timestamp"],
            is_from_me=bool(row["is_from_me"]),
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Scheduled tasks
# ---------------------------------------------------------------------------


def create_task(task: ScheduledTask) -> None:
    """Create a new scheduled task (without last_run / last_result)."""
    db = _get_db()
    db.execute(
        """
        INSERT INTO scheduled_tasks (id, group_folder, chat_jid, prompt, schedule_type, schedule_value, context_mode, next_run, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task.id,
            task.group_folder,
            task.chat_jid,
            task.prompt,
            task.schedule_type,
            task.schedule_value,
            task.context_mode or "isolated",
            task.next_run,
            task.status,
            task.created_at,
        ),
    )
    db.commit()


def _row_to_scheduled_task(row: sqlite3.Row) -> ScheduledTask:
    """Convert a sqlite3.Row to a ScheduledTask dataclass."""
    return ScheduledTask(
        id=row["id"],
        group_folder=row["group_folder"],
        chat_jid=row["chat_jid"],
        prompt=row["prompt"],
        schedule_type=row["schedule_type"],
        schedule_value=row["schedule_value"],
        context_mode=row["context_mode"] or "isolated",
        next_run=row["next_run"],
        last_run=row["last_run"],
        last_result=row["last_result"],
        status=row["status"],
        created_at=row["created_at"],
    )


def get_task_by_id(id: str) -> ScheduledTask | None:
    """Get a task by its ID, or None if not found."""
    db = _get_db()
    row = db.execute("SELECT * FROM scheduled_tasks WHERE id = ?", (id,)).fetchone()
    if row is None:
        return None
    return _row_to_scheduled_task(row)


def get_tasks_for_group(group_folder: str) -> list[ScheduledTask]:
    """Get all tasks for a specific group folder."""
    db = _get_db()
    rows = db.execute(
        "SELECT * FROM scheduled_tasks WHERE group_folder = ? ORDER BY created_at DESC",
        (group_folder,),
    ).fetchall()
    return [_row_to_scheduled_task(row) for row in rows]


def get_all_tasks() -> list[ScheduledTask]:
    """Get all scheduled tasks."""
    db = _get_db()
    rows = db.execute("SELECT * FROM scheduled_tasks ORDER BY created_at DESC").fetchall()
    return [_row_to_scheduled_task(row) for row in rows]


def update_task(
    id: str,
    *,
    prompt: str | None = None,
    schedule_type: str | None = None,
    schedule_value: str | None = None,
    next_run: str | None = None,
    status: str | None = None,
) -> None:
    """Update selected fields on a scheduled task."""
    fields: list[str] = []
    values: list[Any] = []

    if prompt is not None:
        fields.append("prompt = ?")
        values.append(prompt)
    if schedule_type is not None:
        fields.append("schedule_type = ?")
        values.append(schedule_type)
    if schedule_value is not None:
        fields.append("schedule_value = ?")
        values.append(schedule_value)
    if next_run is not None:
        fields.append("next_run = ?")
        values.append(next_run)
    if status is not None:
        fields.append("status = ?")
        values.append(status)

    if not fields:
        return

    values.append(id)
    db = _get_db()
    db.execute(
        f"UPDATE scheduled_tasks SET {', '.join(fields)} WHERE id = ?",
        values,
    )
    db.commit()


def delete_task(id: str) -> None:
    """Delete a task and its run logs."""
    db = _get_db()
    # Delete child records first (FK constraint)
    db.execute("DELETE FROM task_run_logs WHERE task_id = ?", (id,))
    db.execute("DELETE FROM scheduled_tasks WHERE id = ?", (id,))
    db.commit()


def get_due_tasks() -> list[ScheduledTask]:
    """Get all active tasks whose next_run is in the past."""
    db = _get_db()
    now = datetime.now(UTC).isoformat()
    rows = db.execute(
        """
        SELECT * FROM scheduled_tasks
        WHERE status = 'active' AND next_run IS NOT NULL AND next_run <= ?
        ORDER BY next_run
        """,
        (now,),
    ).fetchall()
    return [_row_to_scheduled_task(row) for row in rows]


def update_task_after_run(
    id: str,
    next_run: str | None,
    last_result: str,
) -> None:
    """Update task state after execution."""
    db = _get_db()
    now = datetime.now(UTC).isoformat()
    db.execute(
        """
        UPDATE scheduled_tasks
        SET next_run = ?, last_run = ?, last_result = ?, status = CASE WHEN ? IS NULL THEN 'completed' ELSE status END
        WHERE id = ?
        """,
        (next_run, now, last_result, next_run, id),
    )
    db.commit()


def log_task_run(log: TaskRunLog) -> None:
    """Insert a task run log entry."""
    db = _get_db()
    db.execute(
        """
        INSERT INTO task_run_logs (task_id, run_at, duration_ms, status, result, error)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            log.task_id,
            log.run_at,
            log.duration_ms,
            log.status,
            log.result,
            log.error,
        ),
    )
    db.commit()


# ---------------------------------------------------------------------------
# Router state
# ---------------------------------------------------------------------------


def get_router_state(key: str) -> str | None:
    """Get a value from the router_state table."""
    db = _get_db()
    row = db.execute("SELECT value FROM router_state WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    return row["value"]  # type: ignore[no-any-return]


def set_router_state(key: str, value: str) -> None:
    """Set a value in the router_state table."""
    db = _get_db()
    db.execute(
        "INSERT OR REPLACE INTO router_state (key, value) VALUES (?, ?)",
        (key, value),
    )
    db.commit()


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def get_session(group_folder: str) -> str | None:
    """Get the session ID for a group folder."""
    db = _get_db()
    row = db.execute("SELECT session_id FROM sessions WHERE group_folder = ?", (group_folder,)).fetchone()
    if row is None:
        return None
    return row["session_id"]  # type: ignore[no-any-return]


def set_session(group_folder: str, session_id: str) -> None:
    """Set the session ID for a group folder."""
    db = _get_db()
    db.execute(
        "INSERT OR REPLACE INTO sessions (group_folder, session_id) VALUES (?, ?)",
        (group_folder, session_id),
    )
    db.commit()


def get_all_sessions() -> dict[str, str]:
    """Get all session mappings."""
    db = _get_db()
    rows = db.execute("SELECT group_folder, session_id FROM sessions").fetchall()
    return {row["group_folder"]: row["session_id"] for row in rows}


# ---------------------------------------------------------------------------
# Registered groups
# ---------------------------------------------------------------------------


def _parse_registered_group_row(
    row: sqlite3.Row,
) -> tuple[str, RegisteredGroup] | None:
    """Parse a registered_groups row into (jid, RegisteredGroup).

    Returns None if the folder is invalid.
    """
    jid: str = row["jid"]
    folder: str = row["folder"]

    if not is_valid_group_folder(folder):
        logger.warn("Skipping registered group with invalid folder", jid=jid, folder=folder)
        return None

    container_config_raw: str | None = row["container_config"]
    container_config: ContainerConfig | None = None
    if container_config_raw:
        parsed = json.loads(container_config_raw)
        container_config = ContainerConfig(**parsed) if isinstance(parsed, dict) else None

    requires_trigger_raw: int | None = row["requires_trigger"]
    requires_trigger = True if requires_trigger_raw is None else bool(requires_trigger_raw)

    is_main_raw: int | None = row["is_main"]
    is_main = bool(is_main_raw) if is_main_raw else False

    return jid, RegisteredGroup(
        name=row["name"],
        folder=folder,
        trigger=row["trigger_pattern"],
        added_at=row["added_at"],
        container_config=container_config,
        requires_trigger=requires_trigger,
        is_main=is_main,
    )


def get_registered_group(jid: str) -> RegisteredGroup | None:
    """Get a registered group by JID, or None if not found / invalid folder."""
    db = _get_db()
    row = db.execute("SELECT * FROM registered_groups WHERE jid = ?", (jid,)).fetchone()
    if row is None:
        return None
    result = _parse_registered_group_row(row)
    if result is None:
        return None
    return result[1]


def set_registered_group(jid: str, group: RegisteredGroup) -> None:
    """Insert or replace a registered group."""
    if not is_valid_group_folder(group.folder):
        raise ValueError(f'Invalid group folder "{group.folder}" for JID {jid}')

    db = _get_db()
    db.execute(
        """INSERT OR REPLACE INTO registered_groups (jid, name, folder, trigger_pattern, added_at, container_config, requires_trigger, is_main)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            jid,
            group.name,
            group.folder,
            group.trigger,
            group.added_at,
            json.dumps(
                {
                    "additional_mounts": [
                        {"host_path": m.host_path, "container_path": m.container_path, "readonly": m.readonly}
                        for m in group.container_config.additional_mounts
                    ],
                    "timeout": group.container_config.timeout,
                }
            )
            if group.container_config
            else None,
            1 if group.requires_trigger else 0,
            1 if group.is_main else 0,
        ),
    )
    db.commit()


def get_all_registered_groups() -> dict[str, RegisteredGroup]:
    """Get all registered groups as a dict keyed by JID."""
    db = _get_db()
    rows = db.execute("SELECT * FROM registered_groups").fetchall()
    result: dict[str, RegisteredGroup] = {}
    for row in rows:
        parsed = _parse_registered_group_row(row)
        if parsed is not None:
            result[parsed[0]] = parsed[1]
    return result


# ---------------------------------------------------------------------------
# JSON state migration
# ---------------------------------------------------------------------------


def _migrate_json_state() -> None:
    """Migrate legacy JSON state files into the database."""

    def migrate_file(filename: str) -> Any:
        file_path = DATA_DIR / filename
        if not file_path.exists():
            return None
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
            file_path.rename(Path(str(file_path) + ".migrated"))
            return data
        except (json.JSONDecodeError, OSError, KeyError):
            return None

    # Migrate router_state.json
    router_state = migrate_file("router_state.json")
    if router_state and isinstance(router_state, dict):
        if router_state.get("last_timestamp"):
            set_router_state("last_timestamp", router_state["last_timestamp"])
        if router_state.get("last_agent_timestamp"):
            set_router_state(
                "last_agent_timestamp",
                json.dumps(router_state["last_agent_timestamp"]),
            )

    # Migrate sessions.json
    sessions = migrate_file("sessions.json")
    if sessions and isinstance(sessions, dict):
        for folder, session_id in sessions.items():
            set_session(folder, session_id)

    # Migrate registered_groups.json
    groups = migrate_file("registered_groups.json")
    if groups and isinstance(groups, dict):
        for jid, group_data in groups.items():
            try:
                if isinstance(group_data, dict):
                    container_config = None
                    if group_data.get("containerConfig"):
                        cc = group_data["containerConfig"]
                        container_config = ContainerConfig(**cc) if isinstance(cc, dict) else None
                    group = RegisteredGroup(
                        name=group_data["name"],
                        folder=group_data["folder"],
                        trigger=group_data["trigger"],
                        added_at=group_data["added_at"],
                        container_config=container_config,
                        requires_trigger=group_data.get("requiresTrigger", True),
                        is_main=group_data.get("isMain", False),
                    )
                    set_registered_group(jid, group)
            except (ValueError, KeyError, TypeError) as err:
                logger.warn(
                    "Skipping migrated registered group with invalid folder",
                    jid=jid,
                    folder=group_data.get("folder") if isinstance(group_data, dict) else None,
                    err=str(err),
                )
