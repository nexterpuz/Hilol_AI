"""Haftalik va oylik hisobotlar: rasm chizish va bot orqali yuborish.

Har yakshanba 18:00 da (oxirgi 7 kun) va oyning oxirgi kuni 18:00 da (shu oy) Toshkent
vaqti bilan: filial menejerlariga o'z filiali, bosh adminga umumiy statistika — bitta rasm.
"""
from __future__ import annotations

import asyncio
import calendar
import io
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import select

from .db import Session, Setting, Target, log as db_log

log = logging.getLogger(__name__)
TASHKENT = timezone(timedelta(hours=5))
STATIC = Path(__file__).parent / "static"
MONTHS = ["yanvar", "fevral", "mart", "aprel", "may", "iyun", "iyul", "avgust", "sentabr", "oktabr", "noyabr", "dekabr"]

W, PAD = 1080, 48
BG, CARD, TEXT, MUTED, LINE = "#F4F6F5", "#FFFFFF", "#14201A", "#5A665F", "#E2E7E4"
GREEN, GREEN_DARK, GREEN_SOFT, WARN = "#1F8F55", "#14633A", "#E7F5EC", "#9A4A06"


def font(size: int, weight: str = "Regular") -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(STATIC / "fonts" / f"Inter-{weight}.otf"), size)


def clean(text: str) -> str:
    """Shriftda yo'q belgilarni (emoji va h.k.) olib tashlaydi."""
    return re.sub(r"\s+", " ", "".join(ch for ch in text or "" if ord(ch) < 0x2190 or ch in "№")).strip()


def num(v, suffix: str = "") -> str:
    return "—" if v is None else f"{v:,}".replace(",", " ") + suffix


def signed(v) -> str:
    return "—" if v is None else ("+" if v > 0 else "") + num(v)


def fit(draw: ImageDraw.ImageDraw, text: str, f, max_w: float) -> str:
    if draw.textlength(text, font=f) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=f) > max_w:
        text = text[:-1]
    return text.rstrip() + "…"


def period_label(start: datetime, end: datetime) -> str:
    if start.month == end.month:
        return f"{start.day}–{end.day} {MONTHS[end.month - 1]} {end.year}"
    return f"{start.day} {MONTHS[start.month - 1]} – {end.day} {MONTHS[end.month - 1]} {end.year}"


