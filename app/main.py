"""Agentni ishga tushirish: `python -m app.main`."""
from __future__ import annotations

import asyncio
import json
import logging
import os

import uvicorn
from sqlalchemy import select

from .adminbot import AdminBot
from .agent import Agent
from .config import settings
from .db import Rule, Session, Target, init_db
from .web import create_app

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
    if not (settings.api_id and settings.api_hash):
        raise SystemExit("TG_API_ID va TG_API_HASH o'rnatilmagan")
    if settings.database_url.startswith("sqlite"):
        os.makedirs("data", exist_ok=True)
    await init_db()
    source_username = await seed()

    notifier = None
    if settings.bot_token and settings.admin_ids:
        try:
            notifier = AdminBot()
            await notifier.start()
        except Exception:
            log.exception("Admin bot ishga tushmadi (TG_BOT_TOKEN ni tekshiring)")
            notifier = None
    else:
        log.warning("TG_BOT_TOKEN yoki ADMIN_IDS yo'q: Telegram orqali tasdiq va hisobotlar ishlamaydi")

    agent = Agent(source_username, notifier)
    await agent.load_source()
    starter = asyncio.create_task(agent.start_delayed(settings.start_delay_sec))

    if not settings.admin_password:
        log.warning("ADMIN_PASSWORD yo'q: veb admin panel o'chirilgan")
        await starter
        await asyncio.Event().wait()
    if not settings.secret_from_env:
        log.warning("SECRET_KEY yo'q: panel sessiyalari va akkauntga kirish qayta ishga tushganda yo'qoladi")
    async def shutdown() -> None:
        # SIGTERM kelganda uvicorn shu funksiyani chaqiradi: sessiyani darhol bo'shatamiz,
        # yangi nusxa bilan to'qnashmasin
        starter.cancel()
        await agent.stop()
        log.info("Agent to'xtatildi")

    app = create_app(agent, on_shutdown=shutdown)
    config = uvicorn.Config(
        app, host=settings.web_host, port=settings.web_port,
        log_level="warning", proxy_headers=True, forwarded_allow_ips="*",
    )
    log.info("Admin panel: %s:%s", settings.web_host, settings.web_port)
    await uvicorn.Server(config).serve()


if __name__ == "__main__":
    asyncio.run(main())
