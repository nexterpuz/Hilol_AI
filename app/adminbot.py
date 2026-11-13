"""Bot: bosh adminga hisobot va tasdiq tugmalari; filial menejerlari uchun o'z filiali postlarini tasdiqlash."""
from __future__ import annotations

import logging

from sqlalchemy import select
from telethon import Button, TelegramClient, events
from telethon.sessions import StringSession

from .config import settings
from .db import Delivery, Session, SourcePost, Target
from .pipeline import is_paused, phone_key, phone_keys, preview_text, set_paused

log = logging.getLogger(__name__)


def preview(text: str) -> str:
    return text if len(text) <= 600 else text[:600] + "…"


class AdminBot:
    def __init__(self):
        self.bot = TelegramClient(StringSession(), settings.api_id, settings.api_hash)
        self.pipeline = None  # agent ulanganda o'rnatiladi

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

    async def ask_branch(self, delivery_id: int, target, text: str) -> None:
        """Sotuv postini filial menejer(lar)iga yuboradi; menejer bog'lanmagan bo'lsa — bosh adminga."""
        body = (
            f"Yangi post — {target.name}\n"
            "Kitob filialingizda sotuvga tayyor bo'lgach «Kanalga joylash» ni bosing.\n\nKanalda shunday chiqadi:\n\n" + preview(text)
        )
        buttons = [Button.inline("Kanalga joylash", data=f"ok:{delivery_id}"), Button.inline("Rad etish", data=f"no:{delivery_id}")]
        managers = list((target.manager_ids or {}).keys())
        if not managers or not await self.send(managers, body, buttons):
            await self.say(f"Filial menejeri bog'lanmagan — {target.name}\nSabab: Filial tasdig'i kutilmoqda\n\n{preview(text)}", self._buttons(delivery_id))

    # ---------- menejerlar ----------

    async def _targets_of(self, user_id: int) -> list:
        async with Session() as s:
            targets = list((await s.execute(select(Target))).scalars())
        return [t for t in targets if str(user_id) in (t.manager_ids or {})]

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
            "Kutayotgan postlarni ko'rish: /navbat",
            buttons=Button.clear(),
        )
        await self.say(f"Filial menejeri bog'landi — {names} (Telegram ID {event.sender_id}).")
        await self._send_pending(event.sender_id, bound)

    async def _on_message(self, event) -> None:
        uid, text = event.sender_id, (event.raw_text or "").strip().lower()
        is_admin = uid in settings.admin_ids
        if event.message.contact:
            await self._bind(event)
        elif is_admin and text in ("/stop", "/toxtat"):
            await set_paused(True)
            await event.respond("Favqulodda to'xtatish yoqildi: yangi postlar filiallarga chiqmaydi, navbatda to'planadi. Davom ettirish: /resume")
        elif is_admin and text in ("/resume", "/davom"):
            await set_paused(False)
            await event.respond("Agent davom ettirildi. To'xtatilgan paytda to'plangan postlar Tasdiq navbatida turibdi.")
        elif text.startswith("/start") or text == "/navbat":
            mine = await self._targets_of(uid)
            if is_admin and text.startswith("/start"):
                state = "TO'XTATILGAN" if await is_paused() else "ishlayapti"
                await event.respond(f"Siz bosh adminsiz. Agent holati: {state}.\nBuyruqlar: /stop — favqulodda to'xtatish, /resume — davom ettirish.")
            if mine:
                count = await self._send_pending(uid, mine)
                names = ", ".join(t.name for t in mine)
                await event.respond(f"Filial: {names}. Kutayotgan postlar: {count} ta." if count else f"Filial: {names}. Kutayotgan post yo'q.")
            elif not is_admin:
                await event.respond(
                    "Assalomu alaykum! Bu bot filial menejerlari uchun.\n"
                    "Filialingizni aniqlash uchun pastdagi tugma orqali telefon raqamingizni ulashing.",
                    buttons=Button.request_phone("Raqamni ulashish", resize=True, single_use=True),
                )

    # ---------- tugmalar ----------

    async def _on_click(self, event) -> None:
        action, _, raw_id = event.data.decode().partition(":")
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
