"""SQLite-backed multi-thread chat history."""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..config import database_path

MAX_MESSAGE_CHARS = 120_000


@dataclass(frozen=True)
class ChatSummary:
    id: str
    title: str
    created_at: float
    updated_at: float
    preview: str = ""
    message_count: int = 0
    model_path: str = ""


@dataclass(frozen=True)
class Message:
    id: int
    chat_id: str
    role: str
    content: str
    created_at: float


_SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    model_path TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'system', 'tool')),
    content TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, id);
CREATE INDEX IF NOT EXISTS idx_chats_updated ON chats(updated_at DESC);
"""


class ChatStore:
    """Small persistence layer. One connection per operation keeps it safe."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or database_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.executescript(_SCHEMA)
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(chats)").fetchall()
            }
            if "model_path" not in columns:
                connection.execute(
                    "ALTER TABLE chats ADD COLUMN model_path TEXT NOT NULL DEFAULT ''"
                )

    def create_chat(self, title: str = "New transmission", model_path: str = "") -> str:
        chat_id = uuid.uuid4().hex[:16]
        now = time.time()
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO chats (id, title, created_at, updated_at, model_path) "
                "VALUES (?, ?, ?, ?, ?)",
                (chat_id, title, now, now, model_path or ""),
            )
        return chat_id

    def list_chats(self) -> list[ChatSummary]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT c.id, c.title, c.created_at, c.updated_at, c.model_path,
                       COALESCE((
                           SELECT m.content FROM messages m
                           WHERE m.chat_id = c.id
                           ORDER BY m.id DESC LIMIT 1
                       ), '') AS preview,
                       (SELECT COUNT(*) FROM messages m WHERE m.chat_id = c.id)
                           AS message_count
                FROM chats c
                ORDER BY c.updated_at DESC
                """
            ).fetchall()
        return [
            ChatSummary(
                id=row["id"],
                title=row["title"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                preview=_single_line(row["preview"]),
                message_count=int(row["message_count"]),
                model_path=row["model_path"] or "",
            )
            for row in rows
        ]

    def get_chat(self, chat_id: str) -> ChatSummary | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT id, title, created_at, updated_at, model_path "
                "FROM chats WHERE id = ?",
                (chat_id,),
            ).fetchone()
        if row is None:
            return None
        return ChatSummary(
            id=row["id"],
            title=row["title"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            model_path=row["model_path"] or "",
        )

    def set_chat_model(self, chat_id: str, model_path: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE chats SET model_path = ? WHERE id = ?",
                (model_path or "", chat_id),
            )

    def rename_chat(self, chat_id: str, title: str) -> None:
        title = title.strip() or "Untitled transmission"
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE chats SET title = ?, updated_at = ? WHERE id = ?",
                (title[:80], time.time(), chat_id),
            )

    def auto_title(self, chat_id: str, first_message: str) -> None:
        """Name an untouched thread from its first user prompt."""

        title = _make_title(first_message)
        if not title:
            return
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE chats SET title = ? WHERE id = ? AND title = 'New transmission'",
                (title, chat_id),
            )

    def delete_chat(self, chat_id: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM chats WHERE id = ?", (chat_id,))

    def clear_all_chats(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM messages")
            connection.execute("DELETE FROM chats")

    def add_message(self, chat_id: str, role: str, content: str) -> Message:
        content = content.strip()
        if len(content) > MAX_MESSAGE_CHARS:
            raise ValueError(
                f"Messages are limited to {MAX_MESSAGE_CHARS:,} characters."
            )
        if role not in {"user", "assistant", "system", "tool"}:
            raise ValueError(f"Unsupported role: {role}")
        now = time.time()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO messages (chat_id, role, content, created_at) "
                "VALUES (?, ?, ?, ?)",
                (chat_id, role, content, now),
            )
            connection.execute(
                "UPDATE chats SET updated_at = ? WHERE id = ?", (now, chat_id)
            )
            message_id = int(cursor.lastrowid)
        return Message(message_id, chat_id, role, content, now)

    def get_messages(self, chat_id: str) -> list[Message]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT id, chat_id, role, content, created_at FROM messages "
                "WHERE chat_id = ? ORDER BY id ASC",
                (chat_id,),
            ).fetchall()
        return [
            Message(
                id=row["id"],
                chat_id=row["chat_id"],
                role=row["role"],
                content=row["content"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def recent_context(self, chat_id: str, limit: int = 24) -> list[Message]:
        limit = max(1, min(int(limit or 24), 200))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT id, chat_id, role, content, created_at FROM messages "
                "WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
                (chat_id, limit),
            ).fetchall()
        rows.reverse()
        return [
            Message(
                id=row["id"],
                chat_id=row["chat_id"],
                role=row["role"],
                content=row["content"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def set_last_chat(self, chat_id: str) -> None:
        # UI-level preference; stored by Settings, not the database.
        _ = chat_id


def _single_line(text: str) -> str:
    value = " ".join(str(text).split())
    return value[:90]


def _make_title(text: str) -> str:
    value = " ".join(text.strip().split())
    if not value:
        return ""
    value = value.strip("\"'")
    if len(value) > 46:
        value = value[:45].rstrip() + "…"
    return value
