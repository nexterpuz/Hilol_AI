import asyncio
import io
from datetime import datetime, timedelta

from PIL import Image

from app import reports
from app.db import Delivery, Session, SourcePost, StatChannel, StatPost, Target, init_db, now

T = reports.TASHKENT


def test_due_schedule():
    sunday = datetime(2026, 11, 15, 18, 0, tzinfo=T)  # yakshanba
    assert reports.due(sunday.replace(hour=17, minute=59), "", "") == []
    assert reports.due(sunday, "", "") == ["weekly"]
    assert reports.due(sunday.replace(hour=20), "2026-11-15", "") == []  # bir kunda bir marta
    assert reports.due(datetime(2026, 11, 16, 18, 5, tzinfo=T), "2026-11-15", "") == []  # dushanba
    last = datetime(2026, 11, 30, 18, 0, tzinfo=T)  # oyning oxirgi kuni, dushanba
    assert reports.due(last, "2026-11-29", "2026-10") == ["monthly"]
    assert reports.due(last, "2026-11-29", "2026-11") == []
    both = datetime(2027, 1, 31, 18, 1, tzinfo=T)  # yakshanba va oy oxiri
    assert reports.due(both, "", "") == ["weekly", "monthly"]
    assert reports.due(datetime(2028, 2, 29, 19, 0, tzinfo=T), "", "2028-01") == ["monthly"]  # kabisa yili


def test_period_labels():
    days, label, period = reports.period_of("weekly", datetime(2026, 11, 15, 18, 0, tzinfo=T))
    assert (days, label, period) == (7, "Haftalik hisobot", "9–15 noyabr 2026")
    assert reports.period_of("weekly", datetime(2026, 12, 2, 18, 0, tzinfo=T))[2] == "26 noyabr – 2 dekabr 2026"
    assert reports.period_of("monthly", datetime(2026, 11, 30, 18, 0, tzinfo=T)) == (30, "Oylik hisobot", "noyabr 2026")
    assert reports.clean("✨ Жума муборак! 🎉 «Ilm»") == "Жума муборак! «Ilm»"


def test_render_and_send():
    class Bot:
        def __init__(self):
            self.photos = []

        async def send_photo(self, users, image, caption):
            self.photos.append((list(users), image, caption))
            return len(list(users))

    async def run():
        await init_db()
        async with Session() as s:
            t = Target(name="Hisobot filiali", username="rep_1", manager_ids={"4242": "901112233"})
            s.add(t)
            post = SourcePost(chat_id=-77, msg_ids=[1], text="📕 Yangi «Sabr» kitobi sotuvda")
            s.add(post)
            await s.flush()
            for d in range(7, -1, -1):
                s.add(StatChannel(target_id=t.id, subscribers=1500 + (7 - d) * 12, at=now() - timedelta(days=d)))
            dl = Delivery(post_id=post.id, target_id=t.id, status="sent", sent_at=now() - timedelta(days=2), target_msg_ids=[5])
            s.add(dl)
            await s.flush()
            s.add(StatPost(delivery_id=dl.id, views=640, reach=0.42))
            await s.commit()
        bot = Bot()
        when = datetime(2026, 11, 15, 18, 0, tzinfo=T)
        n = await reports.send_reports(bot, "weekly", when)
        only = Bot()
        await reports.send_reports(only, "monthly", when, only_user=4242)
        return bot.photos, only.photos, n

    photos, only, n = asyncio.run(run())
    branch = next(p for p in photos if p[0] == ["4242"])
    overall = next(p for p in photos if p[0] == [777])
    assert n >= 2 and "Hisobot filiali" in branch[2] and "barcha filiallar" in overall[2] and "9–15 noyabr" in branch[2]
    for _, image, _ in photos:
        im = Image.open(io.BytesIO(image))
        assert im.format == "PNG" and im.width == 1080 and 900 < im.height < 3000
    assert len(only) == 1 and only[0][0] == ["4242"] and "Oylik hisobot" in only[0][2]  # qo'lda so'ralganda faqat o'ziga


