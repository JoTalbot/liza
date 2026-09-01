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

# --- LLM-мозг (чат) ---
# Актуальные модели Groq (сентябрь 2026): llama-3.x сняты с аккаунта,
# доступны groq/compound, openai/gpt-oss-*, qwen/qwen3.x-27b.
GROQ_CHAT_MODEL = os.environ.get("GROQ_CHAT_MODEL", "groq/compound")
GROQ_CHAT_FALLBACK_MODEL = os.environ.get("GROQ_CHAT_FALLBACK_MODEL", "qwen/qwen3.8-27b")
LLM_CONTEXT_SIZE = max(1, int(os.environ.get("LLM_CONTEXT_SIZE", "10")))

# --- Google Docs синк (опционально) ---
GOOGLE_DOC_WEBHOOK_URL = os.environ.get("GOOGLE_DOC_WEBHOOK_URL", "").strip()
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
GOOGLE_DOC_ID = os.environ.get("GOOGLE_DOC_ID", "").strip()

DB_PATH = os.environ.get("DB_PATH", "/data/context.db")
