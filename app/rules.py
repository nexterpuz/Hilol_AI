"""AI ishlamaganda zaxira: postni oddiy qoidalar bilan tahlil qilish.

Hilol Nashr postlari bir xil qolipda yoziladi: mazmun, keyin aloqa bloki yoki yolg'iz
kanal imzosi. Shu qolipga mos postlar ishonchli aniqlanadi; mos kelmaganlari uchun
ishonch past qo'yiladi va qaror adminga qoladi.
"""
from __future__ import annotations

import re

from .ai import Analysis, TargetDecision

# Faqat bosh kanalning O'Z aloqa ma'lumotlari. Boshqa tashkilotlarning telefoni va havolalari
# (masalan, hamkor madrasa e'lonidagi) post mazmuni hisoblanadi va kesilmaydi.
CONTACT_MARKERS = (
    "манзилларимиз", "сотиб олиш учун", "сотиб олсангиз", "сайт орқали", "буюртма",
    "иловаси орқали", "бонус", "manzillarimiz", "sotib olish uchun", "sotib olsangiz",
    "sayt orqali", "buyurtma", "ilovasi orqali", "bonus", "hilol_reception", "hilol.uz",
)
BOSH_PHONES = ("712175999",)  # bosh kanal telefon(lar)i, raqamlar ketma-ketligi


def _bosh_phone(line: str) -> bool:
    digits = re.sub(r"\D", "", line)
    return any(p in digits for p in BOSH_PHONES)
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
# Shaharda o'tadigan tadbir: "Бухородаги ... китоб сайли ўтказилади" — shu shahar filialiga tegishli
EVENT_PLACE_WORDS = (
    "сайл", "ярмарка", "кўргазма", "кургазма", "тадбир", "учрашув", "ўтказилади", "бўлиб ўтади", "кутиб қоламиз",
    "sayl", "yarmarka", "ko'rgazma", "korgazma", "tadbir", "uchrashuv", "o'tkaziladi", "bo'lib o'tadi", "kutib qolamiz",
)
_LOCATIVE = r"(?:даги|деги|да|га|dagi|da|ga)(?![^\W\d_])"
_RESIDENTS = r"(?:лик|lik)\s+(?:китобхон|мухлис|kitobxon|muxlis)"
HEAD_STORE = ("бош дўкон", "бош дукон", "bosh do'kon", "bosh do‘kon", "bosh dokon")


APOSTROPHES = str.maketrans({c: "'" for c in "ʻʼ‘’`´"})


def norm(text: str) -> str:
    """Kichik harf va barcha apostrof turlari bitta "'" ga: Qoʻqon = Qo‘qon = Qo'qon."""
    return (text or "").lower().translate(APOSTROPHES)


# Do'kon nomini jumla tuzilishidan ajratish: "Чорсудаги дўконимиз", "Кўкча дўконимиз",
# "Абу Сахий ва Кўкча дўконларимиз", "Чорсу филиалимиз"
# Faqat "дўконимиз / дўконларимиз / дўкони / филиалимиз / филиали" shakllari: "дўконларимизда",
# "дўконларидан" (barcha do'konlarda sotuvda) kabi umumiy iboralar olinmaydi
_STORE_NOUN = r"(?:дўкон|дукон|do'kon|doʻkon|dokon|филиал|filial)(?:ларимиз|имиз|лари|и|larimiz|imiz|lari|i)?(?![^\W\d_])"
_DAGI = re.compile(r"([^\W\d_]{3,})(?:даги|dagi|деги)\s+" + _STORE_NOUN, re.I)
_BEFORE = re.compile(r"([^\W\d_]{3,}(?:\s+[^\W\d_]{3,})?)(?:\s+(?:ва|va)\s+([^\W\d_]{3,}(?:\s+[^\W\d_]{3,})?))?\s+" + _STORE_NOUN, re.I)
_STORE_STOP = {
    "бошқа", "barcha", "барча", "ушбу", "шу", "бизнинг", "янги", "бош", "асосий", "ҳамма", "қолган", "ҳар", "бир", "бу", "ўша",
    "boshqa", "ushbu", "bizning", "yangi", "bosh", "asosiy", "hamma", "qolgan", "har", "bu", "o'sha", "нашриётнинг", "нашриётининг",
    "hilol", "nashr", "ҳилол", "нашр", "китоб", "kitob", "онлайн", "onlayn", "бугун", "эртага", "bugun", "ertaga", "ва", "va",
    "учун", "uchun", "сиз", "siz", "биз", "biz", "ҳозир", "hozir", "кеча", "kecha",
}


def _lat(word: str) -> str:
    """Solishtirish uchun: kichik harf, lotin (eski alifbo), apostrofsiz."""
    from .textops import to_latin

    return re.sub(r"[ʻʼ'‘’`]", "", to_latin(norm(word), [], "old")[0])


