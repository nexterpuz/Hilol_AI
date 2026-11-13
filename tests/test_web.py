import asyncio
from datetime import timedelta

from fastapi.testclient import TestClient

from app.db import Delivery, Rule, Session, SourcePost, StatChannel, StatPost, Subscriber, Target, init_db, now
from app.web import create_app, svg_points


class FakeAgent:
    """Telegram'siz agent: kirish jarayonini taqlid qiladi."""

    def __init__(self, pipeline):
        self.pipeline = pipeline
        self.status = {"connected": False, "error": "Telegram akkaunt ulanmagan."}
        self.login_step = "phone"
        self.login_hint = "Kod Telegram ilovasiga yuborildi"
        self.login_error = ""
        self.source_username = "eski_manba"

    async def set_source(self, username):
        if username == "yoq":
            return "Kanal topilmadi"
        self.source_username = username
        return ""

    def qr_svg(self):
        return "<svg data-test='qr'></svg>" if self.login_step == "qr" else ""

    async def begin_qr(self):
        self.login_step = "qr"
        return ""

    async def begin_login(self, phone):
        if phone != "+998901234567":
            return "Kod yuborilmadi"
        self.login_step = "code"
        return ""

    async def finish_login(self, code="", password=""):
        if self.login_step == "code":
            if code != "12345":
                return "Kod noto'g'ri."
            self.login_step = "password"
            return ""
        if password != "maxfiy":
            return "Ikki bosqichli parol noto'g'ri."
        self.login_step = "phone"
        self.status = {"connected": True, "error": "", "name": "Sinov", "username": "sinov", "phone": "+99890…67", "premium": True, "source": "manba"}
        return ""

    async def check_targets(self):
        return ["Agent akkaunti @x kanalida admin emas."]

    async def cancel_login(self):
        self.login_step = "phone"

    async def logout(self):
        self.status = {"connected": False, "error": "Telegram akkaunt ulanmagan."}


class FakePipeline:
    def __init__(self):
        self.published = []

    async def approve(self, delivery_id):
        self.published.append(delivery_id)
        return True

    async def recall(self, post_id):
        self.published.append(("recall", post_id))
        return 2

    async def reject(self, delivery_id, remember=True):
        async with Session() as s:
            d = await s.get(Delivery, delivery_id)
            d.status = "rejected"
            await s.commit()

    async def fetch_template(self, link):
        if "t.me/" not in link:
            return "", "Havola noto'g'ri."
        return '<tg-emoji emoji-id="5368324170671202286">🤙</tg-emoji> +998-90-000-00-00', ""

    async def check_target(self, username):
        return "" if username != "yomon" else "Agent admin emas"


async def _seed():
    await init_db()
    async with Session() as s:
        t = Target(name="Qo'qon", username="qoqon_x", template_html="<b>Aloqa</b>")
        s.add(t)
        post = SourcePost(chat_id=-1, msg_ids=[1], text="Yangi kitob <script>alert(1)</script>")
        s.add(post)
        await s.flush()
        sent = Delivery(post_id=post.id, target_id=t.id, status="sent", sent_at=now(), target_msg_ids=[9])
        pend = Delivery(post_id=post.id, target_id=t.id, status="pending", reason="Sinov sababi 7391")
        s.add_all([sent, pend])
        await s.flush()
        s.add(StatChannel(target_id=t.id, subscribers=1000, at=now() - timedelta(days=2)))
        s.add(StatChannel(target_id=t.id, subscribers=1100, at=now()))
        s.add(StatPost(delivery_id=sent.id, views=440, reach=0.4))
        await s.commit()
        return t.id, pend.id


