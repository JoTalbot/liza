"""Liza — Telegram-бот: Web Bridge (Gemini Advanced + Extended Thinking) + память + Google Docs."""
import asyncio
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import config
import google_sync
from ai_brain import GroqBrain
from database import Database
from transcriber import GroqTranscriber
from web_bridge import WebBridge

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher()
router = Router()

db = Database()
transcriber = GroqTranscriber(config.GROQ_API_KEYS)
brain = GroqBrain(config.GROQ_API_KEYS)      # fallback, если Web Bridge недоступен
bridge = WebBridge(config.CDP_URL)           # основной канал — Gemini через CDP

ALLOWED = set(config.ALLOWED_USER_IDS)
MAX_REPLY = 4000


def is_allowed(message: Message) -> bool:
    return message.from_user is not None and message.from_user.id in ALLOWED


def is_allowed_uid(uid: int | None) -> bool:
    return uid is not None and uid in ALLOWED


def _clip(text: str) -> str:
    return text if len(text) <= MAX_REPLY else text[: MAX_REPLY - 3] + "..."


def _sync(kind: str, content: str) -> None:
    """Фоновая синхронизация в Google Docs (не блокирует ответ бота)."""
    asyncio.create_task(google_sync.append_entry(kind, content))


# ---------- анимированное статусное сообщение ----------
SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


class StatusAnimator:
    """Анимированная статусная надпись: спиннер + сменяемый текст."""

    def __init__(self, message: Message):
        self.message = message
        self.base = ""
        self._task: asyncio.Task | None = None

    async def _run(self) -> None:
        i = 0
        while True:
            try:
                await self.message.edit_text(f"{SPINNER_FRAMES[i % len(SPINNER_FRAMES)]} {self.base}")
            except Exception:  # noqa: BLE001 — Telegram ругается на одинаковый текст
                pass
            i += 1
            await asyncio.sleep(0.6)

    def set_text(self, base: str) -> None:
        """Меняет подпись (анимация продолжает крутиться)."""
        self.base = base
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def show_final(self, text: str) -> None:
        """Останавливает анимацию и показывает финальный текст."""
        await self._stop()
        try:
            await self.message.edit_text(text)
        except Exception:  # noqa: BLE001
            pass

    async def stop_and_delete(self) -> None:
        """Останавливает анимацию и удаляет сообщение."""
        await self._stop()
        try:
            await self.message.delete()
        except Exception:  # noqa: BLE001
            pass

    async def _stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None


def _process_memory_updates() -> int:
    """Извлекает [MEM_UPDATE] из bridge.memory_updates, пишет в SQLite
    (type=MEM_UPDATE) и в Google Docs; возвращает количество."""
    updates = list(getattr(bridge, "memory_updates", []) or [])
    bridge.memory_updates = []
    for upd in updates:
        db.add_note("MEM_UPDATE", upd)
        _sync("MEM_UPDATE", upd)
        log.info("MEM_UPDATE сохранён: %.100s", upd)
    return len(updates)


# ---------- статус и кнопки ----------
def _status_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🆕 Новый чат", callback_data="liza:new_chat"),
                InlineKeyboardButton(text="🔄 Обновить статус", callback_data="liza:refresh"),
            ],
            [
                InlineKeyboardButton(text="🧠 Память", callback_data="liza:memory"),
            ],
        ]
    )


def _memory_text(n: int = 10) -> str:
    """Текст последних n обновлений памяти (MEM_UPDATE)."""
    rows = db.last_mem_updates(n)
    if not rows:
        return "🧠 Память пуста — обновлений `[MEM_UPDATE]` пока нет."
    lines = [f"🧠 Последние **{len(rows)}** обновлений памяти:"]
    for i, r in enumerate(rows, start=1):
        content = (r["content"] or "").replace("\n", " ")[:200]
        lines.append(f"{i}. {content}")
    return "\n".join(lines)


def _current_chat_url() -> str:
    if bridge.chat_url:
        return bridge.chat_url
    try:
        return bridge._load_session().get("url") or "не создан"
    except Exception:  # noqa: BLE001
        return "не создан"


