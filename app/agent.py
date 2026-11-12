"""Agentning ishlash holati: Telegram akkaunt, hodisalar, panel orqali kirish/almashtirish."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select, update
from telethon import TelegramClient, errors, events
from telethon.sessions import StringSession

from . import stats
from .config import settings
from .db import Account, Session, Target, log as db_log
from .pipeline import Pipeline

log = logging.getLogger(__name__)


def _fernet() -> Fernet:
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(settings.secret_key.encode()).digest()))


def encrypt(text: str) -> str:
    return _fernet().encrypt(text.encode()).decode()


def decrypt(token: str) -> str | None:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return None


class Agent:
    def __init__(self, source_username: str, notifier=None):
        self.source_username = source_username
        self.notifier = notifier
        self.client: TelegramClient | None = None
        self.pipeline: Pipeline | None = None
        self.status: dict = {"connected": False, "error": ""}
        self._stats_task: asyncio.Task | None = None
        self._login: dict | None = None  # panel orqali kirish jarayoni

    # ---------- ishga tushirish / to'xtatish ----------

    async def start_from_storage(self) -> None:
        """Bazadagi faol akkaunt, bo'lmasa .env dagi TG_SESSION_STRING bilan ishga tushadi."""
        async with Session() as s:
            acc = (await s.execute(select(Account).where(Account.active))).scalars().first()
        session = decrypt(acc.session_enc) if acc else None
        if acc and session is None:
            self.status = {"connected": False, "error": "Saqlangan sessiya o'qilmadi (SECRET_KEY o'zgargan). Qayta kiring."}
            return
        session = session or settings.session_string
        if not session:
            self.status = {"connected": False, "error": "Telegram akkaunt ulanmagan."}
            return
        await self.start(session)

    async def start(self, session: str) -> None:
        await self.stop()
        client = TelegramClient(StringSession(session), settings.api_id, settings.api_hash)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                self.status = {"connected": False, "error": "Sessiya eskirgan. Akkauntga qayta kiring."}
                return
            me = await client.get_me()
            source = await client.get_entity(self.source_username)
        except Exception as e:
            log.exception("Telegram'ga ulanib bo'lmadi")
            await client.disconnect()
            self.status = {"connected": False, "error": f"Telegram'ga ulanib bo'lmadi: {e}"}
            return

        pipeline = Pipeline(client, source, self.notifier, premium=bool(me.premium))

        @client.on(events.NewMessage(chats=source))
        async def _new(event):
            if event.message.grouped_id is None:  # albomlar pastda alohida ushlanadi
                await pipeline.on_new([event.message])

        @client.on(events.Album(chats=source))
        async def _album(event):
            await pipeline.on_new(list(event.messages))

        @client.on(events.MessageEdited(chats=source))
        async def _edit(event):
            await pipeline.on_edit(event.message)

        @client.on(events.MessageDeleted(chats=source))
        async def _delete(event):
            await pipeline.on_delete(event.chat_id, list(event.deleted_ids))

        self.client, self.pipeline = client, pipeline
        if self.notifier:
            self.notifier.pipeline = pipeline
        self._stats_task = asyncio.create_task(stats.run(client))
        name = " ".join(x for x in (me.first_name, me.last_name) if x)
        self.status = {
            "connected": True, "error": "", "name": name, "username": me.username or "",
            "phone": "+" + me.phone[:5] + "…" + me.phone[-2:] if me.phone else "",
            "premium": bool(me.premium), "source": self.source_username,
        }
        log.info("Akkaunt ulandi: %s (premium=%s); manba: %s", name, bool(me.premium), self.source_username)
        await db_log("account_started", name)

    async def stop(self) -> None:
        if self._stats_task:
            self._stats_task.cancel()
            self._stats_task = None
        if self.client:
            await self.client.disconnect()
        self.client = self.pipeline = None
        if self.notifier:
            self.notifier.pipeline = None

    async def check_targets(self) -> list[str]:
        """Faol kanallarning qaysilarida akkaunt post joylay olmaydi."""
        if not self.pipeline:
            return []
        async with Session() as s:
            targets = list((await s.execute(select(Target).where(Target.active))).scalars())
        return [p for p in [await self.pipeline.check_target(t.username) for t in targets] if p]

    # ---------- panel orqali kirish ----------

    async def begin_login(self, phone: str) -> str:
        """Telegram'ga tasdiqlash kodi so'rovini yuboradi. Xato matni yoki bo'sh satr."""
        if not settings.secret_from_env:
            return "Avval SECRET_KEY o'zgaruvchisini o'rnating — usiz sessiya qayta ishga tushganda yo'qoladi."
        await self.cancel_login()
        client = TelegramClient(StringSession(), settings.api_id, settings.api_hash)
        try:
            await client.connect()
            sent = await client.send_code_request(phone)
        except errors.FloodWaitError as e:
            await client.disconnect()
            return f"Telegram kutishni so'radi: {e.seconds} soniyadan keyin urinib ko'ring."
        except Exception as e:
            await client.disconnect()
            return f"Kod yuborilmadi: {e}"
        self._login = {"client": client, "phone": phone, "hash": sent.phone_code_hash, "need_password": False}
        return ""

    @property
    def login_step(self) -> str:
        if not self._login:
            return "phone"
        return "password" if self._login["need_password"] else "code"

    async def cancel_login(self) -> None:
        if self._login:
            await self._login["client"].disconnect()
            self._login = None

    async def finish_login(self, code: str = "", password: str = "") -> str:
        """Kod (va kerak bo'lsa 2FA parol) bilan kirishni yakunlaydi. Xato matni yoki bo'sh satr."""
        if not self._login:
            return "Kirish jarayoni boshlanmagan."
        st = self._login
        client = st["client"]
        try:
            if st["need_password"]:
                await client.sign_in(password=password)
            else:
                await client.sign_in(st["phone"], code.strip(), phone_code_hash=st["hash"])
        except errors.SessionPasswordNeededError:
            st["need_password"] = True
            return ""
        except (errors.PhoneCodeInvalidError, errors.PhoneCodeEmptyError):
            return "Kod noto'g'ri."
        except errors.PhoneCodeExpiredError:
            await self.cancel_login()
            return "Kod eskirgan. Qaytadan boshlang."
        except errors.PasswordHashInvalidError:
            return "Ikki bosqichli parol noto'g'ri."
        except Exception as e:
            return f"Kirib bo'lmadi: {e}"
        me = await client.get_me()
        session = client.session.save()
        await client.disconnect()
        self._login = None
        async with Session() as s:
            await s.execute(update(Account).values(active=False))
            s.add(Account(
                name=" ".join(x for x in (me.first_name, me.last_name) if x),
                phone=me.phone or "", session_enc=encrypt(session), premium=bool(me.premium), active=True,
            ))
            await s.commit()
        await self.start(session)
        return self.status.get("error", "")

    async def logout(self) -> None:
        """Sessiyani Telegram'da ham bekor qiladi va akkauntni uzadi."""
        if self.client:
            try:
                await self.client.log_out()
            except Exception:
                log.exception("log_out")
        await self.stop()
        async with Session() as s:
            await s.execute(update(Account).values(active=False))
            await s.commit()
        self.status = {"connected": False, "error": "Telegram akkaunt ulanmagan."}