def store_names(body: str) -> set[str]:
    """Postda tilga olingan do'kon nomlari (lotin, kichik harf). Sifatdoshlar ("ишлайдиган") va umumiy so'zlar olinmaydi."""
    names = set()
    for line in norm(body).split("\n"):
        before = len(names)
        found = [m.group(1) for m in _DAGI.finditer(line)]
        for m in _BEFORE.finditer(line):
            found += [g for g in m.groups() if g]
        for phrase in found:
            words = phrase.split()
            # do'kon so'zidan oldingi so'z umumiy so'z, sifatdosh yoki kelishikdagi so'z bo'lsa — bu nom emas
            if not words or words[-1] in _STORE_STOP or re.search(r"(ган|gan|ни|ni|нинг|ning|га|ga|да|da|дан|dan)$", words[-1]):
                continue
            words = [w for w in words if w not in _STORE_STOP]
            names.add(re.sub(r"(dagi|degi)$", "", _lat(" ".join(words[-2:]))))
        if len(names) > before and "," in line:  # qatorda haqiqiy do'kon nomi topilgan bo'lsagina
            # "Кўкча, Абу Саҳий ва Хадрадаги дўконимиз": vergul bilan sanalgan oldingi nomlar ham
            m = re.search(r"\s" + _STORE_NOUN, line)
            for item in re.split(r",|\s(?:ва|va)\s", line[:m.start()] if m else ""):
                words = [w for w in re.findall(r"[^\W\d_][\w'ʻ]*", item) if w not in _STORE_STOP]
                if 1 <= len(words) <= 2 and all(len(w) >= 3 for w in words):
                    names.add(re.sub(r"(dagi|degi)$", "", _lat(" ".join(words))))
    return names


def _sig_words(target) -> set[str]:
    return {w for w in re.split(r"[^\w]+", _lat(f"{getattr(target, 'name', '') or ''}")) if len(w) >= 4} - _NOISE_WORDS


def store_targets(targets: list, names: set[str]) -> set:
    """Har bir do'kon nomi uchun mos kanal(lar). Bir nom bir nechta kanalga mos kelsa ("Чорсу" → Chorsu va Chorsu Xadra),
    nomi aynan shu so'zlardan iborat kanal tanlanadi (Chorsu)."""
    out = set()
    for n in names:
        hits = [t for t in targets if target_matches_store(t, {n})]
        words = {w for w in n.split() if len(w) >= 4}
        exact = [t for t in hits if _sig_words(t) and all(any(_close(a, b) for b in words) for a in _sig_words(t))]
        out |= {t.id for t in (exact if len(hits) > 1 and exact else hits)}
    return out


def target_matches_store(target, names: set[str]) -> bool:
    """Postdagi do'kon nomi shu kanalnikimi: kanal nomi va kalit so'zlari bo'yicha, 1 harf farqi bilan ("Саҳий" ≈ "Saxiy")."""
    keys = [_lat(k.strip()) for k in f"{getattr(target, 'keywords', '') or ''}".split(",") if k.strip()]
    for n in names:
        for k in keys:
            if len(k) >= 4 and len(n) >= 4 and (n == k or n.startswith(k) or k.startswith(n) or k in n.split()):
                return True
    return bool(names) and target_in_list(target, list(names))


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
        event = any(w in line for w in EVENT_PLACE_WORDS)
        for i, names in enumerate(CITY_GROUPS):
            for n in names:
                # Kitob tavsifidagi oddiy "Бухоро" emas: faqat "Бухородаги/Бухорода ... сайл" yoki "Бухоролик китобхонлар"
                if (event and re.search(re.escape(n) + _LOCATIVE, line)) or re.search(re.escape(n) + _RESIDENTS, line):
                    groups.add(i)
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
    if _bosh_phone(stripped) or any(m in low for m in CONTACT_MARKERS):
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
    stores = store_names(body)
    known_city = {_lat(n) for i in cities for n in CITY_GROUPS[i]}
    stores -= {s for s in stores if any(s.startswith(c) for c in known_city if len(c) >= 4)}  # shahar nomlari alohida hisoblanadi
    by_store = store_targets(targets, stores)
    named = {t.id for t in targets if mentioned(t) or (cities & target_groups(t)) or t.id in by_store}
    is_local = bool(named or cities or head or stores)
    decisions = []
    for t in targets:
        if not is_local:
            decisions.append(TargetDecision(target_id=t.id, send=True, confidence=base, reason="Qolipga mos umumiy post" if base >= 0.8 else "Post qolipga mos emas"))
        elif t.id in named:
            decisions.append(TargetDecision(target_id=t.id, send=True, confidence=base, reason="Post shu filialga tegishli", scope="own"))
        elif cities or named or stores:
            # Boshqa filialning e'loni: bu kanalga chiqmaydi va so'ralmaydi
            decisions.append(TargetDecision(target_id=t.id, send=False, confidence=0.9, reason="Post boshqa filialga tegishli", scope="other"))
        else:
            decisions.append(TargetDecision(target_id=t.id, send=False, confidence=0.6, reason="Post bosh do'konga tegishli", scope="head"))
    return Analysis(block_type=block_type, cta_start_line=start, decisions=decisions)


