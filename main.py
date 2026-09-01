"""Liza — Telegram-бот: ИИ-компаньон с памятью + реалтайм-синк в Google Docs."""
import asyncio
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

import config
import google_sync
from ai_brain import GroqBrain
from database import Database
from transcriber import GroqTranscriber

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher()
router = Router()

db = Database()
transcriber = GroqTranscriber(config.GROQ_API_KEYS)
brain = GroqBrain(config.GROQ_API_KEYS)

ALLOWED = set(config.ALLOWED_USER_IDS)
MAX_REPLY = 4000  # Telegram-лимит на длину сообщения


def is_allowed(message: Message) -> bool:
    return message.from_user is not None and message.from_user.id in ALLOWED


def _clip(text: str) -> str:
    return text if len(text) <= MAX_REPLY else text[: MAX_REPLY - 3] + "..."


def _sync(kind: str, content: str) -> None:
    """Фоновая синхронизация в Google Docs (не блокирует ответ бота)."""
    asyncio.create_task(google_sync.append_entry(kind, content))


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    if not is_allowed(message):
        await message.answer("⛔ Доступ запрещён.")
        return
    await message.answer(
        "👋 Привет! Я **ЛИЗА** — твой ИИ-компаньон и второй мозг. 🤖\n\n"
        "Пиши мне текстом или голосовыми — я отвечаю и запоминаю всё.\n\n"
        "Команды:\n"
        "• /start — это сообщение\n"
        "• /dump [n] — последние n записей из памяти",
        parse_mode="Markdown",
    )


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

    icons = {"text": "📝", "voice": "🎧", "assistant": "🤖"}
    lines = [f"📚 Последние **{len(rows)}** записей:"]
    for i, row in enumerate(rows, start=1):
        icon = icons.get(row["type"], "📌")
        content = (row["content"] or "").replace("\n", " ")[:150]
        lines.append(f"{i}. {icon} `{content}`")
    await message.answer("\n".join(lines), parse_mode="Markdown")


async def _reply_to(message: Message, status: Message | None, user_input: str, note_id: int) -> None:
    """Общий конвейер: (вход уже сохранён) -> LLM -> ответ -> синк."""
    try:
        if status:
            await status.edit_text("💭 Думаю…")
        else:
            await message.answer_chat_action(ChatAction.TYPING)
        reply = await asyncio.to_thread(brain.reply, db, note_id, user_input)
    except Exception as exc:  # noqa: BLE001
        log.exception("LLM failed")
        reply = f"😵 Упс, я споткнулась: {exc}"

    db.add_note("assistant", reply)
    _sync("assistant", reply)
    if status:
        await status.edit_text(_clip(reply))
    else:
        await message.answer(_clip(reply))


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
    log.info(
        "Bot started | allowed=%s | google_sync=%s | chat_model=%s",
        sorted(ALLOWED),
        google_sync.is_configured(),
        config.GROQ_CHAT_MODEL,
    )
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Bot stopped")