def test_bot_keyboard_and_button_presses(monkeypatch):
    """Pastki tugmalar rolga mos chiqadi va bosilganda mos buyruq bajariladi."""
    from types import SimpleNamespace as NS

    from app import adminbot, pipeline
    from app.adminbot import BTN_MONTH, BTN_QUEUE, BTN_REPORT, BTN_RESUME, BTN_STATE, BTN_STOP, BTN_WEEK, AdminBot, keyboard

    def labels(rows):
        return [b.button.text for row in rows for b in row]

    assert labels(keyboard(False, True)) == [BTN_REPORT, adminbot.BTN_RATING, BTN_QUEUE, adminbot.BTN_SUGGEST]
    assert labels(keyboard(True, False)) == [BTN_REPORT, BTN_QUEUE, BTN_STATE, BTN_STOP, adminbot.BTN_MANAGERS, adminbot.BTN_MESSAGE]
    assert labels(keyboard(True, True, paused=True)) == [BTN_REPORT, adminbot.BTN_RATING, BTN_QUEUE, adminbot.BTN_SUGGEST, BTN_STATE, BTN_RESUME, adminbot.BTN_MANAGERS, adminbot.BTN_MESSAGE]

    class Ev:
        def __init__(self, sender, text):
            self.sender_id, self.raw_text = sender, text
            self.message = NS(contact=None)
            self.out = []

        async def respond(self, text, buttons=None):
            self.out.append((text, labels(buttons) if isinstance(buttons, list) else buttons))

    calls = []

    async def fake_reports(bot, kind, now_local=None, only_user=None):
        calls.append((kind, only_user))
        return 1

    monkeypatch.setattr(reports, "send_reports", fake_reports)

    async def run():
        await init_db()
        bot = AdminBot()
        week, month = Ev(777, BTN_WEEK), Ev(777, BTN_MONTH)
        await bot._on_message(week)
        await bot._on_message(month)
        stop = Ev(777, BTN_STOP)
        await bot._on_message(stop)
        paused = await pipeline.is_paused()
        resume = Ev(777, BTN_RESUME)
        await bot._on_message(resume)
        stranger = Ev(31337, BTN_STOP)  # begona odam to'xtata olmaydi
        await bot._on_message(stranger)
        state = Ev(777, BTN_STATE)
        await bot._on_message(state)
        return stop.out, paused, resume.out, await pipeline.is_paused(), stranger.out, state.out

    stop, paused, resume, after, stranger, state = asyncio.run(run())
    assert calls == [("weekly", 777), ("monthly", 777)]
    assert paused and BTN_RESUME in stop[0][1]  # to'xtatilgach tugma "Davom ettirish" ga almashadi
    assert not after and BTN_STOP in resume[0][1]
    assert stranger == [] and "Agent holati" in state[0][0]


def test_managers_report():
    """Bosh admin qaysi filial menejeri bog'langanini va qaysi biri /start bosmaganini ko'radi."""
    from types import SimpleNamespace as NS

    from sqlalchemy import update

    from app.adminbot import BTN_MANAGERS, AdminBot, managers_report

    class Ev:
        def __init__(self, sender, text):
            self.sender_id, self.raw_text, self.message, self.out = sender, text, NS(contact=None), []

        async def respond(self, text, buttons=None, parse_mode=None):
            self.out.append(text)

    async def run():
        await init_db()
        async with Session() as s:
            await s.execute(update(Target).values(active=False))  # boshqa testlardan qolgan kanallar aralashmasin
            s.add_all([
                Target(name="M-Bog'langan", username="mgr_1", manager_phones="+998 90 111 00 01", manager_ids={"9001": "901110001"}),
                Target(name="M-Qisman", username="mgr_2", manager_phones="+998 90 111 00 02, +998 90 111 00 03", manager_ids={"9002": "901110002"}),
                Target(name="M-Bosmagan", username="mgr_3", manager_phones="+998 90 111 00 04"),
                Target(name="M-Raqamsiz", username="mgr_4"),
            ])
            await s.commit()
        bot = AdminBot()
        await bot._on_message(Ev(9001, "/navbat"))  # menejer faolligi yoziladi
        admin, stranger = Ev(777, BTN_MANAGERS), Ev(9001, BTN_MANAGERS)
        await bot._on_message(admin)
        await bot._on_message(stranger)
        return admin.out[0], stranger.out, await managers_report()

    text, stranger, again = asyncio.run(run())
    assert "✅ M-Bog'langan" in text and "oxirgi faollik" in text
    assert "🟡 M-Qisman" in text and "+998 90 111 00 03 — botga /start bosmagan" in text
    assert "❌ M-Bosmagan" in text and "⚠️ M-Raqamsiz" in text and "raqam kiritilmagan" in text
    assert "Jami: 4 ta filialdan 2 tasida menejer bog'langan." in text and text == again
    assert not any("Filial menejerlari" in t for t in stranger)  # menejer bu ro'yxatni ko'ra olmaydi


