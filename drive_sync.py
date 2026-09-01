"""Google Drive sync: скачивает вики-файлы Лизы (Liza_Brain) через залогиненный браузер.

Папка: https://drive.google.com/drive/folders/12k3z0fdt_c_f6KkWPeKxZ7LD1kvaWg8P
Структура: 00_Master, 10_Projects, 20_Personal, 30_Emotional (по 1 Google Doc в каждой).

Файлы сохраняются в /data/liza_brain/*.txt. При недоступности браузера/входа —
тихий фолбэк на ранее сохранённые файлы (бот работает дальше).
"""
import asyncio
import logging
from pathlib import Path

import config

log = logging.getLogger(__name__)

LIZA_BRAIN_DIR = Path(config.DATA_DIR) / "liza_brain"

# id документов (скопированы из папки Liza_Brain)
WIKI_FILES = [
    ("00_Master_Protocol", "1SVUif4jobWI4Z_hY_QfYgGGCeVSlZvH-9Go9Q43I4H0"),
    ("10_Project_AutoGlass", "1wWgN88QpglmnQWiUrf8mO9oDdZrsuornPIQxLZaTOpc"),
    ("20_Personal_Archive", "1SEr0SWX20gFxGlhSACnTmoO8Q_Aw6FI_9Ug-1l3WYyA"),
    ("30_Emotional_Soul", "1nqHqJkI9GrKl_0sr7yuHh3GF7UUNkvCebr7KMeuLMYc"),
]

# сколько символов каждого файла брать в стартовый промпт (лимит контекста)
MAX_PER_FILE = 3000
# общий лимит всех файлов в промпте
MAX_TOTAL = 12000


def _load_cached() -> dict[str, str]:
    """Возвращает ранее сохранённые вики-файлы (если есть)."""
    out = {}
    if not LIZA_BRAIN_DIR.exists():
        return out
    for name, _fid in WIKI_FILES:
        p = LIZA_BRAIN_DIR / f"{name}.txt"
        try:
            if p.exists():
                out[name] = p.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            continue
    return out


def build_persona_context(use_cached: bool = True) -> str:
    """Собирает контекст из вики-файлов для стартового промпта.

    Сначала пробует свежие файлы с диска (если есть), иначе кэш.
    Возвращает строку-инструкцию для Gemini или "" если ничего нет.
    """
    files = _load_cached()
    if not files:
        return ""
    sections = []
    total = 0
    # порядок важен: Master первым (личность), потом остальные
    for name, _fid in WIKI_FILES:
        content = files.get(name, "")
        if not content:
            continue
        clipped = content[:MAX_PER_FILE]
        sections.append(f"### {name}\n{clipped}")
        total += len(clipped)
        if total >= MAX_TOTAL:
            break
    if not sections:
        return ""
    return "\n\n".join(sections)


async def sync_wiki_from_drive(bridge) -> bool:
    """Скачивает вики-файлы через залогиненный браузер (Ctrl+A/Ctrl+C).

    Возвращает True при успехе (хотя бы 1 файл).
    """
    if not bridge or not bridge._page:
        return bool(_load_cached())
    try:
        if not await bridge.has_google_session():
            log.warning("Нет Google-сессии — использую кэшированные вики-файлы")
            return bool(_load_cached())
    except Exception:  # noqa: BLE001
        return bool(_load_cached())

    LIZA_BRAIN_DIR.mkdir(parents=True, exist_ok=True)
    ok = 0
    try:
        await bridge._page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    except Exception:  # noqa: BLE001
        pass

    for name, fid in WIKI_FILES:
        try:
            page = await bridge._browser.contexts[0].new_page()
            try:
                await page.goto(
                    f"https://docs.google.com/document/d/{fid}/edit",
                    wait_until="domcontentloaded", timeout=45000,
                )
                await page.wait_for_timeout(6000)
                await page.keyboard.press("Control+A")
                await page.wait_for_timeout(400)
                await page.keyboard.press("Control+C")
                await page.wait_for_timeout(800)
                txt = await page.evaluate("navigator.clipboard.readText()")
                if txt and len(txt.strip()) > 50:
                    (LIZA_BRAIN_DIR / f"{name}.txt").write_text(txt, encoding="utf-8")
                    ok += 1
                    log.info("Вики-файл обновлён: %s (%d симв.)", name, len(txt))
            finally:
                await page.close()
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось скачать вики %s: %s", name, exc)
    log.info("Google Drive sync: обновлено %d/%d файлов", ok, len(WIKI_FILES))
    return ok > 0
