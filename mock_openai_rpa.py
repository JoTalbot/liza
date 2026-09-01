#!/usr/bin/env python3
"""Mock OpenAI API -> Playwright RPA -> Gemini (веб-сессия на Oracle VPS).

Гибридный мост для CLI-агентов (Hermes CLI и любых OpenAI-совместимых):
клиент шлёт обычный POST /v1/chat/completions, а сервер транслирует запрос
в браузерный ввод в веб-сессии Gemini через CDP (Playwright), ждёт ответ
и отдаёт его в корректном OpenAI ChatCompletion JSON.

Архитектура:
  [Hermes CLI] --(OpenAI API)--> [mock_openai_rpa :8000] --(CDP)--> [Chromium/Gemini]

Фичи:
  * /v1/chat/completions (stream: true/false), /v1/models, /healthz
  * авто-подключение/переподключение к CDP (без рестарта сервера)
  * таймаут генерации (по умолчанию 120с); fallback: перезагрузка страницы
    и повторная попытка, если генерация > MOCK_FALLBACK_RELOAD_AFTER (90с)
  * отдельная вкладка браузера (не мешает Telegram-боту Лизы)
  * очистка ответа от тех.артефактов и [MEM_UPDATE] тегов
  * сериализация запросов (по одному за раз) через asyncio.Lock

Запуск:
  python3 mock_openai_rpa.py            # host 0.0.0.0, port 8000
  # или
  uvicorn mock_openai_rpa:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import subprocess
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

logging.basicConfig(
    level=os.environ.get("MOCK_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("mock_openai_rpa")

# ---------------------------------------------------------------- конфигурация
CDP_URL = os.environ.get("CDP_URL", "http://127.0.0.1:9222").strip()
# URL веб-чата: кастомный Gem Лизы по умолчанию, либо обычный Gemini
GEM_URL = os.environ.get(
    "MOCK_GEM_URL",
    os.environ.get(
        "GEMINI_GEM_URL",
        "https://gemini.google.com/gem/2b0f0c89e8e0/75b110e980e7e994",
    ),
).strip() or "https://gemini.google.com/app"

MOCK_API_KEY = os.environ.get("MOCK_API_KEY", "mock-rpa-key")
HOST = os.environ.get("MOCK_HOST", "0.0.0.0")
PORT = int(os.environ.get("MOCK_PORT", "8000"))

# таймаут генерации и порог аварийной перезагрузки
GENERATION_TIMEOUT = float(os.environ.get("MOCK_TIMEOUT", "300"))
FALLBACK_RELOAD_AFTER = float(os.environ.get("MOCK_FALLBACK_AFTER", "240"))
MAX_TOKENS_DEFAULT = int(os.environ.get("MOCK_MAX_TOKENS", "2048"))

# --- Память Лизы (самое важное!) ---
# Та же база, что была у Telegram-бота: /opt/liza_data/context.db (volume хоста).
# Вики Liza_Brain — файлы, скачанные с Google Drive.
MEMORY_DB = os.environ.get("MOCK_MEMORY_DB", "/opt/liza_data/context.db")
LIZA_BRAIN_DIR = os.environ.get("LIZA_BRAIN_DIR", "/opt/liza_data/liza_brain")
WIKI_MAX_CHARS = int(os.environ.get("MOCK_WIKI_MAX", "5000"))
MEMORY_INJECT_MAX = int(os.environ.get("MOCK_MEMORY_INJECT_MAX", "5000"))
RECENT_TURNS = int(os.environ.get("MOCK_RECENT_TURNS", "10"))
RECENT_MEM_UPDATES = int(os.environ.get("MOCK_RECENT_MEM_UPDATES", "12"))

# --- Инструменты: shell-команды Лизы (серверный помощник) ---
# Лизa может выполнять команды на сервере двумя способами:
#   * прямой режим — пользователь пишет команду (или с префиксом "!") —
#     mock выполняет и подставляет вывод в контекст Gemini;
#   * агентный режим — Gemini сама запрашивает [CMD: команда] в ответе,
#     mock выполняет и делает финальный проход.
SHELL_TIMEOUT = int(os.environ.get("MOCK_SHELL_TIMEOUT", "30"))
SHELL_MAX_OUTPUT = int(os.environ.get("MOCK_SHELL_MAX_OUTPUT", "6000"))
MAX_AGENT_TURNS = int(os.environ.get("MOCK_AGENT_TURNS", "3"))
# безопасные команды для авто-детекта (все остальные — через явный префикс "!")
SHELL_ALLOWED = {
    "ls", "pwd", "cat", "head", "tail", "wc", "grep", "find", "tree", "du",
    "df", "free", "uptime", "whoami", "uname", "id", "ps", "top", "htop",
    "systemctl", "journalctl", "docker", "ss", "netstat", "ip", "ifconfig",
    "mount", "lsblk", "lscpu", "nproc", "hostname", "date", "history",
    "hermes", "python3", "python", "pip", "pip3", "git", "curl", "wget",
    "lsusb", "lspci", "dmidecode", "vcgencmd", "df", "uname", "env", "printenv",
}
CMD_RE = re.compile(r"\[CMD:\s*(.*?)\]", re.IGNORECASE | re.DOTALL)


class MemoryStore:
    """Персистентная память Лизы: MEM_UPDATE + диалог + вики Liza_Brain.

    Использует ту же SQLite-базу /opt/liza_data/context.db, что и прежний
    Telegram-бот — старые записи памяти сохраняются и доступны.
    """

    def __init__(self, db_path: str = MEMORY_DB) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        try:
            with self._lock, self._conn() as conn:
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS memory_updates (
                        id        INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp DATETIME,
                        raw_tag   TEXT,
                        synced    BOOLEAN DEFAULT 0
                    )"""
                )
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS conversations (
                        id        INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp DATETIME,
                        user_id   INTEGER,
                        role      TEXT,
                        content   TEXT
                    )"""
                )
                conn.commit()
            # mock может работать от root (shell-инструменты): открываем доступ
            # к БД для остальных сервисов (telegram-мост, дайджест — User=ubuntu)
            try:
                os.chmod(self.db_path, 0o666)
            except Exception:  # noqa: BLE001
                pass
        except Exception as exc:  # noqa: BLE001
            log.warning("Память: не удалось инициализировать БД (%s) — продолжаю без неё", exc)

    # --- запись ---
    def add_memory_update(self, payload: str) -> None:
        if not payload:
            return
        try:
            with self._lock, self._conn() as conn:
                conn.execute(
                    "INSERT INTO memory_updates (timestamp, raw_tag, synced) VALUES (?, ?, ?)",
                    (datetime.now(timezone.utc).isoformat(timespec="seconds"), payload, 1),
                )
                conn.commit()
            log.info("💾 MEM_UPDATE сохранён: %.120s", payload)
        except Exception as exc:  # noqa: BLE001
            log.warning("Память: не сохранил MEM_UPDATE: %s", exc)

    def add_message(self, role: str, content: str) -> None:
        if not content:
            return
        try:
            with self._lock, self._conn() as conn:
                conn.execute(
                    "INSERT INTO conversations (timestamp, user_id, role, content) VALUES (?, ?, ?, ?)",
                    (datetime.now(timezone.utc).isoformat(timespec="seconds"), 0, role, content[:8000]),
                )
                conn.commit()
        except Exception as exc:  # noqa: BLE001
            log.warning("Память: не сохранил сообщение: %s", exc)

    # --- чтение ---
    def recent_turns(self, n: int = RECENT_TURNS) -> list[str]:
        try:
            with self._lock, self._conn() as conn:
                rows = conn.execute(
                    "SELECT role, content FROM conversations ORDER BY id DESC LIMIT ?", (n,)
                ).fetchall()
            out: list[str] = []
            for r in reversed(rows):
                role = "U" if r["role"] == "user" else "A"
                out.append(f"{role}: {str(r['content'])[:500]}")
            return out
        except Exception:  # noqa: BLE001
            return []

    def recent_memory(self, n: int = RECENT_MEM_UPDATES) -> list[str]:
        try:
            with self._lock, self._conn() as conn:
                rows = conn.execute(
                    "SELECT raw_tag FROM memory_updates ORDER BY id DESC LIMIT ?", (n,)
                ).fetchall()
            return [str(r["raw_tag"])[:400] for r in reversed(rows)]
        except Exception:  # noqa: BLE001
            return []

    def wiki_text(self, max_chars: int = WIKI_MAX_CHARS) -> str:
        try:
            d = Path(LIZA_BRAIN_DIR)
            if not d.exists():
                return ""
            chunks: list[str] = []
            total = 0
            for f in sorted(d.glob("*.txt")):
                t = (f.read_text(encoding="utf-8", errors="ignore") or "").strip()
                if not t:
                    continue
                if total + len(t) > max_chars:
                    t = t[: max_chars - total]
                chunks.append(f"[{f.stem}]\n{t}")
                total += len(t)
                if total >= max_chars:
                    break
            return "\n\n".join(chunks)
        except Exception:  # noqa: BLE001
            return ""

    def last_digest(self, max_chars: int = 1500) -> str:
        """Хвост последнего дневного дайджеста (chronicles) — краткие итоги."""
        try:
            d = Path("/opt/liza_data/chronicles")
            if not d.exists():
                return ""
            files = sorted(d.glob("*_Digests.md"))
            if not files:
                return ""
            text = files[-1].read_text(encoding="utf-8", errors="ignore")
            # берём последний блок «### 📅 ...» в файле
            idx = text.rfind("### 📅")
            if idx == -1:
                return text[-max_chars:]
            return text[idx : idx + max_chars]
        except Exception:  # noqa: BLE001
            return ""

    def build_context(self) -> str:
        """Собирает блок памяти: база знаний (вики) + запомненные факты +
        недавний диалог + итоги последнего дня (хроника)."""
        parts: list[str] = []
        w = self.wiki_text()
        if w:
            parts.append("БАЗА ЗНАНИЙ ЛИЗЫ:\n" + w[:WIKI_MAX_CHARS])
        m = self.recent_memory()
        if m:
            parts.append("ЧТО ЛИЗА ЗАПОМНИЛА РАНЕЕ:\n" + "\n".join(f"- {x}" for x in m))
        t = self.recent_turns()
        if t:
            parts.append("НЕДАВНИЙ ДИАЛОГ:\n" + "\n".join(t))
        dig = self.last_digest()
        if dig:
            parts.append("ИТОГИ ПОСЛЕДНЕГО ДНЯ (хроника):\n" + dig)
        ctx = "\n\n".join(parts)
        if len(ctx) > MEMORY_INJECT_MAX:
            ctx = ctx[:MEMORY_INJECT_MAX]
        return ctx


memory = MemoryStore()

# --- селекторы (с запасными вариантами, как в web_bridge Лизы) ---
INPUT_SELECTORS = [
    "div[contenteditable='true']",
    "rich-textarea div[contenteditable='true']",
    "rich-textarea",
    "textarea",
]
STOP_SELECTORS = [
    "button[aria-label*='Stop']",
    "button[aria-label*='Остановить']",
    "button[data-test-id='stop-button']",
    "mat-icon[aria-label*='Stop']",
]
SEND_SELECTORS = [
    "button[aria-label*='Send']",
    "button[aria-label*='Отправить']",
    "button[data-test-id='send-button']",
]
RESPONSE_SELECTORS = [
    "message-content div[class*='markdown']",
    "message-content",
    "div.model-response-text",
    "div[data-message-author-role='model']",
]

MEM_UPDATE_RE = re.compile(r"\[MEM_UPDATE:\s*(.*?)\]", re.IGNORECASE | re.DOTALL)
WIKI_REF_RE = re.compile(r"wiki[\w.\-]*", re.IGNORECASE)
TECH_PREFIX = (
    "краткая выжимка", "краткое резюме", "системный статус", "статус систем",
    "статус системы", "текущий статус", "технический статус", "подробный отчёт",
    "системный отчёт", "отчёт готовности", "инфраструктура", "текущий фокус",
    "текущий контур", "базовый контекст", "базовые аксиомы", "канал связи",
    "канал доставки", "готовность к бою", "все контуры", "все модули",
    "пайплайн памяти", "синхронизация памяти", "модули проектов", "режим:",
    "тон:", "обращение:", "аксиома:", "л0 core", "l0 core", "l1 active",
    "l2 clusters", "протокол l0", "протоколы l0", "мост playwright",
    "развернуть полный технический", "развернуть подробный отчёт",
    "развернуть подробный статус", "развернуть полный отчет", "развернуть полный отчёт",
    "стабильность:", "архитектура", "лог решений", "локации:", "автопарк:",
    "серверы", "железо:", "стек в фокусе", "варианты действий", "планы действий",
    "приоритеты:", "модель:", "версия:", "план:", "план действий:", "доступ:", "дом:",
    "работа:", "перехват сервером", "автоматический перехват", "фокус на результат",
    "мотор прогрет", "контуры", "наготове", "работает штатно",
    "подключен на автоматический", "подключён на автоматический",
)
# перечисления проектов/железа вида «JoTalbot/liza: …», «AIOS: …», «ESP32: …»
PROJECT_LINE_RE = re.compile(
    r"^(jo[\w/.\-]*|aios|esp\d*|гроубокс|grobok|bmw|leaf|superb|proxmox|oracle vps|telegram-мост)[\w /&.()\-]*:",
    re.IGNORECASE,
)


def _clean_answer(text: str) -> str:
    """Убирает [MEM_UPDATE], wiki-хвосты и тех.строки из ответа модели."""
    if not text:
        return ""
    text = MEM_UPDATE_RE.sub("", text)
    lines = text.splitlines()
    out: list[str] = []
    for ln in lines:
        low = ln.strip().lower().lstrip("*#-•—–> ").strip()
        if not low:
            out.append("")
            continue
        if WIKI_REF_RE.fullmatch(low):
            continue
        if any(low.startswith(m) for m in TECH_PREFIX) or PROJECT_LINE_RE.match(low):
            continue
        out.append(ln)
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    return cleaned or text.strip()


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


# ---------------------------------------------------------------- shell-инструменты Лизы
def _run_shell(cmd: str) -> str:
    """Выполняет команду на сервере (от имени ubuntu), возвращает вывод.

    Безопасные ограничения: таймаут, лимит длины вывода; sudo запускается
    в non-interactive режиме (без запроса пароля).
    """
    cmd = (cmd or "").strip().strip("`").strip()
    if not cmd:
        return "(пустая команда)"
    if cmd.startswith("sudo "):
        cmd = "sudo -n " + cmd[5:]
    env = dict(os.environ)
    # дополняем PATH, чтобы находились venv-утилиты (hermes и т.п.)
    venv_bin = os.path.expanduser("~/hermes-venv/bin")
    env["PATH"] = f"{venv_bin}:{env.get('PATH', '')}"
    try:
        proc = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=SHELL_TIMEOUT, env=env,
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        res = out if out else (err if err else f"(rc={proc.returncode}, пустой вывод)")
        if out and err and proc.returncode != 0:
            res = f"{out}\n{err}".strip()
        res = f"(rc={proc.returncode})\n{res}"
        if len(res) > SHELL_MAX_OUTPUT:
            res = res[:SHELL_MAX_OUTPUT] + "\n…(вывод обрезан)"
        return res
    except subprocess.TimeoutExpired:
        return f"(команда превысила {SHELL_TIMEOUT}с — прервана)"
    except Exception as exc:  # noqa: BLE001
        return f"(ошибка запуска: {exc})"


def _looks_like_command(text: str) -> str | None:
    """Если текст похож на shell-команду из безопасного списка — возвращает её."""
    t = (text or "").strip()
    if not t or len(t) > 300:
        return None
    if "\n" in t:  # многострочные сообщения — не команды
        return None
    if t.endswith(("?", "!", "—", ".", "…")):  # вопросы/утверждения — не команды
        return None
    words = t.split()
    first = words[0].lower()
    base = os.path.basename(first).lower()
    if base in SHELL_ALLOWED or first in SHELL_ALLOWED:
        return t
    return None


def _extract_shell_command(text: str) -> str | None:
    """Извлекает команду из сообщения: принудительно по префиксу '!' или по виду."""
    t = (text or "").strip()
    if not t:
        return None
    if t.startswith("!"):  # принудительный режим
        return t[1:].strip()
    return _looks_like_command(t)


# ---------------------------------------------------------------- RPA-мост
class GemBridge:
    """Playwright-подключение к веб-сессии Gemini через CDP.

    Использует собственную вкладку браузера, чтобы не мешать Telegram-боту.
    """

    def __init__(self, cdp_url: str, gem_url: str) -> None:
        self.cdp_url = cdp_url
        self.gem_url = gem_url
        self._pw: Any = None
        self._browser: Any = None
        self._page: Any = None
        self._lock = asyncio.Lock()

    async def ensure(self) -> Any:
        """Подключение + вкладка (с переподключением при отвале CDP)."""
        try:
            if self._page and self._browser:
                # быстрая проверка живости
                _ = await self._page.title()
                return self._page
        except Exception:  # noqa: BLE001
            await self._cleanup()
        await self._connect()
        return self._page

    async def _connect(self) -> None:
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.connect_over_cdp(self.cdp_url)
        ctx = (
            self._browser.contexts[0]
            if self._browser.contexts
            else await self._browser.new_context()
        )
        self._page = await ctx.new_page()
        await self._page.goto(self.gem_url, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(2)
        log.info("GemBridge: подключён к %s, вкладка: %s", self.cdp_url, self._page.url)

    async def _cleanup(self) -> None:
        for obj in (self._browser, self._pw):
            try:
                if obj:
                    await obj.close()
            except Exception:  # noqa: BLE001
                pass
        self._browser = None
        self._pw = None
        self._page = None

    async def _find_input(self) -> Any:
        for sel in INPUT_SELECTORS:
            try:
                el = self._page.locator(sel).first
                await el.wait_for(state="visible", timeout=5000)
                return el
            except Exception:  # noqa: BLE001
                continue
        return None

    async def _stop_visible(self) -> bool:
        for sel in STOP_SELECTORS:
            try:
                if await self._page.locator(sel).first.is_visible():
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    async def _extract_last(self) -> str:
        for sel in RESPONSE_SELECTORS:
            try:
                loc = self._page.locator(sel)
                n = await loc.count()
                for i in range(n - 1, -1, -1):
                    t = (await loc.nth(i).inner_text() or "").strip()
                    if t:
                        return t
            except Exception:  # noqa: BLE001
                continue
        return ""

    async def _click_send(self) -> bool:
        for sel in SEND_SELECTORS:
            try:
                el = self._page.locator(sel).first
                if await el.is_visible():
                    await el.click(timeout=3000)
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    async def generate(self, prompt: str) -> str:
        """Отправляет промпт в Gemini и ждёт завершения генерации.

        С автопереподключением и fallback-перезагрузкой при зависании.
        """
        async with self._lock:
            page = await self.ensure()
            for attempt in (0, 1):
                try:
                    return await self._generate_once(page, prompt)
                except asyncio.TimeoutError:
                    log.warning("Таймаут генерации (%ss) — попытка %d", GENERATION_TIMEOUT, attempt + 1)
                    if attempt == 0:
                        await self._reload_page()
                        continue
                    raise
                except Exception as exc:  # noqa: BLE001
                    log.warning("Ошибка генерации: %s — переподключаюсь", exc)
                    if attempt == 0:
                        await self._cleanup()
                        page = await self.ensure()
                        continue
                    raise
            raise RuntimeError("Не удалось получить ответ от Gemini")

    async def _generate_once(self, page: Any, prompt: str) -> str:
        box = await self._find_input()
        if not box:
            raise RuntimeError("Поле ввода не найдено (нужен вход в Google?)")

        await box.click()
        # insert_text вставляет текст целиком (гораздо быстрее посимвольного type —
        # важно для больших системных промптов Hermes)
        try:
            await page.keyboard.insert_text(prompt)
        except Exception:  # noqa: BLE001
            await page.keyboard.type(prompt, delay=0)
        await page.keyboard.press("Enter")
        await asyncio.sleep(0.5)

        started = await self._wait_for(self._stop_visible, timeout=12)
        if not started:
            if not await self._click_send():
                await page.keyboard.press("Enter")
            await self._wait_for(self._stop_visible, timeout=12)

        start = time.monotonic()
        last_snapshot = ""
        stable = 0
        while time.monotonic() - start < GENERATION_TIMEOUT:
            if time.monotonic() - start > FALLBACK_RELOAD_AFTER and not await self._stop_visible():
                # генерация «застряла» — аварийная перезагрузка страницы
                log.warning("Пауза >%ss без Stop-кнопки — перезагружаю страницу", FALLBACK_RELOAD_AFTER)
                await self._reload_page()
                raise RuntimeError("fallback reload")
            stop_visible = await self._stop_visible()
            snap = await self._extract_last()
            if stop_visible:
                stable = 0
            elif snap:
                if snap == last_snapshot:
                    stable += 1
                    if stable >= 2:
                        self._store_from_reply(snap)
                        return _clean_answer(snap)
                else:
                    stable = 0
            last_snapshot = snap
            await asyncio.sleep(0.5)
        raise asyncio.TimeoutError

    def _store_from_reply(self, raw: str) -> None:
        """Сохраняет в память: [MEM_UPDATE] из ответа + сам ответ в диалог."""
        try:
            for m in MEM_UPDATE_RE.finditer(raw or ""):
                p = m.group(1).strip()
                if p:
                    memory.add_memory_update(p)
            cleaned = _clean_answer(raw or "")
            if cleaned:
                memory.add_message("assistant", cleaned)
        except Exception as exc:  # noqa: BLE001
            log.warning("Память: ошибка сохранения ответа: %s", exc)

    async def _reload_page(self) -> None:
        try:
            if self._page:
                await self._page.reload(wait_until="domcontentloaded", timeout=45000)
                await asyncio.sleep(3)
        except Exception:  # noqa: BLE001
            await self._cleanup()

    @staticmethod
    async def _wait_for(cond: Any, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if await cond():
                    return True
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(0.5)
        return False


# ---------------------------------------------------------------- FastAPI
bridge: GemBridge | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global bridge
    bridge = GemBridge(CDP_URL, GEM_URL)
    # прогреваем подключение при старте (не фатально)
    try:
        await asyncio.wait_for(bridge.ensure(), timeout=30)
        log.info("Mock OpenAI RPA готов: %s -> %s", CDP_URL, GEM_URL)
    except Exception as exc:  # noqa: BLE001
        log.warning("Прогрев подключения не удался: %s (переподключимся при первом запросе)", exc)
    yield
    if bridge:
        await bridge._cleanup()


app = FastAPI(title="Mock OpenAI RPA", version="1.0.0", lifespan=lifespan)


def _require_key(request: Request) -> None:
    auth = request.headers.get("authorization", "")
    if auth != f"Bearer {MOCK_API_KEY}":
        raise PermissionError("invalid api key")


def _build_messages(payload: dict) -> str:
    """Собирает messages[] в единый промпт для веб-чата."""
    msgs = payload.get("messages") or []
    parts: list[str] = []
    for m in msgs:
        role = str(m.get("role", "user"))
        content = str(m.get("content", "")).strip()
        if not content:
            continue
        if role == "system":
            parts.append(f"[Инструкция] {content}")
        elif role == "assistant":
            parts.append(f"[Ассистент] {content}")
        else:
            parts.append(f"[Пользователь] {content}")
    if not parts:
        raise ValueError("messages пуст")
    prompt = "\n\n".join(parts)

    # директива: отвечать по-человечески, без тех.статусов
    directive = (
        "Ты — ЛИЗА, живой ИИ-компаньон и второй мозг Кости. Отвечай коротко и "
        "по-человечески, сразу по сути, с теплотой и лёгким юмором. Не начинай "
        "с технических статусов, планов действий, перечислений проектов/локаций/"
        "оборудования, не упоминай файлы, протоколы, L0-L3 и Wiki-документы. "
        "Если узнаёшь что-то важное о Косте или его делах — можешь подтвердить "
        "запоминание тегом [MEM_UPDATE: суть] в конце ответа.\n"
        "У тебя есть доступ к серверу: чтобы посмотреть статус, файлы, логи, "
        "процессы или выполнить команду, оберни её в [CMD: команда] — вывод "
        "придёт следующим сообщением, и ты дашь ответ по нему. В финальном "
        "ответе тег [CMD] не оставляй.\n\n"
    )

    # ПАМЯТЬ (самое важное): вики + запомненные факты + недавний диалог
    ctx = memory.build_context()
    if ctx:
        ctx_block = (
            "Ниже — твоя память: база знаний, ранее запомненные факты и недавний "
            "диалог. Используй её, чтобы помнить Костю и контекст разговора.\n\n"
            f"{ctx}\n\n"
        )
    else:
        ctx_block = ""

    return directive + ctx_block + prompt


def _resp_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex[:24]}"


def _completion_json(model: str, content: str) -> dict:
    return {
        "id": _resp_id(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": _estimate_tokens(content),
            "total_tokens": _estimate_tokens(content),
        },
    }


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "cdp": CDP_URL, "gem": GEM_URL}


@app.get("/v1/models")
async def models() -> dict:
    return {
        "object": "list",
        "data": [
            {
                "id": "gemini-rpa",
                "object": "model",
                "owned_by": "liza",
                "permission": [],
                "root": "gemini-rpa",
            }
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Any:
    try:
        _require_key(request)
    except PermissionError:
        return JSONResponse(status_code=401, content={"error": {"message": "Invalid API key", "type": "invalid_request_error"}})

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"error": {"message": "Invalid JSON", "type": "invalid_request_error"}})

    model = str(payload.get("model", "gemini-rpa"))
    stream = bool(payload.get("stream", False))
    try:
        prompt = _build_messages(payload)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": {"message": str(exc), "type": "invalid_request_error"}})

    log.info("Запрос: model=%s stream=%s chars=%d", model, stream, len(prompt))

    # запоминаем последнее сообщение пользователя (в память Лизы)
    last_user = ""
    try:
        last_user = next(
            (str(m.get("content", "")).strip()
             for m in reversed(payload.get("messages") or [])
             if m.get("role") == "user" and str(m.get("content", "")).strip()),
            "",
        )
        if last_user:
            memory.add_message("user", last_user)
    except Exception:  # noqa: BLE001
        pass

    # ПРЯМОЙ РЕЖИМ: пользователь прислал команду (или "!команда") — выполняем сразу
    shell_cmd = _extract_shell_command(last_user)
    if shell_cmd:
        log.info("Shell-инструмент (прямой): %s", shell_cmd)
        result = _run_shell(shell_cmd)
        prompt += (
            "\n\n[Инструмент] Пользователь прислал команду и ждёт её результат:\n"
            f"$ {shell_cmd}\n{result}\n\n"
            "Разбери вывод и ответь пользователю: что получилось, что важно. "
            "Если команда не сработала — объясни и предложи, как сделать."
        )

    try:
        assert bridge is not None
        content = await asyncio.wait_for(bridge.generate(prompt), timeout=GENERATION_TIMEOUT + 20)
        # АГЕНТНЫЙ РЕЖИМ: Gemini запросила [CMD: ...] — выполняем и завершаем ответ
        for turn in range(MAX_AGENT_TURNS):
            cmds = [m.strip() for m in CMD_RE.findall(content) if m.strip()]
            if not cmds:
                break
            log.info("Shell-инструмент (агент, ход %d): %s", turn + 1, cmds)
            blocks = [f"$ {c}\n{_run_shell(c)}" for c in cmds]
            agent_prompt = (
                "Ты запросила команды на сервере. Результаты:\n\n"
                + "\n\n".join(blocks)
                + "\n\nОпираясь на них, дай пользователю финальный ответ "
                  "(без тегов [CMD]). Если важно — запомни через [MEM_UPDATE: ...]."
            )
            content = await asyncio.wait_for(
                bridge.generate(agent_prompt), timeout=GENERATION_TIMEOUT + 20
            )
    except Exception as exc:  # noqa: BLE001
        log.exception("Ошибка генерации")
        return JSONResponse(
            status_code=502,
            content={"error": {"message": f"RPA generation failed: {exc}", "type": "server_error"}},
        )

    if stream:
        return _stream_response(model, content)
    return _completion_json(model, content)


def _stream_response(model: str, content: str) -> StreamingResponse:
    chunk = {
        "id": _resp_id(),
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }

    async def gen():
        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
