"""LLM-мозг Лизы: Groq chat completions + контекст из SQLite.

Берёт последние N взаимодействий из базы, добавляет новое сообщение
и возвращает ответ ассистента с персоной Лизы. При rate limit (429)
ротирует ключи из GROQ_API_KEYS, при недоступности основной модели
переключается на запасную.
"""
import logging
import time

from groq import Groq, RateLimitError, APIError

import config
from database import Database

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "Ты — ЛИЗА (Liza), личный ИИ-компаньон и «второй мозг» пользователя. "
    "Твой стиль: неформальный, остроумный, игривый, умный и лаконичный. "
    "Ты поддерживаешь, помогаешь думать и помнишь контекст разговора. "
    "Отвечай на языке пользователя (обычно русский), 1–3 предложения, "
    "если не просят развёрнуто. Будь живой и тёплой, но не многословной."
)


class GroqBrain:
    """Ответы через Groq chat completions с ротацией ключей и запасной моделью."""

    def __init__(
        self,
        api_keys: list[str],
        model: str = config.GROQ_CHAT_MODEL,
        fallback_model: str = config.GROQ_CHAT_FALLBACK_MODEL,
        max_tokens: int = 600,
        temperature: float = 0.8,
    ):
        if not api_keys:
            raise ValueError("Нет ни одного Groq API key")
        self.api_keys = api_keys
        self.model = model
        self.fallback_model = fallback_model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._idx = 0

    def _client(self) -> Groq:
        return Groq(api_key=self.api_keys[self._idx % len(self.api_keys)])

    def _rotate(self) -> None:
        self._idx += 1

    def _chat_once(self, client: Groq, model: str, messages: list[dict]) -> str:
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )
        try:
            text = (resp.choices[0].message.content or "").strip()
        except (AttributeError, IndexError):
            text = ""
        if not text:
            raise RuntimeError("Пустой ответ модели")
        return text

    def chat(self, messages: list[dict]) -> str:
        """Один запрос к LLM: перебирает ключи и модели при 429/ошибках."""
        models = [self.model]
        if self.fallback_model and self.fallback_model != self.model:
            models.append(self.fallback_model)

        attempts = 0
        max_attempts = len(self.api_keys) * 4  # несколько кругов по ключам
        while attempts < max_attempts:
            attempts += 1
            client = self._client()
            for model in models:
                try:
                    return self._chat_once(client, model, messages)
                except RateLimitError as exc:
                    log.warning("RateLimit key#%d model=%s: %s", self._idx, model, exc)
                    self._rotate()
                    break  # переключаемся на следующий ключ
                except APIError as exc:
                    if getattr(exc, "status_code", None) == 429:
                        log.warning("HTTP 429 key#%d model=%s", self._idx, model)
                        self._rotate()
                        break
                    log.warning("API error key#%d model=%s: %s", self._idx, model, exc)
                    continue  # пробуем следующую модель
                except Exception as exc:  # noqa: BLE001
                    log.warning("Ошибка key#%d model=%s: %s", self._idx, model, exc)
                    continue
            time.sleep(0.5)
        raise RuntimeError("Все ключи/модели исчерпаны — повторите позже")

    def reply(self, db: Database, exclude_note_id: int | None, user_input: str) -> str:
        """Контекст (последние N записей, без только что добавленной) + новое сообщение."""
        rows = db.last_notes(config.LLM_CONTEXT_SIZE + 2)
        history = [r for r in rows if r["id"] != exclude_note_id][-config.LLM_CONTEXT_SIZE:]
        messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
        for row in history:
            role = "assistant" if row["type"] == "assistant" else "user"
            messages.append({"role": role, "content": row["content"]})
        messages.append({"role": "user", "content": user_input})
        log.info("LLM request: %d контекстных сообщений + 1 новое", len(history))
        return self.chat(messages)
