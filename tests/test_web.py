import asyncio
from datetime import timedelta

from fastapi.testclient import TestClient

from app.db import Delivery, Rule, Session, SourcePost, StatChannel, StatPost, Target, init_db, now
from app.web import create_app, svg_points


class FakeAgent:
    """Telegram'siz agent: kirish jarayonini taqlid qiladi."""

    def __init__(self, pipeline):
        self.pipeline = pipeline
        self.status = {"connected": False, "error": "Telegram akkaunt ulanmagan."}
        self.login_step = "phone"
        self.login_hint = "Kod Telegram ilovasiga yuborildi"
        self.login_error = ""

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

    async def publish(self, delivery_id):
        self.published.append(delivery_id)
        return True

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
        pend = Delivery(post_id=post.id, target_id=t.id, status="pending", reason="AI ishonchi past")
        s.add_all([sent, pend])
        await s.flush()
        s.add(StatChannel(target_id=t.id, subscribers=1000, at=now() - timedelta(days=2)))
        s.add(StatChannel(target_id=t.id, subscribers=1100, at=now()))
        s.add(StatPost(delivery_id=sent.id, views=440))
        await s.commit()
        return t.id, pend.id


def test_panel_flow():
    target_id, pending_id = asyncio.run(_seed())
    pipe = FakePipeline()
    agent = FakeAgent(pipe)
    c = TestClient(create_app(agent), follow_redirects=False)

    # kirmasdan hech narsa ochilmaydi
    assert c.get("/").status_code == 303 and c.get("/").headers["location"] == "/login"
    assert c.post(f"/channels/{target_id}/delete").status_code == 303
    assert "Qo'qon" not in c.get("/channels", follow_redirects=True).text
    assert "noto" in c.post("/login", data={"username": "admin", "password": "xato"}).text
    assert c.post("/login", data={"username": "admin", "password": "sinov-parol"}).headers["location"] == "/"

    home = c.get("/").text
    assert "1 100" in home and "+100" in home and "40%" in home and "440" in home
    assert c.get("/?days=7").status_code == 200

    # kanalni tahrirlash
    r = c.post(f"/channels/{target_id}", data={"name": "Qo'qon 2", "username": "https://t.me/qoqon_x", "mode": "auto", "template_html": "T", "keywords": "k", "sync_edits": "1"})
    assert r.headers["location"].endswith("saved=1")
    page = c.get(f"/channels/{target_id}?saved=1").text
    assert "Qo&#39;qon 2" in page and "Saqlandi" in page
    # yangi kanal, agent admin bo'lmagan holat
    r = c.post("/channels/0", data={"name": "Yangi", "username": "@yomon", "mode": "smart"})
    assert "Agent admin emas" in c.get(r.headers["location"]).text
    # takroriy username rad etiladi
    c.post("/channels/0", data={"name": "Dubl", "username": "qoqon_x"})
    assert "allaqachon" in c.get("/channels/0").text
    c.post(f"/channels/{target_id}/toggle")
    assert "Davom ettirish" in c.get("/channels").text

    # navbat: matn ekranlanadi, yuborish pipeline'ni chaqiradi
    q = c.get("/queue").text
    assert "AI ishonchi past" in q and "<script>alert" not in q
    c.post(f"/queue/{pending_id}/send")
    assert pipe.published == [pending_id]
    c.post(f"/queue/{pending_id}/reject")
    assert "Navbat bo" in c.get("/queue").text

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

    # qoidalar
    c.post("/rules", data={"text": "Aksiya postlari tasdiqdan keyin"})
    assert "Aksiya postlari" in c.get("/rules").text

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
