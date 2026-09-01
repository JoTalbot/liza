# Liza — Telegram-бот (текст + голосовые)

Сохраняет текстовые и голосовые сообщения в SQLite, голос транскрибирует через
Groq Whisper (`whisper-large-v3`) с ротацией ключей при rate limit.

## Стек
Python 3.11 · aiogram 3.x · groq (Whisper) · sqlite3 · aiohttp

## Структура
```
main.py          — Long polling, фильтр ALLOWED_USER_ID, обработка текста/голоса
transcriber.py   — .ogg → текст через Groq, ротация ключей на 429, чистка temp
database.py      — SQLite /data/context.db, таблица notes(id, timestamp, type, content)
config.py        — чтение BOT_TOKEN, ALLOWED_USER_ID, GROQ_API_KEYS
requirements.txt — aiogram, groq, aiohttp
Dockerfile       — python:3.11-slim, WORKDIR /app, база в /data
```

## Команды
- `/start` — приветствие и проверка доступа
- `/dump [n]` — последние n записей из базы списком (Markdown)

## Запуск
```bash
cp .env.example .env   # и заполнить значения
docker build -t liza-bot .
docker run -d --name liza-bot --restart unless-stopped \
  -v /opt/liza_data:/data --env-file .env liza-bot
```
