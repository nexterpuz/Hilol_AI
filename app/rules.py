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
STORE_WORDS = (
    "дўкон", "дукон", "do'kon", "do‘kon", "dokon", "филиал", "filial",
    # ish tartibiga oid so'zlar: "Марғилонда бугун ишламаймиз" kabi e'lonlar
    "ишламай", "ёпиқ", "очилди", "очилиш", "иш вақти", "иш тартиби", "кўчди",
    "ishlamay", "yopiq", "ochildi", "ochilish", "ish vaqti", "ish tartibi", "ko'chdi",
)
HEAD_STORE = ("бош дўкон", "бош дукон", "bosh do'kon", "bosh do‘kon", "bosh dokon")


APOSTROPHES = str.maketrans({c: "'" for c in "ʻʼ‘’`´"})


def norm(text: str) -> str:
    """Kichik harf va barcha apostrof turlari bitta "'" ga: Qoʻqon = Qo‘qon = Qo'qon."""
    return (text or "").lower().translate(APOSTROPHES)


def local_groups(body: str) -> tuple[set[int], bool]:
    """Postda qaysi shaharlarning do'koni tilga olingan va bosh do'kon tilga olinganmi.

    Shahar faqat do'kon/filial so'zi bilan bitta qatorda kelsa hisobga olinadi: kitob
    tavsifidagi "Toshkent" yoki "Buxoro" kabi so'zlar noto'g'ri ishlab ketmasligi uchun.
    """
    groups, head = set(), False
    for line in norm(body).split("\n"):
        if any(h in line for h in HEAD_STORE):
            head = True
        if any(w in line for w in STORE_WORDS):
            groups |= {i for i, names in enumerate(CITY_GROUPS) if any(n in line for n in names)}
    return groups, head


def target_groups(target) -> set[int]:
    """Filial kanali qaysi shahar(lar)ga tegishli: nomi va kalit so'zlari bo'yicha."""
    own = norm(f"{getattr(target, 'name', '') or ''}, {target.keywords or ''}")
    return {i for i, names in enumerate(CITY_GROUPS) if any(n in own for n in names)}


DISCOUNT_WORDS = ("чегирма", "chegirma", "скидка", "skidka", "аксия", "акция", "aksiya", "arzonlashtirish", "арзонлаштириш")


def is_discount(text: str) -> bool:
    low = norm(text)
    return any(w in low for w in DISCOUNT_WORDS)


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
    """Post oxiridagi bosh kanalga tegishli qism: (tur, boshlanish qatori).

    Aloqa bloki bilan imzo orasida 3 tagacha oddiy qator bo'lishi mumkin (masalan, tahrirda
    qo'shilgan "17 ноябрь куни 10% чегирма" eslatmasi) — blok baribir topiladi.
    """
    lines = text.split("\n")
    n = len(lines)
    i, mention = n - 1, False
    while i > 0 and _kind(lines[i]) in ("blank", "mention"):
        mention = mention or _kind(lines[i]) == "mention"
        i -= 1
    sig_start = i + 1
    j, notes = i, 0
    while j > 0 and _kind(lines[j]) != "contact":
        if _kind(lines[j]) is None:
            notes += 1
            if notes > 3:
                break
        j -= 1
    contact_start = None
    if j > 0 and notes <= 3 and _kind(lines[j]) == "contact":
        t = j
        while t > 0 and _kind(lines[t]) in ("contact", "blank", "mention"):
            if _kind(lines[t]) == "contact":
                contact_start = t
            t -= 1
    if contact_start is not None:
        return "contact", contact_start
    while sig_start < n and not lines[sig_start].strip():
        sig_start += 1
    if sig_start >= n or not mention:
        return "none", None
    return "signature", sig_start


def trailing_notes(lines: list[str], cut: int) -> tuple[int, int] | None:
    """Kesiladigan qismda aloqa blokidan keyin kelgan oddiy qatorlar (saqlanishi kerak): (boshi, oxiri)."""
    kinds = [_kind(x) for x in lines]
    contacts = [k for k in range(cut, len(lines)) if kinds[k] == "contact"]
    if not contacts:
        return None
    notes = [k for k in range(contacts[-1] + 1, len(lines)) if kinds[k] is None]
    return (notes[0], notes[-1] + 1) if notes else None


def analyze(text: str, targets: list) -> Analysis:
    block_type, start = find_block(text)
    body = "\n".join(text.split("\n")[:start] if start else text.split("\n")).lower()
    # Qolipga mos post — ishonchli; uzun, lekin aloqa bloki ham, imzosi ham yo'q post — shubhali
    base = 0.9 if block_type != "none" or len(text) < 300 or is_discount(text) else 0.5

    def mentioned(t) -> bool:
        # Kalit so'z faqat do'kon/ish tartibi haqidagi qatorda hisobga olinadi:
        # "Имом ат-Термизий жоме масжиди" Termiz filialiga tegishli e'lon emas
        words = [norm(w.strip()) for w in (t.keywords or "").split(",") if w.strip()]
        return any(
            any(w in line for w in words) and any(sw in line for sw in STORE_WORDS)
            for line in norm(body).split("\n")
        )

    cities, head = local_groups(body)
    named = {t.id for t in targets if mentioned(t) or (cities & target_groups(t))}
    is_local = bool(named or cities or head)
    decisions = []
    for t in targets:
        if not is_local:
            decisions.append(TargetDecision(target_id=t.id, send=True, confidence=base, reason="Qolipga mos umumiy post" if base >= 0.8 else "Post qolipga mos emas"))
        elif t.id in named:
            decisions.append(TargetDecision(target_id=t.id, send=True, confidence=base, reason="Post shu filialga tegishli", scope="own"))
        elif cities or named:
            # Boshqa filialning e'loni: bu kanalga chiqmaydi va so'ralmaydi
            decisions.append(TargetDecision(target_id=t.id, send=False, confidence=0.9, reason="Post boshqa filialga tegishli", scope="other"))
        else:
            decisions.append(TargetDecision(target_id=t.id, send=False, confidence=0.6, reason="Post bosh do'konga tegishli", scope="head"))
    return Analysis(block_type=block_type, cta_start_line=start, decisions=decisions)