def test_admin_message_to_managers():
    """Bosh admin bitta filial menejeriga yoki barcha menejerlarga xabar yuboradi."""
    from types import SimpleNamespace as NS

    from sqlalchemy import update

    from app.adminbot import BTN_MESSAGE, BTN_WEEK, MESSAGE_HEAD, AdminBot

    class Ev:
        def __init__(self, sender, text="", data=b""):
            self.sender_id, self.raw_text, self.data = sender, text, data
            self.message, self.out, self.answers, self.menu = NS(contact=None, media=None), [], [], None

        async def respond(self, text, buttons=None, parse_mode=None):
            self.out.append(text)
            if isinstance(buttons, list) and buttons and hasattr(buttons[0][0], "type"):
                self.menu = [(b.text, b.type.data.decode()) for row in buttons for b in row]

        async def answer(self, text="", alert=False):
            self.answers.append(text)

        async def edit(self, text, buttons=None, parse_mode=None):
            self.out.append(text)

    async def run():
        await init_db()
        async with Session() as s:
            await s.execute(update(Target).values(active=False))
            a = Target(name="X-Qo'qon", username="msg_1", manager_ids={"8001": "1", "8002": "2"})
            b = Target(name="X-Marg'ilon", username="msg_2", manager_ids={"8002": "2", "8003": "3"})
            s.add_all([a, b, Target(name="X-Bo'sh", username="msg_3")])
            await s.commit()
            aid = a.id
        bot, sent = AdminBot(), []

        async def fake_send(users, text, buttons=None):
            sent.append((list(users), text))
            return len(list(users)) - (1 if "8003" in users else 0)  # 8003 botni bloklagan

        bot.send = fake_send
        menu = Ev(777, BTN_MESSAGE)
        await bot._on_message(menu)
        # bitta filialga
        pick = Ev(777, data=f"msg:{aid}".encode())
        await bot._on_click(pick)
        one = Ev(777, "Ertaga soat 9 da yig'ilish")
        await bot._on_message(one)
        after = Ev(777, "bu endi yuborilmaydi")
        await bot._on_message(after)
        # barchaga: takroriy menejer bir marta oladi
        await bot._on_click(Ev(777, data=b"msg:all"))
        everyone = Ev(777, "Bayram bilan!")
        await bot._on_message(everyone)
        # yozish paytida boshqa tugma bosilsa — bekor bo'ladi, xabar ketmaydi
        await bot._on_click(Ev(777, data=b"msg:all"))
        n = len(sent)
        monkey = Ev(777, "/bekor")
        await bot._on_message(monkey)
        # begona odam
        bad = Ev(8001, data=b"msg:all")
        await bot._on_click(bad)
        hack = Ev(8001, BTN_MESSAGE)
        await bot._on_message(hack)
        return menu, pick, one, everyone, sent, n, monkey, bad, hack, dict(bot._compose)

    menu, pick, one, everyone, sent, n, monkey, bad, hack, compose = asyncio.run(run())
    assert menu.menu[0] == ("Barcha menejerlarga (3)", "msg:all") and menu.menu[-1][1] == "msg:cancel"
    assert [m[0] for m in menu.menu[1:-1]] == ["X-Qo'qon (2)", "X-Marg'ilon (2)"]  # menejersiz filial ro'yxatda yo'q
    assert "X-Qo'qon (2 kishi)" in pick.out[0]
    assert sent[0] == (["8001", "8002"], MESSAGE_HEAD + "Ertaga soat 9 da yig'ilish") and "2 / 2" in one.out[0]
    assert sent[1][0] == ["8001", "8002", "8003"] and "2 / 3" in everyone.out[0] and "bloklagan" in everyone.out[0]
    assert len(sent) == 2 == n and "bekor qilindi" in monkey.out[0] and compose == {}
    assert bad.answers == ["Ruxsat yo'q"] and hack.menu is None


