#!/usr/bin/env python3
"""Telegram-мост -> Hermes CLI -> Mock OpenAI RPA.

Лёгкий aiogram-демон: входящее сообщение уходит в `hermes run --query "..."`,
ответ возвращается в чат, разбитый на блоки до 4000 символов.

Требует: BOT_TOKEN, ALLOWED_USER_ID (запятая), HERMES_BIN (путь к hermes).
"""
from __future__ import annotations

import asyncio
import logging
import os
import shlex
import subprocess

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("telegram-hermes")

BOT_TOKEN = os.environ["BOT_TOKEN"]
ALLOWED = {int(x) for x in os.environ.get("ALLOWED_USER_ID", "").split(",") if x.strip()}
HERMES_BIN = os.environ.get("HERMES_BIN", "hermes")
MAX_BLOCK = int(os.environ.get("MAX_BLOCK", "4000"))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


def _chunks(text: str, size: int = MAX_BLOCK) -> list[str]:
    if len(text) <= size:
        return [text]
    parts: list[str] = []
    while len(text) > size:
        cut = text.rfind("\n", 0, size)
        if cut < size // 2:
            cut = size
        parts.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        parts.append(text)
    return parts


def _ask_hermes(query: str, timeout: int = 180) -> str:
    cmd = f"{shlex.quote(HERMES_BIN)} run --query {shlex.quote(query)}"
    proc = subprocess.run(
        cmd, shell=True, capture_output=True, text=True, timeout=timeout,
    )
    out = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    if not out:
        raise RuntimeError(f"hermes вернул пустой ответ (rc={proc.returncode})")
    return out


@dp.message(F.text)
async def on_text(message: Message) -> None:
    if message.from_user is None or message.from_user.id not in ALLOWED:
        return
    status = await message.answer("🤖 Hermes думает…")
    try:
        reply = await asyncio.to_thread(_ask_hermes, message.text)
        await status.delete()
        for block in _chunks(reply):
            await message.answer(block)
    except Exception as exc:  # noqa: BLE001
        log.exception("hermes error")
        try:
            await status.edit_text(f"❌ Ошибка: {exc}")
        except Exception:  # noqa: BLE001
            pass


async def main() -> None:
    dp.message.register(on_text)
    await bot.delete_webhook(drop_pending_updates=True)
    log.info("Telegram-Hermes мост запущен | allowed=%s", sorted(ALLOWED))
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("stopped")
