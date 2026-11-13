"""Analitika yig'ish: obunachilar soni va postlarning ko'rishlari."""
from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import timedelta

from sqlalchemy import delete, select
from telethon.tl.functions.channels import GetFullChannelRequest

from .config import settings
from .db import Delivery, Session, StatChannel, StatPost, Subscriber, Target, now

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


def user_hash(user_id: int) -> str:
    return hashlib.sha256(f"{settings.secret_key}:{user_id}".encode()).hexdigest()


async def collect_subscribers(client) -> None:
    """Har bir kanalning obunachilar ro'yxatini (Telegram bergan qismini) yangilaydi.

    Telegram kanal adminiga ham faqat oxirgi ~200 obunachini beradi, shuning uchun katta
    kanallarda ro'yxat to'liq bo'lmaydi; panel qamrov foizini ko'rsatadi.
    """
    async with Session() as s:
        targets = list((await s.execute(select(Target).where(Target.active))).scalars())
    for t in targets:
        try:
            hashes = set()
            async for user in client.iter_participants(t.username):
                if not getattr(user, "deleted", False) and not getattr(user, "bot", False):
                    hashes.add(user_hash(user.id))
        except Exception:
            log.exception("Obunachilar ro'yxati olinmadi: %s", t.username)
            continue
        async with Session() as s:
            await s.execute(delete(Subscriber).where(Subscriber.target_id == t.id))
            s.add_all([Subscriber(target_id=t.id, user_hash=h) for h in hashes])
            await s.commit()
        await asyncio.sleep(2)  # Telegram limitlariga ehtiyot


async def run(client) -> None:
    cycle = 0
    while True:
        await collect_once(client)
        if cycle % 6 == 0:  # obunachilar ro'yxati kamroq yangilanadi (odatda 6 soatda bir)
            await collect_subscribers(client)
        cycle += 1
        await asyncio.sleep(settings.stats_interval_sec)