def test_manager_suggestion_and_admin_reply():
    """Menejer taklif yuboradi, bosh admin botdan oladi va javob yozadi."""
    from types import SimpleNamespace as NS

    from sqlalchemy import update

    from app.adminbot import BTN_SUGGEST, BTN_WEEK, MESSAGE_HEAD, AdminBot

    class Ev:
        def __init__(self, sender, text="", data=b""):
            self.sender_id, self.raw_text, self.data = sender, text, data
            self.message, self.out, self.answers = NS(contact=None, media=None), [], []

        async def get_sender(self):
            return NS(first_name="Ali", last_name="Valiyev", username="ali_v")

        async def respond(self, text, buttons=None, parse_mode=None):
            self.out.append(text)

        async def answer(self, text="", alert=False):
            self.answers.append(text)

        async def edit(self, text, buttons=None, parse_mode=None):
            self.out.append(text)

    async def run():
        await init_db()
        async with Session() as s:
            await s.execute(update(Target).values(active=False))
            s.add(Target(name="T-Qo'qon", username="sug_1", manager_ids={"6001": "1"}))
            await s.commit()
        bot, sent = AdminBot(), []

        async def fake_send(users, text, buttons=None):
            data = buttons[0].type.data.decode() if buttons else None
            sent.append(([str(u) for u in users], text, data))
            return len(list(users))

        bot.send = fake_send
        ask = Ev(6001, BTN_SUGGEST)
        await bot._on_message(ask)
        idea = Ev(6001, "Do'konga ko'proq bolalar kitobi kerak")
        await bot._on_message(idea)
        again = Ev(6001, "bu oddiy xabar, taklif emas")
        await bot._on_message(again)
        # taklif yozish o'rniga boshqa tugma bosilsa — bekor bo'ladi
        await bot._on_message(Ev(6001, BTN_SUGGEST))
        n = len(sent)
        cancel = Ev(6001, "/bekor")
        await bot._on_message(cancel)
        # begona odam taklif yubora olmaydi
        stranger = Ev(31337, BTN_SUGGEST)
        await bot._on_message(stranger)
        # admin javob yozadi
        click = Ev(777, data=sent[0][2].encode())
        await bot._on_click(click)
        reply = Ev(777, "Rahmat, keyingi hafta yuboramiz")
        await bot._on_message(reply)
        return ask, idea, sent, n, cancel, stranger, click, reply, bot

    ask, idea, sent, n, cancel, stranger, click, reply, bot = asyncio.run(run())
    assert "yozib yuboring" in ask.out[0] and "bosh adminga yuborildi" in idea.out[0]
    users, text, data = sent[0]
    assert users == ["777"] and data == "msg:u6001"
    assert text == "💡 Taklif — T-Qo'qon (Ali Valiyev, @ali_v):\n\nDo'konga ko'proq bolalar kitobi kerak"
    assert n == 1 and "bekor qilindi" in cancel.out[0]  # ikkinchi xabar va bekor qilingan taklif ketmadi
    assert not any("yozib yuboring" in t for t in stranger.out)
    assert "Kimga: menejer (1 kishi)" in click.out[0]
    assert sent[1][:2] == (["6001"], MESSAGE_HEAD + "Rahmat, keyingi hafta yuboramiz") and "1 / 1" in reply.out[0]
    assert not bot._compose and not bot._suggest


