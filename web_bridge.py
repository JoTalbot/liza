"""Web Bridge — выделенный чат Gemini (Advanced + Extended Thinking) через Playwright CDP.

Подключается к Chromium по CDP (browser:9222), ведёт персистентный чат,
URL которого хранится в /data/chat_session.json. При каждом сообщении бот
возвращается в этот же чат, сохраняя непрерывную изолированную историю.

Если Google-аккаунт не залогинен — пишет warning и НЕ падает (бот переходит
на Groq-fallback). Автоматизация селекторов Gemini — best-effort: если интерфейс
изменился, детали логируются, а чат можно настроить вручную через noVNC.
"""
import asyncio
import json
import logging
import socket
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

import config

log = logging.getLogger(__name__)

CHAT_SESSION_FILE = Path(config.DATA_DIR) / "chat_session.json"
GEMINI_APP_URL = "https://gemini.google.com/app"

# --- селекторы (с запасными вариантами) ---
INPUT_SELECTORS = [
    "div[contenteditable='true']",
    "rich-textarea div[contenteditable='true']",
    "rich-textarea",
    "textarea",
]

SEND_BUTTON_SELECTORS = [
    "button[aria-label*='Send']",
    "button[aria-label*='Отправить']",
    "button[data-test-id='send-button']",
    "button[aria-label='Send message']",
]

STOP_SELECTORS = [
    "button[aria-label*='Stop']",
    "button[aria-label*='Остановить']",
    "button[aria-label*='stop generating']",
    "button[data-test-id='stop-button']",
    "mat-icon[aria-label*='Stop']",
]

NEW_CHAT_SELECTORS = [
    "button[aria-label*='New chat']",
    "button[aria-label*='Новый чат']",
    "button[aria-label='Create a new chat']",
    "a[aria-label*='New chat']",
    "mat-icon[aria-label*='New chat']",
    "nav button[aria-label*='chat']",
    "button[data-test-id='new-chat-button']",
]

MODEL_PICKER_SELECTORS = [
    "button[aria-label*='model']",
    "button[aria-label*='Модель']",
    "button[aria-label*='Gemini']",
    "button[data-test-id='model-selector']",
]

MODEL_OPTION_SELECTORS = [
    "div[role='option']",
    "button[role='option']",
    "mat-option",
    "li[role='option']",
]

THINKING_TOGGLE_SELECTORS = [
    "button[aria-label*='Extended thinking']",
    "button[aria-label*='Deep think']",
    "button[aria-label*='Думать']",
    "button[aria-label*='Продолжительное мышление']",
    "button[aria-label*='Расширенное мышление']",
]

RESPONSE_SELECTORS = [
    "message-content div[class*='markdown']",
    "message-content",
    "div.model-response-text",
    "div[data-message-author-role='model']",
    "div[class*='model-response']",
    "div[class*='response-content']",
]

ADVANCED_KEYWORDS = [
    "advanced", "pro", "plus", "thinking", "think", "extended", "deep",
    "продвинут", "мышлен", "думать", "расшир", "продолж",
]
BEST_KEYWORDS = [
    "extended", "thinking", "deep", "мышлен", "продолж", "расшир",
]


