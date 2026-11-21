"""Veb admin panel: kanallar, tasdiq navbati, qoidalar, analitika."""
from __future__ import annotations

import asyncio
import hmac
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, select
from starlette.middleware.sessions import SessionMiddleware

from .config import EPHEMERAL_DB, settings
from .pipeline import is_paused, parse_ids, phone_keys, preview_text, save_admin_ids, set_paused
from .db import Decision, Delivery, Event, Setting, Rule, Session, SourcePost, StatChannel, StatPost, Subscriber, Target, now

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
MODES = {"auto": "Avto", "smart": "Aqlli", "manual": "Qo'lda"}
# Filiallar bo'limlari (hududlar): kanal sozlamasida tanlanadi, analitika va hisobotda guruhlanadi
REGIONS = {"toshkent": "Toshkent", "vodiy": "Vodiy", "voha": "Voha"}


def region_rows(channels: list[dict], members: dict) -> list[dict]:
    """Bo'limlar bo'yicha yig'indi: faqat faol, to'liq nusxa bo'lmagan kanallar."""
    out = []
    for key, name in REGIONS.items():
        chans = [c for c in channels if c["region"] == key and c["active"] and not c["mirror"]]
        if not chans:
            continue
        subs = [c["subs"] for c in chans if c["subs"] is not None]
        growth = [c["growth"] for c in chans if c["growth"] is not None]
        start = sum(subs) - sum(growth) if subs and growth else None
        # Qamrov obunachilar soni bo'yicha tortilgan o'rtacha: katta kanal ko'proq ta'sir qiladi
        weighted = [(c["reach"], c["subs"]) for c in chans if c["reach"] is not None and c["subs"]]
        reach = round(sum(r * n for r, n in weighted) / sum(n for _, n in weighted)) if weighted else None
        hashes = [members.get(c["id"], set()) for c in chans]
        listed = sum(len(h) for h in hashes)
        out.append({
            "key": key, "name": name, "channels": len(chans),
            "subs": sum(subs) if subs else None, "growth": sum(growth) if growth else None,
            "growth_pct": round(100 * sum(growth) / start, 1) + 0.0 if start else None,  # -0.0 -> 0.0
            "posts": sum(c["posts"] for c in chans), "reach": reach,
            # Bo'lim ichida bir odam bir necha kanalda bo'lsa bir marta sanaladi
            "unique": len(set().union(*hashes)) if listed else None, "listed": listed or None,
        })
    return out


def _go(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


async def dashboard_data(days: int) -> dict:
    since = period_start(days)
    async with Session() as s:
        targets = list((await s.execute(select(Target).order_by(Target.id))).scalars())
        chan = list((await s.execute(select(StatChannel).where(StatChannel.at >= since).order_by(StatChannel.at))).scalars())
        sent = list((await s.execute(select(Delivery).where(Delivery.status == "sent", Delivery.sent_at >= since))).scalars())
        pending = len(list((await s.execute(select(Delivery.id).where(Delivery.status == "pending", Delivery.reason != BRANCH_WAIT))).scalars()))
        ids = [d.id for d in sent]
        posts = list((await s.execute(select(StatPost).where(StatPost.delivery_id.in_(ids)).order_by(StatPost.at))).scalars()) if ids else []
        events = list((await s.execute(select(Event).where(Event.created_at >= since))).scalars())

    first, last = {}, {}
    daily: dict = defaultdict(dict)  # kun -> {target_id: oxirgi qiymat}
    for c in chan:
        first.setdefault(c.target_id, c.subscribers)
        last[c.target_id] = c.subscribers
        daily[c.at.date().isoformat()][c.target_id] = c.subscribers
    views = {p.delivery_id: p.views for p in posts}  # har bir nusxaning eng so'nggi ko'rishlar soni

    rows, running = [], {}
    for t in targets:
        mine = [d for d in sent if d.target_id == t.id]
        v = [views[d.id] for d in mine if d.id in views]
        avg = round(sum(v) / len(v)) if v else None
        subs = last.get(t.id)
        rows.append({
            "t": t, "subs": subs,
            "growth": subs - first[t.id] if subs is not None else None,
            "posts": len(mine), "views": avg,
            "reach": round(100 * avg / subs) if avg is not None and subs else None,
        })
    labels, series = [], []
    for day in sorted(daily):
        running.update(daily[day])
        labels.append(day)
        series.append(sum(running.values()))
    reach = [r["reach"] for r in rows if r["reach"] is not None]
    return {
        "rows": rows, "labels": labels, "series": series, "pending": pending,
        "total_subs": sum(v for v in last.values()) if last else None,
        "total_growth": sum(r["growth"] for r in rows if r["growth"] is not None) if last else None,
        "total_posts": len(sent),
        "avg_reach": round(sum(reach) / len(reach)) if reach else None,
        "edited": sum(1 for e in events if e.kind == "edited"),
        "deleted": sum(1 for e in events if e.kind == "deleted"),
    }


TASHKENT = timezone(timedelta(hours=5))


def period_start(days: int) -> datetime:
    """Davr boshi. days=0 — bugun (Toshkent vaqti bo'yicha yarim tundan), qolganlari — shuncha kun oldin."""
    if days == 0:
        local = now().astimezone(TASHKENT)
        return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    return now() - timedelta(days=days)
EVENT_LABELS = {
    "new_post": "Yangi post", "sent": "Yuborildi", "failed": "Xatolik", "edited": "Tahrir sinxronlandi",
    "deleted": "O'chirish sinxronlandi", "account_started": "Akkaunt ulandi", "account_lost": "Akkaunt uzildi",
    "source_changed": "Manba o'zgartirildi", "ai_failed": "AI ishlamadi (zaxira qoidalar)",
    "report_sent": "Hisobot yuborildi", "rating_sent": "Reyting yuborildi", "paused": "Favqulodda to'xtatildi", "resumed": "Davom ettirildi", "recalled": "Post qaytarib olindi", "resend": "Qo'lda yuborildi", "catch_up": "O'tkazib yuborilgan postlar", "admins_changed": "Bot adminlari o'zgartirildi", "ask_managers": "Menejerlardan so'raldi", "stats_failed": "Statistika yig'ilmadi", "delete_ignored": "Noto'g'ri o'chirish signali e'tiborsiz qoldirildi",
}


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)  # SQLite vaqtni zonasiz qaytaradi


