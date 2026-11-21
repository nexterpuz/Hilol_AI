"""Asosiy oqim: yangi post -> tahlil -> joylash; tahrir va o'chirishni sinxronlash."""
from __future__ import annotations

import hashlib
import logging
import re
from difflib import SequenceMatcher

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from telethon import TelegramClient
from telethon.tl.types import MessageMediaWebPage

from . import ai, ocr, rules as fallback
from .config import settings
from .db import Decision, Delivery, Rule, Seen, Setting, Session, SourcePost, Target, log as db_log, now
from .textops import append_template, build_post, fits, replace_handle, to_latin, message_to_template, parse_message_link

log = logging.getLogger(__name__)


def decide(mode: str, decision, threshold: float) -> tuple[str, str]:
    """Kanal rejimi va AI qaroriga ko'ra: ("send" | "skip" | "ask", sabab)."""
    if mode == "auto":
        return "send", "Avto rejim"
    if mode == "manual":
        return "ask", "Qo'lda rejim: tasdiq kerak"
    if decision is None:
        return "ask", "AI tahlil qila olmadi"
    if decision.confidence < threshold:
        return "ask", f"AI ishonchi past ({decision.confidence:.0%}): {decision.reason}"
    if decision.send:
        return "send", decision.reason
    return "skip", decision.reason


def closing_for(target, block_type: str | None) -> str:
    """Post oxiriga qo'yiladigan qism: sotuv postida to'liq aloqa bloki, boshqalarida qisqa imzo."""
    if block_type in (None, "contact"):  # None — bu maydon qo'shilishidan oldingi postlar
        return target.template_html
    return target.signature_html or f"@{target.username}"


# Shu so'zlar uchragan post avtomatik chiqmaydi, admindan so'raladi (panelda o'zgartiriladi)
# Ta'ziya va shunga o'xshash postlar: filiallarga chiqmaydi, so'ralmaydi (to'liq nusxa kanaliga chiqadi).
# Panel ro'yxatidan mustaqil, doimiy qoida.
CONDOLENCE_WORDS = (
    "таъзия, ta'ziya, taʼziya, ta’ziya, вафоти муносабати, vafoti munosabati, "
    "инна лиллаҳи, иннаа лиллааҳи, inna lillahi, innaa lillaahi"
)
DEFAULT_ASK_WORDS = ""  # admin o'zi qo'shadi (panel: Qoidalar)
# Faqat bosh kanal uchun postlar (vakansiya va h.k.): filiallarga chiqmaydi va hech kimdan so'ralmaydi
DEFAULT_SKIP_WORDS = (
    "вакансия, vakansiya, ишга қабул, ishga qabul, ишга таклиф, ishga taklif, ариза юбориш, ariza yuborish, "
    "бўлиб ўтди, bo'lib o'tdi, якунига етди, yakuniga yetdi"
)


# Filial tasdig'i yoqilgan kanalda shu so'zlar uchragan post menejer tasdig'ini kutadi:
# yangi nashr hali filialga yetib kelmagan bo'lishi mumkin. Oddiy sotuv postlari kutmaydi.
DEFAULT_BRANCH_WORDS = "тез кунда, tez kunda, нашрдан чиқди, нашр қилинди, nashrdan chiqdi, nashr qilindi"


# Suhbat, tadbir, jonli efir: har bir filial menejeri o'z kanaliga joylash-joylamaslikni o'zi hal qiladi
DEFAULT_EVENT_WORDS = (
    "суҳбат, suhbat, жонли, jonli, эфир, efir, "
    "тадбир, tadbir, учрашув, uchrashuv, ташриф буюринг, tashrif buyuring, тақдимот, taqdimot"
)


def ask_word_in(text: str, words: str) -> str:
    """Matnda tasdiq talab qiladigan so'z bormi: topilgan so'z yoki bo'sh satr."""
    low = (text or "").lower()
    return next((w for w in (x.strip().lower() for x in words.split(",")) if w and w in low), "")


def phone_key(phone: str) -> str:
    """Telefon raqamni solishtirish uchun kalit: oxirgi 9 ta raqam (+998 va belgilarsiz)."""
    digits = re.sub(r"\D", "", phone or "")
    return digits[-9:] if len(digits) >= 9 else ""


def phone_keys(phones: str) -> set[str]:
    return {k for k in (phone_key(x) for x in re.split(r"[,;\n]", phones or "")) if k}


async def reset_stuck() -> None:
    """Ishga tushganda: yuborish paytida uzilib qolgan nusxalarni navbatga qaytaradi."""
    async with Session() as s:
        await s.execute(
            update(Delivery).where(Delivery.status == "sending")
            .values(status="failed", reason="Yuborish paytida jarayon uzildi — kanalni tekshirib, kerak bo'lsa qayta yuboring")
        )
        await s.commit()


def parse_ids(raw: str) -> list[int]:
    """Vergul yoki bo'sh joy bilan ajratilgan Telegram ID raqamlari."""
    return list(dict.fromkeys(int(x) for x in re.split(r"[\s,;]+", raw or "") if x.isdigit()))


async def load_admin_ids() -> None:
    """Paneldan saqlangan bosh adminlar ro'yxati bo'lsa, muhit o'zgaruvchisidagi (ADMIN_IDS) o'rniga shuni oladi."""
    async with Session() as s:
        row = await s.get(Setting, "admin_ids")
    ids = parse_ids(row.value) if row else []
    if ids:
        settings.admin_ids[:] = ids


async def save_admin_ids(ids: list[int]) -> None:
    async with Session() as s:
        row = await s.get(Setting, "admin_ids")
        value = ", ".join(str(i) for i in ids)
        if row is not None:
            row.value = value
        else:
            s.add(Setting(key="admin_ids", value=value))
        await s.commit()
    settings.admin_ids[:] = ids  # ro'yxat joyida yangilanadi: bot darhol yangi adminlarni taniydi
    await db_log("admins_changed", value)


