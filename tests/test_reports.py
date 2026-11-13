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
    from app.adminbot import BTN_MONTH, BTN_QUEUE, BTN_RESUME, BTN_STATE, BTN_STOP, BTN_WEEK, AdminBot, keyboard

    def labels(rows):
        return [b.button.text for row in rows for b in row]

    assert labels(keyboard(False, True)) == [BTN_WEEK, BTN_MONTH, adminbot.BTN_RATING, BTN_QUEUE, adminbot.BTN_SUGGEST]
    assert labels(keyboard(True, False)) == [BTN_WEEK, BTN_MONTH, BTN_STATE, BTN_STOP, adminbot.BTN_MANAGERS, adminbot.BTN_MESSAGE]
    assert labels(keyboard(True, True, paused=True)) == [BTN_WEEK, BTN_MONTH, adminbot.BTN_RATING, BTN_QUEUE, adminbot.BTN_SUGGEST, BTN_STATE, BTN_RESUME, adminbot.BTN_MANAGERS, adminbot.BTN_MESSAGE]

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
