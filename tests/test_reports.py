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
