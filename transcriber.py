"""Транскрибация голосовых (.ogg) через Groq Whisper с ротацией ключей."""
import logging
import os
import tempfile
import time

from groq import APIError, Groq, RateLimitError

import config

log = logging.getLogger(__name__)


class GroqTranscriber:
    """Отправляет байты .ogg в whisper-large-v3.

    При ошибках 429 (rate limit) автоматически переключается на следующий
    ключ из GROQ_API_KEYS, временные файлы удаляются.
    """

    def __init__(self, api_keys: list[str], model: str = config.GROQ_MODEL):
        if not api_keys:
            raise ValueError("Нет ни одного Groq API key")
        self.api_keys = api_keys
        self.model = model
        self._idx = 0

    def _client(self) -> Groq:
        return Groq(api_key=self.api_keys[self._idx % len(self.api_keys)])

    def _rotate(self) -> None:
        self._idx += 1

    def transcribe_ogg_bytes(self, data: bytes, filename: str = "voice.ogg") -> str:
        """Принимает сырые байты OGG-файла, возвращает распознанный текст."""
        attempts = 0
        max_attempts = len(self.api_keys) * 3  # несколько кругов по ключам

        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp.write(data)
            tmp_path = tmp.name

        try:
            while attempts < max_attempts:
                attempts += 1
                client = self._client()
                try:
                    with open(tmp_path, "rb") as f:
                        result = client.audio.transcriptions.create(
                            model=self.model,
                            file=(filename, f, "audio/ogg"),
                        )
                    text = (result.text or "").strip()
                    log.info("Transcribed %.1f s -> %d chars (key #%d)", len(data) / 20_000, len(text), self._idx + 1)
                    return text
                except RateLimitError as exc:
                    log.warning("RateLimit (attempt %d): %s", attempts, exc)
                    self._rotate()
                    time.sleep(1.0)
                except APIError as exc:
                    # Некоторые версии SDK отдают 429 именно как APIError
                    if getattr(exc, "status_code", None) == 429:
                        log.warning("HTTP 429 (attempt %d): %s", attempts, exc)
                        self._rotate()
                        time.sleep(1.0)
                    else:
                        raise
            raise RuntimeError("Все Groq ключи исчерпаны (rate limit) — повторите позже")
        finally:
            try:
                os.unlink(tmp_path)  # всегда удаляем временный файл
            except OSError:
                pass