def test_rating_text_and_grades():
    rk = {"total": 20, "average": 38.0, "ranks": {5: {"rank": 9, "reach": 42, "name": "Qo'qon"}, 6: {"rank": 15, "reach": 33, "name": "B"}, 7: {"rank": 20, "reach": 12, "name": "C"}}}
    text = reports.rating_text(5, rk, {"5": 8})
    assert text.startswith("🏆 Reyting: 9-o'rin (20 ta filial ichida), kechaga nisbatan ↓1\n🎯 Baho: 🟢 yaxshi\n📈 Qamrov: 42% (filiallar o'rtachasi 38%)")
    assert "kechaga nisbatan ↑3" in reports.rating_text(6, rk, {"6": 18}) and "🟡 o'rtacha" in reports.rating_text(6, rk, None)
    assert "🔴 e'tibor kerak" in reports.rating_text(7, rk, {"7": 20}) and "o'zgarmadi" in reports.rating_text(7, rk, {"7": 20})
    assert "kechagi ma'lumot yo'q" in reports.rating_text(5, rk, None)
    assert "hali hisoblanmadi" in reports.rating_text(99, rk, None)
    assert reports.rating_text(5, rk, None, with_name=True).startswith("Qo'qon\n🏆")
    assert reports.previous_ranks({"2026-11-12": {"5": 3}, "2026-11-13": {"5": 4}, "2026-11-14": {"5": 9}}, "2026-11-14") == {"5": 4}
    assert reports.previous_ranks({"2026-11-14": {"5": 9}}, "2026-11-14") is None


def test_daily_rating_flow(monkeypatch):
    """Kunlik reyting faqat menejerlarga boradi, o'rinlar tarixga yoziladi va ertasi kuni solishtiriladi."""
    from sqlalchemy import update

    state = {"reach": {}}

    async def fake_compare(days):
        return {"channels": [{"id": i, "name": n, "active": True, "reach": r, "growth_pct": 0} for i, (n, r) in state["reach"].items()]}

    import app.web as web

    monkeypatch.setattr(web, "compare_data", fake_compare)

    class Bot:
        def __init__(self):
            self.msgs = []

        async def send(self, users, text, buttons=None):
            self.msgs.append((list(users), text))
            return len(list(users))

    async def run():
        await init_db()
        async with Session() as s:
            await s.execute(update(Target).values(active=False))
            a = Target(name="R-A", username="rate_a", manager_ids={"5101": "1"})
            b = Target(name="R-B", username="rate_b", manager_ids={"5102": "2"})
            c = Target(name="R-C", username="rate_c")
            s.add_all([a, b, c])
            await s.commit()
            ids = (a.id, b.id, c.id)
        await reports._set("rank_history", "")
        bot = Bot()
        state["reach"] = {ids[0]: ("R-A", 50), ids[1]: ("R-B", 40), ids[2]: ("R-C", 30)}
        day1 = datetime(2026, 11, 13, 18, 0, tzinfo=T)
        n1 = await reports.send_ratings(bot, day1, store=True)
        state["reach"] = {ids[0]: ("R-A", 35), ids[1]: ("R-B", 45), ids[2]: ("R-C", 30)}  # A pastga, B yuqoriga
        day2 = datetime(2026, 11, 14, 18, 0, tzinfo=T)
        await reports.send_ratings(bot, day2, store=True)
        press = Bot()
        await reports.send_ratings(press, day2, only_user=5101)  # tugma bosilganda faqat o'ziga, tarix o'zgarmaydi
        return bot.msgs, press.msgs, n1

    msgs, press, n1 = asyncio.run(run())
    assert n1 == 2 and {tuple(u) for u, _ in msgs} == {("5101",), ("5102",)}  # bosh adminga yuborilmaydi
    first_a = next(t for u, t in msgs[:2] if u == ["5101"])
    assert "1-o'rin (3 ta filial ichida), kechagi ma'lumot yo'q" in first_a
    second_a = next(t for u, t in msgs[2:] if u == ["5101"])
    second_b = next(t for u, t in msgs[2:] if u == ["5102"])
    assert "2-o'rin (3 ta filial ichida), kechaga nisbatan ↓1" in second_a
    assert "1-o'rin (3 ta filial ichida), kechaga nisbatan ↑1" in second_b
    assert len(press) == 1 and press[0][0] == ["5101"] and "kechaga nisbatan ↓1" in press[0][1]


