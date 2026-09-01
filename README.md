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
main.py                       — Telegram: текст/голос → Web Bridge (fallback Groq),
                                + планировщик ежедневного дайджеста
ai_brain.py                   — Groq chat (fallback)
google_sync.py                — реалтайм-синк в Google Doc (graceful-fail) +
                                MemorySyncManager: [MEM_UPDATE] -> память,
                                автопаспорта проектов (PRJ_*.md) в /data/projects
transcriber.py                — .ogg → текст (Groq Whisper)
database.py                   — SQLite: notes(id, timestamp, type, content)
daily_digest.py               — ежедневные итоги дня (23:59) в
                                /data/chronicles/YYYY-MM_Digests.md
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
  - **🧠 Память** — последние обновления памяти (MEM_UPDATE)
  - **🔄 Обновить статус** — перечитывает статус/модель
- `/newchat` — то же, что кнопка «Новый чат»
- `/memory [n]` — последние n обновлений памяти (MEM_UPDATE), по умолчанию 10
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

## Перенос на новый сервер (полный redeploy)

Всё хранится на GitHub (код + секреты). Чтобы поднять систему на **новом** OCI-инстансе:

1. **Создай инстанс** в OCI (Ubuntu 22.04/24.04, ARM A1, любой размер — workflow сам
   не трогает железо, но для комфорта 4 OCPU / 24 GB).
2. **Добавь SSH-ключ сервера** в секрет репозитория:
   `Settings → Secrets and variables → Actions → New repository secret`
   - `SSH_PRIVATE_KEY` — приватный ключ `ubuntu` (тот же, что указывал при создании
     инстанса).
3. **Обнови IP в переменной репозитория:**
   `Settings → Secrets and variables → Actions → Variables` → `SERVER_HOST`
   (поставить новый публичный IP инстанса).
4. **Запусти деплой вручную:** GitHub → репозиторий `JoTalbot/liza` → Actions →
   «Deploy Liza Bot» → Run workflow. Workflow сам:
   - установит Docker + compose на новом сервере (если нет);
   - склонирует код, соберёт `.env` из секретов, загрузит его;
   - поднимет браузерный контейнер (`liza-browser`, порты 6080/9222);
   - задеплоит бота (`liza-bot`).
5. **Войди в Google (единственный ручной шаг):** открой
   `http://<SERVER_HOST>:6080/vnc.html`, войди в Google-аккаунт один раз.
6. Напиши боту `/status` или любое сообщение — Лизa создаст/подхватит Gem-чат
   (`GEMINI_GEM_URL` из секретов).

При повторных деплоях workflow **не трогает** `liza-browser` (чтобы не потерять
сессию Google) — пересоздаётся только бот.

### Требуемые секреты репозитория

| Секрет | Что это |
|---|---|
| `BOT_TOKEN` | Токен Telegram-бота (@BotFather) |
| `ALLOWED_USER_ID` | Твой Telegram user ID |
| `GROQ_API_KEYS` | Ключи Groq (whisper + fallback) |
| `GEMINI_GEM_URL` | URL кастомного Gem «Liza» |
| `SSH_PRIVATE_KEY` | Приватный ключ доступа к серверу (ubuntu) |

Опционально: `GOOGLE_DOC_WEBHOOK_URL`, `GOOGLE_SERVICE_ACCOUNT_JSON`,
`GOOGLE_DOC_ID` (Google Docs sync), `DAILY_DIGEST_TIME`.

Переменная репозитория: `SERVER_HOST` (IP сервера).

## Telegram через Hermes (текущая схема)

Сейчас Telegram-бот работает через **Hermes-мост** (без прямого Gemini-канала):

```
Telegram -> telegram_hermes_bridge.py (aiogram, systemd)
         -> hermes CLI (-z) -> mock_openai_rpa.py (FastAPI :8000)
         -> Playwright CDP -> Gemini (браузер liza-browser)
```

- Старый контейнер `liza-bot` отключён (переменная репо `DEPLOY_LEGACY_BOT=0`).
- systemd-сервисы: `liza-mock` (OpenAI API) и `telegram-hermes` (Telegram-мост).
- `.telegram.env` (токен + allowed) создаётся из `.env` при деплое.
- Чтобы вернуть старый прямой канал: поставь `DEPLOY_LEGACY_BOT=1` в переменных
  репозитория и перезапусти workflow (не забудь остановить telegram-hermes).

## Память Лизы (самое важное)

Память хранится в **`/opt/liza_data/context.db`** (тот же файл, что был у
Telegram-бота — старые записи сохраняются):

- **`[MEM_UPDATE: ...]`** из ответов модели автоматически сохраняются в таблицу
  `memory_updates` (mock_openai_rpa.py);
- **диалог** (user/assistant) пишется в таблицу `conversations`;
- **вики Liza_Brain** (`/opt/liza_data/liza_brain/*.txt`) + последние факты +
  недавний диалог **подмешиваются в каждый запрос** к Gemini — Лизa помнит
  контекст между сообщениями даже при `hermes -z`;
- команда **`/memory [n]`** в Telegram показывает последние n запомненных фактов.

## Голосовые + ежедневный дайджест памяти

- **Голосовые в Telegram**: `telegram_hermes_bridge.py` транскрибирует
  .ogg через **Groq Whisper** (`whisper-large-v3`), текст уходит в Hermes
  (нужен `GROQ_API_KEYS` в `.telegram.env` — создаётся из `.env`).
- **Ежедневный дайджест**: `daily_memory_digest.py` + systemd-таймер
  `liza-digest.timer` каждый день в 23:59 пишет итоги дня (диалог + факты
  за 24ч) в `/opt/liza_data/chronicles/YYYY-MM_Digests.md`.
- **Итоги дня в памяти**: mock подмешивает хвост последнего дайджеста в
  контекст — Лизa помнит не только факты, но и недавние итоги.

## Лизa — серверный помощник (shell-инструменты)

Лизa умеет **выполнять команды на сервере** через mock_openai_rpa.py:

- **Прямой режим**: напиши команду в чат (например `ls -la`, `docker ps`,
  `systemctl status liza-mock`) или с префиксом `!` — mock выполняет её
  (от имени ubuntu, таймаут 30с, вывод ≤6000 символов) и Лизa комментирует
  результат;
- **Агентный режим**: если Лизе нужны данные с сервера, она сама оборачивает
  команду в `[CMD: ...]`, mock выполняет, и Лизa даёт финальный ответ
  (до 3 ходов);
- безопасный авто-детект: команды из белого списка (ls, cat, ps, systemctl,
  journalctl, docker, grep, hermes, git, python3 и др.) — всё остальное через
  префикс `!`;
- `hermes` доступен в PATH через symlink `/usr/local/bin/hermes`;
- ubuntu добавлен в группу `docker` — Лизa видит контейнеры.
