# Liza — Telegram-бот: Web Bridge в Gemini (Advanced + Extended Thinking)

ЛИЗА — неформальный, остроумный и лаконичный ИИ-компаньон. Ведёт **выделенный
персистентный чат** в Gemini (самая продвинутая модель + Extended Thinking)
через Playwright CDP. Тексты и голосовые сохраняются в SQLite, голос
транскрибируется (Groq Whisper), весь лог дублируется в Google Doc.

## Стек
Python 3.11 · aiogram 3.x · Playwright (CDP) · groq (Whisper + fallback chat) ·
sqlite3 · aiohttp · google-api-python-client

## Архитектура (docker compose, 2 контейнера)

```
browser (liza-browser)           bot (liza-bot)
──────────────────────           ─────────────────────────────
debian + chromium                python:3.11-slim
Xvfb + fluxbox + x11vnc          aiogram + playwright
noVNC  :6080  (web-UI)           CDP_URL=http://browser:9222
CDP    :9222  (debug port)       база /data (volume)
профиль /config/profile          /data/chat_session.json
```

## Файлы
```
browser/Dockerfile + start.sh — браузерный стек (Chromium + noVNC + CDP)
docker-compose.yml            — оркестрация browser + bot
web_bridge.py                 — Playwright CDP, выделенный чат Gemini,
                                Extended Thinking, /data/chat_session.json
main.py                       — Telegram: текст/голос → Web Bridge (fallback Groq)
ai_brain.py                   — Groq chat (fallback)
google_sync.py                — реалтайм-синк в Google Doc (graceful-fail)
transcriber.py                — .ogg → текст (Groq Whisper)
database.py                   — SQLite: notes(id, timestamp, type, content)
```

## Быстрый старт
```bash
cp .env.example .env   # заполнить BOT_TOKEN, ALLOWED_USER_ID, GROQ_API_KEYS
docker compose up -d --build
```

1. Открой **noVNC**: `http://<IP-сервера>:6080/vnc.html` и **один раз войди в
   Google-аккаунт** (сессия сохранится в `/opt/liza_chrome_profile`).
2. Напиши боту `/start`. Лизa автоматически создаст выделенный чат
   (`/data/chat_session.json`) и в следующий раз будет возвращаться в него же.
3. Проверь `/status` — чат Gemini и модель.

Если авто-выбор Extended Thinking не сработал (интерфейс Google меняется) —
включи режим вручную в том же чате через noVNC; URL чата сохранится и будет
использоваться дальше.

## Команды
- `/start` — приветствие и проверка доступа
- `/status` — статус Web Bridge: CDP, вход в Google, URL выделенного чата,
  **модель и режим Extended Thinking**, память; с кнопками:
  - **🆕 Новый чат** — создаёт новый выделенный чат Gemini с **полной
    авто-инициализацией** (модель → Extended Thinking → стартовый промпт
    Лизы с @Google Drive → подтверждение) и переключает бота на него
  - **🔄 Обновить статус** — перечитывает статус/модель
- `/newchat` — то же, что кнопка «Новый чат»
- `/dump [n]` — последние n записей из памяти (по умолчанию 10)

## Память (MEM_UPDATE)
Если ответ Gemini содержит тег `[MEM_UPDATE: суть обновления]`:
- тег **извлекается** из финального текста (пользователю уходит чистый ответ),
- содержимое сохраняется в SQLite (`type=MEM_UPDATE`, иконка 🧠 в `/dump`),
- дублируется в Google Docs через `google_sync.py`.

## Конфигурация (.env)
Обязательные: `BOT_TOKEN`, `ALLOWED_USER_ID`, `GROQ_API_KEYS`.
Опциональные: `CDP_URL`, `GEMINI_RESPONSE_TIMEOUT`, `GROQ_CHAT_MODEL`,
`GROQ_CHAT_FALLBACK_MODEL`, Google-переменные (см. `.env.example`).

## Обновление
Любой `git push` в `main` → GitHub Actions: clone → .env из секретов →
`docker compose up -d --build` → контейнеры пересоздаются, база и профиль
браузера сохраняются (volumes).

## Полезные команды
```bash
docker compose ps
docker logs -f liza-bot
docker logs liza-browser
docker restart liza-browser   # если Chromium завис
# CDP:  curl http://localhost:9222/json/version
# noVNC: http://<IP>:6080/vnc.html
```

## ⚠️ Безопасность
Порты `6080` и `9222` открыты наружу (noVNC без пароля по умолчанию; CDP =
полный контроль над браузером). Рекомендуется:
- задать `VNC_PASSWORD` в `docker-compose.yml` (для noVNC), и/или
- закрыть порты в security list (Oracle) и ходить только по SSH/превью.
