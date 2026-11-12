"""Matn va formatlash (entity) ustida amallar.

Telegram entity offsetlari UTF-16 birliklarida hisoblanadi, shuning uchun barcha
kesish/qo'shish amallari "surrogate" ko'rinishidagi matn ustida bajariladi.
"""
from __future__ import annotations

import copy

from telethon.extensions import html
from telethon.helpers import add_surrogate, del_surrogate

CAPTION_LIMIT = 1024
CAPTION_LIMIT_PREMIUM = 2048
TEXT_LIMIT = 4096


def utf16_len(text: str) -> int:
    return len(add_surrogate(text))


def numbered_lines(text: str) -> str:
    """AI uchun: har bir qator oldiga tartib raqami qo'yilgan matn."""
    return "\n".join(f"{i}: {line}" for i, line in enumerate(text.split("\n")))


def cut_from_line(text: str, entities: list | None, line_idx: int | None):
    """`line_idx`-qatordan boshlab oxirigacha kesib tashlaydi.

    Qaytaradi: (yangi matn, yangi entitylar). line_idx None yoki chegaradan
    tashqarida bo'lsa, matn o'zgarmaydi.
    """
    entities = [copy.copy(e) for e in (entities or [])]
    lines = text.split("\n")
    if line_idx is None or line_idx <= 0 or line_idx >= len(lines):
        return text.rstrip(), _trim(entities, utf16_len(text.rstrip()))
    kept = "\n".join(lines[:line_idx]).rstrip()
    return kept, _trim(entities, utf16_len(kept))


def _trim(entities: list, limit: int) -> list:
    out = []
    for e in entities:
        if e.offset >= limit:
            continue
        if e.offset + e.length > limit:
            e.length = limit - e.offset
        if e.length > 0:
            out.append(e)
    return out


def append_template(text: str, entities: list | None, template_html: str):
    """Matn oxiriga filialning aloqa blokini (HTML shablon) qo'shadi."""
    entities = [copy.copy(e) for e in (entities or [])]
    template_html = (template_html or "").strip()
    if not template_html:
        return text, entities
    tpl_text, tpl_entities = html.parse(template_html)
    prefix = text + "\n\n" if text else ""
    shift = utf16_len(prefix)
    for e in tpl_entities:
        e.offset += shift
    return prefix + tpl_text, entities + list(tpl_entities)


def build_post(text: str, entities: list | None, cta_start_line: int | None, template_html: str):
    """Manba matnidan filial uchun tayyor matn: eski aloqa bloki kesiladi, shablon qo'yiladi."""
    body, ents = cut_from_line(text or "", entities, cta_start_line)
    return append_template(body, ents, template_html)


def fits(text: str, has_media: bool, premium: bool) -> bool:
    if not has_media:
        return utf16_len(text) <= TEXT_LIMIT
    return utf16_len(text) <= (CAPTION_LIMIT_PREMIUM if premium else CAPTION_LIMIT)


__all__ = [
    "add_surrogate",
    "del_surrogate",
    "utf16_len",
    "numbered_lines",
    "cut_from_line",
    "append_template",
    "build_post",
    "fits",
]