async def is_paused() -> bool:
    async with Session() as s:
        row = await s.get(Setting, "paused")
        return bool(row and row.value == "1")


async def set_paused(flag: bool) -> None:
    async with Session() as s:
        row = await s.get(Setting, "paused")
        if row is not None:
            row.value = "1" if flag else "0"
        else:
            s.add(Setting(key="paused", value="1" if flag else "0"))
        await s.commit()
    await db_log("paused" if flag else "resumed", "")


_SOON = ("тез кунда", "tez kunda")
_SALE_CONTEXT = ("сотув", "sotuv", "филиал", "filial", "дўкон", "do'kon", "doʻkon", "dokon", "дўкон")


def branch_word_in(text: str, words: str) -> str:
    """Filial tasdig'i so'zi. "Тез кунда" faqat sotuv/filial/do'kon haqidagi qatorda hisoblanadi:
    "Тез кунда барча филиалларимизда сотувда" — ha; "Тез кунда иккита китоб нашр қилинади" — yo'q."""
    for line in (text or "").lower().split("\n"):
        w = ask_word_in(line, words)
        if not w:
            continue
        if any(w.startswith(soon) for soon in _SOON) and not any(c in line for c in _SALE_CONTEXT):
            continue
        return w
    return ""


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def similar(a: str, b: str) -> bool:
    """Ikki post matni deyarli bir xilmi (masalan, har hafta takrorlanadigan tabrik)."""
    a, b = normalize(a), normalize(b)
    if not a or not b:
        return False
    return a == b or SequenceMatcher(None, a, b).ratio() >= 0.9


def final_parts(post, target, delivery=None) -> tuple[int | None, str]:
    """Kesish qatori va qo'yiladigan yakun: admin navbatda tuzatgan bo'lsa — o'sha, aks holda tahlil natijasi."""
    if getattr(target, "mirror", False):
        return None, ""  # to'liq nusxa: hech narsa kesilmaydi va qo'shilmaydi
    cut, kind = post.cta_start_line, post.block_type
    if delivery is not None and delivery.cut_line is not None:
        cut = None if delivery.cut_line < 0 else delivery.cut_line
    if delivery is not None and delivery.closing:
        kind = delivery.closing
    closing = "" if (delivery is not None and delivery.closing == "none") else closing_for(target, kind)
    return cut, closing


SOURCE_HANDLE = "hilolnashr"  # bosh kanal username'i (to'liq nusxada almashtiriladi)


def finish_text(text: str, ents: list | None, target, source_handle: str = SOURCE_HANDLE):
    """Kanalga xos oxirgi ishlov: to'liq nusxada @hilolnashr → kanal imzosi, keyin kerak bo'lsa lotin yozuvi."""
    if getattr(target, "mirror", False) and (getattr(target, "signature_html", "") or "").strip().startswith("@"):
        text, ents = replace_handle(text, ents, source_handle, target.signature_html.strip())
    if getattr(target, "latin", False):
        text, ents = to_latin(text, ents, getattr(target, "latin_style", None) or "new")
    return text, ents


# Ish tartibi posti ("Бугун қуйидаги дўконларимиз ишлайди: ...") o'rniga filial kanallariga chiqadigan matnlar.
# Kirillda saqlanadi; lotin yozuvli kanalda o'z alifbosiga o'giriladi. Panelda (Qoidalar) o'zgartiriladi.
# Do'konlarning ish vaqti har xil: umumiy matnda vaqt ko'rsatilmaydi
DEFAULT_OPEN_TEXT = "📚 Бугун якшанба бўлишига қарамай, дўконимиз очиқ!\n\nСиз азизлар учун хизматдамиз."
# Vodiy do'konlari soat 17:00 gacha ishlaydi
DEFAULT_OPEN_TEXT_VODIY = DEFAULT_OPEN_TEXT + "\n\n🕘 Иш вақтимиз: 09:00–17:00"
VODIY_CITIES = ("наманган", "андижон", "фарғона", "қўқон", "марғилон", "асака")


def is_vodiy(target) -> bool:
    """Kanal Vodiy bo'limidami: bo'lim tanlangan bo'lsa — shu, aks holda nomi/kalit so'zi bo'yicha."""
    region = getattr(target, "region", "") or ""
    if region:
        return region == "vodiy"
    cities = {i for i, names in enumerate(fallback.CITY_GROUPS) if names[0] in VODIY_CITIES}
    return bool(fallback.target_groups(target) & cities)


DEFAULT_CLOSED_TEXT = (
    "📚 Бугун якшанба — дўконимизда дам олиш куни.\n\n"
    "Шундай экан, дам олиш кунингиз янада мазмунли ўтиши учун камида 30 дақиқангизни китоб мутолаасига бағишланг.\n\n"
    "Яқинларингиз билан мароқли дам олинг!"
)


def compose(text: str, ents: list | None, post, target, delivery=None):
    """Kanal uchun matn (lotinga o'girishdan oldin): almashtiruvchi matn yoki kesilgan post + yakun."""
    if delivery is not None and getattr(delivery, "override_text", None) and not getattr(target, "mirror", False):
        closing = "" if delivery.closing == "none" else closing_for(target, "signature")
        return append_template(delivery.override_text, [], closing)
    cut, closing = final_parts(post, target, delivery)
    return build_post(text, ents, cut, closing)


def preview_text(post, target, delivery=None) -> str:
    """Post filial kanalida qanday matn bilan chiqishi (formatlashsiz ko'rinish)."""
    return finish_text(compose(post.text or "", None, post, target, delivery)[0], None, target)[0]


def text_hash(msg) -> str:
    ents = repr([e.to_dict() for e in (msg.entities or [])])
    return hashlib.sha256(((msg.message or "") + ents).encode()).hexdigest()


def media_id(msg) -> int | None:
    """Xabardagi rasm yoki faylning Telegram ID si (almashtirilganini aniqlash uchun)."""
    if isinstance(msg.media, MessageMediaWebPage):
        return None  # havola ko'rinishidagi rasm — xabarning o'z fayli emas
    if msg.photo:
        return msg.photo.id
    if msg.document:
        return msg.document.id
    return None


