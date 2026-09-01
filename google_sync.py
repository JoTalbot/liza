"""Реалтайм-синк записей в Google Docs.

Два режима (достаточно одного):
1. GOOGLE_DOC_WEBHOOK_URL — POST JSON на Google Apps Script webhook,
   который сам добавляет строку в нужный документ.
2. GOOGLE_SERVICE_ACCOUNT_JSON + GOOGLE_DOC_ID — напрямую через
   Google Docs API (documents.batchUpdate, insertText в конец документа).

Если ничего не настроено — пишем предупреждение в лог и не падаем.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone

import aiohttp

import config

log = logging.getLogger(__name__)

TYPE_ICONS = {"text": "📝", "voice": "🎧", "assistant": "🤖"}


def is_configured() -> bool:
    return bool(
        config.GOOGLE_DOC_WEBHOOK_URL
        or (config.GOOGLE_SERVICE_ACCOUNT_JSON and config.GOOGLE_DOC_ID)
    )


async def append_entry(entry_type: str, content: str) -> bool:
    """Асинхронно добавляет запись в Google Doc. Никогда не бросает исключений."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    icon = TYPE_ICONS.get(entry_type, "📌")
    line = f"[{ts}] {icon} {content}".strip()

    if not is_configured():
        log.warning(
            "Google sync не настроен (нужен GOOGLE_DOC_WEBHOOK_URL "
            "или GOOGLE_SERVICE_ACCOUNT_JSON + GOOGLE_DOC_ID) — пропускаю"
        )
        return False

    try:
        if config.GOOGLE_DOC_WEBHOOK_URL:
            await _append_via_webhook(line, entry_type, ts)
        else:
            await asyncio.to_thread(_append_via_docs_api, line)
        log.info("Google sync: добавлено (%s)", entry_type)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("Google sync не удался: %s", exc)
        return False


async def _append_via_webhook(line: str, entry_type: str, ts: str) -> None:
    payload = {
        "content": line,
        "type": entry_type,
        "timestamp": ts,
        "source": "liza-bot",
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(
            config.GOOGLE_DOC_WEBHOOK_URL,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status >= 400:
                body = await resp.text()
                log.warning("Webhook ответил %s: %s", resp.status, body[:300])


def _append_via_docs_api(line: str) -> None:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_info(
        json.loads(config.GOOGLE_SERVICE_ACCOUNT_JSON),
        scopes=["https://www.googleapis.com/auth/documents"],
    )
    svc = build("docs", "v1", credentials=creds, cache_discovery=False)
    svc.documents().batchUpdate(
        documentId=config.GOOGLE_DOC_ID,
        body={
            "requests": [
                {"insertText": {"endOfSegmentLocation": {}, "text": line + "\n"}}
            ]
        },
    ).execute()