def local_time(dt: datetime | None) -> str:
    return _utc(dt).astimezone(TASHKENT).strftime("%d.%m %H:%M") if dt else "—"


def views_at(snapshots: list, sent_at: datetime, hours: int, at_time: datetime) -> int | None:
    """Post yuborilganidan `hours` soat o'tgandagi ko'rishlar soni (statistika soatiga bir yig'iladi)."""
    deadline = _utc(sent_at) + timedelta(hours=hours)
    if at_time < deadline:
        return None  # hali shuncha vaqt o'tmagan
    past = [v for at, v in snapshots if _utc(at) <= deadline + timedelta(minutes=40)]
    return past[-1] if past else None


def _total(values: list) -> int | None:
    values = [v for v in values if v is not None]
    return sum(values) if values else None


async def posts_data(days: int) -> dict:
    since = period_start(days)
    async with Session() as s:
        posts = list((await s.execute(
            select(SourcePost).where(SourcePost.created_at >= since).order_by(SourcePost.id.desc()).limit(100)
        )).scalars())
        ids = [p.id for p in posts]
        sent = list((await s.execute(
            select(Delivery).where(Delivery.post_id.in_(ids), Delivery.status.in_(("sent", "deleted")), Delivery.sent_at.is_not(None))
        )).scalars()) if ids else []
        failed_counts: dict = defaultdict(int)
        for pid in (await s.execute(select(Delivery.post_id).where(Delivery.post_id.in_(ids), Delivery.status == "failed"))).scalars() if ids else []:
            failed_counts[pid] += 1
        d_ids = [d.id for d in sent]
        stats_rows = list((await s.execute(
            select(StatPost).where(StatPost.delivery_id.in_(d_ids)).order_by(StatPost.at)
        )).scalars()) if d_ids else []
    snaps: dict = defaultdict(list)
    reach: dict = {}
    for r in stats_rows:
        snaps[r.delivery_id].append((r.at, r.views))
        if r.reach is not None:
            reach[r.delivery_id] = r.reach
    t = now()
    rows = []
    for p in posts:
        mine = [d for d in sent if d.post_id == p.id]
        rr = [reach[d.id] for d in mine if d.id in reach]
        rows.append({
            "id": p.id, "text": (p.text or "(matnsiz post)").split("\n")[0][:90], "when": local_time(p.created_at),
            "deleted": p.deleted, "channels": len(mine), "live": sum(1 for d in mine if d.status == "sent"),
            "failed": failed_counts.get(p.id, 0),
            "v1": _total([views_at(snaps[d.id], d.sent_at, 1, t) for d in mine]),
            "v24": _total([views_at(snaps[d.id], d.sent_at, 24, t) for d in mine]),
            "v72": _total([views_at(snaps[d.id], d.sent_at, 72, t) for d in mine]),
            "now": _total([snaps[d.id][-1][1] if snaps[d.id] else None for d in mine]),
            "reach": round(100 * sum(rr) / len(rr)) if rr else None,
        })
    ranked = sorted((r for r in rows if r["reach"] is not None), key=lambda r: r["reach"], reverse=True)
    return {"rows": rows, "best": ranked[:3], "worst": ranked[-3:][::-1] if len(ranked) > 3 else []}


BRANCH_WAIT = "Filial tasdig'i kutilmoqda"  # bunday postlar adminni emas, filial menejerini kutadi

WEEKDAYS = ["Dushanba", "Seshanba", "Chorshanba", "Payshanba", "Juma", "Shanba", "Yakshanba"]