def is_file(msg) -> bool:
    """Xabarning o'z rasmi/videosi/fayli bormi. Havola ko'rinishi (link preview) rasm hisoblanmaydi:
    Telethon unda ham msg.photo ni qaytaradi, lekin uni fayl sifatida yuborib bo'lmaydi."""
    if isinstance(msg.media, MessageMediaWebPage):
        return False
    return bool(msg.photo or msg.document)


def is_supported(msg) -> bool:
    return is_file(msg) or msg.media is None or isinstance(msg.media, MessageMediaWebPage)


class Pipeline:
    def __init__(self, client: TelegramClient, source, notifier, premium: bool):
        self.client = client
        self.source = source  # manba kanal entity
        self.notifier = notifier  # AdminBot yoki None
        self.premium = premium

    async def check_target(self, username: str) -> str:
        """Agent bu kanalda post joylay oladimi? Muammo bo'lsa matnini qaytaradi."""
        try:
            perms = await self.client.get_permissions(username, "me")
        except Exception as e:
            return f"Kanal tekshirilmadi (@{username}): {e}"
        if not (perms.is_admin and perms.post_messages):
            return f"Agent akkaunti @{username} kanalida post joylash huquqiga ega admin emas."
        return ""

    async def fetch_template(self, link: str) -> tuple[str, str]:
        """Havoladagi Telegram xabarini shablonga aylantiradi. Qaytaradi: (html, xato matni)."""
        parsed = parse_message_link(link)
        if not parsed:
            return "", "Havola noto'g'ri. Xabar havolasi kerak, masalan: https://t.me/kanal/123"
        chat, msg_id = parsed
        try:
            msg = await self.client.get_messages(chat, ids=msg_id)
        except Exception as e:
            return "", f"Xabarni o'qib bo'lmadi: {e}"
        if msg is None or not msg.message:
            return "", "Xabar topilmadi yoki unda matn yo'q."
        return message_to_template(msg.message, msg.entities), ""

    # ---------- yangi post ----------

    async def catch_up(self, limit: int = 30, max_age_hours: int = 12) -> int:
        """Agent o'chiq turgan paytda (masalan, yangilanish vaqtida) bosh kanalga tushgan postlarni qayta ishlaydi.

        Faqat bazadagi oxirgi ma'lum postdan keyingi va yaqinda chiqqan xabarlar olinadi;
        takrorga qarshi qulf tufayli allaqachon ko'chirilgan post ikkinchi marta chiqmaydi.
        """
        from datetime import timedelta

        from telethon import utils

        chat_id = getattr(self, "source_chat_id", None) or utils.get_peer_id(self.source)
        async with Session() as s:
            known = list((await s.execute(select(SourcePost).where(SourcePost.chat_id == chat_id))).scalars())
        key = f"baseline:{chat_id}"
        async with Session() as s:
            row = await s.get(Setting, key)
        baseline = int(row.value) if row and row.value.isdigit() else None
        if not known and baseline is None:
            # Yangi ulangan manba: hozirgi oxirgi post "boshlang'ich nuqta" bo'ladi, eski postlar ko'chirilmaydi
            newest = await self.client.get_messages(self.source, limit=1)
            async with Session() as s:
                s.add(Setting(key=key, value=str(newest[0].id if newest else 0)))
                await s.commit()
            return 0
        last = max([baseline or 0] + [max(p.msg_ids) for p in known if p.msg_ids])
        recent = await self.client.get_messages(self.source, limit=limit, min_id=last)
        cutoff = now() - timedelta(hours=max_age_hours)
        fresh = [m for m in recent if m and m.id > last and (getattr(m, "date", None) is None or m.date >= cutoff)]
        groups: dict = {}
        for m in sorted(fresh, key=lambda m: m.id):
            groups.setdefault(m.grouped_id or f"single-{m.id}", []).append(m)
        for msgs in groups.values():
            try:
                await self.on_new(msgs)
            except Exception:
                log.exception("catch_up")
        if groups:
            await db_log("catch_up", f"{len(groups)} ta o'tkazib yuborilgan post qayta ishlandi")
        return len(groups)

    async def _claim(self, chat_id: int, msg_id: int) -> bool:
        """Xabarni qayta ishlashga "band qiladi". Faqat birinchi chaqiruv True oladi."""
        async with Session() as s:
            s.add(Seen(chat_id=chat_id, msg_id=msg_id))
            try:
                await s.commit()
                return True
            except IntegrityError:
                await s.rollback()
                return False

    @property
    def source_handle(self) -> str:
        return getattr(self.source, "username", None) or SOURCE_HANDLE

    async def _full_album(self, msgs: list) -> list:
        """Telegram albomni ba'zan bo'laklab yetkazadi: bosh kanaldan butun albomni olamiz."""
        gid = msgs[0].grouped_id
        if gid is None:
            return msgs
        lo, hi = min(m.id for m in msgs), max(m.id for m in msgs)
        try:
            around = await self.client.get_messages(self.source, ids=list(range(max(1, lo - 10), hi + 11)))
        except Exception:
            log.warning("Albomni to'liq olib bo'lmadi", exc_info=True)
            return msgs
        full = [m for m in (around or []) if m and m.grouped_id == gid]
        return sorted(full, key=lambda m: m.id) if len(full) >= len(msgs) else msgs

    async def on_new(self, msgs: list) -> None:
        msgs = sorted(await self._full_album(msgs), key=lambda m: m.id)
        ids = [m.id for m in msgs]
        cap = next((m for m in msgs if m.message), msgs[0])
        if not await self._claim(msgs[0].chat_id, ids[0]):
            return  # shu xabar allaqachon (yoki ayni paytda) qayta ishlanmoqda
        async with Session() as s:
            known = (
                await s.execute(select(SourcePost).where(SourcePost.chat_id == msgs[0].chat_id))
            ).scalars()
            if any(set(ids) & set(p.msg_ids) for p in known):
                return  # qayta ishga tushganda takror kelgan xabar
            targets = list((await s.execute(select(Target).where(Target.active))).scalars())
            rules = [r.text for r in (await s.execute(select(Rule).where(Rule.active))).scalars()]

            memory = list((await s.execute(select(Decision).order_by(Decision.id.desc()).limit(300))).scalars())
            row = await s.get(Setting, "ask_words")
            ask_words = row.value if row is not None else DEFAULT_ASK_WORDS
            row = await s.get(Setting, "skip_words")
            skip_words = row.value if row is not None else DEFAULT_SKIP_WORDS
            row = await s.get(Setting, "branch_words")
            branch_words = row.value if row is not None else DEFAULT_BRANCH_WORDS
            row = await s.get(Setting, "event_words")
            event_words = row.value if row is not None else DEFAULT_EVENT_WORDS
            row = await s.get(Setting, "open_text")
            open_text = row.value if row is not None and row.value.strip() else DEFAULT_OPEN_TEXT
            row = await s.get(Setting, "open_text_vodiy")
            open_text_vodiy = row.value if row is not None and row.value.strip() else DEFAULT_OPEN_TEXT_VODIY
            row = await s.get(Setting, "closed_text")
            closed_text = row.value if row is not None and row.value.strip() else DEFAULT_CLOSED_TEXT
            row = await s.get(Setting, "ocr_confirm")
            ocr_confirm = not (row is not None and row.value == "0")  # rasmdan o'qilgan ro'yxat avval adminga ko'rsatiladi
            pause_row = await s.get(Setting, "paused")
            paused = bool(pause_row and pause_row.value == "1")

        text = cap.message or ""
        by_name = {t.id: t.name for t in targets}
        examples = [
            f"- {by_name.get(m.target_id, '?')}: {'yuborilsin' if m.send else 'yuborilmasin'} — {normalize(m.text)[:200]}"
            for m in memory[:15]
        ]
        # Aloqa blokini topish uchun tahlil har qanday rejimda kerak
        analysis = await ai.analyze(text, targets, rules, examples) if text else None
        by_rules = False
        if text and analysis is None:
            await self._log_ai_failure()
            analysis, by_rules = fallback.analyze(text, targets), True  # AI'siz zaxira qoidalar

        # Filialga xos e'lon ("Қўқондаги дўконимиз ишламайди") qoidalar bilan aniq topiladi va
        # AI qaroridan ustun turadi: bunday post faqat o'sha filialga chiqadi.
        local_rules = fallback.analyze(text, targets) if text and not by_rules else None
        # "Бугун қуйидаги дўконларимиз ишлайди: – ...": har bir filialga o'z holati haqida tayyor matn
        schedule = fallback.store_schedule(text) if text else None
        from_image = False
        if schedule is None and len(text) < 400:
            # Ro'yxat matnda emas, rasmda bo'lishi mumkin: rasmni o'qib ko'ramiz (kitob postlarining izohi uzun bo'ladi)
            photo = next((m for m in msgs if m.photo), None)
            if photo is not None:
                schedule = await self._image_schedule(photo)
                from_image = schedule is not None
        opened, closed = [], []

        async with Session() as s:
            post = SourcePost(
                chat_id=msgs[0].chat_id,
                msg_ids=ids,
                grouped_id=msgs[0].grouped_id,
                text=cap.message or "",
                text_hash=text_hash(cap),
                cta_start_line=analysis.cta_start_line if analysis else None,
                block_type=analysis.block_type if analysis else None,
                media_ids={str(m.id): media_id(m) for m in msgs},
            )
            s.add(post)
            await s.flush()
            plan, by_target = [], {}
            for t in targets:
                d = analysis.for_target(t.id) if analysis else None
                if local_rules is not None:
                    rd = local_rules.for_target(t.id)
                    if rd is not None and rd.scope in ("own", "other", "head"):
                        d = rd
                action, reason = decide(t.mode, d, settings.confidence_threshold)
                if by_rules and t.mode != "manual":
                    reason = "AI'siz qoidalar: " + reason.replace("AI ishonchi past", "ishonch past")
                word = ask_word_in(text, ask_words)
                if word and action == "send" and t.mode != "manual":
                    action, reason = "ask", f"Matnda «{word}» so'zi bor: tasdiq kerak"
                if not word and action == "ask" and fallback.is_discount(text) and (d is None or d.scope == "general"):
                    # Chegirma e'loni bitta do'konga tegishli bo'lmasa — barcha filiallarga so'ramasdan
                    action, reason = "send", "Chegirma e'loni"
                quiet = False
                past = next((m for m in memory if m.target_id == t.id and similar(m.text, text)), None)
                if past is not None and t.mode != "manual":
                    # Admin o'xshash post bo'yicha qaror bergan: qayta so'ralmaydi
                    action, reason, quiet = ("send" if past.send else "skip"), "Avvalgi qaroringiz bo'yicha", True
                branch = local = False
                scope = getattr(d, "scope", "general") if d is not None else "general"
                if scope == "other" and t.mode != "manual":
                    # Boshqa filialning e'loni: bu kanalga chiqmaydi va hech kimdan so'ralmaydi.
                    # (O'z filialiga tegishli post — scope "own" — odatdagidek avtomatik chiqadi.)
                    action, reason, quiet = "skip", "Post boshqa filialga tegishli", True
                elif scope == "own" and t.mode != "manual":
                    pass  # aynan shu filialga tegishli e'lon: tadbir so'zi bo'lsa ham so'ramasdan chiqadi
                elif scope == "head" and t.mode != "manual" and t.manager_ids:
                    # Bosh do'kon tadbiri/e'loni: filial kanaliga joylashni filial menejeri hal qiladi
                    action, reason, quiet, branch, local = "ask", "Filial tasdig'i kutilmoqda", False, True, "head"
                elif (t.mode != "manual" and action == "send" and ask_word_in(text, event_words)
                      and not fallback.is_discount(text)):  # umumiy chegirma e'loni tadbir so'zidan ustun
                    if t.manager_ids:
                        # Suhbat / jonli efir / tadbir: filial menejeri tasdiqlasa kanalga chiqadi
                        action, reason, quiet, branch, local = "ask", "Filial tasdig'i kutilmoqda", False, True, "event"
                    else:
                        action, reason = "ask", "Tadbir/efir posti: filial menejeri botga bog'lanmagan"
                elif t.branch_approval and action == "send" and branch_word_in(text, branch_words):
                    # Yangi nashr yoki "tez kunda": kitob filialga kelgach, menejer o'zi tasdiqlaydi
                    action, reason, quiet, branch = "ask", "Filial tasdig'i kutilmoqda", False, True
                skip_word = ask_word_in(text, skip_words) or ask_word_in(text, CONDOLENCE_WORDS)
                if skip_word:
                    # Faqat bosh kanal uchun (masalan, vakansiya): filialga chiqmaydi, so'ralmaydi
                    action, reason, quiet, branch, local = "skip", f"Faqat bosh kanal uchun («{skip_word}»)", True, False, False
                override = None
                if schedule is not None and not t.mirror:
                    listed_open, items = schedule
                    is_open = fallback.target_in_list(t, items) == listed_open
                    override = (open_text_vodiy if is_vodiy(t) else open_text) if is_open else closed_text
                    action, quiet, branch, local = ("ask" if t.mode == "manual" else "send"), True, False, False
                    reason = "Ish tartibi: do'kon ochiq" if is_open else "Ish tartibi: do'kon dam oladi"
                    (opened if is_open else closed).append(t.name)
                    if from_image:
                        reason += " (rasmdan o'qildi)"
                        if ocr_confirm and action == "send":
                            action = "ask"  # bitta umumiy xabar bilan adminga ko'rsatiladi
                if t.mirror:
                    # To'liq nusxa kanali: har bir post filtrsiz va so'rovsiz
                    action, reason, quiet, branch, local = "send", "To'liq nusxa kanali", True, False, False
                if paused and action != "skip":
                    action, reason, quiet, branch, local = "ask", "Favqulodda to'xtatish yoqilgan", True, False, False
                if not all(is_supported(m) for m in msgs):
                    action, reason = "ask", "Post turi avtomatik ko'chirilmaydi (so'rovnoma va h.k.)"
                if action == "ask" and not branch and not quiet and t.manager_ids and t.mode != "manual":
                    # Ikkilangan holat (matnsiz post, ishonch past va h.k.): bosh admin emas, filial menejeri hal qiladi
                    branch, local, reason = True, "review", "Filial tasdig'i kutilmoqda"
                delivery = Delivery(
                    post_id=post.id,
                    target_id=t.id,
                    status="skipped" if action == "skip" else "pending",
                    reason=reason, override_text=override,
                )
                s.add(delivery)
                await s.flush()
                by_target[t.id] = delivery
                plan.append((delivery.id, t, action, reason, quiet, branch, local))
            await s.commit()
            post_id = post.id
            if text:
                views = {t.id: preview_text(post, t, by_target.get(t.id)) for t in targets}
            else:
                kind = "video" if any(m.document for m in msgs) else "rasm" if any(m.photo for m in msgs) else "media"
                views = {t.id: f"(matnsiz post: {kind}{', ' + str(len(msgs)) + ' ta' if len(msgs) > 1 else ''})" for t in targets}

        await db_log("new_post", f"post={post_id} ids={ids}")
        for delivery_id, target, action, reason, quiet, branch, local in plan:
            if action == "send":
                await self.publish(delivery_id)
            elif self.notifier and branch:
                await self.notifier.ask_branch(delivery_id, target, views.get(target.id) or "(matnsiz post)", local=local)
            elif self.notifier and not quiet:
                await self.notifier.ask(delivery_id, target, views.get(target.id) or "(matnsiz post)", reason, skipped=action == "skip")
        if from_image:
            await db_log("ocr_schedule", f"post={post_id} ochiq={len(opened)} dam={len(closed)} tasdiq={'ha' if ocr_confirm else 'yoq'}")
            if ocr_confirm and self.notifier and hasattr(self.notifier, "ask_schedule"):
                await self.notifier.ask_schedule(post_id, opened, closed)

    async def _image_schedule(self, msg):
        """Rasmdagi "Бугун қуйидаги дўконларимиз ишлайди" ro'yxati yoki None."""
        if not ocr.available():
            return None
        try:
            data = await self.client.download_media(msg, file=bytes)
        except Exception:
            log.exception("Rasmni yuklab bo'lmadi")
            return None
        text = await ocr.image_text(data)
        return fallback.image_schedule(text) if text else None

    async def decide_schedule(self, post_id: int, send: bool) -> int:
        """Rasmdan o'qilgan ish tartibi bo'yicha admin qarori: kutayotgan barcha nusxalar yuboriladi yoki rad etiladi."""
        async with Session() as s:
            ids = list((await s.execute(
                select(Delivery.id).where(Delivery.post_id == post_id, Delivery.status == "pending", Delivery.override_text.isnot(None))
            )).scalars())
        done = 0
        for delivery_id in ids:
            if send:
                done += await self.publish(delivery_id)
            else:
                await self.reject(delivery_id)
                done += 1
        await db_log("ocr_schedule_" + ("sent" if send else "rejected"), f"post={post_id} {done} ta")
        return done

    async def _log_ai_failure(self) -> None:
        """AI xatosini jurnalga yozadi; bir xil xato har postda takrorlanmasligi uchun 6 soatda bir marta."""
        key, t = ai.last_error, now()
        last = getattr(self, "_ai_logged", None)
        if last and last[0] == key and (t - last[1]).total_seconds() < 6 * 3600:
            return
        self._ai_logged = (key, t)
        await db_log("ai_failed", key)

    # ---------- joylash ----------

    async def _load(self, delivery_id: int):
        async with Session() as s:
            d = await s.get(Delivery, delivery_id)
            return d, await s.get(SourcePost, d.post_id), await s.get(Target, d.target_id)

    async def _fail(self, delivery_id: int, target: Target, reason: str) -> None:
        async with Session() as s:
            d = await s.get(Delivery, delivery_id)
            d.status, d.reason = "failed", reason
            await s.commit()
        await db_log("failed", f"delivery={delivery_id} {reason}")
        if self.notifier:
            await self.notifier.say(f"Xatolik — {target.name}: {reason}")

    async def publish(self, delivery_id: int) -> bool:
        d, post, target = await self._load(delivery_id)
        if d.status == "sent" or post.deleted:
            return False
        if post.grouped_id is not None:
            async with Session() as s:
                first = (await s.execute(
                    select(SourcePost.id).where(SourcePost.chat_id == post.chat_id, SourcePost.grouped_id == post.grouped_id)
                    .order_by(SourcePost.id)
                )).scalars().first()
                if first != post.id:
                    # Albomning bo'lagi alohida post bo'lib qolgan (eski xato): asosiy post bilan birga yuboriladi
                    await s.execute(
                        update(Delivery).where(Delivery.post_id == post.id, Delivery.status.in_(("pending", "skipped", "failed")))
                        .values(status="rejected", reason="Albom bo'lagi — butun albom asosiy post bilan yuboriladi")
                    )
                    await s.commit()
                    return False
        fetched = await self.client.get_messages(self.source, ids=post.msg_ids)
        msgs = [m for m in fetched if m]
        if post.grouped_id is not None and msgs:
            full = await self._full_album(msgs)
            if len(full) > len(msgs):  # avval bo'lagi saqlangan bo'lsa — endi butun albom
                msgs = full
                async with Session() as s:
                    row = await s.get(SourcePost, post.id)
                    row.msg_ids = [m.id for m in full]
                    row.media_ids = {str(m.id): media_id(m) for m in full}
                    await s.commit()
                post.msg_ids = [m.id for m in full]
        if not msgs:
            await self._fail(delivery_id, target, "Manba post topilmadi")
            return False
        cap = next((m for m in msgs if m.message), msgs[0])
        text, ents = compose(cap.message or "", cap.entities, post, target, d)
        text, ents = finish_text(text, ents, target, self.source_handle)
        replaced = bool(d.override_text) and not target.mirror
        files = [] if replaced else [m for m in msgs if is_file(m)]  # almashtirilgan matn rasmsiz, oddiy xabar
        if not fits(text, bool(files), self.premium):
            await self._fail(delivery_id, target, "Matn Telegram limitidan uzun (Premium kerak yoki qisqartirish)")
            return False
        # Yuborishni "band qilamiz": ikki marta bosilgan tugma yoki parallel chaqiruv ikkinchi nusxa chiqarmaydi
        async with Session() as s:
            claimed = await s.execute(
                update(Delivery).where(Delivery.id == delivery_id, Delivery.status.notin_(("sent", "sending", "deleted")))
                .values(status="sending")
            )
            await s.commit()
        if claimed.rowcount == 0:
            return False
        try:
            if not files:
                sent = await self.client.send_message(
                    target.username, text, formatting_entities=ents,
                    link_preview=isinstance(cap.media, MessageMediaWebPage) and not replaced, silent=target.silent,
                )
            elif len(files) == 1:
                sent = await self.client.send_file(
                    target.username, files[0].media, caption=text,
                    formatting_entities=ents, silent=target.silent,
                )
            else:
                sent = await self.client.send_file(
                    target.username, [m.media for m in files],
                    caption=[text if m.id == cap.id else "" for m in files],
                    formatting_entities=[ents if m.id == cap.id else [] for m in files],
                    silent=target.silent,
                )
        except Exception as e:  # Telegram xatolari turli xil: hammasini adminga yetkazamiz
            log.exception("publish")
            await self._fail(delivery_id, target, f"Telegram: {e}")
            return False
        sent_list = sent if isinstance(sent, list) else [sent]
        by_source = {m.id: sm.id for m, sm in zip(files or [cap], sent_list)}
        async with Session() as s:
            d = await s.get(Delivery, delivery_id)
            d.status, d.sent_at = "sent", now()
            d.target_msg_ids = [by_source.get(i) for i in post.msg_ids]
            # Shu post shu kanal uchun kutib turgan boshqa so'rovlar endi kerak emas
            await s.execute(
                update(Delivery).where(
                    Delivery.post_id == d.post_id, Delivery.target_id == d.target_id, Delivery.id != d.id,
                    Delivery.status.in_(("pending", "skipped", "failed")),
                ).values(status="rejected", reason="Post bu kanalga allaqachon yuborilgan")
            )
            await s.commit()
        await db_log("sent", f"delivery={delivery_id} target={target.username}")
        return True

    async def _remember(self, delivery_id: int, send: bool) -> None:
        """Admin qarorini eslab qoladi: o'xshash post kelganda shu qaror qo'llanadi."""
        async with Session() as s:
            d = await s.get(Delivery, delivery_id)
            post = await s.get(SourcePost, d.post_id) if d else None
            if post is None or not post.text or d.status not in ("pending", "skipped", "failed"):
                return
            if (d.reason or "").startswith(("Filial", "Post boshqa filialga")):
                return  # filialga xos postlar har safar filial menejeridan so'raladi
            s.add(Decision(target_id=d.target_id, text=post.text, send=send))
            await s.commit()

    async def approve(self, delivery_id: int) -> bool:
        """Admin "Yuborish" ni bosdi: qaror eslab qolinadi va post joylanadi."""
        await self._remember(delivery_id, True)
        return await self.publish(delivery_id)

    async def resend(self, post_id: int, target_id: int) -> bool:
        """Admin postni tanlangan kanalga qo'lda (qayta) yuboradi — holatidan qat'i nazar."""
        async with Session() as s:
            d = (await s.execute(
                select(Delivery).where(Delivery.post_id == post_id, Delivery.target_id == target_id).order_by(Delivery.id.desc())
            )).scalars().first()
            if d is None:
                d = Delivery(post_id=post_id, target_id=target_id, status="pending", reason="Admin qo'lda yubordi")
                s.add(d)
            elif d.status == "sending":
                return False
            else:
                d.status, d.reason, d.target_msg_ids = "pending", "Admin qo'lda yubordi", []
            await s.commit()
            delivery_id = d.id
        await db_log("resend", f"post={post_id} target={target_id}")
        return await self.publish(delivery_id)

    async def ask_managers(self, post_id: int, target_id: int | None = None) -> tuple[int, int]:
        """Admin postni barcha filial menejerlariga tasdiqlash uchun yuboradi.

        Post allaqachon chiqqan kanallar o'tkazib yuboriladi. Qaytaradi: (so'ralgan filiallar, menejersiz filiallar).
        """
        async with Session() as s:
            post = await s.get(SourcePost, post_id)
            if post is None or post.deleted:
                return 0, 0
            targets = list((await s.execute(select(Target).where(Target.active).order_by(Target.id))).scalars())
            if target_id:
                targets = [t for t in targets if t.id == target_id]  # faqat tanlangan filial (masalan, sinov uchun)
            sent = set((await s.execute(
                select(Delivery.target_id).where(Delivery.post_id == post_id, Delivery.status == "sent")
            )).scalars())
            plan, without = [], 0
            for t in targets:
                if t.id in sent:
                    continue
                if not t.manager_ids:
                    without += 1
                    continue
                d = (await s.execute(
                    select(Delivery).where(Delivery.post_id == post_id, Delivery.target_id == t.id).order_by(Delivery.id.desc())
                )).scalars().first()
                if d is None or d.status in ("sending", "deleted"):
                    d = Delivery(post_id=post_id, target_id=t.id)
                    s.add(d)
                d.status, d.reason = "pending", "Filial tasdig'i kutilmoqda"
                await s.flush()
                plan.append((d.id, t, preview_text(post, t, d) or "(matnsiz post)"))
            await s.commit()
        if self.notifier:
            for delivery_id, t, text in plan:
                await self.notifier.ask_branch(delivery_id, t, text, local="admin")
        await db_log("ask_managers", f"post={post_id} filiallar={len(plan)}")
        return len(plan), without

    async def retry_failed(self, post_id: int) -> tuple[int, int]:
        """Xato bergan nusxalarni qayta yuboradi. Qaytaradi: (yuborildi, jami urinildi)."""
        async with Session() as s:
            failed = list((await s.execute(
                select(Delivery).where(Delivery.post_id == post_id, Delivery.status == "failed")
            )).scalars())
        ok = 0
        for d in failed:
            ok += await self.resend(post_id, d.target_id)
        return ok, len(failed)

    async def republish(self, post_id: int) -> tuple[int, int]:
        """Postni chiqqan barcha kanallardan olib, qaytadan (to'liq holda) yuboradi."""
        async with Session() as s:
            targets = list(set((await s.execute(
                select(Delivery.target_id).where(Delivery.post_id == post_id, Delivery.status == "sent")
            )).scalars()))
        if not targets:
            return 0, 0
        await self.recall(post_id)
        ok = 0
        for tid in targets:
            ok += await self.resend(post_id, tid)
        return ok, len(targets)

    async def recall(self, post_id: int) -> int:
        """Postni barcha filial kanallaridan o'chiradi (bosh kanalga tegmaydi). Qaytaradi: nechta kanaldan."""
        removed = 0
        async with Session() as s:
            deliveries = list((await s.execute(select(Delivery).where(Delivery.post_id == post_id, Delivery.status == "sent"))).scalars())
            targets = {t.id: t for t in (await s.execute(select(Target))).scalars()}
            for d in deliveries:
                ids = [i for i in d.target_msg_ids if i]
                target = targets.get(d.target_id)
                if not ids or target is None:
                    continue
                try:
                    await self.client.delete_messages(target.username, ids)
                except Exception as e:
                    log.exception("recall")
                    if self.notifier:
                        await self.notifier.say(f"Qaytarib olinmadi — {target.name}: {e}")
                    continue
                d.status, d.reason, d.target_msg_ids = "deleted", "Admin qaytarib oldi", [None] * len(d.target_msg_ids)
                removed += 1
            # kutayotgan nusxalar ham bekor qilinadi
            waiting = (await s.execute(select(Delivery).where(Delivery.post_id == post_id, Delivery.status.in_(("pending", "skipped"))))).scalars()
            for d in waiting:
                d.status, d.reason = "rejected", "Admin qaytarib oldi"
            await s.commit()
        await db_log("recalled", f"post={post_id} removed_from={removed}")
        return removed

    async def adjust(self, delivery_id: int, cut_line: int, closing: str) -> None:
        """Admin navbatda kesish joyi va yakun turini qo'lda belgilaydi."""
        async with Session() as s:
            d = await s.get(Delivery, delivery_id)
            if d and d.status in ("pending", "skipped", "failed"):
                d.cut_line = cut_line
                d.closing = closing if closing in ("contact", "signature", "none") else None
                await s.commit()

    async def reject(self, delivery_id: int, remember: bool = True) -> None:
        if remember:
            await self._remember(delivery_id, False)
        async with Session() as s:
            d = await s.get(Delivery, delivery_id)
            if d and d.status in ("pending", "skipped", "failed"):
                d.status = "rejected"
                await s.commit()

    # ---------- tahrir ----------

    async def on_edit(self, msg) -> None:
        async with Session() as s:
            posts = (await s.execute(select(SourcePost).where(SourcePost.chat_id == msg.chat_id))).scalars()
            post = next((p for p in posts if msg.id in p.msg_ids and not p.deleted), None)
            if post is None:
                return
            new_hash = text_hash(msg)
            is_caption_msg = bool(msg.message) or len(post.msg_ids) == 1
            text_changed = is_caption_msg and new_hash != post.text_hash
            old_media, new_media = (post.media_ids or {}).get(str(msg.id)), media_id(msg)
            # Eski postlarda media ID saqlanmagan: ularda media almashganini aniqlab bo'lmaydi
            media_changed = old_media is not None and new_media is not None and old_media != new_media
            if not text_changed and not media_changed:
                return  # ko'rishlar/reaksiya o'zgarishi
            targets = list((await s.execute(select(Target))).scalars())
            if text_changed:
                rules = [r.text for r in (await s.execute(select(Rule).where(Rule.active))).scalars()]
                active = [t for t in targets if t.active]
                analysis = await ai.analyze(msg.message or "", active, rules) if msg.message else None
                if msg.message and analysis is None:
                    analysis = fallback.analyze(msg.message, active)
                post.text, post.text_hash = msg.message or "", new_hash
                post.cta_start_line = analysis.cta_start_line if analysis else None
                post.block_type = analysis.block_type if analysis else None
            if media_changed:
                post.media_ids = {**(post.media_ids or {}), str(msg.id): new_media}
            deliveries = list(
                (await s.execute(select(Delivery).where(Delivery.post_id == post.id, Delivery.status == "sent"))).scalars()
            )
            await s.commit()
            by_id = {t.id: t for t in targets}
            idx = post.msg_ids.index(msg.id)

        updated = 0
        for d in deliveries:
            target = by_id[d.target_id]
            if not target.sync_edits or idx >= len(d.target_msg_ids) or not d.target_msg_ids[idx]:
                continue
            if d.override_text and not target.mirror:
                continue  # kanalda asl post emas, tayyor matn turibdi: manbadagi tahrir unga taalluqli emas
            if is_caption_msg:
                if target.mirror:
                    text, ents = build_post(msg.message or "", msg.entities, None, "")
                else:
                    text, ents = build_post(msg.message or "", msg.entities, post.cta_start_line, closing_for(target, post.block_type))
                text, ents = finish_text(text, ents, target, self.source_handle)
            else:
                text, ents = "", []  # albomning izohsiz elementi
            if not fits(text, is_file(msg), self.premium):
                await self._fail_edit(target, "tahrirdan keyin matn limitdan uzun")
                continue
            try:
                await self.client.edit_message(
                    target.username, d.target_msg_ids[idx], text, formatting_entities=ents,
                    file=msg.media if media_changed else None,
                )
                updated += 1
            except Exception as e:
                log.exception("edit")
                await self._fail_edit(target, str(e))
        what = "matn va media" if text_changed and media_changed else "media" if media_changed else "matn"
        await db_log("edited", f"post={post.id} ({what}) updated={updated}")
        if self.notifier and updated:
            await self.notifier.say(f"Manba postda {what} tahrirlandi — {updated} ta kanaldagi nusxa yangilandi.")

    async def _fail_edit(self, target: Target, reason: str) -> None:
        if self.notifier:
            await self.notifier.say(f"Tahrirni sinxronlab bo'lmadi — {target.name}: {reason}")

    # ---------- o'chirish ----------

    async def on_delete(self, chat_id: int, deleted_ids: list[int]) -> None:
        gone = set(deleted_ids)
        # Himoya: Telegram ba'zan (tahrir, qayta ulanish paytida) noto'g'ri "o'chirildi" signalini yuboradi.
        # Filial nusxalari faqat bosh kanalda post haqiqatan yo'qligi tasdiqlansa o'chiriladi.
        try:
            still = await self.client.get_messages(self.source, ids=sorted(gone))
            alive = {m.id for m in (still or []) if m}
        except Exception as e:
            log.warning("O'chirishni tekshirib bo'lmadi, nusxalarga tegilmaydi: %s", e)
            await db_log("delete_ignored", f"ids={sorted(gone)}: tekshirib bo'lmadi ({e})")
            return
        if alive:
            await db_log("delete_ignored", f"ids={sorted(alive)}: bosh kanalda post hali bor, nusxalar o'chirilmadi")
            gone -= alive
        if not gone:
            return
        removed = 0
        async with Session() as s:
            posts = [
                p for p in (await s.execute(select(SourcePost).where(SourcePost.chat_id == chat_id))).scalars()
                if gone & set(p.msg_ids) and not p.deleted
            ]
            targets = {t.id: t for t in (await s.execute(select(Target))).scalars()}
            for post in posts:
                idxs = [i for i, mid in enumerate(post.msg_ids) if mid in gone]
                deliveries = (await s.execute(select(Delivery).where(Delivery.post_id == post.id))).scalars()
                for d in deliveries:
                    target = targets[d.target_id]
                    if d.status == "pending":
                        d.status, d.reason = "rejected", "Manba post o'chirilgan"
                        continue
                    if d.status != "sent" or not target.sync_deletes:
                        continue
                    ids = list(d.target_msg_ids)
                    to_delete = [ids[i] for i in idxs if i < len(ids) and ids[i]]
                    if to_delete:
                        try:
                            await self.client.delete_messages(target.username, to_delete)
                            removed += 1
                        except Exception as e:
                            log.exception("delete")
                            if self.notifier:
                                await self.notifier.say(f"O'chirib bo'lmadi — {target.name}: {e}")
                            continue
                    for i in idxs:
                        if i < len(ids):
                            ids[i] = None
                    d.target_msg_ids = ids
                    if not any(ids):
                        d.status = "deleted"
                if gone >= set(post.msg_ids):
                    post.deleted = True
            await s.commit()
        if posts:
            await db_log("deleted", f"ids={sorted(gone)} removed_from={removed}")
            if self.notifier:
                await self.notifier.say(f"Manbada post o'chirildi — {removed} ta kanaldan ham o'chirildi.")
