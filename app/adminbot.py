"""Bot: bosh adminga hisobot va tasdiq tugmalari; filial menejerlari uchun o'z filiali postlarini tasdiqlash."""
from __future__ import annotations

import logging
import re

from sqlalchemy import select
from telethon import Button, TelegramClient, events
from telethon.sessions import StringSession

from .config import settings
from .db import Delivery, Session, SourcePost, Target
from .pipeline import is_paused, phone_key, phone_keys, preview_text, set_paused

log = logging.getLogger(__name__)


# Pastki tugmalar: buyruqlarni qo'lda yozmaslik uchun
BTN_REPORT = "📊 Hisobot"
BTN_WEEK, BTN_MONTH = "📊 Haftalik hisobot", "📅 Oylik hisobot"  # eski tugmalar: ishlayveradi
BTN_QUEUE, BTN_STATE = "📥 Kutayotgan postlar", "ℹ️ Holat"
BTN_STOP, BTN_RESUME = "⏸ To'xtatish", "▶️ Davom ettirish"
BTN_MANAGERS = "👥 Menejerlar"
BTN_MESSAGE = "✉️ Xabar yuborish"
BTN_SUGGEST = "💡 Taklif yuborish"
BTN_RATING = "🏆 Reyting"
MESSAGE_HEAD = "📢 Bosh admindan xabar:\n\n"
ALIASES = {
    BTN_REPORT.lower(): "/hisobot", BTN_WEEK.lower(): "/hisob_hafta", BTN_MONTH.lower(): "/hisob_oy", BTN_QUEUE.lower(): "/navbat",
    BTN_STATE.lower(): "/holat", BTN_STOP.lower(): "/stop", BTN_RESUME.lower(): "/resume",
    BTN_MANAGERS.lower(): "/menejerlar", BTN_MESSAGE.lower(): "/xabar", BTN_SUGGEST.lower(): "/taklif", BTN_RATING.lower(): "/reyting",
}


def keyboard(is_admin: bool, is_manager: bool, paused: bool = False):
    """Foydalanuvchi roliga mos pastki tugmalar."""
    rows = [[Button.text(BTN_REPORT, resize=True)]]
    if is_manager:
        rows.append([Button.text(BTN_RATING)])
    second = []
    if is_manager:
        second.append(Button.text(BTN_QUEUE))
        second.append(Button.text(BTN_SUGGEST))
    if is_admin:
        second.append(Button.text(BTN_STATE))
        second.append(Button.text(BTN_RESUME if paused else BTN_STOP))
    if second:
        rows.append(second)
    if is_admin:
        rows.append([Button.text(BTN_MANAGERS), Button.text(BTN_MESSAGE)])
    return rows


async def managers_report() -> str:
    """Qaysi filial menejeri botga bog'langan, qaysi biri hali /start bosmagan."""
    from datetime import datetime, timedelta, timezone

    tz = timezone(timedelta(hours=5))
    async with Session() as s:
        targets = list((await s.execute(select(Target).where(Target.active).order_by(Target.id))).scalars())
    lines, ready = ["👥 Filial menejerlari", ""], 0
    for t in targets:
        phones = [p.strip() for p in re.split(r"[,;\n]", t.manager_phones or "") if phone_key(p)]
        bound = {key: uid for uid, key in (t.manager_ids or {}).items()}
        seen = t.manager_seen or {}
        if not phones:
            lines += [f"⚠️ {t.name}", "   raqam kiritilmagan (paneldagi kanal sozlamasida)", ""]
            continue
        done = [p for p in phones if phone_key(p) in bound]
        ready += bool(done)
        lines.append(f"{'✅' if len(done) == len(phones) else ('🟡' if done else '❌')} {t.name}")
        for p in phones:
            uid = bound.get(phone_key(p))
            if uid is None:
                lines.append(f"   {p} — botga /start bosmagan")
                continue
            when = seen.get(uid)
            try:
                when = datetime.fromisoformat(when).astimezone(tz).strftime("%d.%m %H:%M") if when else ""
            except ValueError:
                when = ""
            lines.append(f"   {p} — bog'langan" + (f", oxirgi faollik {when}" if when else ""))
        lines.append("")
    if not targets:
        return "Hali faol filial kanali yo'q."
    lines.append(f"Jami: {len(targets)} ta filialdan {ready} tasida menejer bog'langan.")
    return "\n".join(lines)


def preview(text: str) -> str:
    return text if len(text) <= 600 else text[:600] + "…"


