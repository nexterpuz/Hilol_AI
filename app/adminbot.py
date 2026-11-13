"""Admin bilan aloqa: hisobotlar va tasdiq tugmalari (oddiy Telegram bot)."""
from __future__ import annotations

import logging

from telethon import Button, TelegramClient, events
from telethon.sessions import StringSession

from .config import settings

log = logging.getLogger(__name__)


class AdminBot:
    def __init__(self):
        self.bot = TelegramClient(StringSession(), settings.api_id, settings.api_hash)
        self.pipeline = None  # main.py da ulanadi

    async def start(self) -> None:
        await self.bot.start(bot_token=settings.bot_token)
        self.bot.add_event_handler(self._on_click, events.CallbackQuery())

    async def say(self, text: str, buttons=None) -> None:
        for admin in settings.admin_ids:
            try:
                await self.bot.send_message(admin, text, buttons=buttons, parse_mode=None, link_preview=False)
            except Exception:
                # Admin botga /start bosmagan bo'lsa, bot unga yoza olmaydi
                log.exception("Adminga xabar yuborilmadi: %s", admin)

    async def ask(self, delivery_id: int, target, text: str, reason: str, skipped: bool) -> None:
        preview = text if len(text) <= 600 else text[:600] + "…"
        head = "Yuborilmadi" if skipped else "Tasdiq kerak"
        body = f"{head} — {target.name}\nSabab: {reason}\n\n{preview}"
        ok = "Baribir yuborish" if skipped else "Yuborish"
        buttons = [Button.inline(ok, data=f"ok:{delivery_id}")]
        if not skipped:
            buttons.append(Button.inline("Rad etish", data=f"no:{delivery_id}"))
        await self.say(body, buttons=buttons)

    async def _on_click(self, event) -> None:
        if event.sender_id not in settings.admin_ids:
            await event.answer("Ruxsat yo'q", alert=True)
            return
        action, _, raw_id = event.data.decode().partition(":")
        if not raw_id.isdigit() or self.pipeline is None:
            return
        delivery_id = int(raw_id)
        if action == "ok":
            done = await self.pipeline.approve(delivery_id)
            note = "Yuborildi" if done else "Yuborilmadi (allaqachon yuborilgan yoki xato)"
        else:
            await self.pipeline.reject(delivery_id)
            note = "Rad etildi"
        await event.answer(note)
        msg = await event.get_message()
        await event.edit(f"{msg.message}\n\n— {note}", buttons=None, parse_mode=None)
