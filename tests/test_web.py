import asyncio
from datetime import timedelta

from fastapi.testclient import TestClient

from app.db import Delivery, Rule, Session, SourcePost, StatChannel, StatPost, Target, init_db, now
from app.web import create_app, svg_points


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
    c = TestClient(create_app(pipe), follow_redirects=False)

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
