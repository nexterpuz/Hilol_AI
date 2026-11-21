"""Rasmdagi yozuvni o'qish (Tesseract, bepul, serverda ishlaydi). Claude AI shart emas."""
from __future__ import annotations

import asyncio
import logging
import shutil

log = logging.getLogger(__name__)
LANGS = "uzb_cyrl+rus"


def available() -> bool:
    return shutil.which("tesseract") is not None


async def image_text(data: bytes, timeout: float = 30) -> str:
    """Rasm baytlaridan matn. Tesseract o'rnatilmagan yoki xato bo'lsa — bo'sh satr."""
    if not data or not available():
        return ""
    try:
        proc = await asyncio.create_subprocess_exec(
            "tesseract", "stdin", "stdout", "-l", LANGS, "--psm", "3",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(data), timeout)
        return out.decode("utf-8", "ignore")
    except Exception:  # vaqt tugashi, buzuq rasm va h.k. — post odatdagidek ishlanadi
        log.exception("OCR")
        return ""
