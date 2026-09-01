"""Реалтайм-синк записей в Google Docs.

Два режима (достаточно одного):
1. GOOGLE_DOC_WEBHOOK_URL — POST JSON на Google Apps Script webhook,
   который сам добавляет строку в нужный документ.
2. GOOGLE_SERVICE_ACCOUNT_JSON + GOOGLE_DOC_ID — напрямую через
   Google Docs API (documents.batchUpdate, insertText в конец документа).

Если ничего не настроено — пишем предупреждение в лог и не падаем.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone

import aiohttp

import config

log = logging.getLogger(__name__)

TYPE_ICONS = {"text": "📝", "voice": "🎧", "assistant": "🤖", "MEM_UPDATE": "🧠"}


def is_configured() -> bool:
    return bool(
        config.GOOGLE_DOC_WEBHOOK_URL
        or (config.GOOGLE_SERVICE_ACCOUNT_JSON and config.GOOGLE_DOC_ID)
    )


async def append_entry(entry_type: str, content: str) -> bool:
    """Асинхронно добавляет запись в Google Doc. Никогда не бросает исключений."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    icon = TYPE_ICONS.get(entry_type, "📌")
    line = f"[{ts}] {icon} {content}".strip()

    if not is_configured():
        log.warning(
            "Google sync не настроен (нужен GOOGLE_DOC_WEBHOOK_URL "
            "или GOOGLE_SERVICE_ACCOUNT_JSON + GOOGLE_DOC_ID) — пропускаю"
        )
        return False

    try:
        if config.GOOGLE_DOC_WEBHOOK_URL:
            await _append_via_webhook(line, entry_type, ts)
        else:
            await asyncio.to_thread(_append_via_docs_api, line)
        log.info("Google sync: добавлено (%s)", entry_type)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("Google sync не удался: %s", exc)
        return False