async def _status_text() -> str:
    cdp = "✅ подключено" if bridge._connected else "⏳ не подключено"
    chat = _current_chat_url()
    try:
        if await bridge.has_google_session():
            login = "✅ аккаунт"
        elif await bridge.requires_login():
            login = "❌ нужен вход (noVNC)"
        else:
            login = "⚠️ анонимный режим"
    except Exception:  # noqa: BLE001
        login = "?"
    try:
        model = await bridge.get_model_status()
    except Exception as exc:  # noqa: BLE001
        model = f"Модель: ошибка определения ({exc})"
    lines = [
        "🔧 **Статус:**",
        f"• CDP: {cdp}",
        f"• Вход в Google: {login}",
        f"• Чат: `{chat}`",
        f"• {model}",
        f"• Память: {db.count_notes()} записей",
        f"• Google Docs синк: {'✅' if google_sync.is_configured() else '—'}",
    ]
    return "\n".join(lines)


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    if not is_allowed(message):
        await message.answer("⛔ Доступ запрещён.")
        return
    await message.answer(
        "👋 Привет! Я **ЛИЗА** — твой ИИ-компаньон и второй мозг. 🤖\n\n"
        "Я работаю через **Gemini (Advanced + Extended Thinking)** в выделенном чате.\n\n"
        "Пиши текстом или голосовыми — я отвечаю и запоминаю всё.\n\n"
        "Команды:\n"
        "• /start — это сообщение\n"
        "• /status — статус Web Bridge (кнопки: новый чат / память / обновить)\n"
        "• /newchat — создать и инициализировать новый выделенный чат\n"
        "• /memory [n] — последние n обновлений памяти (MEM_UPDATE)\n"
        "• /dump [n] — последние n записей из памяти",
        parse_mode="Markdown",
    )


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    if not is_allowed(message):
        await message.answer("⛔ Доступ запрещён.")
        return
    text = await _status_text()
    await message.answer(text, parse_mode="Markdown", reply_markup=_status_kb())


