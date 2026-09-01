# Hermes CLI + Mock OpenAI RPA (без API-токенов, через Gemini в браузере)

Гибридный мост: Hermes CLI общается по стандартному OpenAI API с локальным
`mock_openai_rpa.py`, а тот транслирует запрос в веб-сессию Gemini через
Playwright CDP (браузерный контейнер `liza-browser` на сервере).

```
Hermes CLI -> http://127.0.0.1:8000/v1  ->  Playwright CDP  ->  Gemini (браузер)
```

## 1. Развёртывание mock-сервера (на сервере)

```bash
# 1) файл и venv
sudo mkdir -p /opt/liza-mock
sudo cp mock_openai_rpa.py /opt/liza-mock/
sudo chown -R ubuntu:ubuntu /opt/liza-mock
cd /opt/liza-mock
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install fastapi "uvicorn[standard]" playwright

# 2) systemd-юнит
sudo cp systemd/liza-mock.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now liza-mock

# 3) проверка
curl http://127.0.0.1:8000/healthz
curl http://127.0.0.1:8000/v1/models
journalctl -u liza-mock -f
```

CDP_URL=http://127.0.0.1:9222 — порт, опубликованный браузерным контейнером
на хосте (проверяется: `curl http://localhost:9222/json/version`).

## 2. Установка и настройка Hermes CLI

```bash
# установка (Python 3.10+)
python3 -m venv ~/hermes-venv
~/hermes-venv/bin/pip install --upgrade pip
~/hermes-venv/bin/pip install hermes-agent
echo 'export PATH="$HOME/hermes-venv/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
hermes --version
```

Конфиг `~/.hermes/config.yaml` (провайдер **custom** — для локальных
OpenAI-совместимых эндпоинтов):

```yaml
model:
  provider: custom
  default: gemini-rpa
  base_url: http://127.0.0.1:8000/v1
  api_key: mock-rpa-key
```

Либо через переменные окружения (если Hermes их поддерживает):

```bash
export OPENAI_BASE_URL="http://127.0.0.1:8000/v1"
export OPENAI_API_KEY="mock-rpa-key"
export OPENAI_TIMEOUT=120
```

Проверка (в этой версии разовый запрос — `-z`):

```bash
hermes -z "Привет! Ответь одним предложением."
```

## 3. Таймауты

- `MOCK_TIMEOUT` (default 300) — лимит генерации в браузере.
- `MOCK_FALLBACK_AFTER` (default 240) — если генерация застряла и нет кнопки
  Stop, страница перезагружается и делается повторная попытка.
- В Hermes/клиенте HTTP read timeout должен быть не меньше таймаута сервера.

## 4. Telegram-мост (опционально)

Для проброса сообщений из Telegram в Hermes используется лёгкий демон
(aiogram 3.x): текст сообщения -> `hermes run --query "..."` -> ответ режется
на блоки до 4000 символов и отправляется обратно. Скрипт и systemd-юнит —
`telegram_hermes_bridge.py` / `systemd/telegram-hermes.service` (см. код).

## 5. Краевые случаи (реализовано в mock_openai_rpa.py)

| Случай | Обработка |
|---|---|
| Таймаут/зависание DOM | перезагрузка страницы + повтор, лимит 120с |
| Отвал CDP | авто-переподключение без рестарта сервиса |
| Тех.мусор в ответе | чистка [MEM_UPDATE], wiki-хвостов, тех.шапки |
| Несколько запросов | сериализация через asyncio.Lock |
| Неверный ключ | 401 |
| stream: true | SSE-ответ (полный чанк + [DONE]) |
