"""SQLite database operations — port of src/db.ts."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from nanoclaw.config import DATA_DIR, STORE_DIR
from nanoclaw.logger import logger
from nanoclaw.models import NewMessage, RegisteredGroup, ScheduledTask, TaskRunLog

_db: sqlite3.Connection | None = None


def _get_db() -> sqlite3.Connection:
    assert _db is not None, "Database not initialized — call init_database() first"
    return _db


_SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    jid TEXT PRIMARY KEY,
    name TEXT,
    last_message_time TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT,
    chat_jid TEXT,
    sender TEXT,
    sender_name TEXT,
    content TEXT,
    timestamp TEXT,
    is_from_me INTEGER,
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
    context_mode TEXT DEFAULT 'isolated',
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
"""


def _create_schema(db: sqlite3.Connection) -> None:
    db.executescript(_SCHEMA)


def init_database() -> None:
    global _db
    db_path = STORE_DIR / "messages.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _db = sqlite3.connect(str(db_path))
    _db.row_factory = sqlite3.Row
    _create_schema(_db)
    _migrate_json_state()


def init_test_database() -> None:
    """Create an in-memory database for tests."""
    global _db
    _db = sqlite3.connect(":memory:")
    _db.row_factory = sqlite3.Row
    _create_schema(_db)


# --- Chat metadata ---


def store_chat_metadata(chat_jid: str, timestamp: str, name: str | None = None) -> None:
    db = _get_db()
    if name:
        db.execute(
            """
            INSERT INTO chats (jid, name, last_message_time) VALUES (?, ?, ?)
            ON CONFLICT(jid) DO UPDATE SET
                name = excluded.name,
                last_message_time = MAX(last_message_time, excluded.last_message_time)
            """,
            (chat_jid, name, timestamp),
        )
    else:
        db.execute(
            """
            INSERT INTO chats (jid, name, last_message_time) VALUES (?, ?, ?)
            ON CONFLICT(jid) DO UPDATE SET
                last_message_time = MAX(last_message_time, excluded.last_message_time)
            """,
            (chat_jid, chat_jid, timestamp),
        )
    db.commit()


def update_chat_name(chat_jid: str, name: str) -> None:
    db = _get_db()
    from datetime import datetime, timezone

    db.execute(
        """
        INSERT INTO chats (jid, name, last_message_time) VALUES (?, ?, ?)
        ON CONFLICT(jid) DO UPDATE SET name = excluded.name
        """,
        (chat_jid, name, datetime.now(timezone.utc).isoformat()),
    )
    db.commit()


def get_all_chats() -> list[dict]:
    db = _get_db()
    rows = db.execute(
        "SELECT jid, name, last_message_time FROM chats ORDER BY last_message_time DESC"
    ).fetchall()
    return [dict(r) for r in rows]


# --- Messages ---


def store_message(msg: NewMessage) -> None:
    db = _get_db()
    db.execute(
        """INSERT OR REPLACE INTO messages
           (id, chat_jid, sender, sender_name, content, timestamp, is_from_me)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (msg.id, msg.chat_jid, msg.sender, msg.sender_name, msg.content, msg.timestamp,
         1 if msg.is_from_me else 0),
    )
    db.commit()


def get_new_messages(
    jids: list[str], last_timestamp: str, bot_prefix: str
) -> tuple[list[NewMessage], str]:
    if not jids:
        return [], last_timestamp

    db = _get_db()
    placeholders = ",".join("?" for _ in jids)
    sql = f"""
        SELECT id, chat_jid, sender, sender_name, content, timestamp
        FROM messages
        WHERE timestamp > ? AND chat_jid IN ({placeholders}) AND content NOT LIKE ?
        ORDER BY timestamp
    """
    rows = db.execute(sql, [last_timestamp, *jids, f"{bot_prefix}:%"]).fetchall()

    messages = [
        NewMessage(
            id=r["id"],
            chat_jid=r["chat_jid"],
            sender=r["sender"],
            sender_name=r["sender_name"],
            content=r["content"],
            timestamp=r["timestamp"],
        )
        for r in rows
    ]

    new_ts = last_timestamp
    for m in messages:
        if m.timestamp > new_ts:
            new_ts = m.timestamp
    return messages, new_ts


def get_messages_since(
    chat_jid: str, since_timestamp: str, bot_prefix: str
) -> list[NewMessage]:
    db = _get_db()
    rows = db.execute(
        """
        SELECT id, chat_jid, sender, sender_name, content, timestamp
        FROM messages
        WHERE chat_jid = ? AND timestamp > ? AND content NOT LIKE ?
        ORDER BY timestamp
        """,
        (chat_jid, since_timestamp, f"{bot_prefix}:%"),
    ).fetchall()
    return [
        NewMessage(
            id=r["id"],
            chat_jid=r["chat_jid"],
            sender=r["sender"],
            sender_name=r["sender_name"],
            content=r["content"],
            timestamp=r["timestamp"],
        )
        for r in rows
    ]


# --- Scheduled tasks ---


def create_task(task: ScheduledTask) -> None:
    db = _get_db()
    db.execute(
        """INSERT INTO scheduled_tasks
           (id, group_folder, chat_jid, prompt, schedule_type, schedule_value,
            context_mode, next_run, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            task.id, task.group_folder, task.chat_jid, task.prompt,
            task.schedule_type, task.schedule_value, task.context_mode,
            task.next_run, task.status, task.created_at,
        ),
    )
    db.commit()