def test_panel_flow():
    target_id, pending_id = asyncio.run(_seed())
    pipe = FakePipeline()
    agent = FakeAgent(pipe)
    c = TestClient(create_app(agent), follow_redirects=False)

    # kirmasdan API ham yopiq
    assert c.get("/api/compare").status_code == 401
    # kirmasdan hech narsa ochilmaydi
    assert c.get("/").status_code == 303 and c.get("/").headers["location"] == "/login"
    assert c.post(f"/channels/{target_id}/delete").status_code == 303
    assert "Qo'qon" not in c.get("/channels", follow_redirects=True).text
    assert "noto" in c.post("/login", data={"username": "admin", "password": "xato"}).text
    assert c.post("/login", data={"username": "admin", "password": "sinov-parol"}).headers["location"] == "/"

    home = c.get("/").text
    assert "1 100" in home and "+100" in home and "40%" in home and "440" in home
    assert c.get("/?days=7").status_code == 200

    # favqulodda to'xtatish: banner chiqadi, davom ettirilganda yo'qoladi
    assert "Favqulodda to'xtatish yoqilgan" not in c.get("/").text
    c.post("/pause", data={"state": "on"})
    assert "Favqulodda to'xtatish yoqilgan" in c.get("/channels").text
    c.post("/pause", data={"state": "off"})
    assert "Favqulodda to'xtatish yoqilgan" not in c.get("/").text
    # postni qaytarib olish
    assert "Qaytarib olish" in c.get("/posts").text
    c.post("/posts/1/recall")
    assert ("recall", 1) in pipe.published and "2 ta filial kanalidan" in c.get("/posts").text
    pipe.published.clear()

    # postlar analitikasi va jurnal
    posts_page = c.get("/posts").text
    assert "Yangi kitob" in posts_page and "440" in posts_page and "40%" in posts_page
    assert c.get("/log").status_code == 200

    # analitika: solishtirish, kanal sahifasi, eng yaxshi vaqt
    assert c.get("/analytics").status_code == 200 and c.get(f"/channels/{target_id}/stats").status_code == 200
    cmp_ = c.get("/api/compare?days=30").json()
    row = next(x for x in cmp_["channels"] if x["id"] == target_id)
    assert row["subs"] == 1100 and row["growth"] == 100 and row["growth_pct"] == 10.0 and row["reach"] == 40
    ch = c.get(f"/api/channel/{target_id}?days=30").json()
    assert ch["subs"] == 1100 and ch["growth"] == 100 and len(ch["series"]) == 2
    assert ch["posts_count"] == 1 and ch["avg_views"] == 440 and ch["avg_reach"] == 40
    assert c.get("/api/channel/999999").status_code == 404
    bt = c.get(f"/api/besttime?days=30&target_id={target_id}").json()
    assert bt["metric"] == "reach" and len(bt["cells"]) == 1 and bt["cells"][0]["reach"] == 40.0 and bt["cells"][0]["posts"] == 1
    assert len(bt["weekdays"]) == 7 and bt["top"][0]["hour"] == bt["cells"][0]["hour"]

    # kanalni tahrirlash
    r = c.post(f"/channels/{target_id}", data={"name": "Qo'qon 2", "username": "https://t.me/qoqon_x", "mode": "auto", "template_html": "T", "signature_html": "@Hilolnashr_qoqon", "keywords": "k", "sync_edits": "1", "manager_phones": "+998 90 111 22 33", "branch_approval": "1"})
    assert r.headers["location"].endswith("saved=1")
    page = c.get(f"/channels/{target_id}?saved=1").text
    assert "Qo&#39;qon 2" in page and "Saqlandi" in page and 'value="@Hilolnashr_qoqon"' in page
    assert 'value="+998 90 111 22 33"' in page and 'name="branch_approval" value="1" checked' in page
    # shablonni Telegram xabaridan olish (premium emoji bilan)
    c.post(f"/channels/{target_id}/template-import", data={"link": "yaroqsiz"})
    assert "Havola noto" in c.get(f"/channels/{target_id}").text
    c.post(f"/channels/{target_id}/template-import", data={"link": "https://t.me/qoqon_x/15"})
    tpl_page = c.get(f"/channels/{target_id}").text
    assert "tg-emoji emoji-id=&#34;5368324170671202286&#34;" in tpl_page and "Premium yo" in tpl_page
    # yangi kanal, agent admin bo'lmagan holat
    r = c.post("/channels/0", data={"name": "Yangi", "username": "@yomon", "mode": "smart"})
    assert "Agent admin emas" in c.get(r.headers["location"]).text
    # takroriy username rad etiladi
    c.post("/channels/0", data={"name": "Dubl", "username": "qoqon_x"})
    assert "allaqachon" in c.get("/channels/0").text
    # manba kanalni almashtirish
    assert 'value="eski_manba"' in c.get("/channels").text
    c.post("/source", data={"username": "yoq"})
    assert "Kanal topilmadi" in c.get("/channels").text
    c.post("/source", data={"username": "yangi_manba"})
    ch = c.get("/channels").text
    assert "o&#39;zgartirildi" in ch and 'value="yangi_manba"' in ch
    c.post(f"/channels/{target_id}/toggle")
    assert "Davom ettirish" in c.get("/channels").text

    # navbat: matn ekranlanadi, yuborish pipeline'ni chaqiradi
    q = c.get("/queue").text
    assert "Sinov sababi 7391" in q and "<script>alert" not in q
    c.post(f"/queue/{pending_id}/send")
    assert pipe.published == [pending_id]
    c.post(f"/queue/{pending_id}/reject")
    assert "Sinov sababi 7391" not in c.get("/queue").text  # rad etilgan post navbatdan chiqdi

    # Telegram akkauntga panel orqali kirish
    assert "ulanmagan" in c.get("/account").text
    c.post("/account/phone", data={"phone": "123"})
    assert "xalqaro formatda" in c.get("/account").text
    c.post("/account/phone", data={"phone": "+998 90 123-45-67"})
    assert "Tasdiqlash kodi" in c.get("/account").text
    c.post("/account/confirm", data={"code": "00000"})
    assert "Kod noto" in c.get("/account").text
    c.post("/account/confirm", data={"code": "12345"})
    assert "Ikki bosqichli parol" in c.get("/account").text
    c.post("/account/confirm", data={"password": "maxfiy"})
    acc = c.get("/account").text
    assert "Ulangan" in acc and "Premium" in acc and "admin emas" in acc
    c.post("/account/logout")
    assert "ulanmagan" in c.get("/account").text
    # QR orqali kirish sahifasi
    c.post("/account/qr")
    qr = c.get("/account").text
    assert "data-test='qr'" in qr and "Qurilmani ulash" in qr and 'http-equiv="refresh"' in qr
    c.post("/account/cancel")
    assert "QR kod orqali kirish" in c.get("/account").text

    # integratsiyalar
    integ = c.get("/integrations").text
    assert "Claude API" in integ and "Instagram" in integ and "Ulanmagan" in integ

    # qoidalar
    c.post("/rules", data={"text": "Aksiya postlari tasdiqdan keyin"})
    rules_page = c.get("/rules").text
    assert "Aksiya postlari" in rules_page and "вакансия" in rules_page
    c.post("/rules/ask-words", data={"words": "tanlov ,  konkurs,,"})
    assert 'value="tanlov, konkurs"' in c.get("/rules").text

    # o'chirish bog'liq yozuvlar bilan birga ishlaydi
    c.post(f"/channels/{target_id}/delete")
    assert "qoqon_x" not in c.get("/channels").text
    c.post("/logout")
    assert c.get("/").headers["location"] == "/login"