# ---------- "Bugun quyidagi do'konlarimiz ishlaydi:" ro'yxati ----------

_SCHEDULE_HEAD = re.compile(
    r"(?:қуйидаги|куйидаги|quyidagi)\s+(?:дўкон|дукон|do'kon|dokon|филиал|filial)\w*\s+(?P<verb>ишлайди|ишламайди|дам\s+олади|ishlaydi|ishlamaydi|dam\s+oladi)",
    re.I,
)
_ITEM = re.compile(r"^\s*[-–—•·*]\s*(.+?)\s*[;,.]?\s*$")
_NOISE_WORDS = {"hilol", "nashr", "filial", "filiali", "kanal", "kanali", "test", "dokon", "dokoni", "bosh", "shahar", "shahri", "tuman", "tumani"}


def store_schedule(body: str) -> tuple[bool, list[str]] | None:
    """Ish tartibi ro'yxati: (ro'yxatdagilar ishlaydimi, do'kon nomlari) yoki None.

    "Бугун қуйидаги дўконларимиз ишлайди: – Хадра; – Кўкча; ..." — ro'yxatdagilar ochiq, qolganlari dam oladi.
    "... ишламайди / дам олади:" — aksincha. Ro'yxatdan keyingi izohlar ("* Навоий ... душанба куни") olinmaydi.
    """
    lines = norm(body).split("\n")
    for i, line in enumerate(lines):
        m = _SCHEDULE_HEAD.search(line)
        if not m:
            continue
        items = []
        for nxt in lines[i + 1:]:
            if not nxt.strip():
                if items:
                    break
                continue
            im = _ITEM.match(nxt)
            if not im or nxt.lstrip().startswith("*"):
                break
            items.append(im.group(1).strip(" ;,."))
        if len(items) >= 2:
            return m.group("verb").startswith(("ишлайди", "ishlaydi")), items
    return None


def image_schedule(ocr_text: str) -> tuple[bool, list[str]] | None:
    """Rasmdan o'qilgan matndagi ish tartibi: sarlavha bir necha qatorga bo'lingan, ro'yxat raqamli va xarita yozuvlari bilan.

    Sarlavhadan keyingi barcha qatorlar ("Бошқа дўконларимиз ..." gacha) nomzod sifatida qaytadi; kanal nomi bilan
    solishtirish target_in_list() da. Kamida 2 ta qator bo'lmasa — None.
    """
    low = norm(ocr_text)
    m = _SCHEDULE_HEAD.search(re.sub(r"\s+", " ", low))
    if not m:
        return None
    flat = re.sub(r"\s+", " ", low)
    rest = flat[m.end():]
    stop = re.search(r"(?:бошқа|бошка|boshqa)\s+(?:дўкон|дукон|do'kon|dokon)", rest)
    if stop:
        rest = rest[:stop.start()]
    items = [w for w in re.split(r"[^\w'ʻ]+", rest) if len(w) >= 4]
    # "Абу Сахий" kabi ikki so'zli nomlar uchun qo'shni juftliklar ham
    items += [f"{a} {b}" for a, b in zip(items, items[1:])]
    if len(items) < 2:
        return None
    return m.group("verb").startswith(("ишлайди", "ishlaydi")), items


def _close(a: str, b: str) -> bool:
    """Bir xil do'kon nomi: teng, biri ikkinchisining boshi yoki 1 harf farq ("Сергели" ≈ "Sergili")."""
    if a == b or (min(len(a), len(b)) >= 4 and (a.startswith(b) or b.startswith(a))):
        return True
    if min(len(a), len(b)) < 5 or abs(len(a) - len(b)) > 1:
        return False
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1] <= 1


def target_in_list(target, items: list[str]) -> bool:
    """Filial kanali ro'yxatdagi do'konlardan biriga mosmi (kanal nomi va kalit so'zlari bo'yicha)."""
    own = {w for w in re.split(r"[^\w]+", _lat(f"{getattr(target, 'name', '') or ''} {getattr(target, 'keywords', '') or ''}")) if len(w) >= 4} - _NOISE_WORDS
    for item in items:
        words = {w for w in re.split(r"[^\w]+", _lat(item)) if len(w) >= 4} - _NOISE_WORDS
        if any(_close(a, b) for a in words for b in own):
            return True
    return False
