"""Agentni ishga tushirish: `python -m app.main`."""
from __future__ import annotations

import asyncio
import json
import logging
import os

from sqlalchemy import select
from telethon import TelegramClient, events
from telethon.sessions import StringSession

from . import stats
from .adminbot import AdminBot
from .config import settings
from .db import Rule, Session, Target, init_db
from .pipeline import Pipeline

log = logging.getLogger("agent")


async def seed() -> str:
    """seed.json dan manba kanalni o'qiydi; baza bo'sh bo'lsa kanallar va qoidalarni yozadi."""
    with open(settings.seed_file, encoding="utf-8") as f:
        data = json.load(f)
    async with Session() as s:
        if not (await s.execute(select(Target))).first():
            for t in data.get("targets", []):
                s.add(Target(**t))
            for r in data.get("rules", []):
                s.add(Rule(text=r))
            await s.commit()
    return data["source"]


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if settings.database_url.startswith("sqlite"):
        os.makedirs("data", exist_ok=True)
    await init_db()
    source_username = await seed()

    client = TelegramClient(StringSession(settings.session_string), settings.api_id, settings.api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        raise SystemExit("Telegram sessiyasi yo'q yoki eskirgan. Avval: python scripts/login.py")
    me = await client.get_me()
    source = await client.get_entity(source_username)
    log.info("Akkaunt: %s (premium=%s); manba: %s", me.first_name, bool(me.premium), source_username)

    notifier = None
    if settings.bot_token and settings.admin_ids:
        notifier = AdminBot()
        await notifier.start()
    else:
        log.warning("TG_BOT_TOKEN yoki ADMIN_IDS yo'q: tasdiq va hisobotlar ishlamaydi")

    pipeline = Pipeline(client, source, notifier, premium=bool(me.premium))
    if notifier:
        notifier.pipeline = pipeline

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

    if notifier:
        await notifier.say("Agent ishga tushdi.")
    asyncio.create_task(stats.run(client))
    if settings.admin_password:
        import uvicorn

        from .web import create_app

        config = uvicorn.Config(create_app(pipeline), host=settings.web_host, port=settings.web_port, log_level="warning")
        asyncio.create_task(uvicorn.Server(config).serve())
        log.info("Admin panel: http://%s:%s", settings.web_host, settings.web_port)
    else:
        log.warning("ADMIN_PASSWORD yo'q: veb admin panel o'chirilgan")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