def test_svg_points():
    assert svg_points([5]) == ""
    assert svg_points([0, 10]).split() == ["0.0,190.0", "600.0,10.0"]


def test_session_encryption_roundtrip():
    from app.agent import decrypt, encrypt

    token = encrypt("1BVtsOK-sessiya")
    assert token != "1BVtsOK-sessiya" and decrypt(token) == "1BVtsOK-sessiya"
    assert decrypt("buzilgan") is None


def test_db_url_normalised(monkeypatch):
    from app import config

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host:5432/db")
    assert config._db_url() == "postgresql+asyncpg://u:p@host:5432/db"


def test_code_hint_and_qr_svg():
    from telethon.tl import types

    from app.agent import Agent, _code_hint

    assert "ilovasiga" in _code_hint(types.auth.SentCodeTypeApp(length=5))
    assert "SMS" in _code_hint(types.auth.SentCodeTypeSms(length=5))
    agent = Agent("manba")
    assert agent.qr_svg() == "" and agent.login_step == "phone"
    agent._login = {"mode": "qr", "qr": type("Q", (), {"url": "tg://login?token=abc"})(), "need_password": False}
    assert agent.qr_svg().startswith("<svg") and agent.login_step == "qr"


def test_source_setting_persists():
    from app.agent import Agent

    async def run():
        await init_db()
        a = Agent("seeddagi")
        assert await a.set_source("https://t.me/Haqiqiy_kanal") == ""
        b = Agent("seeddagi")
        await b.load_source()
        return b.source_username

    assert asyncio.run(run()) == "Haqiqiy_kanal"


