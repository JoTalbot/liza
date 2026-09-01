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
import re
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

# Стартовый промпт авто-инициализации (активация персоны Лизы + Google Drive)
STARTER_PROMPT = (
    '@Google Drive найди и прочитай файл "LizaBrain". '
    "Ты — Лиза, мой автономный ассистент и второй мозг. "
    "Загрузи личность и память. "
    "Правило: если появляется важная новая информация для сохранения в память, "
    "добавь в конце ответа тег [MEM_UPDATE: суть обновления]. "
    "Подтверди готовность!"
)

# Тег памяти: [MEM_UPDATE: суть обновления] в конце ответа модели
MEM_UPDATE_RE = re.compile(r"\[MEM_UPDATE:\s*(.*?)\]", re.IGNORECASE | re.DOTALL)

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

MODEL_LABEL_SELECTORS = [
    "button[aria-label*='model' i]",
    "button[aria-label*='Model' i]",
    "div[aria-label*='model' i]",
    "[data-test-id='model-selector']",
    "button[data-test-id='model-picker-button']",
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

# Слова-маркеры интерфейса Gemini (левое меню и т.п.) — если извлечённый
# «ответ» состоит из них, это ещё не ответ модели, а мусор DOM.
INTERFACE_NOISE_KEYWORDS = [
    "новый чат", "поиск по чатам", "начать чат", "видео", "библиотека",
    "gem-боты", "блокноты", "гаражи", "недавние", "настроить",
    "чат с gemini", "обзор", "создано с", "подтвердить", "продолжить",
]


def _looks_like_interface(text: str) -> bool:
    """True, если текст больше похож на интерфейс Gemini, чем на ответ модели."""
    if not text:
        return True
    low = text.lower()
    hits = sum(1 for k in INTERFACE_NOISE_KEYWORDS if k in low)
    return hits >= 2 and len(text) < 600

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
        self.memory_updates: list[str] = []   # извлечённые [MEM_UPDATE: ...] последнего ответа
        self._init_confirmation = ""          # текст подтверждения после стартового промпта
        self.stage_callback = None            # опц.: вызывается на этапах sending/waiting/done

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

    async def _has_input_fast(self) -> bool:
        """Быстрая проверка: есть ли поле ввода промпта (без долгих ожиданий)."""
        for sel in INPUT_SELECTORS:
            try:
                if await self._page.locator(sel).first.count():
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    async def has_google_session(self) -> bool:
        """True, если в браузере есть сессионные куки Google (SID/SAPISID/1PSID)."""
        try:
            cookies = await self._page.context.cookies()
            names = {c["name"] for c in cookies if "google" in c["domain"]}
            return bool(names & {"SID", "__Secure-1PSID", "SAPISID", "__Secure-3PSID"})
        except Exception:  # noqa: BLE001
            return False

    async def requires_login(self) -> bool:
        """True, если нужен вход в Google: страница входа ИЛИ промо-обложка
        «Meet Gemini / Sign in» без поля ввода."""
        try:
            if self._is_login_page():
                return True
            if await self._has_input_fast():
                return False
            body = ((await self._page.locator("body").inner_text()) or "").lower()
            if "sign in" in body or "meet gemini" in body:
                return True
        except Exception as exc:  # noqa: BLE001
            log.debug("requires_login: %s", exc)
        return False

    # ---------- инициализация выделенного чата ----------
    async def ensure_dedicated_chat(self) -> bool:
        """Открывает сохранённый чат или создаёт новый (Advanced + Extended Thinking)."""
        saved = self._load_session()
        saved_url = (saved.get("url") or "").strip()
        if saved_url and "gemini.google.com" in saved_url:
            try:
                await self._page.goto(saved_url, wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(3)
                if await self.requires_login():
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
            if await self.requires_login():
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

    # ---------- смена чата + авто-инициализация ----------
    async def new_dedicated_chat(self) -> str:
        """Создаёт НОВЫЙ выделенный чат Gemini с полной авто-инициализацией."""
        return await self.init_dedicated_chat()

    async def init_dedicated_chat(self) -> str:
        """Авто-инициализация выделенного чата:

        1) создаёт новый чат Gemini;
        2) выбирает самую продвинутую модель и включает Extended Thinking;
        3) отправляет стартовый промпт (персона Лизы + @Google Drive LizaBrain);
        4) ждёт подтверждение модели;
        5) сохраняет URL чата в /data/chat_session.json.
        """
        async with self._lock:
            await self.ensure_ready()
            try:
                await self._page.goto(GEMINI_APP_URL, wait_until="domcontentloaded", timeout=60000)
            except Exception as exc:  # noqa: BLE001
                log.warning("goto %s: %s", GEMINI_APP_URL, exc)
            await asyncio.sleep(3)
            if await self.requires_login():
                raise RuntimeError("Требуется вход в Google (noVNC)")

            await self._try_click_new_chat()
            await asyncio.sleep(2)
            await self._try_enable_extended_thinking()
            await asyncio.sleep(1)

            # стартовый промпт активации персоны + привязки Google Drive
            try:
                confirmation = await self._submit_and_extract(STARTER_PROMPT)
                self._init_confirmation = confirmation
                log.info(
                    "Инициализация Лизы: подтверждение получено (%d симв.): %.120s",
                    len(confirmation), confirmation,
                )
            except Exception as exc:  # noqa: BLE001
                self._init_confirmation = ""
                log.warning("Стартовый промпт Лизы не удался: %s", exc)

            url = self._page.url or GEMINI_APP_URL
            self.chat_url = url
            self._save_session(url, self.current_model)
            log.info("Выделенный чат инициализирован: %s (model=%s)", url, self.current_model or "?")
            return url

    # ---------- MEM_UPDATE парсинг ----------
    def _parse_memory_updates(self, reply: str) -> tuple[str, list[str]]:
        """Извлекает [MEM_UPDATE: ...] теги из ответа.

        Возвращает (очищенный ответ, список обновлений памяти).
        Тег удаляется из финального текста, который уходит пользователю.
        """
        updates = [m.group(1).strip() for m in MEM_UPDATE_RE.finditer(reply) if m.group(1).strip()]
        cleaned = MEM_UPDATE_RE.sub("", reply)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
        return cleaned, updates

    # ---------- статус модели ----------
    async def get_model_status(self) -> str:
        """Best-effort: определяет текущую модель и режим Extended Thinking из UI."""
        if await self.requires_login():
            return "Модель: — (нужен вход в Google)"
        parts = []
        try:
            model = await self._detect_model_name()
            if model:
                parts.append(f"Модель: **{model}**")
        except Exception as exc:  # noqa: BLE001
            log.warning("detect model: %s", exc)
        try:
            thinking = await self._detect_thinking_mode()
            if thinking:
                parts.append(thinking)
        except Exception as exc:  # noqa: BLE001
            log.warning("detect thinking: %s", exc)
        if not parts:
            return "Модель: не определена (настройте вручную в noVNC)"
        return "; ".join(parts)

    async def _detect_model_name(self) -> str:
        """Читает название активной модели: сначала кнопка-лейбл, потом пикер."""
        for sel in MODEL_LABEL_SELECTORS:
            try:
                el = self._page.locator(sel).first
                if await el.is_visible():
                    txt = (await el.inner_text() or "").strip().splitlines()
                    if txt:
                        name = txt[0].strip()
                        if name and len(name) > 1:
                            return name[:60]
            except Exception:  # noqa: BLE001
                continue
        # fallback: открыть пикер и прочитать выбранную (aria-selected) опцию
        try:
            await self._open_model_picker()
            await asyncio.sleep(1.2)
            for sel in MODEL_OPTION_SELECTORS:
                try:
                    loc = self._page.locator(sel)
                    n = await loc.count()
                    for i in range(n):
                        selected = await loc.nth(i).get_attribute("aria-selected")
                        if selected == "true":
                            txt = (await loc.nth(i).inner_text() or "").strip().replace("\n", " ")
                            await self._page.keyboard.press("Escape")
                            return txt[:60]
                except Exception:  # noqa: BLE001
                    continue
            await self._page.keyboard.press("Escape")
        except Exception as exc:  # noqa: BLE001
            log.warning("не удалось прочитать модель из пикера: %s", exc)
        return ""

    async def _detect_thinking_mode(self) -> str:
        """Проверяет, включён ли Extended/Deep Thinking (по тумблеру или тексту)."""
        for sel in THINKING_TOGGLE_SELECTORS:
            try:
                el = self._page.locator(sel).first
                if await el.is_visible():
                    pressed = await el.get_attribute("aria-pressed")
                    checked = await el.get_attribute("aria-checked")
                    if pressed == "true" or checked == "true":
                        return "Extended Thinking: **ВКЛ**"
                    if pressed == "false" or checked == "false":
                        return "Extended Thinking: **ВЫКЛ**"
            except Exception:  # noqa: BLE001
                continue
        try:
            body = await self._page.locator("body").inner_text()
            low = body.lower()
            if "extended thinking" in low or "deep think" in low:
                return "Extended Thinking: вероятно ВКЛ"
        except Exception:  # noqa: BLE001
            pass
        return ""

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
        """Отправляет текст в выделенный чат, ждёт ответ.

        Возвращает ОЧИЩЕННЫЙ ответ (без [MEM_UPDATE: ...] тегов).
        Обновления памяти доступны в self.memory_updates (обрабатывает main.py).
        """
        async with self._lock:
            await self.ensure_ready()
            if not await self.ensure_dedicated_chat():
                raise RuntimeError("Нет доступа к Gemini (требуется вход в Google через noVNC)")
            if await self.requires_login():
                raise RuntimeError("Нет входа в Google — откройте http://<IP>:6080/vnc.html и войдите в аккаунт")

            reply = await self._submit_and_extract(text)
            reply, updates = self._parse_memory_updates(reply)
            self.memory_updates = updates
            if updates:
                log.info("MEM_UPDATE: извлечено %d обновлений памяти", len(updates))
            return reply

    def _emit_stage(self, stage: str) -> None:
        """Уведомляет внешний код о смене этапа: sending → waiting → done."""
        cb = self.stage_callback
        if cb:
            try:
                cb(stage)
            except Exception as exc:  # noqa: BLE001
                log.debug("stage_callback(%s) error: %s", stage, exc)

    async def _submit_and_extract(self, text: str) -> str:
        """Вводит промпт, запускает генерацию, ждёт завершения и возвращает сырой ответ."""
        text = (text or "").strip()
        if not text:
            raise RuntimeError("Пустой промпт")
        self._last_prompt = text

        self._emit_stage("sending")

        box = await self._find_input()
        if not box:
            raise RuntimeError("Не найдено поле ввода Gemini (страница входа?)")

        await box.click()
        await self._page.keyboard.type(text, delay=1)
        await self._page.keyboard.press("Enter")

        self._emit_stage("waiting")

        started = await self._wait_stop_button(timeout=10)
        if not started:
            clicked = await self._click_send_button()
            if not clicked:
                await self._page.keyboard.press("Enter")
            await self._wait_stop_button(timeout=10)

        await self._wait_generation_done(timeout=config.GEMINI_RESPONSE_TIMEOUT)

        # ответ мог ещё не появиться в DOM (гонка) — ждём и переспрашиваем
        reply = ""
        for attempt in range(6):
            reply = await self._extract_last_response()
            if reply and not _looks_like_interface(reply):
                break
            await asyncio.sleep(2)
        if not reply:
            raise RuntimeError("Пустой ответ от Gemini")
        if _looks_like_interface(reply):
            raise RuntimeError("Ответ Gemini не распознан (интерфейс изменился)")

        self._emit_stage("done")

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
        """Ждёт завершения генерации: исчез Stop И/ИЛИ стабилизация ответа.

        «Мусор» интерфейса (левое меню) не считается ответом — ждём дальше.
        """
        last_snapshot = ""
        stable_for = 0
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            stop_visible = await self._any_visible(STOP_SELECTORS)
            snapshot = await self._last_response_text()
            if stop_visible:
                stable_for = 0
            elif snapshot and not _looks_like_interface(snapshot):
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
        """Ищет последний НЕПУСТОЙ и НЕ-интерфейсный ответ модели.

        Если все message-content — мусор/пустые, возвращает "" (значит,
        настоящий ответ ещё не появился, нужно подождать).
        """
        for sel in RESPONSE_SELECTORS:
            try:
                loc = self._page.locator(sel)
                n = await loc.count()
                for i in range(n - 1, -1, -1):
                    t = (await loc.nth(i).inner_text() or "").strip()
                    if t and not _looks_like_interface(t):
                        return t
            except Exception:  # noqa: BLE001
                continue
        return ""