def get_task_by_id(task_id: str) -> ScheduledTask | None:
    db = _get_db()
    row = db.execute("SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        return None
    return _row_to_task(row)


def get_all_tasks() -> list[ScheduledTask]:
    db = _get_db()
    rows = db.execute(
        "SELECT * FROM scheduled_tasks ORDER BY created_at DESC"
    ).fetchall()
    return [_row_to_task(r) for r in rows]


def get_due_tasks() -> list[ScheduledTask]:
    from datetime import datetime, timezone

    db = _get_db()
    now = datetime.now(timezone.utc).isoformat()
    rows = db.execute(
        """SELECT * FROM scheduled_tasks
           WHERE status = 'active' AND next_run IS NOT NULL AND next_run <= ?
           ORDER BY next_run""",
        (now,),
    ).fetchall()
    return [_row_to_task(r) for r in rows]


def update_task(task_id: str, **updates: str | None) -> None:
    db = _get_db()
    fields = []
    values: list[str | None] = []
    for key in ("prompt", "schedule_type", "schedule_value", "next_run", "status"):
        if key in updates:
            fields.append(f"{key} = ?")
            values.append(updates[key])
    if not fields:
        return
    values.append(task_id)
    db.execute(f"UPDATE scheduled_tasks SET {', '.join(fields)} WHERE id = ?", values)
    db.commit()


def delete_task(task_id: str) -> None:
    db = _get_db()
    db.execute("DELETE FROM task_run_logs WHERE task_id = ?", (task_id,))
    db.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))
    db.commit()


def update_task_after_run(task_id: str, next_run: str | None, last_result: str) -> None:
    from datetime import datetime, timezone

    db = _get_db()
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        """UPDATE scheduled_tasks
           SET next_run = ?, last_run = ?, last_result = ?,
               status = CASE WHEN ? IS NULL THEN 'completed' ELSE status END
           WHERE id = ?""",
        (next_run, now, last_result, next_run, task_id),
    )
    db.commit()


def log_task_run(log: TaskRunLog) -> None:
    db = _get_db()
    db.execute(
        """INSERT INTO task_run_logs (task_id, run_at, duration_ms, status, result, error)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (log.task_id, log.run_at, log.duration_ms, log.status, log.result, log.error),
    )
    db.commit()


# --- Router state ---


def get_router_state(key: str) -> str | None:
    db = _get_db()
    row = db.execute("SELECT value FROM router_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_router_state(key: str, value: str) -> None:
    db = _get_db()
    db.execute(
        "INSERT OR REPLACE INTO router_state (key, value) VALUES (?, ?)", (key, value)
    )
    db.commit()


# --- Sessions ---


def get_session(group_folder: str) -> str | None:
    db = _get_db()
    row = db.execute(
        "SELECT session_id FROM sessions WHERE group_folder = ?", (group_folder,)
    ).fetchone()
    return row["session_id"] if row else None


def set_session(group_folder: str, session_id: str) -> None:
    db = _get_db()
    db.execute(
        "INSERT OR REPLACE INTO sessions (group_folder, session_id) VALUES (?, ?)",
        (group_folder, session_id),
    )
    db.commit()


def get_all_sessions() -> dict[str, str]:
    db = _get_db()
    rows = db.execute("SELECT group_folder, session_id FROM sessions").fetchall()
    return {r["group_folder"]: r["session_id"] for r in rows}


# --- Registered groups ---


def get_registered_group(jid: str) -> RegisteredGroup | None:
    db = _get_db()
    row = db.execute("SELECT * FROM registered_groups WHERE jid = ?", (jid,)).fetchone()
    if not row:
        return None
    return _row_to_group(row)


def set_registered_group(jid: str, group: RegisteredGroup) -> None:
    db = _get_db()
    db.execute(
        """INSERT OR REPLACE INTO registered_groups
           (jid, name, folder, trigger_pattern, added_at, container_config, requires_trigger)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            jid, group.name, group.folder, group.trigger, group.added_at,
            json.dumps(group.container_config.model_dump()) if group.container_config else None,
            1 if group.requires_trigger else 0,
        ),
    )
    db.commit()


def get_all_registered_groups() -> dict[str, RegisteredGroup]:
    db = _get_db()
    rows = db.execute("SELECT * FROM registered_groups").fetchall()
    return {r["jid"]: _row_to_group(r) for r in rows}


# --- Helpers ---


def _row_to_group(row: sqlite3.Row) -> RegisteredGroup:
    cc = None
    if row["container_config"]:
        from nanoclaw.models import ContainerConfig
        cc = ContainerConfig.model_validate_json(row["container_config"])
    return RegisteredGroup(
        name=row["name"],
        folder=row["folder"],
        trigger=row["trigger_pattern"],
        added_at=row["added_at"],
        container_config=cc,
        requires_trigger=bool(row["requires_trigger"]) if row["requires_trigger"] is not None else True,
    )


def _row_to_task(row: sqlite3.Row) -> ScheduledTask:
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


# --- JSON migration ---


def _migrate_json_state() -> None:
    def _migrate_file(filename: str) -> dict | list | None:
        file_path = DATA_DIR / filename
        if not file_path.exists():
            return None
        try:
            data = json.loads(file_path.read_text())
            file_path.rename(file_path.with_suffix(file_path.suffix + ".migrated"))
            return data
        except Exception:
            return None

    router_state = _migrate_file("router_state.json")
    if router_state and isinstance(router_state, dict):
        if router_state.get("last_timestamp"):
            set_router_state("last_timestamp", router_state["last_timestamp"])
        if router_state.get("last_agent_timestamp"):
            set_router_state(
                "last_agent_timestamp",
                json.dumps(router_state["last_agent_timestamp"]),
            )

    sessions = _migrate_file("sessions.json")
    if sessions and isinstance(sessions, dict):
        for folder, sid in sessions.items():
            set_session(folder, sid)

    groups = _migrate_file("registered_groups.json")
    if groups and isinstance(groups, dict):
        for jid, g in groups.items():
            set_registered_group(jid, RegisteredGroup.model_validate(g))