def test_views_at():
    from app.web import views_at

    sent = now() - timedelta(hours=30)
    snaps = [(sent + timedelta(minutes=50), 100), (sent + timedelta(hours=24, minutes=10), 400), (sent + timedelta(hours=29), 450)]
    assert views_at(snaps, sent, 1, now()) == 100
    assert views_at(snaps, sent, 24, now()) == 400
    assert views_at(snaps, sent, 72, now()) is None  # hali 72 soat o'tmagan
    assert views_at([], sent, 1, now()) is None


def test_unique_audience():
    """Bir odam ikki kanalda bo'lsa bir marta sanaladi."""
    from app.web import compare_data

    async def run():
        await init_db()
        async with Session() as s:
            a, b = Target(name="U1", username="uniq_1"), Target(name="U2", username="uniq_2")
            s.add_all([a, b])
            await s.flush()
            s.add_all([StatChannel(target_id=a.id, subscribers=3), StatChannel(target_id=b.id, subscribers=2)])
            for h in ("x1", "x2", "x3"):
                s.add(Subscriber(target_id=a.id, user_hash=h))
            for h in ("x3", "x4"):
                s.add(Subscriber(target_id=b.id, user_hash=h))
            await s.commit()
            ids = (a.id, b.id)
        data = await compare_data(30)
        rows = {c["id"]: c for c in data["channels"]}
        return rows[ids[0]], rows[ids[1]], data["audience"]

    a, b, aud = asyncio.run(run())
    assert (a["listed"], a["only_here"], a["shared"]) == (3, 2, 1)
    assert (b["listed"], b["only_here"], b["shared"]) == (2, 1, 1)
    assert aud["multi"] >= 1 and aud["unique"] >= 4 and aud["unique"] == aud["listed"] - aud["multi"]


def test_collect_subscribers_hashes_and_skips_bots():
    from types import SimpleNamespace as U

    from sqlalchemy import select

    from app import stats

    class Client:
        def iter_participants(self, username):
            async def gen():
                for u in (U(id=1, bot=False, deleted=False), U(id=2, bot=True, deleted=False), U(id=3, bot=False, deleted=True), U(id=4, bot=False, deleted=False)):
                    yield u
            return gen()

    async def run():
        await init_db()
        async with Session() as s:
            t = Target(name="H", username="hash_1")
            s.add(t)
            await s.commit()
            tid = t.id
        orig = asyncio.sleep

        async def nosleep(_):
            await orig(0)

        stats.asyncio.sleep = nosleep
        try:
            await stats.collect_subscribers(Client())
            await stats.collect_subscribers(Client())  # takror yig'ish ro'yxatni almashtiradi, ko'paytirmaydi
        finally:
            stats.asyncio.sleep = orig
        async with Session() as s:
            return [r for r in (await s.execute(select(Subscriber.user_hash).where(Subscriber.target_id == tid))).scalars()]

    hashes = asyncio.run(run())
    assert len(hashes) == 2 and all(len(h) == 64 for h in hashes) and "1" not in hashes  # ID emas, xesh
    assert stats.user_hash(1) in hashes and stats.user_hash(4) in hashes