def test_report_menu_and_period_choice(monkeypatch):
    """📊 Hisobot → Kunlik / Haftalik / Oylik tugmalari; tanlangan davr bo'yicha hisobot yuboriladi."""
    from types import SimpleNamespace as NS

    from app.adminbot import BTN_REPORT, AdminBot

    class Ev:
        def __init__(self, sender, text="", data=b""):
            self.sender_id, self.raw_text, self.data = sender, text, data
            self.message, self.out, self.answers = NS(contact=None, media=None), [], []

        async def respond(self, text, buttons=None, parse_mode=None):
            self.out.append((text, buttons))

        async def answer(self, text="", alert=False):
            self.answers.append((text, alert))

    calls = []

    async def fake_reports(bot, kind, now_local=None, only_user=None):
        calls.append((kind, only_user))
        return 1

    monkeypatch.setattr(reports, "send_reports", fake_reports)

    async def run():
        await init_db()
        bot = AdminBot()
        menu = Ev(777, BTN_REPORT)
        await bot._on_message(menu)
        for data in (b"rep:daily", b"rep:weekly", b"rep:monthly"):
            await bot._on_click(Ev(777, data=data))
        stranger = Ev(31337, "/hisobot")
        await bot._on_message(stranger)
        return menu, stranger

    menu, stranger = asyncio.run(run())
    text, rows = menu.out[0]
    assert text == "Qaysi davr uchun hisobot kerak?"
    assert [b.type.data.decode() for b in rows[0]] == ["rep:daily", "rep:weekly", "rep:monthly"]
    assert [b.text for b in rows[0]] == ["Kunlik", "Haftalik", "Oylik"]
    assert calls == [("daily", 777), ("weekly", 777), ("monthly", 777)]
    assert stranger.out[0][0].startswith("Sizga tegishli hisobot yo'q")


def test_daily_period_label():
    assert reports.period_of("daily", datetime(2026, 11, 14, 18, 0, tzinfo=T)) == (1, "Kunlik hisobot", "14 noyabr 2026")


def test_rating_counts_all_channels():
    """Reyting xabari barcha faol filiallarni sanaydi; ma'lumotsizlar alohida aytiladi."""
    rk = {"total": 12, "rated": 4, "average": 22.0, "ranks": {1: {"rank": 1, "reach": 25, "name": "Q"}}}
    text = reports.rating_text(1, rk, {"1": 1})
    assert text.startswith("🏆 Reyting: 1-o'rin (12 ta filial ichida), kechaga nisbatan o'zgarmadi")
    assert "8 ta filialda hali yetarli ma'lumot yo'q" in text
    assert "12 ta filialdan 4 tasida reyting bor" in reports.rating_text(99, rk, None)
    full = {"total": 3, "rated": 3, "average": 30.0, "ranks": {1: {"rank": 2, "reach": 31, "name": "Q"}}}
    assert "yetarli ma'lumot yo'q" not in reports.rating_text(1, full, None)


def test_stats_failure_is_logged_once():
    from types import SimpleNamespace as NS

    from sqlalchemy import select, update

    from app import stats
    from app.db import Event

    class Bad:
        async def __call__(self, req):
            raise RuntimeError("CHANNEL_PRIVATE")

    async def run():
        await init_db()
        async with Session() as s:
            await s.execute(update(Target).values(active=False))
            s.add(Target(name="SF", username="sf_1"))
            await s.commit()
        await stats.collect_once(Bad())
        await stats.collect_once(Bad())  # ikkinchi marta jurnalga yozilmaydi
        async with Session() as s:
            return [e.detail for e in (await s.execute(select(Event).where(Event.kind == "stats_failed"))).scalars() if "sf_1" in e.detail]

    rows = asyncio.run(run())
    assert len(rows) == 1 and "CHANNEL_PRIVATE" in rows[0]


def test_mirror_channel_not_in_ranking(monkeypatch):
    import app.web as web

    async def fake_compare(days):
        return {"channels": [{"id": 1, "name": "A", "active": True, "reach": 40, "growth_pct": 0},
                             {"id": 2, "name": "Lotin", "active": True, "reach": 90, "growth_pct": 0, "mirror": True}]}

    monkeypatch.setattr(web, "compare_data", fake_compare)
    rk = asyncio.run(reports.ranking())
    assert rk["total"] == 1 and list(rk["ranks"]) == [1]