async def compare_data(days: int) -> dict:
    """Filial kanallarini solishtirish uchun qatorlar."""
    data = await dashboard_data(days)
    channels = []
    for r in data["rows"]:
        subs, growth = r["subs"], r["growth"]
        start = subs - growth if subs is not None and growth is not None else None
        channels.append({
            "id": r["t"].id, "name": r["t"].name, "active": r["t"].active, "subs": subs, "growth": growth,
            "mirror": bool(getattr(r["t"], "mirror", False)), "region": getattr(r["t"], "region", "") or "",
            "growth_pct": round(100 * growth / start, 1) if start else None,
            "posts": r["posts"], "views": r["views"], "reach": r["reach"],
        })
    # Unikal auditoriya: bir odam bir nechta kanalda bo'lsa bir marta sanaladi
    async with Session() as s:
        rows = (await s.execute(select(Subscriber.target_id, Subscriber.user_hash))).all()
    members: dict = defaultdict(set)
    for target_id, h in rows:
        members[target_id].add(h)
    seen: dict = defaultdict(int)  # xesh -> nechta kanalda uchraydi
    for hashes in members.values():
        for h in hashes:
            seen[h] += 1
    for c in channels:
        mine = members.get(c["id"], set())
        c["listed"] = len(mine) if mine else None
        c["only_here"] = sum(1 for h in mine if seen[h] == 1) if mine else None
        c["shared"] = sum(1 for h in mine if seen[h] > 1) if mine else None
    listed = sum(len(m) for m in members.values())
    total_subs = sum(c["subs"] or 0 for c in channels)
    audience = {
        "listed": listed, "unique": len(seen), "multi": sum(1 for n in seen.values() if n > 1),
        "total_subs": total_subs or None,
        "coverage": round(100 * min(listed, total_subs) / total_subs) if total_subs and listed else None,
    }
    return {
        "channels": channels, "avg_reach": data["avg_reach"], "days": days, "audience": audience,
        "regions": region_rows(channels, members), "region_names": REGIONS,
        "unassigned": sum(1 for c in channels if c["active"] and not c["mirror"] and c["region"] not in REGIONS),
    }


async def _deliveries_with_stats(since: datetime, target_id: int | None = None):
    """Yuborilgan nusxalar va har birining eng so'nggi ko'rishlar soni / qamrovi."""
    async with Session() as s:
        q = select(Delivery).where(Delivery.status.in_(("sent", "deleted")), Delivery.sent_at >= since)
        if target_id:
            q = q.where(Delivery.target_id == target_id)
        deliveries = list((await s.execute(q.order_by(Delivery.sent_at.desc()))).scalars())
        ids = [d.id for d in deliveries]
        rows = list((await s.execute(select(StatPost).where(StatPost.delivery_id.in_(ids)).order_by(StatPost.at))).scalars()) if ids else []
        post_ids = {d.post_id for d in deliveries}
        posts = {p.id: p for p in (await s.execute(select(SourcePost).where(SourcePost.id.in_(post_ids)))).scalars()} if post_ids else {}
    views, reach = {}, {}
    for r in rows:
        views[r.delivery_id] = r.views
        if r.reach is not None:
            reach[r.delivery_id] = r.reach
    return deliveries, posts, views, reach


async def channel_data(target_id: int, days: int) -> dict | None:
    since = period_start(days)
    async with Session() as s:
        target = await s.get(Target, target_id)
        if target is None:
            return None
        chan = list((await s.execute(
            select(StatChannel).where(StatChannel.target_id == target_id, StatChannel.at >= since).order_by(StatChannel.at)
        )).scalars())
    if len(chan) > 400:  # uzoq davrda kuniga bitta (oxirgi) nuqta yetarli
        by_day = {}
        for c in chan:
            by_day[_utc(c.at).astimezone(TASHKENT).date()] = c
        chan = list(by_day.values())
    series = [{"t": _utc(c.at).astimezone(TASHKENT).strftime("%d.%m %H:%M"), "subs": c.subscribers} for c in chan]
    deliveries, posts, views, reach = await _deliveries_with_stats(since, target_id)
    rows = []
    for d in deliveries:
        post = posts.get(d.post_id)
        rows.append({
            "text": ((post.text if post else "") or "(matnsiz post)").split("\n")[0][:90],
            "when": local_time(d.sent_at), "deleted": d.status == "deleted",
            "views": views.get(d.id), "reach": round(100 * reach[d.id]) if d.id in reach else None,
        })
    ranked = sorted((r for r in rows if r["reach"] is not None), key=lambda r: r["reach"], reverse=True)
    vv = [r["views"] for r in rows if r["views"] is not None]
    rr = [r["reach"] for r in rows if r["reach"] is not None]
    subs = chan[-1].subscribers if chan else None
    return {
        "name": target.name, "username": target.username, "active": target.active, "days": days,
        "series": series, "subs": subs, "growth": subs - chan[0].subscribers if chan else None,
        "posts_count": len(rows), "avg_views": round(sum(vv) / len(vv)) if vv else None,
        "avg_reach": round(sum(rr) / len(rr)) if rr else None,
        "posts": rows[:100], "best": ranked[:3], "worst": ranked[-3:][::-1] if len(ranked) > 3 else [],
    }


