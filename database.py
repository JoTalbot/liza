"""SQLite-хранилище заметок (текст + голос)."""
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config


class Database:
    def __init__(self, path: str | Path = config.DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # каждое соединение привязано к потоку, в котором создано
        # (бот вызывает LLM через asyncio.to_thread — отдельный поток)
        self._local = threading.local()
        self._init_db(self._conn())

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    def _init_db(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS notes (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT    NOT NULL,
                type      TEXT    NOT NULL,
                content   TEXT    NOT NULL
            )
            """
        )
        # схема из ТЗ: conversations + memory_updates (зеркала/регистр памяти)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME,
                user_id   INTEGER,
                role      TEXT,
                content   TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_updates (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME,
                raw_tag   TEXT,
                synced    BOOLEAN DEFAULT 0
            )
            """
        )
        conn.commit()

    def add_note(self, type_: str, content: str, user_id: int = 0) -> int:
        """Сохраняет запись (type: 'text' | 'voice' | 'assistant' | 'MEM_UPDATE')
        и возвращает её id. Дублируется в conversations (схема ТЗ)."""
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        cur = self._conn().execute(
            "INSERT INTO notes (timestamp, type, content) VALUES (?, ?, ?)",
            (ts, type_, content),
        )
        self._conn().commit()
        if type_ in ("text", "voice", "assistant"):
            role = "assistant" if type_ == "assistant" else "user"
            try:
                self._conn().execute(
                    "INSERT INTO conversations (timestamp, user_id, role, content) VALUES (?, ?, ?, ?)",
                    (ts, user_id, role, content),
                )
                self._conn().commit()
            except Exception:  # noqa: BLE001 — зеркало не критично
                pass
        return cur.lastrowid

    def last_notes(self, n: int = 10) -> list[sqlite3.Row]:
        """Последние n записей в хронологическом порядке (старые → новые)."""
        cur = self._conn().execute(
            "SELECT id, timestamp, type, content FROM notes ORDER BY id DESC LIMIT ?",
            (n,),
        )
        rows = cur.fetchall()
        return list(reversed(rows))

    def count_notes(self) -> int:
        return self._conn().execute("SELECT COUNT(*) FROM notes").fetchone()[0]

    def last_mem_updates(self, n: int = 10) -> list[sqlite3.Row]:
        """Последние n обновлений памяти (type=MEM_UPDATE), старые → новые."""
        cur = self._conn().execute(
            "SELECT id, timestamp, type, content FROM notes "
            "WHERE type='MEM_UPDATE' ORDER BY id DESC LIMIT ?",
            (n,),
        )
        rows = cur.fetchall()
        return list(reversed(rows))

    def notes_since(self, since: datetime | None = None) -> list[sqlite3.Row]:
        """Все записи с указанного момента (по умолчанию — за последние 24 часа),
        в хронологическом порядке (старые → новые). Для ежедневного дайджеста."""
        if since is None:
            since = datetime.now(timezone.utc) - timedelta(hours=24)
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        since_iso = since.isoformat(timespec="seconds")
        cur = self._conn().execute(
            "SELECT id, timestamp, type, content FROM notes "
            "WHERE timestamp >= ? ORDER BY id ASC",
            (since_iso,),
        )
        return cur.fetchall()