def test_overall_report_skips_inactive_channels():
    from datetime import datetime

    from PIL import Image

    rows = [{"id": i, "name": f"K{i}", "active": i < 3, "subs": 100, "growth": 1, "growth_pct": 1.0, "posts": 1, "views": 10, "reach": 10}
            for i in range(30)]
    img = Image.open(io.BytesIO(reports.render_overall({"channels": rows, "avg_reach": 10, "audience": {}}, "Haftalik hisobot", "x",
                                                       datetime(2026, 11, 19, 18, 0, tzinfo=T))))
    assert img.height < 2000  # faqat 3 ta faol kanal chizildi


def test_overall_report_groups_by_region():
    import io
    from datetime import datetime

    from PIL import Image

    from app import reports
    from app.web import REGIONS, region_rows

    ch = [dict(id=i, name=n, subs=s, growth=g, posts=p, reach=r, region=rg, active=a, mirror=m, views=None)
          for i, (n, s, g, p, r, rg, a, m) in enumerate([
              ("Qo'qon", 1000, -7, 5, 10, "vodiy", True, False), ("Andijon", 500, 1, 5, 20, "vodiy", True, False),
              ("Chorsu", 300, 6, 2, 30, "toshkent", True, False), ("Lotin", 600, 0, 7, 11, "", True, True),
              ("Eski", 900, 0, 0, None, "voha", False, False)])]
    members = {0: {"a", "b"}, 1: {"b", "c"}}
    rows = {r["key"]: r for r in region_rows(ch, members)}
    assert set(rows) == {"vodiy", "toshkent"}  # to'xtatilgan va to'liq nusxa kanallar kirmaydi
    v = rows["vodiy"]
    assert (v["channels"], v["subs"], v["growth"], v["posts"]) == (2, 1500, -6, 10)
    assert v["reach"] == round((10 * 1000 + 20 * 500) / 1500)  # obunachi bo'yicha tortilgan
    assert v["unique"] == 3 and rows["toshkent"]["unique"] is None
    assert region_rows([dict(ch[2], growth=0)], {})[0]["growth_pct"] == 0.0
    cmp_ = {"channels": ch, "avg_reach": 15, "audience": {}, "regions": list(rows.values()), "region_names": REGIONS}
    img = Image.open(io.BytesIO(reports.render_overall(cmp_, "Kunlik hisobot", "x", datetime(2026, 11, 20, 18))))
    assert img.width == 1080 and img.height > 1500


def test_pending_saturday_schedule_and_texts():
    from datetime import datetime
    from types import SimpleNamespace as T

    from app import reports

    sat = lambda h: datetime(2026, 11, 28, h, 5, tzinfo=reports.TASHKENT)  # noqa: E731 — shanba
    assert reports.pending_due(sat(12), "", "") == []
    assert reports.pending_due(sat(13), "", "") == ["remind"]
    assert reports.pending_due(sat(16), "", "") == ["remind", "summary"]
    assert reports.pending_due(sat(17), "2026-11-28", "") == ["summary"]
    assert reports.pending_due(sat(17), "2026-11-28", "2026-11-28") == []
    assert reports.pending_due(datetime(2026, 11, 27, 16, 0, tzinfo=reports.TASHKENT), "", "") == []  # juma

    q, j, n = T(id=1, name="Qo'qon", manager_ids={"1": "a"}), T(id=2, name="Jizzax", manager_ids={}), T(id=3, name="Nukus", manager_ids={"2": "b"})
    counts = {1: 3, 2: 5}
    assert reports.reminder_text([n], counts) is None  # kutayotgan post yo'q — bezovta qilinmaydi
    assert "3 ta post" in reports.reminder_text([q], counts)
    assert "Qo'qon: 3 ta" in reports.reminder_text([q, n], {1: 3, 3: 0}) or "3 ta post" in reports.reminder_text([q, n], {1: 3})
    text = reports.summary_text([q, j, n], counts)
    assert "2 ta filialda jami 8 ta post" in text
    assert text.index("Jizzax: 5 ta") < text.index("Qo'qon: 3 ta")  # ko'pi yuqorida
    assert "Jizzax: 5 ta — menejer botga bog'lanmagan" in text and "Qolgan 1 ta filialda" in text
    assert "hech bir filialda" in reports.summary_text([q, j], {})


