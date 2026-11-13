"""Asosiy oqim: yangi post -> tahlil -> joylash; tahrir va o'chirishni sinxronlash."""
from __future__ import annotations

import hashlib
import logging
import re
from difflib import SequenceMatcher

from sqlalchemy import select
from telethon import TelegramClient
from telethon.tl.types import MessageMediaWebPage

from . import ai, rules as fallback
from .config import settings
from .db import Decision, Delivery, Rule, Setting, Session, SourcePost, Target, log as db_log, now
from .textops import build_post, fits, message_to_template, parse_message_link

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
DEFAULT_ASK_WORDS = "вакансия, vakansiya, ишга таклиф, ариза юбориш"


# Filial tasdig'i yoqilgan kanalda shu so'zlar uchragan post menejer tasdig'ini kutadi:
# yangi nashr hali filialga yetib kelmagan bo'lishi mumkin. Oddiy sotuv postlari kutmaydi.
DEFAULT_BRANCH_WORDS = "тез кунда, tez kunda, нашрдан чиқди, нашр қилинди, nashrdan chiqdi, nashr qilindi"


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
    cut, kind = post.cta_start_line, post.block_type
    if delivery is not None and delivery.cut_line is not None:
        cut = None if delivery.cut_line < 0 else delivery.cut_line
    if delivery is not None and delivery.closing:
        kind = delivery.closing
    closing = "" if (delivery is not None and delivery.closing == "none") else closing_for(target, kind)
    return cut, closing


def preview_text(post, target, delivery=None) -> str:
    """Post filial kanalida qanday matn bilan chiqishi (formatlashsiz ko'rinish)."""
    cut, closing = final_parts(post, target, delivery)
    return build_post(post.text or "", None, cut, closing)[0]


def text_hash(msg) -> str:
    ents = repr([e.to_dict() for e in (msg.entities or [])])
    return hashlib.sha256(((msg.message or "") + ents).encode()).hexdigest()


def media_id(msg) -> int | None:
    """Xabardagi rasm yoki faylning Telegram ID si (almashtirilganini aniqlash uchun)."""
    if msg.photo:
        return msg.photo.id
    if msg.document:
        return msg.document.id
    return None


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

            memory = list((await s.execute(select(Decision).order_by(Decision.id.desc()).limit(300))).scalars())
            row = await s.get(Setting, "ask_words")
            ask_words = row.value if row is not None else DEFAULT_ASK_WORDS
            row = await s.get(Setting, "branch_words")
            branch_words = row.value if row is not None else DEFAULT_BRANCH_WORDS
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
            plan = []
            for t in targets:
                d = analysis.for_target(t.id) if analysis else None
                action, reason = decide(t.mode, d, settings.confidence_threshold)
                if by_rules and t.mode != "manual":
                    reason = "AI'siz qoidalar: " + reason.replace("AI ishonchi past", "ishonch past")
                word = ask_word_in(text, ask_words)
                if word and action == "send" and t.mode != "manual":
                    action, reason = "ask", f"Matnda «{word}» so'zi bor: tasdiq kerak"
                quiet = False
                past = next((m for m in memory if m.target_id == t.id and similar(m.text, text)), None)
                if past is not None and t.mode != "manual":
                    # Admin o'xshash post bo'yicha qaror bergan: qayta so'ralmaydi
                    action, reason, quiet = ("send" if past.send else "skip"), "Avvalgi qaroringiz bo'yicha", True
                branch = local = False
                scope = getattr(d, "scope", "general") if d is not None else "general"
                if scope == "own" and t.mode != "manual":
                    # Aynan shu filialga tegishli e'lon: har safar filial menejeridan so'raladi
                    # (xotiradagi avvalgi qaror ham buni chetlab o'tmaydi)
                    action, reason, quiet, branch, local = "ask", "Filialga tegishli post: filial tasdig'i kutilmoqda", False, True, True
                elif scope == "other" and t.mode != "manual":
                    action, reason, quiet = "skip", "Post boshqa filialga tegishli", True  # so'ralmaydi ham
                elif t.branch_approval and action == "send" and ask_word_in(text, branch_words):
                    # Yangi nashr yoki "tez kunda": kitob filialga kelgach, menejer o'zi tasdiqlaydi
                    action, reason, quiet, branch = "ask", "Filial tasdig'i kutilmoqda", False, True
                if paused:
                    action, reason, quiet, branch, local = "ask", "Favqulodda to'xtatish yoqilgan", True, False, False
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
                plan.append((delivery.id, t, action, reason, quiet, branch, local))
            await s.commit()
            post_id = post.id
            views = {t.id: preview_text(post, t) for t in targets} if text else {}

        await db_log("new_post", f"post={post_id} ids={ids}")
        for delivery_id, target, action, reason, quiet, branch, local in plan:
            if action == "send":
                await self.publish(delivery_id)
            elif self.notifier and branch:
                await self.notifier.ask_branch(delivery_id, target, views.get(target.id) or "(matnsiz post)", local=local)
            elif self.notifier and not quiet:
                await self.notifier.ask(delivery_id, target, views.get(target.id) or "(matnsiz post)", reason, skipped=action == "skip")

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
        fetched = await self.client.get_messages(self.source, ids=post.msg_ids)
        msgs = [m for m in fetched if m]
        if not msgs:
            await self._fail(delivery_id, target, "Manba post topilmadi")
            return False
        cap = next((m for m in msgs if m.message), msgs[0])
        cut, closing = final_parts(post, target, d)
        text, ents = build_post(cap.message or "", cap.entities, cut, closing)
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
            if is_caption_msg:
                text, ents = build_post(msg.message or "", msg.entities, post.cta_start_line, closing_for(target, post.block_type))
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
