"""SQLite-хранилище заметок (текст + голос)."""
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import config


class Database:
    def __init__(self, path: str | Path = config.DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS notes (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT    NOT NULL,
                type      TEXT    NOT NULL,
                content   TEXT    NOT NULL
            )
            """
        )
        self.conn.commit()

    def add_note(self, type_: str, content: str) -> int:
        """Сохраняет запись (type: 'text' | 'voice') и возвращает её id."""
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        cur = self.conn.execute(
            "INSERT INTO notes (timestamp, type, content) VALUES (?, ?, ?)",
            (ts, type_, content),
        )
        self.conn.commit()
        return cur.lastrowid

    def last_notes(self, n: int = 10) -> list[sqlite3.Row]:
        """Последние n записей в хронологическом порядке (старые → новые)."""
        cur = self.conn.execute(
            "SELECT id, timestamp, type, content FROM notes ORDER BY id DESC LIMIT ?",
            (n,),
        )
        rows = cur.fetchall()
        return list(reversed(rows))

    def count_notes(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]

    def last_mem_updates(self, n: int = 10) -> list[sqlite3.Row]:
        """Последние n обновлений памяти (type=MEM_UPDATE), старые → новые."""
        cur = self.conn.execute(
            "SELECT id, timestamp, type, content FROM notes "
            "WHERE type='MEM_UPDATE' ORDER BY id DESC LIMIT ?",
            (n,),
        )
        rows = cur.fetchall()
        return list(reversed(rows))
