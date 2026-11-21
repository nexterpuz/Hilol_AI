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

from .db import Delivery, Session, Setting, SourcePost, Target, log as db_log

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

    def regions(self, title: str, items: list[dict]) -> None:
        """Bo'limlar: bir qatorda 3 tagacha karta — nom, obunachilar, o'sish, qamrov, postlar."""
        self.d.text((PAD + 4, self.y), title, font=font(28, "SemiBold"), fill=TEXT)
        self.y += 52
        per_row, gap, ch = 3, 24, 230
        cw = (W - 2 * PAD - gap * (per_row - 1)) // per_row
        for i, r in enumerate(items):
            x = PAD + (i % per_row) * (cw + gap)
            y = self.y + (i // per_row) * (ch + gap)
            self.d.rounded_rectangle((x, y, x + cw, y + ch), radius=24, fill=CARD, outline=LINE, width=2)
            self.d.text((x + 28, y + 24), fit(self.d, r["name"], font(28, "Bold"), cw - 56), font=font(28, "Bold"), fill=GREEN_DARK)
            self.d.text((x + 28, y + 66), num(r["subs"]), font=font(46, "Bold"), fill=TEXT)
            pct = f" ({'+' if r['growth_pct'] > 0 else ''}{r['growth_pct']}%)" if r.get("growth_pct") is not None else ""
            lines = [f"o'sish {signed(r['growth'])}{pct}", f"qamrov {num(r['reach'], '%')} · {num(r['posts'])} post",
                     f"{r['channels']} ta kanal" + (f" · unikal {num(r['unique'])}" if r.get("unique") else "")]
            for k, line in enumerate(lines):
                self.d.text((x + 28, y + 128 + k * 30), fit(self.d, line, font(21), cw - 56), font=font(21), fill=MUTED)
        self.y += ((len(items) + per_row - 1) // per_row) * (ch + gap)

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

    def table(self, title: str, head: list[str], body: list) -> None:
        """body qatori ro'yxat bo'lsa — oddiy qator, satr bo'lsa — guruh sarlavhasi (masalan, bo'lim nomi)."""
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
            if isinstance(row, str):
                self.d.rectangle((x0 + 36, y - 8, x1 - 36, y + 42), fill=GREEN_SOFT)
                self.d.text((x0 + 48, y + 2), row, font=font(22, "Bold"), fill=GREEN_DARK)
                continue
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
    chans = [c for c in cmp_["channels"] if c.get("active", True)]  # to'xtatilgan kanallar hisobotga kirmaydi
    regions = cmp_.get("regions") or []
    c = Canvas(1500 + 130 * max(1, len(chans)) + 260 * ((len(regions) + 2) // 3))
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
    if regions:
        c.regions("Bo'limlar bo'yicha", regions)
    ranked = sorted((x for x in chans if x["reach"] is not None), key=lambda x: x["reach"], reverse=True)
    c.bars("Qamrov bo'yicha reyting", [(x["name"], x["reach"], f"{x['reach']}%") for x in ranked])
    by_subs = sorted(chans, key=lambda x: x["subs"] or 0, reverse=True)
    row = lambda x: [x["name"], num(x["subs"]), signed(x["growth"]), num(x["posts"]), num(x["reach"], "%")]  # noqa: E731
    names = cmp_.get("region_names") or {}
    if regions:  # kanallar bo'lim bo'yicha guruhlanadi, bo'limsizlari oxirida
        body: list = []
        for key, title in list(names.items()) + [("", "Bo'limsiz")]:
            group = [x for x in by_subs if (x.get("region") if x.get("region") in names else "") == key]
            if group:
                body += [title] + [row(x) for x in group]
    else:
        body = [row(x) for x in by_subs]
    c.table("Kanallar", ["Kanal", "Obunachi", "O'sish", "Postlar", "Qamrov"], body)
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
    if kind == "daily":
        return 1, "Kunlik hisobot", f"{now_local.day} {MONTHS[now_local.month - 1]} {now_local.year}"
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
    active = [c for c in data["channels"] if c["active"] and not c.get("mirror")]
    rated = [c for c in active if c["reach"] is not None]
    rated.sort(key=lambda c: (c["reach"], c["growth_pct"] or 0), reverse=True)
    average = sum(c["reach"] for c in rated) / len(rated) if rated else 0
    return {
        "total": len(active), "rated": len(rated), "average": average,
        "ranks": {c["id"]: {"rank": i + 1, "reach": c["reach"], "name": c["name"]} for i, c in enumerate(rated)},
    }


def rating_text(target_id: int, rk: dict, previous: dict | None, with_name: bool = False) -> str:
    mine = rk["ranks"].get(target_id)
    rated = rk.get("rated", len(rk["ranks"]))
    if mine is None:
        return (
            f"🏆 Reyting: hali hisoblanmadi ({rk['total']} ta filialdan {rated} tasida reyting bor).\n"
            "Reyting uchun filial kanalida oxirgi 7 kunda chiqqan postlarning ko'rishlar soni kerak — "
            "statistika soatiga bir marta yig'iladi."
        )
    prev = (previous or {}).get(str(target_id))
    head = f"{mine['name']}\n" if with_name else ""
    return (
        f"{head}🏆 Reyting: {mine['rank']}-o'rin ({rk['total']} ta filial ichida), {change_label(mine['rank'], prev)}\n"
        f"🎯 Baho: {grade(mine['reach'], rk['average'])}\n"
        f"📈 Qamrov: {mine['reach']}% (filiallar o'rtachasi {round(rk['average'])}%)\n"
        + (f"ℹ️ {rk['total'] - rated} ta filialda hali yetarli ma'lumot yo'q, ular reytingga kirmagan.\n" if rk["total"] > rated else "")
        + "\n"
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


# ---------- kutayotgan postlar: shanba eslatmalari ----------

PENDING_REMIND_HOUR, PENDING_SUMMARY_HOUR = 13, 16  # Toshkent vaqti, har shanba


def pending_due(now_local: datetime, last_remind: str, last_summary: str) -> list[str]:
    """Shanba kuni: 13:00 dan keyin menejerlarga eslatma, 16:00 dan keyin adminga umumiy ro'yxat (kuniga bir marta)."""
    if now_local.weekday() != 5:
        return []
    today = now_local.date().isoformat()
    out = []
    if now_local.hour >= PENDING_REMIND_HOUR and last_remind != today:
        out.append("remind")
    if now_local.hour >= PENDING_SUMMARY_HOUR and last_summary != today:
        out.append("summary")
    return out


async def pending_counts() -> tuple[list, dict]:
    """Faol filial kanallari va har birida tasdiq kutayotgan postlar soni (manbada o'chirilganlar sanalmaydi)."""
    async with Session() as s:
        targets = list((await s.execute(select(Target).where(Target.active).order_by(Target.id))).scalars())
        rows = (await s.execute(
            select(Delivery.target_id).join(SourcePost, SourcePost.id == Delivery.post_id)
            .where(Delivery.status == "pending", SourcePost.deleted.is_(False))
        )).scalars()
        counts: dict = {}
        for tid in rows:
            counts[tid] = counts.get(tid, 0) + 1
    return [t for t in targets if not t.mirror], counts


def reminder_text(mine: list, counts: dict) -> str | None:
    """Menejerga: o'z filial(lar)ida nechta post kutyapti. Kutayotgan post bo'lmasa — None (bezovta qilinmaydi)."""
    total = sum(counts.get(t.id, 0) for t in mine)
    if not total:
        return None
    lines = [f"📥 Eslatma: kanalingizga joylanishini kutayotgan {total} ta post bor."]
    if len(mine) > 1:
        lines += [f"• {t.name}: {counts.get(t.id, 0)} ta" for t in mine if counts.get(t.id)]
    lines.append("\nKo'rib chiqing: kerakli postni «Kanalga joylash», keraksizini «Rad etish» bilan yoping.")
    return "\n".join(lines)


def summary_text(targets: list, counts: dict) -> str:
    """Bosh adminga: qaysi filialda nechta post kutib turibdi (ko'pi yuqorida)."""
    waiting = sorted((t for t in targets if counts.get(t.id)), key=lambda t: (-counts[t.id], t.name))
    if not waiting:
        return "📥 Haftalik ko'rinish: hech bir filialda kutayotgan post yo'q. Barcha menejerlar postlarni ko'rib chiqqan."
    total = sum(counts[t.id] for t in waiting)
    lines = [f"📥 Haftalik ko'rinish: {len(waiting)} ta filialda jami {total} ta post menejer qarorini kutmoqda.\n"]
    for t in waiting:
        note = "" if t.manager_ids else " — menejer botga bog'lanmagan"
        lines.append(f"• {t.name}: {counts[t.id]} ta{note}")
    done = len(targets) - len(waiting)
    if done:
        lines.append(f"\n✅ Qolgan {done} ta filialda kutayotgan post yo'q.")
    lines.append("Menejerlarga soat 13:00 da eslatma yuborilgan.")
    return "\n".join(lines)


REGION_LABELS = {"all": "Barcha filiallar", "toshkent": "Toshkent", "vodiy": "Vodiy", "voha": "Voha"}
PER_CHANNEL = 6  # rasmda har kanal uchun ko'rsatiladigan postlar soni


def region_of(target) -> str:
    """Kanal bo'limi: panelda tanlangani; tanlanmagan bo'lsa — Vodiy shaharlari nomidan, aks holda "" (bo'limsiz)."""
    if getattr(target, "region", ""):
        return target.region
    from .pipeline import is_vodiy

    return "vodiy" if is_vodiy(target) else ""


def _first_line(text: str) -> str:
    for line in (text or "").split("\n"):
        c = clean(line)
        if len(c) > 2:
            return c
    return ""


async def pending_board(region: str, now_local: datetime | None = None) -> dict:
    """Rasm uchun: tanlangan bo'limdagi har bir kanal va unda kutayotgan postlar (eskisi yuqorida)."""
    now_local = now_local or datetime.now(TASHKENT)
    async with Session() as s:
        targets = [t for t in (await s.execute(select(Target).where(Target.active).order_by(Target.name))).scalars() if not t.mirror]
        rows = list((await s.execute(
            select(Delivery, SourcePost).join(SourcePost, SourcePost.id == Delivery.post_id)
            .where(Delivery.status == "pending", SourcePost.deleted.is_(False)).order_by(Delivery.created_at)
        )).all())
    chosen = targets if region == "all" else [t for t in targets if region_of(t) == region]
    unassigned = sum(1 for t in targets if not region_of(t)) if region != "all" else 0
    items: dict = {t.id: [] for t in chosen}
    for d, post in rows:
        if d.target_id not in items:
            continue
        created = d.created_at if d.created_at.tzinfo else d.created_at.replace(tzinfo=timezone.utc)
        local = created.astimezone(TASHKENT)
        text = _first_line(d.override_text or post.text) or "(matnsiz post: rasm, video yoki audio)"
        items[d.target_id].append({"when": local.strftime("%d.%m %H:%M"), "days": (now_local.date() - local.date()).days, "text": text, "why": d.reason or ""})
    channels = [{"name": t.name, "manager": bool(t.manager_ids), "posts": items[t.id]} for t in chosen]
    channels.sort(key=lambda c: (-len(c["posts"]), c["name"]))
    return {"region": region, "label": REGION_LABELS.get(region, region), "channels": channels, "unassigned": unassigned,
            "total": sum(len(c["posts"]) for c in channels)}


def render_pending(board: dict, generated: datetime) -> bytes:
    waiting = [c for c in board["channels"] if c["posts"]]
    idle = [c for c in board["channels"] if not c["posts"]]
    rows = sum(min(len(c["posts"]), PER_CHANNEL) + (1 if len(c["posts"]) > PER_CHANNEL else 0) for c in waiting)
    c = Canvas(900 + len(waiting) * 190 + rows * 64 + (200 if idle else 0))
    c.header(f"Kutayotgan postlar · {board['label']}", f"{generated.day} {MONTHS[generated.month - 1]} {generated.year}, {generated:%H:%M}")
    oldest = max((p["days"] for ch in waiting for p in ch["posts"]), default=None)
    c.kpis([
        ("Kutayotgan postlar", num(board["total"]), f"{len(waiting)} ta kanalda"),
        ("Eng eskisi", f"{oldest} kun" if oldest is not None else "—", "menejer qarorini kutmoqda" if oldest is not None else "kutayotgan post yo'q"),
    ])
    for ch in waiting:
        shown = ch["posts"][:PER_CHANNEL]
        more = len(ch["posts"]) - len(shown)
        top = 104 if ch["manager"] else 140  # "menejer bog'lanmagan" yozuvi uchun joy
        h = top + 6 + len(shown) * 64 + (50 if more else 0)
        x0, y0, x1, _ = c.card(h)
        c.d.text((x0 + 36, y0 + 28), fit(c.d, clean(ch["name"]), font(30, "Bold"), x1 - x0 - 320), font=font(30, "Bold"), fill=TEXT)
        badge = f"{len(ch['posts'])} ta post"
        bw = c.d.textlength(badge, font=font(24, "SemiBold")) + 36
        c.d.rounded_rectangle((x1 - 36 - bw, y0 + 26, x1 - 36, y0 + 70), radius=22, fill=GREEN_SOFT)
        c.d.text((x1 - 36 - bw + 18, y0 + 33), badge, font=font(24, "SemiBold"), fill=GREEN_DARK)
        if not ch["manager"]:
            c.d.text((x0 + 36, y0 + 78), "Menejer botga bog'lanmagan — eslatma unga yetmaydi", font=font(21, "SemiBold"), fill=WARN)
        for i, p in enumerate(shown):
            y = y0 + top + i * 64
            c.d.line((x0 + 36, y - 10, x1 - 36, y - 10), fill=LINE, width=2)
            age = "bugun" if p["days"] <= 0 else f"{p['days']} kun"
            c.d.text((x0 + 36, y + 2), p["when"], font=font(21), fill=MUTED)
            c.d.text((x0 + 36, y + 28), age, font=font(19, "SemiBold"), fill=WARN if p["days"] >= 7 else MUTED)
            c.d.text((x0 + 200, y + 10), fit(c.d, p["text"], font(24), x1 - x0 - 240), font=font(24), fill=TEXT)
        if more:
            c.d.text((x0 + 200, y0 + top + len(shown) * 64), f"+ yana {more} ta post — menejer botida", font=font(21), fill=MUTED)
    if not waiting:
        c.rows("Holat", [], "Bu bo'limda kutayotgan post yo'q. Barcha menejerlar postlarni ko'rib chiqqan.")
    if idle:
        names = ", ".join(clean(ch["name"]) for ch in idle)
        x0, y0, x1, _ = c.card(150)
        c.d.text((x0 + 36, y0 + 28), f"Kutayotgan post yo'q ({len(idle)})", font=font(26, "SemiBold"), fill=TEXT)
        c.d.text((x0 + 36, y0 + 76), fit(c.d, names, font(22), x1 - x0 - 72), font=font(22), fill=MUTED)
    if board.get("unassigned"):
        c.d.text((PAD, c.y), f"{board['unassigned']} ta kanalga bo'lim tanlanmagan — ular faqat «Barcha filiallar»da ko'rinadi.", font=font(20), fill=WARN)
        c.y += 44
    return c.footer(generated)


async def send_pending_reminders(notifier) -> int:
    from telethon import Button

    targets, counts = await pending_counts()
    per_user: dict = {}
    for t in targets:
        for uid in t.manager_ids or {}:
            per_user.setdefault(uid, []).append(t)
    sent = 0
    for uid, mine in per_user.items():
        text = reminder_text(mine, counts)
        if text:
            sent += await notifier.send([uid], text, [Button.inline("📥 Kutayotgan postlarni ko'rish", data="pend:show")])
    await db_log("pending_remind", f"{sent} ta menejerga")
    return sent


async def send_pending_summary(notifier) -> int:
    from .config import settings

    targets, counts = await pending_counts()
    sent = await notifier.send(settings.admin_ids, summary_text(targets, counts))
    await db_log("pending_summary", f"{sum(counts.get(t.id, 0) for t in targets)} ta post")
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
            # shanba: 13:00 — menejerlarga kutayotgan postlar eslatmasi, 16:00 — adminga filiallar bo'yicha ro'yxat
            for kind in pending_due(now_local, await _get("pending_remind_last"), await _get("pending_summary_last")):
                if kind == "remind":
                    await _set("pending_remind_last", now_local.date().isoformat())
                    await send_pending_reminders(notifier)
                else:
                    await _set("pending_summary_last", now_local.date().isoformat())
                    await send_pending_summary(notifier)
            # kunlik reyting: har kuni 18:00 da faqat menejerlarga
            if now_local.hour >= 18 and await _get("rating_last") != now_local.date().isoformat():
                await _set("rating_last", now_local.date().isoformat())
                await send_ratings(notifier, now_local, store=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Hisobot yuborishda xato")
        await asyncio.sleep(60)