@router.message(Command("newchat"))
async def cmd_new_chat(message: Message) -> None:
    if not is_allowed(message):
        await message.answer("⛔ Доступ запрещён.")
        return
    status = await message.answer("🆕 Создаю новый выделенный чат…")
    try:
        url = await bridge.new_dedicated_chat()
        n_upd = _process_memory_updates()
        extra = f"\n\n🧠 Обновлений памяти: {n_upd}." if n_upd else ""
        await status.edit_text(
            f"🆕 Новый выделенный чат создан и инициализирован:\n`{url}`{extra}\n\n"
            "Следующие сообщения пойдут уже в него.",
            parse_mode="Markdown",
            reply_markup=_status_kb(),
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("new chat failed")
        await status.edit_text(f"❌ Не удалось создать чат: {exc}", reply_markup=_status_kb())


@router.callback_query(F.data == "liza:new_chat")
async def cb_new_chat(call: CallbackQuery) -> None:
    if not is_allowed_uid(call.from_user.id if call.from_user else None):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    await call.answer("Создаю новый чат…")
    try:
        url = await bridge.new_dedicated_chat()
        n_upd = _process_memory_updates()
        extra = f"\n\n🧠 Обновлений памяти: {n_upd}." if n_upd else ""
        await call.message.edit_text(
            f"🆕 Новый выделенный чат создан и инициализирован:\n`{url}`{extra}\n\n"
            "Следующие сообщения пойдут уже в него.",
            parse_mode="Markdown",
            reply_markup=_status_kb(),
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("new chat failed")
        await call.message.edit_text(f"❌ Не удалось создать чат: {exc}", reply_markup=_status_kb())


@router.callback_query(F.data == "liza:refresh")
async def cb_refresh(call: CallbackQuery) -> None:
    if not is_allowed_uid(call.from_user.id if call.from_user else None):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    await call.answer("Обновляю…")
    text = await _status_text()
    try:
        await call.message.edit_text(text, parse_mode="Markdown", reply_markup=_status_kb())
    except Exception:  # noqa: BLE001 — текст не изменился
        await call.message.answer(text, parse_mode="Markdown", reply_markup=_status_kb())


@router.message(Command("memory"))
async def cmd_memory(message: Message) -> None:
    if not is_allowed(message):
        await message.answer("⛔ Доступ запрещён.")
        return
    args = message.text.split()
    n = 10
    if len(args) > 1:
        try:
            n = int(args[1])
        except ValueError:
            n = 10
    n = max(1, min(n, 50))
    await message.answer(_memory_text(n), parse_mode="Markdown", reply_markup=_status_kb())


@router.callback_query(F.data == "liza:memory")
async def cb_memory(call: CallbackQuery) -> None:
    if not is_allowed_uid(call.from_user.id if call.from_user else None):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    await call.answer("Читаю память…")
    text = _memory_text()
    try:
        await call.message.edit_text(text, parse_mode="Markdown", reply_markup=_status_kb())
    except Exception:  # noqa: BLE001 — текст не изменился
        await call.message.answer(text, parse_mode="Markdown", reply_markup=_status_kb())


@router.message(Command("dump"))
async def cmd_dump(message: Message) -> None:
    if not is_allowed(message):
        await message.answer("⛔ Доступ запрещён.")
        return

    args = message.text.split()
    n = 10
    if len(args) > 1:
        try:
            n = int(args[1])
        except ValueError:
            n = 10
    n = max(1, min(n, 100))

    rows = db.last_notes(n)
    if not rows:
        await message.answer("📭 Память пока пуста.")
        return

    icons = {"text": "📝", "voice": "🎧", "assistant": "🤖", "MEM_UPDATE": "🧠"}
    lines = [f"📚 Последние **{len(rows)}** записей:"]
    for i, row in enumerate(rows, start=1):
        icon = icons.get(row["type"], "📌")
        content = (row["content"] or "").replace("\n", " ")[:150]
        lines.append(f"{i}. {icon} `{content}`")
    await message.answer("\n".join(lines), parse_mode="Markdown")


async def _reply_to(message: Message, status: Message | None, user_input: str, note_id: int) -> None:
    """Конвейер: статус-анимация → Web Bridge (Gemini) → fallback Groq → ответ.

    Этапы надписи:
      «Запрашиваю модель» → «Ожидание ответа модели» → «✅ Ответ получен!»
    Затем отправляется сам ответ, статусная надпись удаляется.
    """
    if status is None:
        status = await message.answer("⏳")
    anim = StatusAnimator(status)
    anim.set_text("Запрашиваю модель")

    def on_stage(stage: str) -> None:
        # вызывается из web_bridge (sending → waiting → done)
        if stage == "waiting":
            anim.set_text("Ожидание ответа модели")

    bridge.stage_callback = on_stage

    try:
        reply = await bridge.send_prompt_to_chat(user_input)
        if not reply:
            raise RuntimeError("Пустой ответ от Gemini")
        source = "gemini"
        log.info("Ответ получен из Gemini (%d симв.)", len(reply))
        # [MEM_UPDATE: ...] из ответа → SQLite + Google Docs
        n_upd = _process_memory_updates()
        if n_upd:
            log.info("Обработано MEM_UPDATE: %d", n_upd)
    except Exception as exc:  # noqa: BLE001
        log.warning("Web Bridge недоступен (%s) — fallback к Groq", exc)
        anim.set_text("Запрашиваю модель (запасной канал)")
        try:
            reply = await asyncio.to_thread(brain.reply, db, note_id, user_input)
            source = "groq"
        except Exception as exc2:  # noqa: BLE001
            log.exception("Groq fallback не сработал")
            reply = f"😵 Упс, всё сломалось: {exc2}"
            source = "none"

    db.add_note("assistant", reply)
    _sync("assistant", reply)

    # «Ответ получен» → отправляем ответ → удаляем статус
    await anim.show_final("✅ Ответ получен!")
    try:
        await message.answer(_clip(reply))
    finally:
        await anim.stop_and_delete()


@router.message(F.text)
async def on_text(message: Message) -> None:
    if not is_allowed(message):
        return
    note_id = db.add_note("text", message.text)
    _sync("text", message.text)
    log.info("Text note #%d from %s", note_id, message.from_user.id)
    await _reply_to(message, None, message.text, note_id)


@router.message(F.voice)
async def on_voice(message: Message) -> None:
    if not is_allowed(message):
        return

    status = await message.answer("🎧 Скачиваю голосовое…")
    try:
        data = await bot.download(message.voice.file_id)
        raw = data.read() if hasattr(data, "read") else data

        await status.edit_text("🔊 Транскрибирую через Whisper…")
        text = await asyncio.to_thread(transcriber.transcribe_ogg_bytes, raw)

        if not text:
            await status.edit_text("🤷 Не удалось распознать речь — попробуйте ещё раз.")
            return

        note_id = db.add_note("voice", text)
        _sync("voice", text)
        log.info("Voice note #%d from %s", note_id, message.from_user.id)
        await _reply_to(message, status, text, note_id)
    except Exception as exc:  # noqa: BLE001
        log.exception("Voice processing failed")
        try:
            await status.edit_text(f"❌ Ошибка обработки голосового: {exc}")
        except Exception:  # noqa: BLE001
            pass


@router.message()
async def on_other(message: Message) -> None:
    if not is_allowed(message):
        return
    await message.answer("Пришли мне текст или голосовое 🙂")


async def main() -> None:
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)

    # пробуем подключиться к браузеру при старте (не фатально)
    try:
        await bridge.ensure_ready(retries=2, delay=3)
        await bridge.ensure_dedicated_chat()
    except Exception as exc:  # noqa: BLE001
        log.warning("Web Bridge недоступен при старте: %s", exc)

    log.info(
        "Bot started | allowed=%s | cdp=%s | google_sync=%s | chat_url=%s",
        sorted(ALLOWED),
        config.CDP_URL,
        google_sync.is_configured(),
        bridge.chat_url or "-",
    )
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Bot stopped")
