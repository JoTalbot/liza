#!/usr/bin/env python3
"""Ежедневный дайджест памяти Лизы (хроника в /opt/liza_data/chronicles).

Автономный скрипт: собирает за последние 24 часа записи диалога
(conversations) и запомненные факты (memory_updates) из context.db
и дописывает итог дня в стандартном LizaBrain-формате в
chronicles/YYYY-MM_Digests.md.

Запуск: systemd timer liza-digest.timer (каждый день в 23:59),
либо вручную: python3 daily_memory_digest.py

Всегда завершается успешно (никогда не роняет таймер).
"""
from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("daily-memory-digest")

DB_PATH = Path("/opt/liza_data/context.db")
CHRONICLES_DIR = Path("/opt/liza_data/chronicles")

# Внешний конфиг (для тестов и деплоя)
import os  # noqa: E402

DB_PATH = Path(os.environ.get("MEMORY_DB", str(DB_PATH)))
CHRONICLES_DIR = Path(os.environ.get("CHRONICLES_DIR", str(CHRONICLES_DIR)))

DIGEST_TEMPLATE = """### 📅 {date} — Итоги дня и ключевые решения (память Лизы)

- **Главный фокус дня:** {focus}
- **Принятые решения / сделанное:**{decisions}
- **Запомненные факты [MEM_UPDATE]:**{mem}
- **Открытые задачи на завтра:**{open}
"""


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_rows() -> tuple[list[tuple[str, str]], list[str]]:
    """(диалог [(role, content)], факты [raw_tag]) за последние 24 часа."""
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    dialog: list[tuple[str, str]] = []
    mem: list[str] = []
    try:
        con = sqlite3.connect(DB_PATH, timeout=10)
        con.row_factory = sqlite3.Row
        for r in con.execute(
            "SELECT role, content FROM conversations WHERE timestamp >= ? ORDER BY id ASC",
            (since,),
        ):
            dialog.append((r["role"], (r["content"] or "").strip()))
        for r in con.execute(
            "SELECT raw_tag FROM memory_updates WHERE timestamp >= ? ORDER BY id ASC",
            (since,),
        ):
            t = (r["raw_tag"] or "").strip()
            if t:
                mem.append(t)
        con.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("Не удалось прочитать память: %s", exc)
    return dialog, mem


def _one_line(text: str, limit: int = 180) -> str:
    return (text.replace("\n", " ").replace("  ", " ").strip())[:limit]


def _bullet(items: list[str], limit_each: int = 180, max_items: int = 6) -> str:
    if not items:
        return "—"
    out = []
    for i in items[:max_items]:
        out.append(f"\n  - {_one_line(i, limit_each)}")
    return "".join(out)


def _build_digest() -> str:
    dialog, mem = _load_rows()
    user_msgs = [c for r, c in dialog if r == "user" and c]
    asst_msgs = [c for r, c in dialog if r == "assistant" and c]

    focus = max(user_msgs, key=len, default="")
    if not focus:
        focus = "Нет текстовых сообщений от пользователя за сутки."
    else:
        focus = _one_line(focus, 220)

    decisions = mem or [c for c in asst_msgs if len(c) > 40]

    open_tasks = [
        c for c in user_msgs
        if any(k in c.lower() for k in ("завтра", "todo", "нужно", "доделать", "потом", "осталось", "вопрос"))
    ]

    return DIGEST_TEMPLATE.format(
        date=datetime.now().strftime("%Y-%m-%d"),
        focus=focus,
        decisions=_bullet(decisions),
        mem=_bullet(mem, max_items=8),
        open=_bullet(open_tasks, max_items=5),
    )


def run() -> int:
    CHRONICLES_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{datetime.now().strftime('%Y-%m')}_Digests.md"
    path = CHRONICLES_DIR / filename
    digest = _build_digest()
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n\n" + digest.strip() + "\n")
    log.info("Дайджест записан: %s", path)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except Exception as exc:  # noqa: BLE001
        log.exception("Ошибка дайджеста: %s", exc)
        raise SystemExit(1)
