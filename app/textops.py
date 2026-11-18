"""Matn va formatlash (entity) ustida amallar.

Telegram entity offsetlari UTF-16 birliklarida hisoblanadi, shuning uchun barcha
kesish/qo'shish amallari "surrogate" ko'rinishidagi matn ustida bajariladi.
"""
from __future__ import annotations

import copy
import re

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


MESSAGE_LINK = re.compile(r"t\.me/(?:c/(\d+)|([A-Za-z][A-Za-z0-9_]{3,}))/(?:\d+/)?(\d+)")


def parse_message_link(link: str):
    """t.me/kanal/123 yoki t.me/c/1234567890/123 -> (kanal, xabar id) yoki None."""
    m = MESSAGE_LINK.search(link or "")
    if not m:
        return None
    private_id, username, msg_id = m.groups()
    return (int("-100" + private_id) if private_id else username), int(msg_id)


def message_to_template(text: str, entities: list | None) -> str:
    """Telegram xabarini shablon HTML'iga aylantiradi: havolalar, qalin matn va premium emojilar saqlanadi."""
    return html.unparse(text or "", entities or []).strip()


def remove_lines(text: str, entities: list | None, a: int, b: int):
    """[a, b) qatorlarini olib tashlaydi; formatlash (entity) offsetlari moslanadi."""
    lines = text.split("\n")
    if not 0 <= a < b <= len(lines):
        return text, [copy.copy(e) for e in (entities or [])]
    start = utf16_len("\n".join(lines[:a]) + ("\n" if a else ""))
    removed = utf16_len("\n".join(lines[a:b]) + ("\n" if b < len(lines) else ""))
    stop = start + removed
    out = []
    for e in entities or []:
        e = copy.copy(e)
        end = e.offset + e.length
        if end <= start:
            out.append(e)
        elif e.offset >= stop:
            e.offset -= removed
            out.append(e)
        else:  # olib tashlangan qism bilan kesishadi
            keep = max(0, min(end, start) - e.offset) + max(0, end - max(e.offset, stop))
            if keep > 0:
                e.offset = min(e.offset, start)
                e.length = keep
                out.append(e)
    return "\n".join(lines[:a] + lines[b:]), out


def build_post(text: str, entities: list | None, cta_start_line: int | None, template_html: str):
    """Manba matnidan filial uchun tayyor matn: eski aloqa bloki kesiladi, shablon qo'yiladi.

    Aloqa blokidan keyin kelgan oddiy qatorlar (masalan, tahrirda qo'shilgan chegirma eslatmasi)
    yo'qolmaydi: ular filial blokidan oldinga o'tkaziladi.
    """
    text = text or ""
    lines = text.split("\n")
    if cta_start_line is not None and 0 < cta_start_line < len(lines):
        from .rules import trailing_notes

        notes = trailing_notes(lines, cta_start_line)
        if notes:
            ns, ne = notes
            text, entities = remove_lines(text, entities, cta_start_line, ns)
            cta_start_line += ne - ns
    body, ents = cut_from_line(text, entities, cta_start_line)
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


# ---------- O'zbek kirill → lotin (rasmiy alifbo) ----------

_CYR = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "ж": "j", "з": "z", "и": "i", "й": "y", "к": "k",
    "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
    "х": "x", "ч": "ch", "ш": "sh", "щ": "sh", "ъ": "\u02bc", "ы": "i", "ь": "", "э": "e", "ю": "yu",
    "я": "ya", "ё": "yo", "ў": "o\u02bb", "қ": "q", "ғ": "g\u02bb", "ҳ": "h",
}
_VOWELS = set("аеёиоуэюяў")
# 2026-yil oktabrdagi takomillashtirilgan alifbo: oʻ→ö, gʻ→ğ, sh→ş, ch→ç
_NEW = {"ў": "ö", "ғ": "ğ", "ш": "ş", "щ": "ş", "ч": "ç"}
_KEEP = ("MessageEntityCode", "MessageEntityPre", "MessageEntityUrl", "MessageEntityEmail", "MessageEntityMention")


def _is_upper_word(s: str, i: int) -> bool:
    """Harf butunlay katta harfli so'z ichidami (ШАЙХ → SHAYX, Шайх → Shayx)."""
    for j in (i + 1, i - 1):
        if 0 <= j < len(s) and s[j].isalpha():
            return s[j].isupper()
    return False


def _latin_char(s: str, i: int, style: str = "new") -> str:
    ch = s[i]
    low = ch.lower()
    if style == "new" and low in _NEW:
        rep = _NEW[low]
        return rep.upper() if ch != low else rep
    if low == "е":
        prev = s[i - 1].lower() if i else ""
        rep = "ye" if (not prev.isalpha() or prev in _VOWELS or prev in "ъь") else "e"
    elif low == "ц":
        prev = s[i - 1].lower() if i else ""
        rep = "ts" if prev in _VOWELS else "s"
    elif low in _CYR:
        rep = _CYR[low]
    else:
        return ch
    if ch != low and rep:  # katta harf
        if _is_upper_word(s, i):
            return rep.upper()
        return rep[0].upper() + rep[1:]
    return rep


def to_latin(text: str, entities: list | None, style: str = "new"):
    """Matnni lotin yozuviga o'giradi; formatlash (entity) offsetlari moslanadi.

    style="new" — takomillashtirilgan alifbo (ö, ğ, ş, ç); "old" — oʻ, gʻ, sh, ch.

    Havola, kod, @username kabi qismlarga tegilmaydi. Lotin matn o'zgarmaydi.
    """
    s = add_surrogate(text or "")
    keep = set()
    for e in entities or []:
        if type(e).__name__ in _KEEP:
            keep.update(range(e.offset, e.offset + e.length))
    out, pos, mp = [], 0, [0] * (len(s) + 1)
    for i in range(len(s)):
        mp[i] = pos
        piece = s[i] if i in keep else _latin_char(s, i, style)
        out.append(piece)
        pos += len(piece)
    mp[len(s)] = pos
    new = []
    for e in entities or []:
        e = copy.copy(e)
        start, end = mp[min(e.offset, len(s))], mp[min(e.offset + e.length, len(s))]
        e.offset, e.length = start, end - start
        if e.length > 0:
            new.append(e)
    return del_surrogate("".join(out)), new
