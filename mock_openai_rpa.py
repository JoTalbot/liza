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
import time
import uuid
from contextlib import asynccontextmanager
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
                        return _clean_answer(snap)
                else:
                    stable = 0
            last_snapshot = snap
            await asyncio.sleep(0.5)
        raise asyncio.TimeoutError

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
    # короткая директива перед каждым запросом: отвечать по-человечески,
    # без технических статусов/планов/перечислений (как в Telegram-боте Лизы)
    directive = (
        "Отвечай коротко и по-человечески, сразу по сути. Не начинай с технических "
        "статусов, планов действий, перечислений проектов/локаций/оборудования, "
        "не упоминай файлы, протоколы, L0-L3 и Wiki-документы. Одно-два предложения, "
        "если не просят развёрнуто.\n\n"
    )
    return directive + prompt


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

    try:
        assert bridge is not None
        content = await asyncio.wait_for(bridge.generate(prompt), timeout=GENERATION_TIMEOUT + 20)
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