def test_pending_counts_skip_deleted_and_mirror():
    import asyncio

    from sqlalchemy import update

    from app import reports
    from app.db import Delivery, Session, SourcePost, Target, init_db

    async def run():
        await init_db()
        async with Session() as s:
            await s.execute(update(Target).values(active=False))
            a, m = Target(name="PC-A", username="pc_a"), Target(name="PC-M", username="pc_m", mirror=True)
            live, gone = SourcePost(chat_id=1, msg_ids=[9101]), SourcePost(chat_id=1, msg_ids=[9102], deleted=True)
            s.add_all([a, m, live, gone])
            await s.flush()
            s.add_all([Delivery(post_id=live.id, target_id=a.id, status="pending"), Delivery(post_id=gone.id, target_id=a.id, status="pending"),
                       Delivery(post_id=live.id, target_id=m.id, status="pending"), Delivery(post_id=live.id, target_id=a.id, status="sent")])
            await s.commit()
            ids = (a.id, m.id)
        targets, counts = await reports.pending_counts()
        async with Session() as s:  # boshqa testlarga ta'sir qilmasin (baza umumiy)
            await s.execute(update(Target).where(Target.id.in_(ids)).values(active=False))
            await s.commit()
        return ids, [t.id for t in targets], counts

    (a, m), tids, counts = asyncio.run(run())
    assert counts[a] == 1 and a in tids and m not in tids


def test_pending_board_by_region_and_image():
    import asyncio
    import io
    from datetime import datetime

    from PIL import Image
    from sqlalchemy import update

    from app import reports
    from app.db import Delivery, Session, SourcePost, Target, init_db

    async def run():
        await init_db()
        async with Session() as s:
            await s.execute(update(Target).values(active=False))
            q = Target(name="PB Qo'qon", username="pb_q", manager_ids={"1": "a"})  # bo'limsiz, lekin Vodiy shahri
            v = Target(name="PB Voha", username="pb_v", region="voha")
            t = Target(name="PB Chorsu", username="pb_t")  # bo'limsiz
            post, old = SourcePost(chat_id=2, msg_ids=[9201], text="✨ Жума муборак!\n\n@hilolnashr"), SourcePost(chat_id=2, msg_ids=[9202], text="")
            s.add_all([q, v, t, post, old])
            await s.flush()
            s.add_all([Delivery(post_id=post.id, target_id=q.id, status="pending"), Delivery(post_id=old.id, target_id=q.id, status="pending"),
                       Delivery(post_id=post.id, target_id=v.id, status="pending")])
            await s.commit()
        now = datetime.now(reports.TASHKENT)
        res = {r: await reports.pending_board(r, now) for r in ("all", "vodiy", "voha", "toshkent")}
        async with Session() as s:  # yaratilganlarni to'liq o'chiramiz: boshqa testlar umumiy bazada ishlaydi
            from sqlalchemy import delete

            ids = [x.id for x in (q, v, t)]
            await s.execute(delete(Delivery).where(Delivery.target_id.in_(ids)))
            await s.execute(delete(SourcePost).where(SourcePost.id.in_((post.id, old.id))))
            await s.execute(delete(Target).where(Target.id.in_(ids)))
            await s.commit()
        return res, now

    res, now = asyncio.run(run())
    assert res["all"]["total"] == 3 and len(res["all"]["channels"]) == 3
    vodiy = res["vodiy"]
    assert [c["name"] for c in vodiy["channels"]] == ["PB Qo'qon"] and vodiy["total"] == 2 and vodiy["unassigned"] == 1
    texts = [p["text"] for p in vodiy["channels"][0]["posts"]]
    assert "Жума муборак!" in texts[0] and texts[1].startswith("(matnsiz post")
    assert res["voha"]["total"] == 1 and res["toshkent"]["total"] == 0
    for r in res.values():
        img = Image.open(io.BytesIO(reports.render_pending(r, now)))
        assert img.width == 1080
