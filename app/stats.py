"""Analitika yig'ish: obunachilar soni va postlarning ko'rishlari."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import select
from telethon.tl.functions.channels import GetFullChannelRequest

from .config import settings
from .db import Delivery, Session, StatChannel, StatPost, Target, now

log = logging.getLogger(__name__)
TRACK_DAYS = 7  # post shuncha kun davomida kuzatiladi


async def collect_once(client) -> None:
    async with Session() as s:
        targets = list((await s.execute(select(Target).where(Target.active))).scalars())
        since = now() - timedelta(days=TRACK_DAYS)
        for t in targets:
            try:
                full = await client(GetFullChannelRequest(t.username))
                subs = full.full_chat.participants_count or 0
                s.add(StatChannel(target_id=t.id, subscribers=subs))
                deliveries = list(
                    (await s.execute(
                        select(Delivery).where(
                            Delivery.target_id == t.id, Delivery.status == "sent", Delivery.sent_at >= since
                        )
                    )).scalars()
                )
                for d in deliveries:
                    ids = [i for i in d.target_msg_ids if i]
                    if not ids:
                        continue
                    msgs = await client.get_messages(t.username, ids=ids)
                    views = max((m.views or 0 for m in msgs if m), default=0)
                    s.add(StatPost(delivery_id=d.id, views=views, reach=views / subs if subs else None))
            except Exception:
                log.exception("Statistika yig'ilmadi: %s", t.username)
        await s.commit()


async def run(client) -> None:
    while True:
        await collect_once(client)
        await asyncio.sleep(settings.stats_interval_sec)
