"""Ежедневный дайджест: итоги дня в формате LizaBrain.

Раз в сутки (по умолчанию 23:59, настраивается через DAILY_DIGEST_TIME)
собирает все записи и [MEM_UPDATE] из /data/context.db за последние 24 часа,
собирает из них итоговый отчёт в стандартном формате и дописывает его в
/data/chronicles/YYYY-MM_Digests.md. Дайджест также пробует уйти в Google Docs
через google_sync (best-effort).

Генерация: сначала пробуем LLM (Groq — не зависит от браузера), при любой
ошибке — эвристическая сборка из сырых записей. Дайджест никогда не роняет бота.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import config
import google_sync

log = logging.getLogger(__name__)

# Директория с хрониками (маунтится на хосте как /opt/liza_data/chronicles)
CHRONICLES_DIR = Path(config.CHRONICLES_DIR)

# Шаблон из ТЗ (LizaBrain markdown)
DIGEST_TEMPLATE = """### 📅 {date} — Итоги дня и ключевые решения

- **Главный фокус дня:** {focus}
- **Принятые решения:**{decisions}
- **Архитектурные / Кодовые изменения:**{arch}
- **Новые факты [MEM_UPDATE]:**{mem}
- **Открытые задачи на завтра:**{open}
"""

# Ключевые слова для эвристических секций «код/архитектура» и «открытые задачи»
ARCH_KEYWORDS = (
    "код", "бот", "сервер", "депло", "фикс", "исправил", "обновил", "добавил",
    "сделал", "написал", "gith", "docker", "база", "скрипт", "браузер",
    "gemini", "ошибк", "авторазбор", "бизнес",
)
OPEN_KEYWORDS = (
    "завтра", "todo", "нужно", "потом", "открыт", "осталось", "доделать",
    "вопрос", "идея", "надо", "планиру", "хочу",
)

_LLM_SYSTEM = (
    "Ты — ЛИЗА, личный ИИ-компаньон и «второй мозг». Составь ежедневный "
    "дайджест из сырых записей за сутки. Строго придерживайся этого формата "
    "Markdown (секции не пропускай, если данных нет — ставь «—»):\n\n"
    "### 📅 ГГГГ-ММ-ДД — Итоги дня и ключевые решения\n\n"
    "- **Главный фокус дня:** <1–2 предложения о главном деле дня>\n"
    "- **Принятые решения:** <кратко, что решено/сделано>\n"
    "- **Архитектурные / Кодовые изменения:** <что менялось в коде/сервере/проекте>\n"
    "- **Новые факты [MEM_UPDATE]:** <важные факты, которые ЛИЗА запомнила>\n"
    "- **Открытые задачи на завтра:** <что осталось>\n"
    "Отвечай только самим дайджестом, без лишних слов."
)

# ---------------------------------------------------------------- сбор данных


def _split_hhmm(ts_iso: str) -> str:
    """'2026-09-01T09:41:12+00:00' -> '09:41' (время в том виде, как в БД, UTC)."""
    m = re.search(r"T(\d{2}:\d{2})", ts_iso or "")
    return m.group(1) if m else (ts_iso or "")[:16]


def gather_notes(db: Any, since: datetime | None = None) -> dict[str, list[str]]:
    """Собирает записи за последние 24 часа и раскладывает по ролям.

    Возвращает:
      user: [время, текст] пользовательских сообщений (text/voice)
      assistant: [время, текст] ответов ЛИЗЫ
      mem: [время, текст] обновлений памяти [MEM_UPDATE]
    """
    rows = db.notes_since(since)
    user: list[str] = []
    assistant: list[str] = []
    mem: list[str] = []
    for r in rows:
        content = (r["content"] or "").strip()
        if not content:
            continue
        stamp = _split_hhmm(r["timestamp"])
        prefix = f"[{stamp}]"
        if r["type"] == "MEM_UPDATE":
            mem.append(f"{prefix} {content}")
        elif r["type"] in ("text", "voice"):
            user.append(f"{prefix} {content}")
        elif r["type"] == "assistant":
            assistant.append(f"{prefix} {content}")
    return {"user": user, "assistant": assistant, "mem": mem}


def _one_line(text: str, limit: int = 160) -> str:
    return (text.replace("\n", " ").replace("  ", " ").strip())[:limit]


def _bullet(items: list[str], limit_each: int = 120, max_items: int = 5) -> str:
    """Многострочный блок «  - пункт» (пусто → «—»).

    Используется после «Label:» — чтобы не получалось
    «- **Label:** - item», пункты идут с отступом на своих строках.
    """
    if not items:
        return "—"
    lines = [_one_line(i, limit_each) for i in items[:max_items]]
    return "\n  - ".join(["", *lines])


# ------------------------------------------------------------ эвристический


def _heuristic_digest(data: dict[str, list[str]]) -> str:
    """Собирает дайджест по шаблону без LLM (запасной вариант)."""
    date = datetime.now().strftime("%Y-%m-%d")

    user_all = [t.split(" ", 1)[1] for t in data["user"] if " " in t]
    assistant_all = [t.split(" ", 1)[1] for t in data["assistant"] if " " in t]
    mem_all = [t.split(" ", 1)[1] for t in data["mem"] if " " in t]
    all_texts = user_all + assistant_all

    # Главный фокус: самое содержательное сообщение пользователя
    focus = max(user_all, key=len, default="")
    if focus:
        focus = _one_line(focus, 200)
    else:
        focus = "Нет текстовых сообщений от пользователя."

    # Принятые решения: новые факты + ответы ЛИЗЫ
    decisions = mem_all or assistant_all

    # Архитектурные/кодовые изменения
    arch = [
        t for t in all_texts
        if any(k in t.lower() for k in ARCH_KEYWORDS)
    ]

    # Открытые задачи
    open_tasks = [
        t for t in user_all
        if any(k in t.lower() for k in OPEN_KEYWORDS)
    ]

    return DIGEST_TEMPLATE.format(
        date=date,
        focus=focus,
        decisions=_bullet(decisions, limit_each=200, max_items=6),
        arch=_bullet(arch, limit_each=200, max_items=5),
        mem=_bullet(mem_all, limit_each=200, max_items=8),
        open=_bullet(open_tasks, limit_each=160, max_items=5),
    )


# ------------------------------------------------------------------ LLM-путь


def _transcript(data: dict[str, list[str]]) -> str:
    parts: list[str] = []
    for t in data["user"]:
        parts.append(f"👤 Пользователь: {_one_line(t.split(' ', 1)[1] if ' ' in t else t, 300)}")
    for t in data["mem"]:
        parts.append(f"🧠 MEM_UPDATE: {_one_line(t.split(' ', 1)[1] if ' ' in t else t, 300)}")
    for t in data["assistant"]:
        parts.append(f"🤖 ЛИЗА: {_one_line(t.split(' ', 1)[1] if ' ' in t else t, 300)}")
    return "\n".join(parts) if parts else "(за сутки записей не было)"


def _generate_with_llm(data: dict[str, list[str]]) -> str | None:
    """Пытается сгенерировать дайджест через Groq (без браузера)."""
    try:
        from ai_brain import GroqBrain

        brain = GroqBrain(config.GROQ_API_KEYS)
        user_prompt = (
            f"Сегодня: {datetime.now().strftime('%Y-%m-%d')}.\n"
            "Сырые записи за последние 24 часа:\n\n"
            f"{_transcript(data)}"
        )
        text = brain.chat([
            {"role": "system", "content": _LLM_SYSTEM},
            {"role": "user", "content": user_prompt},
        ])
        text = text.strip()
        if not text:
            return None
        # гарантируем заголовок дня
        if not text.startswith("### 📅"):
            text = f"### 📅 {datetime.now().strftime('%Y-%m-%d')} — Итоги дня и ключевые решения\n\n" + text
        return text
    except Exception as exc:  # noqa: BLE001
        log.warning("LLM-дайджест не удался (%s) — использую эвристику", exc)
        return None


# ---------------------------------------------------------------- запись


def append_digest(text: str) -> Path:
    """Дописывает дайджест в /data/chronicles/YYYY-MM_Digests.md, возвращает путь."""
    CHRONICLES_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{datetime.now().strftime('%Y-%m')}_Digests.md"
    path = CHRONICLES_DIR / filename
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n\n" + text.strip() + "\n")
    log.info("Дайджест записан: %s", path)
    return path


async def build_and_append(db: Any) -> dict[str, Any]:
    """Полный конвейер: собрать → сгенерировать → записать → google_sync.

    Никогда не бросает исключений наружу (дайджест не должен ломать бота).
    Возвращает {"text": ..., "path": ..., "counts": {...}}.
    """
    data = gather_notes(db)
    counts = {k: len(v) for k, v in data.items()}

    digest = _generate_with_llm(data) or _heuristic_digest(data)
    path = append_digest(digest)

    # дублируем в Google Docs (best-effort, не блокируем)
    try:
        await google_sync.append_entry("digest", digest)
    except Exception as exc:  # noqa: BLE001
        log.warning("Google sync дайджеста не удался: %s", exc)

    log.info("Дайджест собран: user=%d assistant=%d mem=%d",
             counts.get("user", 0), counts.get("assistant", 0), counts.get("mem", 0))
    return {"text": digest, "path": str(path), "counts": counts}


# ------------------------------------------------------------- планировщик


def seconds_until(target: str) -> float:
    """Секунд до ближайшего наступления времени HH:MM (локальное время сервера)."""
    hh, mm = (int(x) for x in target.split(":", 1))
    now = datetime.now()
    target_dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    delta = (target_dt - now).total_seconds()
    if delta <= 0:  # уже прошло сегодня — переносим на завтра
        delta += 24 * 3600
    return delta


async def digest_loop(db: Any, time_str: str) -> None:
    """Фоновая задача: раз в сутки в указанное время собирает дайджест."""
    log.info("Ежедневный дайджест запланирован на %s (каждый день)", time_str)
    while True:
        delay = seconds_until(time_str)
        log.info("Следующий дайджест через %.0f мин (в %s)", delay / 60, time_str)
        await asyncio.sleep(delay)
        try:
            result = await build_and_append(db)
            log.info("Дайджест готов: %s", result["path"])
        except Exception as exc:  # noqa: BLE001
            log.exception("Ошибка генерации дайджеста: %s", exc)
        # небольшой сдвиг, чтобы не задваивать на стыке суток
        await asyncio.sleep(5)