async def besttime_data(days: int, target_id: int | None = None) -> dict:
    """Hafta kuni va soat bo'yicha o'rtacha qamrov (Toshkent vaqti)."""
    since = period_start(days)
    deliveries, _, views, reach = await _deliveries_with_stats(since, target_id)
    cutoff = now() - timedelta(hours=24)
    # Adolatli solishtirish uchun kamida 24 soat yig'ilgan postlar; ular bo'lmasa — borini olamiz
    mature = [d for d in deliveries if _utc(d.sent_at) <= cutoff]
    used = mature or deliveries
    buckets: dict = defaultdict(lambda: {"reach": [], "views": []})
    for d in used:
        local = _utc(d.sent_at).astimezone(TASHKENT)
        b = buckets[(local.weekday(), local.hour)]
        if d.id in reach:
            b["reach"].append(100 * reach[d.id])
        if d.id in views:
            b["views"].append(views[d.id])
    cells = []
    for (dow, hour), b in buckets.items():
        if not b["views"] and not b["reach"]:
            continue
        cells.append({
            "dow": dow, "hour": hour, "posts": max(len(b["views"]), len(b["reach"])),
            "reach": round(sum(b["reach"]) / len(b["reach"]), 1) if b["reach"] else None,
            "views": round(sum(b["views"]) / len(b["views"])) if b["views"] else None,
        })
    metric = "reach" if any(c["reach"] is not None for c in cells) else "views"
    top = sorted((c for c in cells if c[metric] is not None), key=lambda c: c[metric], reverse=True)[:3]
    return {"cells": cells, "metric": metric, "top": top, "weekdays": WEEKDAYS, "sample": len(used), "mature_only": bool(mature)}


def svg_points(series: list[int], w: int = 600, h: int = 200) -> str:
    """Chiziqli grafik uchun SVG nuqtalari."""
    if len(series) < 2:
        return ""
    lo, hi = min(series), max(series)
    span = (hi - lo) or 1
    step = w / (len(series) - 1)
    return " ".join(f"{i * step:.1f},{h - 10 - (v - lo) / span * (h - 20):.1f}" for i, v in enumerate(series))


