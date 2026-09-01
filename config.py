"""Загрузка и валидация переменных окружения."""
import os


def parse_user_ids(raw: str) -> list[int]:
    """ALLOWED_USER_ID может содержать один ID или несколько через запятую."""
    ids = []
    for part in raw.replace(" ", "").split(","):
        if part:
            ids.append(int(part))
    return ids


def required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"Отсутствует обязательная переменная окружения: {name}")
    return value


BOT_TOKEN = required("BOT_TOKEN")
ALLOWED_USER_IDS = parse_user_ids(os.environ.get("ALLOWED_USER_ID", ""))
if not ALLOWED_USER_IDS:
    raise RuntimeError("ALLOWED_USER_ID не задан или пуст")

GROQ_API_KEYS = [k.strip() for k in os.environ.get("GROQ_API_KEYS", "").split(",") if k.strip()]
if not GROQ_API_KEYS:
    raise RuntimeError("GROQ_API_KEYS не задан или пуст")

GROQ_MODEL = os.environ.get("GROQ_MODEL", "whisper-large-v3")
DB_PATH = os.environ.get("DB_PATH", "/data/context.db")
