"""AI ishlamaganda zaxira: postni oddiy qoidalar bilan tahlil qilish.

Hilol Nashr postlari bir xil qolipda yoziladi: mazmun, keyin aloqa bloki yoki yolg'iz
kanal imzosi. Shu qolipga mos postlar ishonchli aniqlanadi; mos kelmaganlari uchun
ishonch past qo'yiladi va qaror adminga qoladi.
"""
from __future__ import annotations

import re

from .ai import Analysis, TargetDecision

CONTACT_MARKERS = (
    "манзилларимиз", "манзил", "сотиб олиш учун", "сотиб олсангиз", "сайт орқали", "буюртма",
    "иловаси орқали", "бонус", "manzillarimiz", "manzil", "sotib olish uchun", "sotib olsangiz",
    "sayt orqali", "buyurtma", "ilovasi orqali", "bonus",
)
PHONE = re.compile(r"\+?\s?998[\d\s\-()]{9,}")
URL = re.compile(r"https?://|t\.me/|www\.", re.I)
MENTION_ONLY = re.compile(r"^[\W_]*(@\w+[\W_]*)+$")


def _kind(line: str) -> str | None:
    """Qator turi: 'blank', 'mention' (faqat @username), 'contact' yoki None (oddiy mazmun)."""
    stripped = line.strip()
    if not stripped:
        return "blank"
    if MENTION_ONLY.match(stripped):
        return "mention"
    low = stripped.lower()
    if PHONE.search(stripped) or URL.search(stripped) or any(m in low for m in CONTACT_MARKERS):
        return "contact"
    return None


def find_block(text: str) -> tuple[str, int | None]:
    """Post oxiridagi bosh kanalga tegishli qism: (tur, boshlanish qatori)."""
    lines = text.split("\n")
    start, kinds = len(lines), []
    for i in range(len(lines) - 1, 0, -1):  # 0-qator har doim mazmun
        kind = _kind(lines[i])
        if kind is None:
            break
        start = i
        kinds.append(kind)
    while start < len(lines) and not lines[start].strip():
        start += 1  # blok bo'sh qatordan boshlanmaydi
    if start >= len(lines):
        return "none", None
    if "contact" in kinds:
        return "contact", start
    if "mention" in kinds:
        return "signature", start
    return "none", None


def analyze(text: str, targets: list) -> Analysis:
    block_type, start = find_block(text)
    body = "\n".join(text.split("\n")[:start] if start else text.split("\n")).lower()
    # Qolipga mos post — ishonchli; uzun, lekin aloqa bloki ham, imzosi ham yo'q post — shubhali
    base = 0.9 if block_type != "none" or len(text) < 300 else 0.5

    def mentioned(t) -> bool:
        words = [w.strip().lower() for w in (t.keywords or "").split(",") if w.strip()]
        return any(w in body for w in words)

    named = {t.id for t in targets if mentioned(t)}
    decisions = []
    for t in targets:
        if not named:
            decisions.append(TargetDecision(target_id=t.id, send=True, confidence=base, reason="Qolipga mos umumiy post" if base >= 0.8 else "Post qolipga mos emas"))
        elif t.id in named:
            decisions.append(TargetDecision(target_id=t.id, send=True, confidence=base, reason="Postda shu filial tilga olingan"))
        else:
            decisions.append(TargetDecision(target_id=t.id, send=False, confidence=0.6, reason="Postda boshqa filial tilga olingan"))
    return Analysis(block_type=block_type, cta_start_line=start, decisions=decisions)