async def _append_via_webhook(line: str, entry_type: str, ts: str) -> None:
    payload = {
        "content": line,
        "type": entry_type,
        "timestamp": ts,
        "source": "liza-bot",
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(
            config.GOOGLE_DOC_WEBHOOK_URL,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status >= 400:
                body = await resp.text()
                log.warning("Webhook ответил %s: %s", resp.status, body[:300])


def _append_via_docs_api(line: str) -> None:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_info(
        json.loads(config.GOOGLE_SERVICE_ACCOUNT_JSON),
        scopes=["https://www.googleapis.com/auth/documents"],
    )
    svc = build("docs", "v1", credentials=creds, cache_discovery=False)
    svc.documents().batchUpdate(
        documentId=config.GOOGLE_DOC_ID,
        body={
            "requests": [
                {"insertText": {"endOfSegmentLocation": {}, "text": line + "\n"}}
            ]
        },
    ).execute()


# =========================================================================
# MemorySyncManager — динамическая память + автопаспорта проектов (Hub-and-Spoke)
# -------------------------------------------------------------------------
# Обрабатывает теги [MEM_UPDATE: ...] из ответов модели:
#   * пишет обновление в таблицу memory_updates (SQLite);
#   * payload вида "NEW_PROJECT: Имя | Стек | Описание" создаёт паспорт проекта
#     PRJ_<имя>.md в /data/projects и регистрирует его в Мастер-Индексе;
#   * остальные обновления дописывает в дневной лог (chronicles/YYYY-MM_Digests.md).
# =========================================================================
import os
import re
from pathlib import Path

import aiosqlite

# пути по умолчанию совпадают с конфигом (docker: /data/...)
DB_PATH = os.getenv("DB_PATH", config.DB_PATH)
CHRONICLES_DIR = os.getenv("CHRONICLES_DIR", config.CHRONICLES_DIR)
PROJECTS_DIR = os.getenv("PROJECTS_DIR", config.PROJECTS_DIR)

MEM_UPDATE_PATTERN = re.compile(r"\[MEM_UPDATE:\s*(.*?)\]", re.IGNORECASE | re.DOTALL)

PROJECT_TEMPLATE = """# Project: {name}
- **ID:** PRJ-{idx:02d}
- **Статус:** 🟢 В активной разработке
- **Дата создания:** {date}
- **Стек и компоненты:** {stack}
- **Цель / Результат:** {desc}

## 1. Архитектура и схема

- Порты, модули, конфигурации: (Заполняется в процессе диалога)

## 2. Лог решений и этапов

- **{date}:** Инициализация проекта через тег автоматической памяти [MEM_UPDATE].
"""


class MemorySyncManager:
    """Асинхронный менеджер памяти: теги [MEM_UPDATE] -> SQLite + паспорта проектов.

    Никогда не бросает исключений наружу (память не должна ломать бота).
    """

    def __init__(self, db_path: str = DB_PATH) -> None:
        self.db_path = db_path
        Path(CHRONICLES_DIR).mkdir(parents=True, exist_ok=True)
        Path(PROJECTS_DIR).mkdir(parents=True, exist_ok=True)
        self._db = None  # ленивое aiosqlite-соединение

    async def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            self._db = await aiosqlite.connect(self.db_path, timeout=15)
            self._db.row_factory = aiosqlite.Row
            await self._db.execute("PRAGMA busy_timeout = 10000")
            await self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_updates (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME,
                    raw_tag   TEXT,
                    synced    BOOLEAN DEFAULT 0
                )
                """
            )
            await self._db.commit()
        return self._db

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def process_text_for_memory(self, text: str) -> tuple[str, str | None]:
        """Извлекает первый [MEM_UPDATE: ...] из текста.

        Возвращает (очищенный текст без тега, payload или None).
        Payload обрабатывается (БД + паспорт проекта / дневной лог).
        """
        match = MEM_UPDATE_PATTERN.search(text or "")
        if not match:
            return text, None
        payload = match.group(1).strip()
        cleaned = MEM_UPDATE_PATTERN.sub("", text).strip()
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        await self.handle_memory_payload(payload)
        return cleaned, payload

    async def handle_memory_payload(self, payload: str) -> None:
        try:
            log.info("💾 Память: %s", payload[:200])
            conn = await self._conn()
            await conn.execute(
                "INSERT INTO memory_updates (timestamp, raw_tag, synced) VALUES (?, ?, ?)",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"), payload, 1),
            )
            await conn.commit()

            if payload.startswith("NEW_PROJECT:"):
                await self._create_new_project(payload)
            else:
                await self._append_to_daily_log(payload)
        except Exception as exc:  # noqa: BLE001
            log.warning("Обработка памяти не удалась: %s", exc)

    async def _create_new_project(self, payload: str) -> None:
        """NEW_PROJECT: Имя | Стек | Описание -> PRJ_<Имя>.md + Мастер-Индекс."""
        try:
            content = payload.replace("NEW_PROJECT:", "", 1).strip()
            parts = [p.strip() for p in content.split("|")]
            name = parts[0] if parts and parts[0] else "Unnamed_Project"
            stack = parts[1] if len(parts) > 1 else "Not specified"
            desc = parts[2] if len(parts) > 2 else "No description"

            safe_name = re.sub(r"[^\w\- ]+", "", name).replace(" ", "_").strip("_") or "Unnamed_Project"
            filename = f"PRJ_{safe_name}.md"
            filepath = Path(PROJECTS_DIR) / filename
            date_str = datetime.now().strftime("%Y-%m-%d")

            # уникальный ID: берём индекс существующего файла или следующий
            idx = self._existing_idx(filepath)
            if idx is None:
                idx = self._next_idx()

            filepath.write_text(
                PROJECT_TEMPLATE.format(name=name, idx=idx, date=date_str, stack=stack, desc=desc),
                encoding="utf-8",
            )
            log.info("📁 Паспорт проекта создан: %s (PRJ-%02d)", filepath, idx)
            self._update_master_index()
            await self._append_to_daily_log(f"Зарегистрирован новый проект: {name} (Стек: {stack})")
        except Exception as exc:  # noqa: BLE001
            log.error("Ошибка создания проекта: %s", exc)

    def _existing_idx(self, filepath: Path) -> int | None:
        """Возвращает PRJ-индекс, если файл проекта уже существует."""
        try:
            text = filepath.read_text(encoding="utf-8")
            m = re.search(r"\*\*ID:\*\*\s*PRJ-(\d+)", text)
            if m:
                return int(m.group(1))
        except Exception:  # noqa: BLE001
            pass
        return None

    def _next_idx(self) -> int:
        """Следующий свободный номер PRJ: max(ID) среди существующих паспортов + 1."""
        idx = 0
        try:
            for f in Path(PROJECTS_DIR).glob("PRJ_*.md"):
                text = f.read_text(encoding="utf-8")
                m = re.search(r"\*\*ID:\*\*\s*PRJ-(\d+)", text)
                if m:
                    idx = max(idx, int(m.group(1)))
            return idx + 1
        except Exception:  # noqa: BLE001
            return 1

    def _update_master_index(self) -> None:
        """Пересобирает Мастер-Индекс /data/projects/INDEX.md из всех паспортов.

        Индекс всегда выводится из фактических PRJ_*.md — дубли исключены,
        обновление существующего проекта перезаписывает его строку.
        """
        try:
            entries: list[tuple[str, str, str, str, str]] = []
            for f in sorted(Path(PROJECTS_DIR).glob("PRJ_*.md")):
                text = f.read_text(encoding="utf-8")
                m_id = re.search(r"\*\*ID:\*\*\s*(PRJ-\d+)", text)
                m_name = re.search(r"^# Project:\s*(.+)$", text, re.M)
                m_date = re.search(r"\*\*Дата создания:\*\*\s*(\S+)", text)
                m_stack = re.search(r"\*\*Стек и компоненты:\*\*\s*(.+)", text)
                entries.append((
                    m_id.group(1) if m_id else "?",
                    m_name.group(1).strip() if m_name else f.name,
                    m_date.group(1) if m_date else "?",
                    m_stack.group(1).strip() if m_stack else "?",
                    f.name,
                ))
            lines = [
                "# 📇 Мастер-Индекс проектов (Liza Brain)",
                "",
                "| ID | Проект | Дата | Стек | Паспорт |",
                "|----|--------|------|------|---------|",
            ]
            for e in sorted(entries, key=lambda x: x[0]):
                lines.append(f"| {e[0]} | {e[1]} | {e[2]} | {e[3]} | {e[4]} |")
            (Path(PROJECTS_DIR) / "INDEX.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
            log.info("📇 Мастер-Индекс пересобран: %d проектов", len(entries))
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось обновить мастер-индекс: %s", exc)

    async def _append_to_daily_log(self, update_text: str) -> None:
        """Дописывает MEM_UPDATE в /data/chronicles/YYYY-MM_Digests.md."""
        now = datetime.now()
        month_file = Path(CHRONICLES_DIR) / f"{now.strftime('%Y-%m')}_Digests.md"
        entry = f"- `[{now.strftime('%Y-%m-%d %H:%M:%S')}]` [MEM_UPDATE]: {update_text}\n"
        try:
            with open(month_file, "a", encoding="utf-8") as f:
                f.write(entry)
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось дописать дневной лог: %s", exc)


# единый экземпляр для всего приложения
memory_manager = MemorySyncManager()