class AdminBot:
    def __init__(self):
        self.bot = TelegramClient(StringSession(), settings.api_id, settings.api_hash)
        self.pipeline = None  # agent ulanganda o'rnatiladi
        self._compose: dict[int, object] = {}  # xabar yozayotgan admin -> "all", filial id yoki ("user", id)
        self._suggest: set[int] = set()  # taklif yozayotgan menejerlar

    async def start(self) -> None:
        await self.bot.start(bot_token=settings.bot_token)
        self.bot.add_event_handler(self._on_click, events.CallbackQuery())
        self.bot.add_event_handler(self._on_message, events.NewMessage(incoming=True, func=lambda e: e.is_private))

    # ---------- xabar yuborish ----------

    async def send(self, user_ids, text: str, buttons=None) -> int:
        sent = 0
        for uid in user_ids:
            try:
                await self.bot.send_message(int(uid), text, buttons=buttons, parse_mode=None, link_preview=False)
                sent += 1
            except Exception:
                # Foydalanuvchi botga /start bosmagan bo'lsa, bot unga yoza olmaydi
                log.exception("Xabar yuborilmadi: %s", uid)
        return sent

    async def send_photo(self, user_ids, image: bytes, caption: str) -> int:
        import io

        sent = 0
        for uid in user_ids:
            try:
                f = io.BytesIO(image)
                f.name = "hisobot.png"
                await self.bot.send_file(int(uid), f, caption=caption, parse_mode=None, force_document=False)
                sent += 1
            except Exception:
                log.exception("Hisobot yuborilmadi: %s", uid)
        return sent

    async def say(self, text: str, buttons=None) -> None:
        await self.send(settings.admin_ids, text, buttons)

    @staticmethod
    def _buttons(delivery_id: int, skipped: bool = False):
        ok = "Baribir yuborish" if skipped else "Yuborish"
        row = [Button.inline(ok, data=f"ok:{delivery_id}")]
        if not skipped:
            row.append(Button.inline("Rad etish", data=f"no:{delivery_id}"))
        return row

    async def ask(self, delivery_id: int, target, text: str, reason: str, skipped: bool) -> None:
        head = "Yuborilmadi" if skipped else "Tasdiq kerak"
        await self.say(
            f"{head} — {target.name}\nSabab: {reason}\n\nKanalda shunday chiqadi:\n\n{preview(text)}",
            self._buttons(delivery_id, skipped),
        )

    async def ask_branch(self, delivery_id: int, target, text: str, local: bool = False) -> None:
        """Postni filial menejer(lar)iga tasdiqlash uchun yuboradi; menejer bog'lanmagan bo'lsa — bosh adminga."""
        if local:
            intro = "Bu post aynan sizning filialingizga tegishli. Kanalingizga joylansinmi?"
            why = "Filialga tegishli post"
        else:
            intro = "Kitob filialingizda sotuvga tayyor bo'lgach «Kanalga joylash» ni bosing."
            why = "Filial tasdig'i kutilmoqda"
        body = f"Yangi post — {target.name}\n{intro}\n\nKanalda shunday chiqadi:\n\n{preview(text)}"
        buttons = [Button.inline("Kanalga joylash", data=f"ok:{delivery_id}"), Button.inline("Rad etish", data=f"no:{delivery_id}")]
        managers = list((target.manager_ids or {}).keys())
        if not managers or not await self.send(managers, body, buttons):
            # Menejer botga /start bosmagan yoki botni bloklagan: post kanalga o'zi chiqmaydi, bosh admin hal qiladi
            await self.say(
                f"Filial menejeri botga bog'lanmagan — {target.name}\nSabab: {why}\n\nKanalda shunday chiqadi:\n\n{preview(text)}",
                self._buttons(delivery_id),
            )

    async def _report_choice(self, event, kind: str) -> None:
        """Tanlangan davr bo'yicha hisobotni shu foydalanuvchiga yuboradi (admin — umumiy, menejer — o'z filiali)."""
        from . import reports

        if kind not in ("daily", "weekly", "monthly"):
            return
        sent = await reports.send_reports(self, kind, only_user=event.sender_id)
        if hasattr(event, "data"):
            await event.answer("Hisobot yuborildi" if sent else "Sizga tegishli hisobot yo'q", alert=not sent)
        elif not sent:
            await event.respond("Sizga tegishli hisobot yo'q. Avval /start orqali filialga bog'laning.")

    # ---------- menejerlarga xabar ----------

    async def _recipients(self, choice) -> tuple[list[str], str]:
        """Tanlov bo'yicha qabul qiluvchilar (takrorsiz) va tanlov nomi."""
        async with Session() as s:
            targets = list((await s.execute(select(Target).where(Target.active).order_by(Target.id))).scalars())
        if isinstance(choice, tuple):  # bitta menejerga javob
            return [str(choice[1])], "menejer"
        if choice != "all":
            targets = [t for t in targets if t.id == choice]
        users = list(dict.fromkeys(uid for t in targets for uid in (t.manager_ids or {})))
        label = "barcha menejerlar" if choice == "all" else (targets[0].name if targets else "filial")
        return users, label

    async def _message_menu(self, event) -> None:
        async with Session() as s:
            targets = list((await s.execute(select(Target).where(Target.active).order_by(Target.id))).scalars())
        with_mgr = [t for t in targets if t.manager_ids]
        if not with_mgr:
            await event.respond("Hali hech bir filial menejeri botga bog'lanmagan. «👥 Menejerlar» tugmasi orqali holatni ko'ring.")
            return
        total = len({uid for t in with_mgr for uid in t.manager_ids})
        rows = [[Button.inline(f"Barcha menejerlarga ({total})", data="msg:all")]]
        rows += [[Button.inline(f"{t.name} ({len(t.manager_ids)})", data=f"msg:{t.id}")] for t in with_mgr]
        rows.append([Button.inline("Bekor qilish", data="msg:cancel")])
        await event.respond("Xabar kimga yuborilsin?", buttons=rows)

    async def _deliver_message(self, event, choice) -> None:
        users, label = await self._recipients(choice)
        if not users:
            await event.respond(f"Yuborilmadi: {label} uchun bog'langan menejer yo'q.")
            return
        text = event.raw_text or ""
        media = getattr(event.message, "media", None)
        if media is not None:
            sent = await self.send_media(users, media, MESSAGE_HEAD + text)
        else:
            sent = await self.send(users, MESSAGE_HEAD + text)
        note = f"Yuborildi — {label}: {sent} / {len(users)} kishiga yetdi."
        if sent < len(users):
            note += " Yetmaganlar botni bloklagan yoki o'chirgan bo'lishi mumkin."
        await event.respond(note)

    async def _forward_suggestion(self, event, mine: list) -> None:
        """Menejer taklifini bosh adminga yetkazadi."""
        who = ""
        try:
            sender = await event.get_sender()
            name = " ".join(x for x in (getattr(sender, "first_name", ""), getattr(sender, "last_name", "")) if x)
            who = name + (f", @{sender.username}" if getattr(sender, "username", None) else "")
        except Exception:
            pass
        branch = ", ".join(t.name for t in mine)
        head = f"💡 Taklif — {branch}" + (f" ({who})" if who else "") + ":\n\n"
        reply = [Button.inline("Javob yozish", data=f"msg:u{event.sender_id}")]
        text, media = event.raw_text or "", getattr(event.message, "media", None)
        if media is not None:
            sent = await self.send_media(settings.admin_ids, media, head + text)
            if sent:
                await self.send(settings.admin_ids, f"Yuqoridagi taklifga javob — {branch}:", reply)
        else:
            sent = await self.send(settings.admin_ids, head + text, reply)
        await event.respond("Taklifingiz bosh adminga yuborildi. Rahmat!" if sent else "Taklif yuborilmadi, keyinroq urinib ko'ring.")

    async def send_media(self, user_ids, media, caption: str) -> int:
        sent = 0
        for uid in user_ids:
            try:
                await self.bot.send_file(int(uid), media, caption=caption, parse_mode=None)
                sent += 1
            except Exception:
                log.exception("Media xabar yuborilmadi: %s", uid)
        return sent

    # ---------- menejerlar ----------

    async def _targets_of(self, user_id: int) -> list:
        async with Session() as s:
            targets = list((await s.execute(select(Target))).scalars())
        return [t for t in targets if str(user_id) in (t.manager_ids or {})]

    async def _touch(self, user_id: int) -> None:
        """Menejerning oxirgi faollik vaqtini yozadi."""
        from datetime import datetime, timezone

        async with Session() as s:
            changed = False
            for t in (await s.execute(select(Target))).scalars():
                if str(user_id) in (t.manager_ids or {}):
                    t.manager_seen = {**(t.manager_seen or {}), str(user_id): datetime.now(timezone.utc).isoformat()}
                    changed = True
            if changed:
                await s.commit()

    async def _send_pending(self, user_id: int, targets: list) -> int:
        ids = [t.id for t in targets]
        names = {t.id: t.name for t in targets}
        by_id = {t.id: t for t in targets}
        async with Session() as s:
            deliveries = list((await s.execute(
                select(Delivery).where(Delivery.target_id.in_(ids), Delivery.status == "pending").order_by(Delivery.id.desc()).limit(10)
            )).scalars())
            items = [(d, await s.get(SourcePost, d.post_id)) for d in deliveries]
        for d, post in reversed(items):
            if post is None or post.deleted:
                continue
            shown = preview_text(post, by_id[d.target_id], d) or "(matnsiz post)"
            body = f"Kutmoqda — {names[d.target_id]}\n\nKanalda shunday chiqadi:\n\n{preview(shown)}"
            await self.send([user_id], body, [Button.inline("Kanalga joylash", data=f"ok:{d.id}"), Button.inline("Rad etish", data=f"no:{d.id}")])
        return len(items)

    async def _bind(self, event) -> None:
        contact = event.message.contact
        if contact.user_id != event.sender_id:
            await event.respond("Faqat o'z raqamingizni pastdagi tugma orqali ulashing.", buttons=Button.clear())
            return
        key = phone_key(contact.phone_number)
        bound = []
        async with Session() as s:
            for t in (await s.execute(select(Target))).scalars():
                if key and key in phone_keys(t.manager_phones):
                    t.manager_ids = {**(t.manager_ids or {}), str(event.sender_id): key}
                    bound.append(t)
            await s.commit()
        if not bound:
            await event.respond("Bu raqam hech bir filialga biriktirilmagan. Bosh adminga murojaat qiling.", buttons=Button.clear())
            return
        names = ", ".join(t.name for t in bound)
        await event.respond(
            f"Tasdiqlandi. Siz bog'langan filial: {names}.\n"
            "Endi yangi nashr va «tez kunda» postlari sizga keladi: kitob filialingizda sotuvga tayyor bo'lgach «Kanalga joylash» ni bosasiz.\n"
            "Har kuni soat 18:00 da filialingiz reytingi, har yakshanba va oy oxirida hisoboti keladi.\n"
            "Pastdagi tugmalar orqali hisobotni va kutayotgan postlarni istagan payt ko'rasiz.",
            buttons=keyboard(event.sender_id in settings.admin_ids, True, await is_paused()),
        )
        await self.say(f"Filial menejeri bog'landi — {names} (Telegram ID {event.sender_id}).")
        await self._send_pending(event.sender_id, bound)

    async def _on_message(self, event) -> None:
        uid, text = event.sender_id, (event.raw_text or "").strip().lower()
        text = ALIASES.get(text, text)  # pastki tugma bosilgan bo'lsa — mos buyruq
        is_admin = uid in settings.admin_ids
        if event.message.contact:
            await self._bind(event)
            await self._touch(uid)
            return
        if is_admin and uid in self._compose:
            choice = self._compose.pop(uid)
            if not text.startswith("/"):  # buyruq yoki boshqa tugma bosilsa — yozish bekor qilinadi
                await self._deliver_message(event, choice)
                return
            if text == "/bekor":
                await event.respond("Xabar yuborish bekor qilindi.")
                return
        mine = await self._targets_of(uid)
        if mine:
            await self._touch(uid)
        if uid in self._suggest:
            self._suggest.discard(uid)
            if mine and not text.startswith("/"):  # buyruq yoki boshqa tugma — taklif bekor qilinadi
                await self._forward_suggestion(event, mine)
                return
            if text == "/bekor":
                await event.respond("Taklif yuborish bekor qilindi.")
                return

        async def kb():
            return keyboard(is_admin, bool(mine), await is_paused())

        if is_admin and text in ("/stop", "/toxtat"):
            await set_paused(True)
            await event.respond(
                "Favqulodda to'xtatish yoqildi: yangi postlar filiallarga chiqmaydi, navbatda to'planadi.", buttons=await kb()
            )
        elif is_admin and text in ("/resume", "/davom"):
            await set_paused(False)
            await event.respond("Agent davom ettirildi. To'xtatilgan paytda to'plangan postlar Tasdiq navbatida turibdi.", buttons=await kb())
        elif mine and text == "/reyting":
            from . import reports

            await reports.send_ratings(self, only_user=uid)
        elif mine and text == "/taklif":
            self._suggest.add(uid)
            await event.respond("Taklif yoki fikringizni yozib yuboring (rasm ham bo'ladi) — u bosh adminga yetkaziladi.\nBekor qilish: /bekor")
        elif is_admin and text == "/xabar":
            await self._message_menu(event)
        elif is_admin and text == "/menejerlar":
            await event.respond(await managers_report(), buttons=await kb(), parse_mode=None)
        elif is_admin and text == "/holat":
            state = "TO'XTATILGAN" if await is_paused() else "ishlayapti"
            linked = "ulangan" if self.pipeline is not None else "ULANMAGAN"
            await event.respond(f"Agent holati: {state}.\nTelegram akkaunt: {linked}.", buttons=await kb())
        elif text == "/hisobot":
            if is_admin or mine:
                await event.respond("Qaysi davr uchun hisobot kerak?", buttons=[[
                    Button.inline("Kunlik", data="rep:daily"),
                    Button.inline("Haftalik", data="rep:weekly"),
                    Button.inline("Oylik", data="rep:monthly"),
                ]])
            else:
                await event.respond("Sizga tegishli hisobot yo'q. Avval /start orqali filialga bog'laning.")
        elif text in ("/hisob_kun", "/hisob_hafta", "/hisobot_oy", "/hisob_oy"):
            kind = {"/hisob_kun": "daily", "/hisob_hafta": "weekly"}.get(text, "monthly")
            await self._report_choice(event, kind)
        elif text.startswith("/start") or text == "/navbat":
            if is_admin and text.startswith("/start"):
                state = "TO'XTATILGAN" if await is_paused() else "ishlayapti"
                await event.respond(f"Siz bosh adminsiz. Agent holati: {state}.\nPastdagi tugmalardan foydalaning.", buttons=await kb())
            if mine:
                count = await self._send_pending(uid, mine)
                names = ", ".join(t.name for t in mine)
                await event.respond(
                    f"Filial: {names}. Kutayotgan postlar: {count} ta." if count else f"Filial: {names}. Kutayotgan post yo'q.",
                    buttons=await kb(),
                )
            elif not is_admin:
                await event.respond(
                    "Assalomu alaykum! Bu bot filial menejerlari uchun.\n"
                    "Filialingizni aniqlash uchun pastdagi tugma orqali telefon raqamingizni ulashing.",
                    buttons=Button.request_phone("📱 Raqamni ulashish", resize=True, single_use=True),
                )

    # ---------- tugmalar ----------

    async def _on_click(self, event) -> None:
        action, _, raw_id = event.data.decode().partition(":")
        if action == "rep":
            await self._report_choice(event, raw_id)
            return
        if action == "msg":
            if event.sender_id not in settings.admin_ids:
                await event.answer("Ruxsat yo'q", alert=True)
                return
            if raw_id == "cancel":
                self._compose.pop(event.sender_id, None)
                await event.answer("Bekor qilindi")
                await event.edit("Xabar yuborish bekor qilindi.", buttons=None)
                return
            if raw_id.startswith("u") and raw_id[1:].isdigit():
                choice = ("user", int(raw_id[1:]))  # taklif yuborgan menejerga javob
            else:
                choice = "all" if raw_id == "all" else int(raw_id) if raw_id.isdigit() else None
            if choice is None:
                return
            users, label = await self._recipients(choice)
            self._compose[event.sender_id] = choice
            await event.answer("Xabarni yozing")
            prompt = (
                f"Kimga: {label} ({len(users)} kishi).\n"
                "Endi xabar matnini yozib yuboring (rasm yoki fayl ham bo'ladi) — keyingi xabaringiz jo'natiladi.\n"
                "Bekor qilish: /bekor"
            )
            if isinstance(choice, tuple):
                await event.respond(prompt)  # taklif matni chatda qolsin
            else:
                await event.edit(prompt, buttons=None)
            return
        if not raw_id.isdigit() or self.pipeline is None:
            await event.answer("Agent hozir ulanmagan", alert=True)
            return
        delivery_id = int(raw_id)
        is_admin = event.sender_id in settings.admin_ids
        if not is_admin:
            async with Session() as s:
                d = await s.get(Delivery, delivery_id)
                target = await s.get(Target, d.target_id) if d else None
            if target is None or str(event.sender_id) not in (target.manager_ids or {}):
                await event.answer("Ruxsat yo'q", alert=True)
                return
            await self._touch(event.sender_id)
        if action == "ok":
            # Bosh admin qarori eslab qolinadi; menejerniki — yo'q (har bir kitob alohida tasdiqlanadi)
            done = await (self.pipeline.approve(delivery_id) if is_admin else self.pipeline.publish(delivery_id))
            note = "Kanalga joylandi" if done else "Joylanmadi (allaqachon joylangan yoki xato)"
        else:
            await self.pipeline.reject(delivery_id, remember=is_admin)
            note = "Rad etildi"
        await event.answer(note)
        msg = await event.get_message()
        await event.edit(f"{msg.message}\n\n— {note}", buttons=None, parse_mode=None)