class WebBridge:
    def __init__(self, cdp_url: str):
        self.cdp_url = cdp_url
        self._pw = None
        self._browser = None
        self._page = None
        self._connected = False
        self._lock = asyncio.Lock()
        self._last_prompt = ""
        self.chat_url = ""
        self.current_model = ""

    # ---------- подключение ----------
    async def connect(self) -> None:
        if self._connected and self._page:
            return
        self._pw = await async_playwright().start()
        endpoint = await self._resolve_endpoint(self.cdp_url)
        self._browser = await self._pw.chromium.connect_over_cdp(endpoint)
        ctx = self._browser.contexts[0] if self._browser.contexts else await self._browser.new_context()
        self._page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        self._connected = True
        log.info("Подключено к Chromium CDP: %s (endpoint=%s)", self.cdp_url, endpoint)

    async def _resolve_endpoint(self, url: str) -> str:
        """Chromium 151+ отклоняет CDP HTTP-запросы, где Host != IP/localhost
        (защита от DNS-rebinding). Поэтому hostname резолвится в IP, а
        WebSocket-адрес браузера берётся из /json/version и переписывается
        на рабочий IP:port (socat-проброс 9222 -> 127.0.0.1:9223)."""
        if url.startswith("ws://") or url.startswith("wss://"):
            return url
        p = urlparse(url)
        host = p.hostname
        port = p.port or 9222
        ip = host
        if host and host not in ("localhost", "127.0.0.1", "::1"):
            try:
                ip = socket.gethostbyname(host)
            except Exception:  # noqa: BLE001
                ip = host
        try:
            req = urllib.request.Request(
                f"http://{ip}:{port}/json/version",
                headers={"Host": ip},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                info = json.loads(resp.read().decode("utf-8", "replace"))
            raw_ws = (info.get("webSocketDebuggerUrl") or "").strip()
            if raw_ws:
                wp = urlparse(raw_ws)
                ws = f"ws://{ip}:{port}{wp.path}"
                log.info("CDP endpoint через /json/version: %s", ws)
                return ws
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось получить /json/version (%s) — fallback на http://%s:%s", exc, ip, port)
        return f"http://{ip}:{port}"

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
        self._connected = False

    async def ensure_ready(self, retries: int = 4, delay: float = 5.0) -> None:
        last = None
        for i in range(retries):
            try:
                await self.connect()
                return
            except Exception as exc:  # noqa: BLE001
                last = exc
                log.warning("CDP недоступен (попытка %d/%d): %s", i + 1, retries, exc)
                await self._cleanup()
                await asyncio.sleep(delay)
        raise RuntimeError(f"Нет связи с Chromium CDP ({self.cdp_url}): {last}")

    # ---------- персистентная сессия чата ----------
    def _load_session(self) -> dict:
        try:
            with open(CHAT_SESSION_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            return {}

    def _save_session(self, url: str = "", model: str = "") -> None:
        try:
            CHAT_SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
            data = self._load_session()
            if url:
                data["url"] = url
            if model:
                data["model"] = model
            data["saved_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            tmp = CHAT_SESSION_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(CHAT_SESSION_FILE)
            log.info("Сохранён chat_session.json (url=%s)", url or "без изменений")
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось сохранить chat_session.json: %s", exc)

    def _is_login_page(self) -> bool:
        u = self._page.url.lower()
        return "accounts.google.com" in u or "servicelogin" in u or "signin" in u

    # ---------- инициализация выделенного чата ----------
    async def ensure_dedicated_chat(self) -> bool:
        """Открывает сохранённый чат или создаёт новый (Advanced + Extended Thinking)."""
        saved = self._load_session()
        saved_url = (saved.get("url") or "").strip()
        if saved_url and "gemini.google.com" in saved_url:
            try:
                await self._page.goto(saved_url, wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(3)
                if self._is_login_page():
                    log.warning("Google требует вход — откройте noVNC (http://<IP>:6080/vnc.html) и войдите в аккаунт")
                    return False
                self.chat_url = self._page.url
                log.info("Вернулись в выделенный чат: %s", self.chat_url)
                return True
            except Exception as exc:  # noqa: BLE001
                log.warning("Не удалось открыть сохранённый чат (%s) — создам новый", exc)

        try:
            await self._page.goto(GEMINI_APP_URL, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(4)
            if self._is_login_page():
                log.warning("Требуется вход в Google: http://<IP>:6080/vnc.html — войдите в аккаунт, затем напишите боту снова")
                return False

            await self._try_click_new_chat()
            await asyncio.sleep(2)
            await self._try_enable_extended_thinking()
            await asyncio.sleep(1)

            self.chat_url = self._page.url
            self._save_session(self.chat_url, self.current_model)
            log.info("Выделенный чат готов: %s (model=%s)", self.chat_url, self.current_model or "не определена")
            return True
        except Exception as exc:  # noqa: BLE001
            log.exception("ensure_dedicated_chat не удался: %s", exc)
            return False

    async def _try_click_new_chat(self) -> bool:
        for sel in NEW_CHAT_SELECTORS:
            try:
                el = self._page.locator(sel).first
                if await el.is_visible():
                    await el.click(timeout=4000)
                    log.info("Нажата кнопка «Новый чат» (%s)", sel)
                    await asyncio.sleep(2)
                    return True
            except Exception:  # noqa: BLE001
                continue
        log.warning("Кнопка «Новый чат» не найдена — работаю с текущим чатом")
        return False

    async def _try_enable_extended_thinking(self) -> None:
        """Best-effort: включает Extended/Deep Thinking и выбирает продвинутую модель."""
        # 1) Переключатель мышления прямо в композере
        for sel in THINKING_TOGGLE_SELECTORS:
            try:
                el = self._page.locator(sel).first
                if await el.is_visible():
                    await el.click(timeout=3000)
                    self.current_model = "Extended Thinking (toggle)"
                    log.info("Включён режим Extended/Deep Thinking (%s)", sel)
                    return
            except Exception:  # noqa: BLE001
                continue

        # 2) Через выбор модели
        try:
            await self._open_model_picker()
            await asyncio.sleep(1.5)
            best = await self._find_model_option()
            if best:
                label = (await best.inner_text()).strip().replace("\n", " ")
                await best.click(timeout=4000)
                self.current_model = label[:80]
                log.info("Выбрана модель: %s", self.current_model)
                await asyncio.sleep(1)
                for sel in THINKING_TOGGLE_SELECTORS:
                    try:
                        el = self._page.locator(sel).first
                        if await el.is_visible():
                            await el.click(timeout=3000)
                            log.info("Дополнительно включён Extended Thinking")
                            break
                    except Exception:  # noqa: BLE001
                        continue
            await self._page.keyboard.press("Escape")
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось автоматически выбрать модель: %s", exc)
        log.info("Выбор модели завершён (если не удалось — настройте вручную в noVNC)")

    async def _open_model_picker(self) -> None:
        for sel in MODEL_PICKER_SELECTORS:
            try:
                el = self._page.locator(sel).first
                if await el.is_visible():
                    await el.click(timeout=4000)
                    log.info("Открыт выбор модели (%s)", sel)
                    return
            except Exception:  # noqa: BLE001
                continue
        raise RuntimeError("Не найден селектор выбора модели")

    async def _find_model_option(self):
        best_opt = None
        best_score = 0
        for sel in MODEL_OPTION_SELECTORS:
            try:
                loc = self._page.locator(sel)
                n = await loc.count()
                for i in range(n):
                    try:
                        txt = (await loc.nth(i).inner_text() or "").lower()
                    except Exception:  # noqa: BLE001
                        continue
                    score = 0
                    if any(k in txt for k in BEST_KEYWORDS):
                        score += 3
                    if any(k in txt for k in ADVANCED_KEYWORDS):
                        score += 1
                    if score > best_score:
                        best_score = score
                        best_opt = loc.nth(i)
            except Exception:  # noqa: BLE001
                continue
        if best_opt:
            log.info("Лучший вариант модели (score=%d)", best_score)
        return best_opt

    # ---------- отправка промпта и извлечение ответа ----------
    async def send_prompt_to_chat(self, text: str) -> str:
        async with self._lock:
            await self.ensure_ready()
            if not await self.ensure_dedicated_chat():
                raise RuntimeError("Нет доступа к Gemini (требуется вход в Google через noVNC)")

            text = (text or "").strip()
            if not text:
                raise RuntimeError("Пустой промпт")
            self._last_prompt = text

            box = await self._find_input()
            if not box:
                raise RuntimeError("Не найдено поле ввода Gemini (страница входа?)")

            await box.click()
            await self._page.keyboard.type(text, delay=1)
            await self._page.keyboard.press("Enter")

            started = await self._wait_stop_button(timeout=10)
            if not started:
                clicked = await self._click_send_button()
                if not clicked:
                    await self._page.keyboard.press("Enter")
                await self._wait_stop_button(timeout=10)

            await self._wait_generation_done(timeout=config.GEMINI_RESPONSE_TIMEOUT)
            await asyncio.sleep(1)
            reply = await self._extract_last_response()
            if not reply:
                raise RuntimeError("Пустой ответ от Gemini")

            # сохраняем актуальный URL чата (появляется id после первого сообщения)
            try:
                url = self._page.url
                if "gemini.google.com" in url and url != GEMINI_APP_URL:
                    self.chat_url = url
                    self._save_session(url, self.current_model)
            except Exception:  # noqa: BLE001
                pass
            return reply

    async def _find_input(self):
        for sel in INPUT_SELECTORS:
            try:
                el = self._page.locator(sel).first
                await el.wait_for(state="visible", timeout=4000)
                return el
            except Exception:  # noqa: BLE001
                continue
        return None

    async def _click_send_button(self) -> bool:
        for sel in SEND_BUTTON_SELECTORS:
            try:
                el = self._page.locator(sel).first
                if await el.is_visible():
                    await el.click(timeout=3000)
                    log.info("Нажата кнопка отправки (%s)", sel)
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    async def _any_visible(self, selectors: list[str]) -> bool:
        for sel in selectors:
            try:
                if await self._page.locator(sel).first.is_visible():
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    async def _wait_stop_button(self, timeout: float) -> bool:
        """Ждёт появления кнопки Stop (генерация началась)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if await self._any_visible(STOP_SELECTORS):
                return True
            await asyncio.sleep(0.5)
        return False

    async def _wait_generation_done(self, timeout: float) -> None:
        """Ждёт завершения генерации: исчез Stop И/ИЛИ стабилизация ответа."""
        last_snapshot = ""
        stable_for = 0
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            stop_visible = await self._any_visible(STOP_SELECTORS)
            snapshot = await self._last_response_text()
            if stop_visible:
                stable_for = 0
            elif snapshot:
                if snapshot == last_snapshot:
                    stable_for += 1
                    if stable_for >= 2:
                        log.info("Ответ стабилен (~%.0f с)", time.monotonic() - start)
                        return
                else:
                    stable_for = 0
            last_snapshot = snapshot
            await asyncio.sleep(1)
        log.warning("Таймаут ожидания генерации (%s с)", timeout)

    async def _last_response_text(self) -> str:
        try:
            return await self._extract_last_response()
        except Exception:  # noqa: BLE001
            return ""

    async def _extract_last_response(self) -> str:
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
        # фолбэк: текст body после последнего вхождения промпта
        try:
            body = await self._page.locator("body").inner_text()
            if self._last_prompt:
                idx = body.rfind(self._last_prompt)
                if idx != -1:
                    return body[idx + len(self._last_prompt):].strip()[:4000]
            return body[-4000:].strip()
        except Exception:  # noqa: BLE001
            return ""
