"""Veb admin panel: kanallar, tasdiq navbati, qoidalar, analitika."""
from __future__ import annotations

import asyncio
import hmac
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, select
from starlette.middleware.sessions import SessionMiddleware

from .config import settings
from .db import Delivery, Event, Rule, Session, SourcePost, StatChannel, StatPost, Target, now

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
MODES = {"auto": "Avto", "smart": "Aqlli", "manual": "Qo'lda"}


def _go(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


async def dashboard_data(days: int) -> dict:
    since = now() - timedelta(days=days)
    async with Session() as s:
        targets = list((await s.execute(select(Target).order_by(Target.id))).scalars())
        chan = list((await s.execute(select(StatChannel).where(StatChannel.at >= since).order_by(StatChannel.at))).scalars())
        sent = list((await s.execute(select(Delivery).where(Delivery.status == "sent", Delivery.sent_at >= since))).scalars())
        pending = len(list((await s.execute(select(Delivery.id).where(Delivery.status == "pending"))).scalars()))
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


def svg_points(series: list[int], w: int = 600, h: int = 200) -> str:
    """Chiziqli grafik uchun SVG nuqtalari."""
    if len(series) < 2:
        return ""
    lo, hi = min(series), max(series)
    span = (hi - lo) or 1
    step = w / (len(series) - 1)
    return " ".join(f"{i * step:.1f},{h - 10 - (v - lo) / span * (h - 20):.1f}" for i, v in enumerate(series))


def create_app(agent=None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    @app.middleware("http")
    async def auth(request: Request, call_next):
        if request.url.path != "/login" and not request.session.get("user"):
            return _go("/login")
        return await call_next(request)

    # Oxirgi qo'shilgan middleware eng tashqarida ishlaydi: sessiya auth'dan oldin ochilishi kerak
    app.add_middleware(
        SessionMiddleware, secret_key=settings.secret_key, same_site="strict",
        https_only=settings.cookie_secure, max_age=7 * 24 * 3600,
    )

    def page(request: Request, name: str, **ctx):
        connected = bool(agent and agent.status.get("connected"))
        return templates.TemplateResponse(
            request, name, {"modes": MODES, "path": request.url.path, "connected": connected, **ctx}
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

    # ---------- kanallar ----------

    @app.get("/channels")
    async def channels(request: Request):
        async with Session() as s:
            targets = list((await s.execute(select(Target).order_by(Target.id))).scalars())
        return page(request, "channels.html", targets=targets)

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
        template_html: str = Form(""), keywords: str = Form(""),
        sync_edits: str = Form(""), sync_deletes: str = Form(""), silent: str = Form(""),
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
            target.keywords = keywords.strip()
            target.sync_edits, target.sync_deletes, target.silent = bool(sync_edits), bool(sync_deletes), bool(silent)
            s.add(target)
            await s.commit()
            new_id = target.id
        if agent is not None and agent.pipeline is not None:
            problem = await agent.pipeline.check_target(username)
            if problem:
                request.session["warning"] = problem
        return _go(f"/channels/{new_id}?saved=1")

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
            items = []
            for d in deliveries:
                post = await s.get(SourcePost, d.post_id)
                if post is None or post.deleted:
                    continue
                items.append({"d": d, "target": await s.get(Target, d.target_id), "text": post.text or "(matnsiz post)"})
        return page(request, "queue.html", items=items, warning=request.session.pop("warning", ""))

    @app.post("/queue/{delivery_id}/send")
    async def queue_send(request: Request, delivery_id: int):
        if agent is None or agent.pipeline is None:
            request.session["warning"] = "Telegram akkaunt ulanmagan — post yuborilmadi."
        else:
            await agent.pipeline.publish(delivery_id)
        return _go("/queue")

    @app.post("/queue/{delivery_id}/reject")
    async def queue_reject(delivery_id: int):
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
        )

    @app.post("/account/qr")
    async def account_qr(request: Request):
        error = await agent.begin_qr()
        if error:
            request.session["warning"] = error
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

    # ---------- qoidalar ----------

    @app.get("/rules")
    async def rules(request: Request):
        async with Session() as s:
            items = list((await s.execute(select(Rule).order_by(Rule.id))).scalars())
        return page(request, "rules.html", rules=items)

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
