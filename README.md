# Liza — Telegram-бот: ИИ-компаньон + реалтайм-синк в Google Docs

ЛИЗА — неформальный, остроумный и лаконичный ИИ-компаньон. Сохраняет тексты
и голосовые в SQLite, транскрибирует голос (Groq Whisper), отвечает через
LLM (Groq) с контекстом последних 10 взаимодействий и дублирует весь лог
в Google Doc в реальном времени.

## Стек
Python 3.11 · aiogram 3.x · groq (Whisper + chat) · sqlite3 · aiohttp ·
google-api-python-client (Google Docs API)

## Структура
```
main.py          — Long polling, фильтр ALLOWED_USER_ID, конвейер текст/голос → LLM
ai_brain.py      — LLM-мозг: Groq chat (llama-3.3-70b-versatile + fallback),
                   контекст из SQLite, ротация ключей при 429
google_sync.py   — реалтайм-синк в Google Doc: webhook ИЛИ Docs API (graceful-fail)
transcriber.py   — .ogg → текст через Groq Whisper, ротация ключей, чистка temp
database.py      — SQLite /data/context.db, таблица notes(id, timestamp, type, content)
config.py        — чтение всех переменных окружения
Dockerfile       — python:3.11-slim, WORKDIR /app, база в /data
```

Типы записей в базе: `text` (📝), `voice` (🎧), `assistant` (🤖).

## Команды
- `/start` — приветствие и проверка доступа
- `/dump [n]` — последние n записей из памяти (по умолчанию 10, максимум 100)

## Конфигурация (.env)
Обязательные: `BOT_TOKEN`, `ALLOWED_USER_ID`, `GROQ_API_KEYS`.
Опциональные: `GROQ_CHAT_MODEL` (по умолч. `llama-3.3-70b-versatile`),
`GROQ_CHAT_FALLBACK_MODEL` (по умолч. `llama-3.1-8b-instant`), `LLM_CONTEXT_SIZE`.

## Google Docs синк
Достаточно одного из способов (иначе бот пишет warning в лог и работает дальше):

**1. Webhook (проще всего).** Создай Google Apps Script:
```js
function doPost(e) {
  var body = JSON.parse(e.postData.contents);
  var doc = DocumentApp.openById(PropertiesService.getScriptProperties().getProperty('DOC_ID'));
  doc.getBody().appendParagraph(body.content);
  return ContentService.createTextOutput('ok');
}
```
Положи ID документа в Properties (или захардкодь) и укажи URL вебхука в
`GOOGLE_DOC_WEBHOOK_URL`.

**2. Service Account + Docs API.** Создай service account в Google Cloud,
выдай ему доступ на редактирование документа (share → email сервис-аккаунта),
положи JSON ключ в `GOOGLE_SERVICE_ACCOUNT_JSON` и ID документа в `GOOGLE_DOC_ID`.

## Запуск
```bash
cp .env.example .env   # и заполнить значения
docker build -t liza-bot .
docker run -d --name liza-bot --restart unless-stopped \
  -v /opt/liza_data:/data --env-file .env liza-bot
```
