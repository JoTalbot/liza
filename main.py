"""Liza — Telegram-бот: сохраняет текст и голосовые, выдаёт /dump."""
import asyncio
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

import config
from database import Database
from transcriber import GroqTranscriber

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher()
router = Router()

db = Database()
transcriber = GroqTranscriber(config.GROQ_API_KEYS)

ALLOWED = set(config.ALLOWED_USER_IDS)


def is_allowed(message: Message) -> bool:
    return message.from_user is not None and message.from_user.id in ALLOWED


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    if not is_allowed(message):
        await message.answer("⛔ Доступ запрещён.")
        return
    await message.answer(
        "👋 Привет! Я бот Liza.\n\n"
        "Присылай мне текст или голосовые — я сохраняю их в базу.\n\n"
        "Команды:\n"
        "• /start — это сообщение\n"
        "• /dump [n] — последние n записей (по умолчанию 10)"
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
        await message.answer("📭 База пока пуста.")
        return

    lines = [f"📚 Последние **{len(rows)}** записей:"]
    for i, row in enumerate(rows, start=1):
        kind = "📝" if row["type"] == "text" else "🎧"
        content = (row["content"] or "").replace("\n", " ")[:150]
        lines.append(f"{i}. {kind} `{content}`")
    await message.answer("\n".join(lines), parse_mode="Markdown")


@router.message(F.text)
async def on_text(message: Message) -> None:
    if not is_allowed(message):
        return
    note_id = db.add_note("text", message.text)
    log.info("Saved text note #%d from %s", note_id, message.from_user.id)
    await message.answer(f"📝 Сохранил текст ({len(message.text)} симв.), запись #{note_id}.")


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
        log.info("Saved voice note #%d from %s", note_id, message.from_user.id)
        await status.edit_text(f"🎧 Распознано (запись #{note_id}):\n\n{text}")
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
    await message.answer("Пришли мне текст или голосовое сообщение 🙂")


async def main() -> None:
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    log.info("Bot started, allowed users: %s", sorted(ALLOWED))
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Bot stopped")