class Canvas:
    """Yuqoridan pastga kartalar joylanadigan oddiy chizish maydoni."""

    def __init__(self, height: int):
        self.img = Image.new("RGB", (W, height), BG)
        self.d = ImageDraw.Draw(self.img)
        self.y = PAD

    def card(self, h: int) -> tuple[int, int, int, int]:
        box = (PAD, self.y, W - PAD, self.y + h)
        self.d.rounded_rectangle(box, radius=24, fill=CARD, outline=LINE, width=2)
        self.y += h + 24
        return box

    def header(self, title: str, subtitle: str) -> None:
        x0, y0, x1, _ = self.card(190)
        try:
            logo = Image.open(STATIC / "logo.png").convert("RGBA")
            logo.thumbnail((230, 84))
            self.img.paste(logo, (x1 - 36 - logo.width, y0 + 36), logo)
        except OSError:
            pass
        self.d.text((x0 + 36, y0 + 36), fit(self.d, title, font(46, "Bold"), x1 - x0 - 330), font=font(46, "Bold"), fill=TEXT)
        self.d.text((x0 + 36, y0 + 108), subtitle, font=font(26), fill=MUTED)

    def kpis(self, items: list[tuple[str, str, str]]) -> None:
        """2 ustunli ko'rsatkichlar: (nom, qiymat, izoh)."""
        gap, cw, ch = 24, (W - 2 * PAD - 24) // 2, 176
        for i, (label, value, note) in enumerate(items):
            x = PAD + (i % 2) * (cw + gap)
            y = self.y + (i // 2) * (ch + gap)
            self.d.rounded_rectangle((x, y, x + cw, y + ch), radius=24, fill=CARD, outline=LINE, width=2)
            self.d.text((x + 32, y + 26), label, font=font(24), fill=MUTED)
            self.d.text((x + 32, y + 62), value, font=font(58, "Bold"), fill=TEXT)
            self.d.text((x + 32, y + 134), fit(self.d, note, font(22), cw - 64), font=font(22), fill=MUTED)
        rows = (len(items) + 1) // 2
        self.y += rows * (ch + gap)

    def line_chart(self, title: str, values: list[int], left: str, right: str) -> None:
        x0, y0, x1, y1 = self.card(380)
        self.d.text((x0 + 36, y0 + 28), title, font=font(28, "SemiBold"), fill=TEXT)
        px0, py0, px1, py1 = x0 + 36, y0 + 96, x1 - 36, y1 - 70
        if len(values) < 2:
            self.d.text((px0, py0 + 80), "Grafik uchun ma'lumot hali yetarli emas.", font=font(24), fill=MUTED)
            return
        lo, hi = min(values), max(values)
        span = (hi - lo) or 1
        for k in range(4):  # yordamchi chiziqlar
            gy = py0 + k * (py1 - py0) / 3
            self.d.line((px0, gy, px1, gy), fill=LINE, width=2)
        pts = [(px0 + i * (px1 - px0) / (len(values) - 1), py1 - (v - lo) / span * (py1 - py0)) for i, v in enumerate(values)]
        self.d.polygon(pts + [(px1, py1), (px0, py1)], fill=GREEN_SOFT)
        self.d.line(pts, fill=GREEN, width=5, joint="curve")
        ex, ey = pts[-1]
        self.d.ellipse((ex - 9, ey - 9, ex + 9, ey + 9), fill=GREEN, outline=CARD, width=3)
        self.d.text((px0, py1 + 18), left, font=font(22), fill=MUTED)
        rw = self.d.textlength(right, font=font(22, "SemiBold"))
        self.d.text((px1 - rw, py1 + 18), right, font=font(22, "SemiBold"), fill=TEXT)

    def rows(self, title: str, items: list[tuple[str, str]], empty: str, value_color: str = GREEN_DARK) -> None:
        h = 96 + max(1, len(items)) * 58 + 16
        x0, y0, x1, _ = self.card(h)
        self.d.text((x0 + 36, y0 + 28), title, font=font(28, "SemiBold"), fill=TEXT)
        if not items:
            self.d.text((x0 + 36, y0 + 92), empty, font=font(24), fill=MUTED)
            return
        for i, (label, value) in enumerate(items):
            y = y0 + 92 + i * 58
            vw = self.d.textlength(value, font=font(26, "SemiBold"))
            self.d.text((x0 + 36, y), fit(self.d, clean(label), font(24), x1 - x0 - 120 - vw), font=font(24), fill=TEXT)
            self.d.text((x1 - 36 - vw, y - 2), value, font=font(26, "SemiBold"), fill=value_color)

    def bars(self, title: str, items: list[tuple[str, float, str]]) -> None:
        """Gorizontal ustunlar: (nom, qiymat, yozuv). Bitta ko'rsatkich — bitta rang."""
        h = 96 + max(1, len(items)) * 62 + 16
        x0, y0, x1, _ = self.card(h)
        self.d.text((x0 + 36, y0 + 28), title, font=font(28, "SemiBold"), fill=TEXT)
        if not items:
            self.d.text((x0 + 36, y0 + 92), "Ma'lumot hali yetarli emas.", font=font(24), fill=MUTED)
            return
        top = max(v for _, v, _ in items) or 1
        name_w, val_w = 250, 110
        bx0, bx1 = x0 + 36 + name_w, x1 - 36 - val_w
        for i, (name, value, label) in enumerate(items):
            y = y0 + 92 + i * 62
            self.d.text((x0 + 36, y + 4), fit(self.d, clean(name), font(24), name_w - 16), font=font(24), fill=TEXT)
            self.d.rounded_rectangle((bx0, y + 6, bx1, y + 34), radius=8, fill="#EEF1EF")
            width = max(10, (bx1 - bx0) * value / top)
            self.d.rounded_rectangle((bx0, y + 6, bx0 + width, y + 34), radius=8, fill=GREEN)
            lw = self.d.textlength(label, font=font(24, "SemiBold"))
            self.d.text((x1 - 36 - lw, y + 3), label, font=font(24, "SemiBold"), fill=TEXT)

    def table(self, title: str, head: list[str], body: list[list[str]]) -> None:
        h = 96 + 50 + max(1, len(body)) * 54 + 16
        x0, y0, x1, _ = self.card(h)
        self.d.text((x0 + 36, y0 + 28), title, font=font(28, "SemiBold"), fill=TEXT)
        widths = [330] + [(x1 - x0 - 72 - 330) // (len(head) - 1)] * (len(head) - 1)

        def cell(col: int, y: int, text: str, f, color: str) -> None:
            left = x0 + 36 + sum(widths[:col])
            if col == 0:
                self.d.text((left, y), fit(self.d, clean(text), f, widths[0] - 16), font=f, fill=color)
            else:  # sonlar o'ngga tekislanadi
                self.d.text((left + widths[col] - self.d.textlength(text, font=f), y), text, font=f, fill=color)

        for c, t in enumerate(head):
            cell(c, y0 + 92, t, font(20, "SemiBold"), MUTED)
        for r, row in enumerate(body):
            y = y0 + 142 + r * 54
            self.d.line((x0 + 36, y - 10, x1 - 36, y - 10), fill=LINE, width=2)
            for c, t in enumerate(row):
                cell(c, y, t, font(23, "SemiBold" if c == 0 else "Regular"), TEXT)

    def footer(self, generated: datetime) -> bytes:
        self.d.text((PAD, self.y), f"Hilol Nashr · Kanallar markazi · {generated:%d.%m.%Y %H:%M}", font=font(20), fill=MUTED)
        out = io.BytesIO()
        self.img.crop((0, 0, W, self.y + 28 + PAD)).save(out, "PNG", optimize=True)
        return out.getvalue()


def render_branch(d: dict, kind_label: str, period: str, generated: datetime) -> bytes:
    c = Canvas(2400)
    c.header(d["name"], f"{kind_label} · {period}")
    c.kpis([
        ("Obunachilar", num(d["subs"]), f"davr ichida {signed(d['growth'])}" if d["growth"] is not None else "ma'lumot yig'ilmoqda"),
        ("Ko'chirilgan postlar", num(d["posts_count"]), "shu davrda kanalga chiqqan"),
        ("O'rtacha ko'rish", num(d["avg_views"]), "bitta postga"),
        ("O'rtacha qamrov", num(d["avg_reach"], "%"), "ko'rishlar ÷ obunachilar"),
    ])
    values = [p["subs"] for p in d["series"]]
    c.line_chart("Obunachilar soni", values, f"boshida: {num(values[0])}" if values else "", f"hozir: {num(values[-1])}" if values else "")
    c.rows("Eng yaxshi postlar", [(r["text"], num(r["reach"], "%")) for r in d["best"]], "Bu davrda ko'rsatkichi bor post yo'q.")
    if d["worst"]:
        c.rows("Eng sust postlar", [(r["text"], num(r["reach"], "%")) for r in d["worst"]], "", value_color=WARN)
    return c.footer(generated)


def render_overall(cmp_: dict, kind_label: str, period: str, generated: datetime) -> bytes:
    chans = cmp_["channels"]
    c = Canvas(1200 + 130 * max(1, len(chans)))
    c.header("Barcha filiallar", f"{kind_label} · {period}")
    subs = [x["subs"] for x in chans if x["subs"] is not None]
    growth = [x["growth"] for x in chans if x["growth"] is not None]
    aud = cmp_.get("audience") or {}
    c.kpis([
        ("Jami obunachilar", num(sum(subs)) if subs else "—", f"davr ichida {signed(sum(growth))}" if growth else "ma'lumot yig'ilmoqda"),
        ("Ko'chirilgan postlar", num(sum(x["posts"] for x in chans)), f"{len(chans)} ta kanal bo'yicha jami"),
        ("O'rtacha qamrov", num(cmp_["avg_reach"], "%"), "ko'rishlar ÷ obunachilar"),
        ("Bir nechta kanalda", num(aud.get("multi")) if aud.get("listed") else "—",
         f"ro'yxat qamrovi {aud['coverage']}%" if aud.get("coverage") is not None else "obunachilar ro'yxati yig'ilmoqda"),
    ])
    ranked = sorted((x for x in chans if x["reach"] is not None), key=lambda x: x["reach"], reverse=True)
    c.bars("Qamrov bo'yicha reyting", [(x["name"], x["reach"], f"{x['reach']}%") for x in ranked])
    by_subs = sorted(chans, key=lambda x: x["subs"] or 0, reverse=True)
    c.table("Kanallar", ["Kanal", "Obunachi", "O'sish", "Postlar", "Qamrov"],
            [[x["name"], num(x["subs"]), signed(x["growth"]), num(x["posts"]), num(x["reach"], "%")] for x in by_subs])
    return c.footer(generated)


# ---------- davr va jadval ----------

def due(now_local: datetime, last_weekly: str, last_monthly: str) -> list[str]:
    """Shu daqiqada qaysi hisobotlar yuborilishi kerak: ["weekly"], ["monthly"], ikkalasi yoki bo'sh."""
    if now_local.hour < 18:
        return []
    out = []
    today = now_local.date().isoformat()
    if now_local.weekday() == 6 and last_weekly != today:
        out.append("weekly")
    last_day = calendar.monthrange(now_local.year, now_local.month)[1]
    if now_local.day == last_day and last_monthly != today[:7]:
        out.append("monthly")
    return out


def period_of(kind: str, now_local: datetime) -> tuple[int, str, str]:
    """(kunlar soni, hisobot nomi, davr yozuvi)."""
    if kind == "monthly":
        return now_local.day, "Oylik hisobot", f"{MONTHS[now_local.month - 1]} {now_local.year}"
    return 7, "Haftalik hisobot", period_label(now_local - timedelta(days=6), now_local)


async def send_reports(notifier, kind: str, now_local: datetime | None = None, only_user: int | None = None) -> int:
    """Hisobotlarni yuboradi. only_user berilsa — faqat shu foydalanuvchiga (qo'lda so'ralganda)."""
    from .config import settings
    from .web import channel_data, compare_data

    now_local = now_local or datetime.now(TASHKENT)
    days, label, period = period_of(kind, now_local)
    sent = 0
    async with Session() as s:
        targets = list((await s.execute(select(Target).where(Target.active))).scalars())
    for t in targets:
        users = [u for u in (t.manager_ids or {}) if only_user is None or int(u) == only_user]
        if not users:
            continue
        data = await channel_data(t.id, days)
        image = render_branch(data, label, period, now_local)
        sent += await notifier.send_photo(users, image, f"{label}: {t.name} · {period}")
    admins = [a for a in settings.admin_ids if only_user is None or a == only_user]
    if admins:
        image = render_overall(await compare_data(days), label, period, now_local)
        sent += await notifier.send_photo(admins, image, f"{label}: barcha filiallar · {period}")
    await db_log("report_sent", f"{kind}: {sent} ta xabar")
    return sent


# ---------- kunlik reyting ----------

def grade(reach: float, average: float) -> str:
    """Baho: filial qamrovi barcha filiallar o'rtachasiga nisbatan."""
    if average <= 0 or reach >= average:
        return "🟢 yaxshi"
    if reach >= average * 0.8:
        return "🟡 o'rtacha"
    return "🔴 e'tibor kerak"


def change_label(rank: int, previous: int | None) -> str:
    if previous is None:
        return "kechagi ma'lumot yo'q"
    if previous == rank:
        return "kechaga nisbatan o'zgarmadi"
    return f"kechaga nisbatan {'↑' if rank < previous else '↓'}{abs(previous - rank)}"


async def ranking() -> dict:
    """Filiallar reytingi: oxirgi 7 kundagi postlarning o'rtacha qamrovi bo'yicha (teng bo'lsa — o'sish foizi)."""
    from .web import compare_data

    data = await compare_data(7)
    rated = [c for c in data["channels"] if c["active"] and c["reach"] is not None]
    rated.sort(key=lambda c: (c["reach"], c["growth_pct"] or 0), reverse=True)
    average = sum(c["reach"] for c in rated) / len(rated) if rated else 0
    return {
        "total": len(rated), "average": average,
        "ranks": {c["id"]: {"rank": i + 1, "reach": c["reach"], "name": c["name"]} for i, c in enumerate(rated)},
    }


def rating_text(target_id: int, rk: dict, previous: dict | None, with_name: bool = False) -> str:
    mine = rk["ranks"].get(target_id)
    if mine is None:
        return "🏆 Reyting: hali hisoblanmadi.\nReyting uchun filial kanalida oxirgi 7 kunda chiqqan postlarning ko'rishlar soni kerak."
    prev = (previous or {}).get(str(target_id))
    head = f"{mine['name']}\n" if with_name else ""
    return (
        f"{head}🏆 Reyting: {mine['rank']}-o'rin ({rk['total']} ta filial ichida), {change_label(mine['rank'], prev)}\n"
        f"🎯 Baho: {grade(mine['reach'], rk['average'])}\n"
        f"📈 Qamrov: {mine['reach']}% (filiallar o'rtachasi {round(rk['average'])}%)\n\n"
        "Reyting oxirgi 7 kundagi postlarning o'rtacha qamrovi (ko'rishlar ÷ obunachilar) bo'yicha hisoblanadi."
    )


async def _history() -> dict:
    import json

    raw = await _get("rank_history")
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


def previous_ranks(history: dict, today: str) -> dict | None:
    """Bugundan oldingi eng so'nggi kunning o'rinlari."""
    days = sorted(d for d in history if d < today)
    return history[days[-1]] if days else None


async def send_ratings(notifier, now_local: datetime | None = None, only_user: int | None = None, store: bool = False) -> int:
    """Menejerlarga o'z filiali reytingini yuboradi. store=True — bugungi o'rinlar tarixga yoziladi."""
    import json

    now_local = now_local or datetime.now(TASHKENT)
    today = now_local.date().isoformat()
    rk, history = await ranking(), await _history()
    previous = previous_ranks(history, today)
    if store:
        history[today] = {str(tid): v["rank"] for tid, v in rk["ranks"].items()}
        for old in sorted(history)[:-14]:  # ikki haftalik tarix yetarli
            del history[old]
        await _set("rank_history", json.dumps(history))
    async with Session() as s:
        targets = list((await s.execute(select(Target).where(Target.active).order_by(Target.id))).scalars())
    per_user: dict = {}
    for t in targets:
        for uid in t.manager_ids or {}:
            if only_user is None or int(uid) == only_user:
                per_user.setdefault(uid, []).append(t)
    sent = 0
    for uid, mine in per_user.items():
        for t in mine:
            sent += await notifier.send([uid], rating_text(t.id, rk, previous, with_name=len(mine) > 1))
    if store:
        await db_log("rating_sent", f"{sent} ta xabar")
    return sent


async def _get(key: str) -> str:
    async with Session() as s:
        row = await s.get(Setting, key)
        return row.value if row else ""


async def _set(key: str, value: str) -> None:
    async with Session() as s:
        row = await s.get(Setting, key)
        if row is not None:
            row.value = value
        else:
            s.add(Setting(key=key, value=value))
        await s.commit()


async def run(notifier) -> None:
    """Har daqiqada vaqtni tekshiradi; qayta ishga tushganda takror yubormaydi."""
    while True:
        try:
            now_local = datetime.now(TASHKENT)
            for kind in due(now_local, await _get("report_weekly_last"), await _get("report_monthly_last")):
                # Avval belgilab qo'yamiz: yuborishda xato bo'lsa ham har daqiqada takrorlanmasin
                if kind == "weekly":
                    await _set("report_weekly_last", now_local.date().isoformat())
                else:
                    await _set("report_monthly_last", now_local.date().isoformat()[:7])
                await send_reports(notifier, kind, now_local)
            # kunlik reyting: har kuni 18:00 da faqat menejerlarga
            if now_local.hour >= 18 and await _get("rating_last") != now_local.date().isoformat():
                await _set("rating_last", now_local.date().isoformat())
                await send_ratings(notifier, now_local, store=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Hisobot yuborishda xato")
        await asyncio.sleep(60)