def create_app(agent=None, on_shutdown=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app):
        yield
        if on_shutdown:
            await on_shutdown()  # server to'xtayotganda (SIGTERM) chaqiriladi

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    # Chart.js kutubxonasi panel ichidan beriladi (tashqi saytga bog'liq emas)
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

    @app.middleware("http")
    async def auth(request: Request, call_next):
        public = request.url.path == "/login" or request.url.path in ("/static/logo.png", "/static/mark.png")
        if not public and not request.session.get("user"):
            if request.url.path.startswith("/api/"):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return _go("/login")
        if request.method == "GET" and not request.url.path.startswith(("/api/", "/static/")) and request.url.path != "/login":
            request.state.pending = await pending_count()  # menyudagi navbat belgisi uchun
            request.state.paused = await is_paused()
        return await call_next(request)

    # Oxirgi qo'shilgan middleware eng tashqarida ishlaydi: sessiya auth'dan oldin ochilishi kerak
    app.add_middleware(
        SessionMiddleware, secret_key=settings.secret_key, same_site="strict",
        https_only=settings.cookie_secure, max_age=7 * 24 * 3600,
    )

    async def pending_count() -> int:
        async with Session() as s:
            return len(list((await s.execute(select(Delivery.id).where(Delivery.status == "pending", Delivery.reason != BRANCH_WAIT))).scalars()))

    def page(request: Request, name: str, **ctx):
        connected = bool(agent and agent.status.get("connected"))
        return templates.TemplateResponse(
            request, name,
            {"modes": MODES, "regions": REGIONS, "path": request.url.path, "connected": connected, "ephemeral_db": EPHEMERAL_DB,
             "pending_count": getattr(request.state, "pending", 0),
             "paused": getattr(request.state, "paused", False), **ctx},
        )

    # ---------- kirish ----------

    @app.get("/login")
    async def login_form(request: Request):
        return page(request, "login.html", error=False)

    @app.post("/login")
    async def login(request: Request, username: str = Form(""), password: str = Form("")):
        ok = hmac.compare_digest(username.encode(), settings.admin_user.encode()) & hmac.compare_digest(
            password.encode(), settings.admin_password.encode()
        )
        if not ok or not settings.admin_password:
            await asyncio.sleep(1)  # parol tanlashni sekinlashtirish
            return page(request, "login.html", error=True)
        request.session["user"] = username
        return _go("/")

    @app.post("/logout")
    async def logout(request: Request):
        request.session.clear()
        return _go("/login")

    # ---------- dashboard ----------

    @app.get("/")
    async def dashboard(request: Request, days: int = 30):
        days = days if days in (7, 30, 90) else 30
        data = await dashboard_data(days)
        return page(request, "dashboard.html", days=days, points=svg_points(data["series"]), **data)

    # ---------- favqulodda to'xtatish va qaytarib olish ----------

    @app.post("/pause")
    async def pause(request: Request, state: str = Form("")):
        await set_paused(state == "on")
        if agent is not None and getattr(agent, "notifier", None):
            await agent.notifier.say("Favqulodda to'xtatish yoqildi (paneldan)." if state == "on" else "Agent davom ettirildi (paneldan).")
        return _go(request.headers.get("referer") or "/")

    @app.post("/posts/{post_id}/resend")
    async def post_resend(request: Request, post_id: int, target_id: int = Form(0)):
        if agent is None or agent.pipeline is None:
            request.session["warning"] = "Telegram akkaunt ulanmagan — postni yuborib bo'lmaydi."
            return _go("/posts")
        async with Session() as s:
            target = await s.get(Target, target_id)
        if target is None:
            request.session["warning"] = "Kanal tanlanmagan."
        elif await agent.pipeline.resend(post_id, target_id):
            request.session["saved"] = f"Post {target.name} kanaliga yuborildi."
        else:
            request.session["warning"] = f"Post {target.name} kanaliga yuborilmadi — Jurnal sahifasida sababini ko'ring."
        return _go("/posts")

    @app.post("/posts/{post_id}/ask-managers")
    async def post_ask_managers(request: Request, post_id: int, target_id: int = Form(0)):
        if agent is None or agent.pipeline is None:
            request.session["warning"] = "Telegram akkaunt ulanmagan."
            return _go("/posts")
        asked, without = await agent.pipeline.ask_managers(post_id, target_id or None)
        note = f"{asked} ta filial menejeriga so'rov yuborildi."
        if without:
            note += f" {without} ta filialda menejer botga bog'lanmagan — ularga yuborilmadi."
        request.session["saved" if asked else "warning"] = (
            note if asked or without else "Post tanlangan kanal(lar)ga allaqachon chiqqan — so'rov yuborilmadi."
        )
        return _go("/posts")

    @app.post("/posts/{post_id}/republish")
    async def post_republish(request: Request, post_id: int):
        if agent is None or agent.pipeline is None:
            request.session["warning"] = "Telegram akkaunt ulanmagan."
        else:
            ok, total = await agent.pipeline.republish(post_id)
            request.session["saved" if ok == total and total else "warning"] = (
                f"Post {total} ta kanaldan olinib, {ok} tasiga qaytadan yuborildi." if total else "Post hech bir kanalga chiqmagan."
            )
        return _go("/posts")

    @app.post("/posts/{post_id}/retry")
    async def post_retry(request: Request, post_id: int):
        if agent is None or agent.pipeline is None:
            request.session["warning"] = "Telegram akkaunt ulanmagan — postni yuborib bo'lmaydi."
        else:
            ok, total = await agent.pipeline.retry_failed(post_id)
            key = "saved" if ok == total else "warning"
            request.session[key] = f"Xato bergan {total} ta kanaldan {ok} tasiga qayta yuborildi."
        return _go("/posts")

    @app.post("/posts/{post_id}/recall")
    async def post_recall(request: Request, post_id: int):
        if agent is None or agent.pipeline is None:
            request.session["warning"] = "Telegram akkaunt ulanmagan — postni qaytarib bo'lmaydi."
        else:
            n = await agent.pipeline.recall(post_id)
            request.session["saved"] = f"Post {n} ta filial kanalidan o'chirildi."
        return _go("/posts")

    # ---------- analitika ----------

    def _days(days: int) -> int:
        return days if days in (0, 7, 30, 90) else 30

    @app.get("/analytics")
    async def analytics(request: Request):
        async with Session() as s:
            targets = list((await s.execute(select(Target).order_by(Target.id))).scalars())
        return page(request, "analytics.html", targets=targets)

    @app.get("/channels/{target_id}/stats")
    async def channel_stats(request: Request, target_id: int):
        async with Session() as s:
            target = await s.get(Target, target_id)
        if target is None:
            return _go("/channels")
        return page(request, "channel_stats.html", t=target)

    @app.get("/api/compare")
    async def api_compare(days: int = 30):
        return await compare_data(_days(days))

    @app.get("/api/channel/{target_id}")
    async def api_channel(target_id: int, days: int = 30):
        data = await channel_data(target_id, _days(days))
        return data if data is not None else JSONResponse({"error": "not found"}, status_code=404)

    @app.get("/api/besttime")
    async def api_besttime(days: int = 30, target_id: int = 0):
        return await besttime_data(_days(days), target_id or None)

    # ---------- postlar va jurnal ----------

    @app.get("/posts")
    async def posts(request: Request, days: int = 30):
        days = days if days in (7, 30, 90) else 30
        async with Session() as s:
            targets = list((await s.execute(select(Target).where(Target.active).order_by(Target.id))).scalars())
        return page(
            request, "posts.html", days=days, targets=targets, saved=request.session.pop("saved", ""),
            warning=request.session.pop("warning", ""), **(await posts_data(days)),
        )

    @app.get("/log")
    async def journal(request: Request):
        async with Session() as s:
            events = list((await s.execute(select(Event).order_by(Event.id.desc()).limit(200))).scalars())
        items = [
            {"when": local_time(e.created_at), "label": EVENT_LABELS.get(e.kind, e.kind), "detail": e.detail,
             "bad": e.kind in ("failed", "account_lost"), "warn": e.kind in ("ai_failed", "paused", "recalled", "stats_failed")}
            for e in events
        ]
        return page(request, "log.html", items=items)

    # ---------- kanallar ----------

    @app.get("/channels")
    async def channels(request: Request):
        async with Session() as s:
            targets = list((await s.execute(select(Target).order_by(Target.id))).scalars())
        return page(
            request, "channels.html", targets=targets, source=agent.source_username if agent else "",
            warning=request.session.pop("warning", ""), saved=request.session.pop("saved", ""),
        )

    @app.post("/source")
    async def source_save(request: Request, username: str = Form("")):
        error = await agent.set_source(username)
        request.session["warning" if error else "saved"] = error or "Manba kanal o'zgartirildi."
        return _go("/channels")

    @app.get("/channels/{target_id}")
    async def channel_form(request: Request, target_id: int, saved: int = 0):
        async with Session() as s:
            target = await s.get(Target, target_id) if target_id else None
        if target_id and target is None:
            return _go("/channels")
        return page(request, "channel.html", t=target, saved=saved, warning=request.session.pop("warning", ""))

    @app.post("/channels/{target_id}")
    async def channel_save(
        request: Request, target_id: int,
        name: str = Form(...), username: str = Form(...), mode: str = Form("smart"),
        template_html: str = Form(""), signature_html: str = Form(""), keywords: str = Form(""),
        sync_edits: str = Form(""), sync_deletes: str = Form(""), silent: str = Form(""),
        manager_phones: str = Form(""), branch_approval: str = Form(""), script: str = Form("cyr"), mirror: str = Form(""),
        region: str = Form(""),
    ):
        username = username.strip().removeprefix("https://t.me/").lstrip("@")
        async with Session() as s:
            target = await s.get(Target, target_id) if target_id else Target()
            if target is None:
                return _go("/channels")
            clash = (await s.execute(select(Target).where(Target.username == username))).scalars().first()
            if clash and clash.id != target.id:
                request.session["warning"] = "Bu kanal allaqachon qo'shilgan."
                return _go(f"/channels/{target_id}")
            target.name, target.username = name.strip(), username
            target.mode = mode if mode in MODES else "smart"
            target.template_html = template_html.replace("\r\n", "\n").strip()
            target.signature_html = signature_html.replace("\r\n", "\n").strip()
            target.keywords = keywords.strip()
            target.sync_edits, target.sync_deletes, target.silent = bool(sync_edits), bool(sync_deletes), bool(silent)
            target.manager_phones, target.branch_approval = manager_phones.strip(), bool(branch_approval)
            target.mirror = bool(mirror)
            target.region = region if region in REGIONS and not target.mirror else ""
            target.latin = script in ("new", "old")
            if target.latin:
                target.latin_style = script
            # Ro'yxatdan olib tashlangan raqam egasi endi tasdiqlay olmaydi
            keys = phone_keys(target.manager_phones)
            target.manager_ids = {uid: k for uid, k in (target.manager_ids or {}).items() if k in keys}
            s.add(target)
            await s.commit()
            new_id = target.id
        if agent is not None and agent.pipeline is not None:
            problem = await agent.pipeline.check_target(username)
            if problem:
                request.session["warning"] = problem
        return _go(f"/channels/{new_id}?saved=1")

    @app.post("/channels/{target_id}/template-import")
    async def channel_template_import(request: Request, target_id: int, link: str = Form("")):
        if agent is None or agent.pipeline is None:
            request.session["warning"] = "Telegram akkaunt ulanmagan — xabarni o'qib bo'lmaydi."
            return _go(f"/channels/{target_id}")
        template, error = await agent.pipeline.fetch_template(link)
        if error:
            request.session["warning"] = error
            return _go(f"/channels/{target_id}")
        async with Session() as s:
            target = await s.get(Target, target_id)
            if target is None:
                return _go("/channels")
            target.template_html = template
            await s.commit()
        if "<tg-emoji" in template and not agent.status.get("premium"):
            request.session["warning"] = "Shablonda premium emoji bor, lekin agent akkauntida Premium yo'q: Telegram ularni oddiy emojiga almashtiradi."
        return _go(f"/channels/{target_id}?saved=1")

    @app.post("/channels/{target_id}/toggle")
    async def channel_toggle(target_id: int):
        async with Session() as s:
            target = await s.get(Target, target_id)
            if target:
                target.active = not target.active
                await s.commit()
        return _go("/channels")

    @app.post("/channels/{target_id}/delete")
    async def channel_delete(target_id: int):
        async with Session() as s:
            ids = list((await s.execute(select(Delivery.id).where(Delivery.target_id == target_id))).scalars())
            if ids:
                await s.execute(delete(StatPost).where(StatPost.delivery_id.in_(ids)))
                await s.execute(delete(Delivery).where(Delivery.id.in_(ids)))
            await s.execute(delete(StatChannel).where(StatChannel.target_id == target_id))
            await s.execute(delete(Decision).where(Decision.target_id == target_id))
            await s.execute(delete(Subscriber).where(Subscriber.target_id == target_id))
            await s.execute(delete(Target).where(Target.id == target_id))
            await s.commit()
        return _go("/channels")

    # ---------- tasdiq navbati ----------

    @app.get("/queue")
    async def queue(request: Request):
        async with Session() as s:
            deliveries = list(
                (await s.execute(
                    select(Delivery).where(Delivery.status.in_(("pending", "skipped", "failed")))
                    .order_by(Delivery.id.desc()).limit(100)
                )).scalars()
            )
            # Post shu kanalga boshqa yo'l bilan (menejer, qo'lda) allaqachon chiqqan bo'lsa — navbatda ko'rsatilmaydi
            sent_pairs = set((await s.execute(
                select(Delivery.post_id, Delivery.target_id).where(
                    Delivery.status == "sent", Delivery.post_id.in_({d.post_id for d in deliveries} or {0})
                )
            )).all())
            items = []
            for d in deliveries:
                if (d.post_id, d.target_id) in sent_pairs:
                    continue
                post = await s.get(SourcePost, d.post_id)
                if post is None or post.deleted:
                    continue
                target = await s.get(Target, d.target_id)
                lines = [(i, line[:70]) for i, line in enumerate((post.text or "").split("\n")) if i > 0 and line.strip()]
                cut = d.cut_line if d.cut_line is not None else (post.cta_start_line if post.cta_start_line is not None else -1)
                items.append({
                    "d": d, "target": target, "text": post.text or "(matnsiz post)",
                    "preview": (preview_text(post, target, d) if target else "") or "(matnsiz post)",
                    "lines": lines, "cut": cut, "closing": d.closing or post.block_type or "contact",
                })
        mine = [i for i in items if i["d"].reason != BRANCH_WAIT]
        branch = [i for i in items if i["d"].reason == BRANCH_WAIT]
        return page(request, "queue.html", items=mine, branch=branch, warning=request.session.pop("warning", ""))

    @app.post("/queue/{delivery_id}/adjust")
    async def queue_adjust(delivery_id: int, cut_line: int = Form(-1), closing: str = Form("contact")):
        if agent is not None and agent.pipeline is not None:
            await agent.pipeline.adjust(delivery_id, cut_line, closing)
        return _go("/queue")

    @app.post("/queue/{delivery_id}/send")
    async def queue_send(request: Request, delivery_id: int):
        if agent is None or agent.pipeline is None:
            request.session["warning"] = "Telegram akkaunt ulanmagan — post yuborilmadi."
        else:
            await agent.pipeline.approve(delivery_id)
        return _go("/queue")

    @app.post("/queue/{delivery_id}/dismiss")
    async def queue_dismiss(delivery_id: int):
        """Navbatdan olib tashlash: hech narsa yuborilmaydi va o'chirilmaydi, qaror ham eslab qolinmaydi."""
        async with Session() as s:
            d = await s.get(Delivery, delivery_id)
            if d and d.status in ("pending", "skipped", "failed"):
                d.status, d.reason = "rejected", "Admin navbatdan olib tashladi"
                await s.commit()
        return _go("/queue")

    @app.post("/queue/{delivery_id}/reject")
    async def queue_reject(delivery_id: int):
        if agent is not None and agent.pipeline is not None:
            await agent.pipeline.reject(delivery_id)  # qaror eslab qolinadi
        else:
            async with Session() as s:
                d = await s.get(Delivery, delivery_id)
                if d and d.status in ("pending", "skipped", "failed"):
                    d.status = "rejected"
                    await s.commit()
        return _go("/queue")

    # ---------- Telegram akkaunt ----------

    @app.get("/account")
    async def account(request: Request):
        return page(
            request, "account.html", status=agent.status, step=agent.login_step,
            error=request.session.pop("warning", "") or agent.login_error,
            problems=request.session.pop("problems", []), hint=agent.login_hint, qr_svg=agent.qr_svg(),
            saved=request.session.pop("saved", ""),
            admin_ids=", ".join(str(i) for i in settings.admin_ids),
        )

    @app.post("/account/qr")
    async def account_qr(request: Request):
        error = await agent.begin_qr()
        if error:
            request.session["warning"] = error
        return _go("/account")

    @app.post("/account/admins")
    async def account_admins(request: Request, ids: str = Form("")):
        parsed = parse_ids(ids)
        if not parsed:
            request.session["warning"] = "Kamida bitta Telegram ID kiriting (faqat raqamlar)."
            return _go("/account")
        await save_admin_ids(parsed)
        note = "Bot bosh adminlari saqlandi. Yangi admin botga bir marta /start bosishi kerak."
        if not getattr(agent, "notifier", None):
            note += " Bot hozir ishlamayapti: TG_BOT_TOKEN ni tekshiring yoki xizmatni qayta ishga tushiring."
        request.session["saved"] = note
        return _go("/account")

    @app.post("/account/phone")
    async def account_phone(request: Request, phone: str = Form("")):
        phone = "+" + "".join(ch for ch in phone if ch.isdigit())
        error = await agent.begin_login(phone) if len(phone) > 8 else "Telefon raqamni xalqaro formatda kiriting."
        if error:
            request.session["warning"] = error
        return _go("/account")

    @app.post("/account/confirm")
    async def account_confirm(request: Request, code: str = Form(""), password: str = Form("")):
        error = await agent.finish_login(code=code, password=password)
        if error:
            request.session["warning"] = error
        elif agent.status.get("connected"):
            request.session["problems"] = await agent.check_targets()
        return _go("/account")

    @app.post("/account/cancel")
    async def account_cancel():
        await agent.cancel_login()
        return _go("/account")

    @app.post("/account/logout")
    async def account_logout():
        await agent.logout()
        return _go("/account")

    # ---------- integratsiyalar ----------

    @app.get("/integrations")
    async def integrations(request: Request):
        import os

        st = agent.status if agent else {}
        connected = [
            {
                "name": "Telegram akkaunt", "ok": bool(st.get("connected")), "link": "/account",
                "role": "Manbani o'qish, postlarni joylash, sinxronlash, analitika",
                "detail": f"{st.get('name', '')} · manba: @{st.get('source', '')}" if st.get("connected") else st.get("error", "Ulanmagan"),
            },
            {
                "name": "Telegram bot", "ok": bool(getattr(agent, "notifier", None)), "link": "",
                "role": "Adminga hisobot va tasdiq tugmalari",
                "detail": f"{len(settings.admin_ids)} ta admin" if getattr(agent, "notifier", None) else "TG_BOT_TOKEN va ADMIN_IDS o'zgaruvchilarini tekshiring",
            },
            {
                "name": "Claude API", "ok": bool(os.environ.get("ANTHROPIC_API_KEY")), "link": "",
                "role": "Postni tahlil qilish va qaror qabul qilish",
                "detail": f"Model: {settings.ai_model}" if os.environ.get("ANTHROPIC_API_KEY") else "ANTHROPIC_API_KEY o'rnatilmagan",
            },
        ]
        is_pg = settings.database_url.startswith("postgresql")
        connected.append({
            "name": "Ma'lumotlar bazasi", "ok": not EPHEMERAL_DB, "link": "",
            "role": "Kanallar, sozlamalar, Telegram sessiyasi, statistika",
            "detail": "PostgreSQL" if is_pg else (
                "Vaqtinchalik SQLite: har bir yangilanishda hamma narsa o'chadi. DATABASE_URL ni tekshiring."
                if EPHEMERAL_DB else "SQLite (lokal fayl)"
            ),
        })
        planned = [
            {"name": "Instagram", "stage": "Keyingi bosqich", "role": "Post va Reels joylash", "detail": "Biznes akkaunt va unga bog'langan Facebook sahifasi kerak"},
            {"name": "Facebook sahifa", "stage": "Keyingi bosqich", "role": "Sahifaga post joylash", "detail": "Instagram bilan bitta ulanish orqali ishlaydi"},
            {"name": "YouTube", "stage": "Rejada", "role": "Shorts va video joylash", "detail": "Google akkaunt orqali ulanadi"},
        ]
        return page(request, "integrations.html", items=connected, planned=planned)

    # ---------- qoidalar ----------

    @app.get("/rules")
    async def rules(request: Request):
        async with Session() as s:
            items = list((await s.execute(select(Rule).order_by(Rule.id))).scalars())
            row = await s.get(Setting, "ask_words")
            brow = await s.get(Setting, "branch_words")
            erow = await s.get(Setting, "event_words")
            srow = await s.get(Setting, "skip_words")
            orow = await s.get(Setting, "open_text")
            crow = await s.get(Setting, "closed_text")
            vrow = await s.get(Setting, "open_text_vodiy")
            ocrow = await s.get(Setting, "ocr_confirm")
        from .pipeline import (
            DEFAULT_ASK_WORDS, DEFAULT_BRANCH_WORDS, DEFAULT_CLOSED_TEXT, DEFAULT_EVENT_WORDS, DEFAULT_OPEN_TEXT, DEFAULT_OPEN_TEXT_VODIY,
            DEFAULT_SKIP_WORDS,
        )

        return page(
            request, "rules.html", rules=items, ask_words=row.value if row is not None else DEFAULT_ASK_WORDS,
            branch_words=brow.value if brow is not None else DEFAULT_BRANCH_WORDS,
            event_words=erow.value if erow is not None else DEFAULT_EVENT_WORDS,
            skip_words=srow.value if srow is not None else DEFAULT_SKIP_WORDS,
            open_text=orow.value if orow is not None and orow.value.strip() else DEFAULT_OPEN_TEXT,
            open_text_vodiy=vrow.value if vrow is not None and vrow.value.strip() else DEFAULT_OPEN_TEXT_VODIY,
            ocr_confirm=not (ocrow is not None and ocrow.value == "0"),
            closed_text=crow.value if crow is not None and crow.value.strip() else DEFAULT_CLOSED_TEXT,
            saved=request.session.pop("saved", ""),
        )

    @app.post("/rules/ask-words")
    async def ask_words_save(request: Request, words: str = Form(""), kind: str = Form("ask_words")):
        key = kind if kind in ("ask_words", "branch_words", "event_words", "skip_words") else "ask_words"
        clean = ", ".join(w.strip() for w in words.replace("\n", ",").split(",") if w.strip())
        async with Session() as s:
            row = await s.get(Setting, key)
            if row is not None:
                row.value = clean
            else:
                s.add(Setting(key=key, value=clean))
            await s.commit()
        request.session["saved"] = "So'zlar saqlandi."
        return _go("/rules")

    @app.post("/rules/schedule-texts")
    async def schedule_texts_save(
        request: Request, open_text: str = Form(""), open_text_vodiy: str = Form(""), closed_text: str = Form(""),
        ocr_confirm: str = Form(""),
    ):
        async with Session() as s:
            row = await s.get(Setting, "ocr_confirm")
            if row is not None:
                row.value = "1" if ocr_confirm else "0"
            else:
                s.add(Setting(key="ocr_confirm", value="1" if ocr_confirm else "0"))
            for key, value in (("open_text", open_text), ("open_text_vodiy", open_text_vodiy), ("closed_text", closed_text)):
                value = value.replace("\r\n", "\n").strip()  # bo'sh qoldirilsa — standart matn
                row = await s.get(Setting, key)
                if row is not None:
                    row.value = value
                else:
                    s.add(Setting(key=key, value=value))
            await s.commit()
        request.session["saved"] = "Ish tartibi matnlari saqlandi."
        return _go("/rules")

    @app.post("/rules")
    async def rule_add(text: str = Form("")):
        if text.strip():
            async with Session() as s:
                s.add(Rule(text=text.strip()))
                await s.commit()
        return _go("/rules")

    @app.post("/rules/{rule_id}/delete")
    async def rule_delete(rule_id: int):
        async with Session() as s:
            await s.execute(delete(Rule).where(Rule.id == rule_id))
            await s.commit()
        return _go("/rules")

    return app
