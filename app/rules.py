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


# Filial shaharlari: har bir guruh — bitta shaharning turli yozilishi (kirill va lotin)
CITY_GROUPS = (
    ("тошкент", "toshkent", "tashkent"), ("наманган", "namangan"), ("андижон", "andijon", "andijan"),
    ("фарғона", "farg'ona", "farg‘ona", "fargona", "fergana"), ("қўқон", "қуқон", "qo'qon", "qo‘qon", "qoqon", "kokand"),
    ("марғилон", "marg'ilon", "marg‘ilon", "margilon"), ("самарқанд", "samarqand", "samarkand"),
    ("бухоро", "buxoro", "bukhara"), ("термиз", "termiz"), ("нукус", "nukus"), ("урганч", "urganch"),
    ("хива", "xiva"), ("жиззах", "jizzax"), ("гулистон", "guliston"), ("чирчиқ", "chirchiq"),
    ("олмалиқ", "olmaliq"), ("ангрен", "angren"), ("шаҳрисабз", "shahrisabz"), ("денов", "denov"),
    ("асака", "asaka"),
)
STORE_WORDS = ("дўкон", "дукон", "do'kon", "do‘kon", "dokon", "филиал", "filial")
HEAD_STORE = ("бош дўкон", "бош дукон", "bosh do'kon", "bosh do‘kon", "bosh dokon")


def local_groups(body: str) -> tuple[set[int], bool]:
    """Postda qaysi shaharlarning do'koni tilga olingan va bosh do'kon tilga olinganmi.

    Shahar faqat do'kon/filial so'zi bilan bitta qatorda kelsa hisobga olinadi: kitob
    tavsifidagi "Toshkent" yoki "Buxoro" kabi so'zlar noto'g'ri ishlab ketmasligi uchun.
    """
    groups, head = set(), False
    for line in body.lower().split("\n"):
        if any(h in line for h in HEAD_STORE):
            head = True
        if any(w in line for w in STORE_WORDS):
            groups |= {i for i, names in enumerate(CITY_GROUPS) if any(n in line for n in names)}
    return groups, head


def target_groups(target) -> set[int]:
    """Filial kanali qaysi shahar(lar)ga tegishli: nomi va kalit so'zlari bo'yicha."""
    own = f"{getattr(target, 'name', '') or ''}, {target.keywords or ''}".lower()
    return {i for i, names in enumerate(CITY_GROUPS) if any(n in own for n in names)}


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

    cities, head = local_groups(body)
    named = {t.id for t in targets if mentioned(t) or (cities & target_groups(t))}
    is_local = bool(named or cities or head)
    decisions = []
    for t in targets:
        if not is_local:
            decisions.append(TargetDecision(target_id=t.id, send=True, confidence=base, reason="Qolipga mos umumiy post" if base >= 0.8 else "Post qolipga mos emas"))
        elif t.id in named:
            decisions.append(TargetDecision(target_id=t.id, send=True, confidence=base, reason="Postda shu filial tilga olingan"))
        else:
            why = "Postda boshqa filial do'koni tilga olingan" if (cities or named) else "Post bosh do'konga tegishli"
            decisions.append(TargetDecision(target_id=t.id, send=False, confidence=0.6, reason=why))
    return Analysis(block_type=block_type, cta_start_line=start, decisions=decisions)
