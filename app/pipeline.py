"""Asosiy oqim: yangi post -> tahlil -> joylash; tahrir va o'chirishni sinxronlash."""
from __future__ import annotations

import hashlib
import logging

from sqlalchemy import select
from telethon import TelegramClient
from telethon.tl.types import MessageMediaWebPage

from . import ai
from .config import settings
from .db import Delivery, Rule, Session, SourcePost, Target, log as db_log, now
from .textops import build_post, fits

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


def text_hash(msg) -> str:
    ents = repr([e.to_dict() for e in (msg.entities or [])])
    return hashlib.sha256(((msg.message or "") + ents).encode()).hexdigest()


def is_file(msg) -> bool:
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

    # ---------- yangi post ----------

    async def on_new(self, msgs: list) -> None:
        msgs = sorted(msgs, key=lambda m: m.id)
        ids = [m.id for m in msgs]
        cap = next((m for m in msgs if m.message), msgs[0])
        async with Session() as s:
            known = (
                await s.execute(select(SourcePost).where(SourcePost.chat_id == msgs[0].chat_id))
            ).scalars()
            if any(set(ids) & set(p.msg_ids) for p in known):
                return  # qayta ishga tushganda takror kelgan xabar
            targets = list((await s.execute(select(Target).where(Target.active))).scalars())
            rules = [r.text for r in (await s.execute(select(Rule).where(Rule.active))).scalars()]

        # Aloqa blokini topish uchun tahlil har qanday rejimda kerak
        analysis = await ai.analyze(cap.message or "", targets, rules) if cap.message else None

        async with Session() as s:
            post = SourcePost(
                chat_id=msgs[0].chat_id,
                msg_ids=ids,
                grouped_id=msgs[0].grouped_id,
                text=cap.message or "",
                text_hash=text_hash(cap),
                cta_start_line=analysis.cta_start_line if analysis else None,
            )
            s.add(post)
            await s.flush()
            plan = []
            for t in targets:
                d = analysis.for_target(t.id) if analysis else None
                action, reason = decide(t.mode, d, settings.confidence_threshold)
                if cap.message and analysis is None and action == "send":
                    # Aloqa bloki topilmagan holda ko'r-ko'rona yubormaymiz
                    action, reason = "ask", "AI ishlamadi: aloqa bloki tekshirilmagan"
                if not all(is_supported(m) for m in msgs):
                    action, reason = "ask", "Post turi avtomatik ko'chirilmaydi (so'rovnoma va h.k.)"
                delivery = Delivery(
                    post_id=post.id,
                    target_id=t.id,
                    status="skipped" if action == "skip" else "pending",
                    reason=reason,
                )
                s.add(delivery)
                await s.flush()
                plan.append((delivery.id, t, action, reason))
            await s.commit()
            post_id = post.id

        await db_log("new_post", f"post={post_id} ids={ids}")
        for delivery_id, target, action, reason in plan:
            if action == "send":
                await self.publish(delivery_id)
            elif self.notifier:
                await self.notifier.ask(delivery_id, target, cap.message or "(matnsiz post)", reason, skipped=action == "skip")

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
        fetched = await self.client.get_messages(self.source, ids=post.msg_ids)
        msgs = [m for m in fetched if m]
        if not msgs:
            await self._fail(delivery_id, target, "Manba post topilmadi")
            return False
        cap = next((m for m in msgs if m.message), msgs[0])
        text, ents = build_post(cap.message or "", cap.entities, post.cta_start_line, target.template_html)
        files = [m for m in msgs if is_file(m)]
        if not fits(text, bool(files), self.premium):
            await self._fail(delivery_id, target, "Matn Telegram limitidan uzun (Premium kerak yoki qisqartirish)")
            return False
        try:
            if not files:
                sent = await self.client.send_message(
                    target.username, text, formatting_entities=ents,
                    link_preview=isinstance(cap.media, MessageMediaWebPage), silent=target.silent,
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
            await s.commit()
        await db_log("sent", f"delivery={delivery_id} target={target.username}")
        return True

    async def reject(self, delivery_id: int) -> None:
        async with Session() as s:
            d = await s.get(Delivery, delivery_id)
            if d.status in ("pending", "skipped"):
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
            if not is_caption_msg or new_hash == post.text_hash:
                return  # ko'rishlar/reaksiya o'zgarishi yoki matnga aloqasiz tahrir
            targets = list((await s.execute(select(Target))).scalars())
            rules = [r.text for r in (await s.execute(select(Rule).where(Rule.active))).scalars()]
            analysis = await ai.analyze(msg.message or "", [t for t in targets if t.active], rules) if msg.message else None
            if msg.message and analysis is None:
                if self.notifier:
                    await self.notifier.say("Manba post tahrirlandi, lekin AI ishlamadi — nusxalar yangilanmadi. Qo'lda tekshiring.")
                return
            post.text, post.text_hash = msg.message or "", new_hash
            post.cta_start_line = analysis.cta_start_line if analysis else None
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
            text, ents = build_post(msg.message or "", msg.entities, post.cta_start_line, target.template_html)
            if not fits(text, is_file(msg), self.premium):
                await self._fail_edit(target, "tahrirdan keyin matn limitdan uzun")
                continue
            try:
                await self.client.edit_message(target.username, d.target_msg_ids[idx], text, formatting_entities=ents)
                updated += 1
            except Exception as e:
                log.exception("edit")
                await self._fail_edit(target, str(e))
        await db_log("edited", f"post={post.id} updated={updated}")
        if self.notifier and updated:
            await self.notifier.say(f"Manba post tahrirlandi — {updated} ta kanaldagi nusxa yangilandi.")

    async def _fail_edit(self, target: Target, reason: str) -> None:
        if self.notifier:
            await self.notifier.say(f"Tahrirni sinxronlab bo'lmadi — {target.name}: {reason}")

    # ---------- o'chirish ----------

    async def on_delete(self, chat_id: int, deleted_ids: list[int]) -> None:
        gone = set(deleted_ids)
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
