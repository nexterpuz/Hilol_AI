"""Butun oqim soxta Telegram va soxta AI bilan: albom, istisno, tahrir, media almashtirish, o'chirish."""
import asyncio
import json
from types import SimpleNamespace as NS

from sqlalchemy import select

from app import ai, pipeline
from app.db import Delivery, Session, SourcePost, Target, init_db

CHAT = -100777


def mk(i, text, grouped=None, photo_id=None):
    photo = NS(id=photo_id) if photo_id else None
    return NS(id=i, chat_id=CHAT, message=text, entities=[], grouped_id=grouped, photo=photo,
              document=None, media=f"media-{photo_id}" if photo_id else None)


class Client:
    def __init__(self, store):
        self.store, self.sent, self.edits, self.deleted, self.n = store, [], [], [], 500

    async def get_messages(self, src, ids):
        return [self.store.get(i) for i in ids]

    async def send_message(self, target, text, **k):
        self.n += 1
        self.sent.append((target, text, None))
        return NS(id=self.n)

    async def send_file(self, target, file, caption=None, **k):
        out = []
        for _ in file if isinstance(file, list) else [file]:
            self.n += 1
            out.append(NS(id=self.n))
        self.sent.append((target, caption, file))
        return out if isinstance(file, list) else out[0]

    async def edit_message(self, target, msg_id, text, file=None, **k):
        self.edits.append((target, msg_id, text, file))

    async def delete_messages(self, target, ids):
        self.deleted.append((target, ids))


class Bot:
    def __init__(self):
        self.msgs = []

    async def say(self, text, buttons=None):
        self.msgs.append(text)

    async def ask(self, delivery_id, target, text, reason, skipped):
        self.msgs.append(("ask", delivery_id, skipped))


async def fake_analyze(text, targets, rules):
    if "Manzillarimiz" in text:
        kind, line = "contact", 2
    elif "@hilolnashr" in text:
        kind, line = "signature", text.split("\n").index("@hilolnashr")
    else:
        kind, line = "none", None
    return ai.Analysis(
        block_type=kind, cta_start_line=line,
        decisions=[ai.TargetDecision(target_id=t.id, send="Toshkent" not in text, confidence=0.95, reason="sinov") for t in targets],
    )


async def scenario(monkeypatch):
    monkeypatch.setattr(ai, "analyze", fake_analyze)
    await init_db()
    seed = json.load(open("seed.json", encoding="utf-8"))
    async with Session() as s:
        for t in seed["targets"]:
            s.add(Target(**{**t, "username": "p_" + t["username"]}))
        await s.commit()
        mine = {t.id for t in (await s.execute(select(Target).where(Target.username.like("p_%")))).scalars()}
    store = {}
    c, b = Client(store), Bot()
    p = pipeline.Pipeline(c, "manba", b, premium=False)
    only = lambda rows: [r for r in rows if r[0].startswith("p_")]

    # albom: ikkala filialga, aloqa bloki almashgan holda
    album = [mk(1, "Yangi kitob\nNarxi 39 000\nManzillarimiz\n@hilol", grouped=7, photo_id=11), mk(2, "", grouped=7, photo_id=12)]
    store.update({m.id: m for m in album})
    await p.on_new(album)
    sent = only(c.sent)
    assert len(sent) == 2 and len(sent[0][2]) == 2
    assert "@hilol\n" not in sent[0][1][0] and "Manzillarimiz" not in sent[0][1][0] and "Buyurtma berish uchun" in sent[0][1][0]
    await p.on_new(album)
    assert len(only(c.sent)) == 2  # takror kelgan xabar qayta yuborilmaydi

    # faqat bitta filialga tegishli yangilik: yuborilmaydi, adminga xabar boradi
    store[3] = mk(3, "Toshkent filiali bugun ishlamaydi")
    await p.on_new([store[3]])
    assert len(only(c.sent)) == 2 and any(m[0] == "ask" and m[2] for m in b.msgs if isinstance(m, tuple))
    async with Session() as s:
        skipped = (await s.execute(select(Delivery).where(Delivery.status == "skipped", Delivery.target_id.in_(mine)))).scalars().first()
    assert await p.publish(skipped.id) and len(only(c.sent)) == 3  # admin "baribir yuborish"

    # matn tahriri
    store[1] = mk(1, "Yangi kitob\nNarxi 35 000\nManzillarimiz\n@hilol", grouped=7, photo_id=11)
    await p.on_edit(store[1])
    edits = only(c.edits)
    assert len(edits) == 2 and "35 000" in edits[0][2] and edits[0][3] is None
    await p.on_edit(store[1])
    assert len(only(c.edits)) == 2  # o'zgarishsiz hodisa e'tiborsiz

    # media almashtirish: albomning izohsiz ikkinchi rasmi
    store[2] = mk(2, "", grouped=7, photo_id=99)
    await p.on_edit(store[2])
    edits = only(c.edits)
    assert len(edits) == 4 and edits[2][3] == "media-99" and edits[2][2] == ""
    # izohli rasm almashganda matn saqlanadi
    store[1] = mk(1, "Yangi kitob\nNarxi 35 000\nManzillarimiz\n@hilol", grouped=7, photo_id=55)
    await p.on_edit(store[1])
    edits = only(c.edits)
    assert len(edits) == 6 and edits[4][3] == "media-55" and "35 000" in edits[4][2] and "Buyurtma" in edits[4][2]

    # tabrik posti: faqat imzo almashadi, to'liq aloqa bloki qo'shilmaydi
    store[4] = mk(4, "Juma muborak!\n\n@hilolnashr", photo_id=40)
    n = len(only(c.sent))
    await p.on_new([store[4]])
    greet = only(c.sent)[n:]
    assert len(greet) == 2
    assert greet[0][1] == "Juma muborak!\n\n@p_Hilol_qoqon_test"  # imzo berilmagan: kanal username'i
    assert "Buyurtma" not in greet[1][1] and "+998" not in greet[1][1] and "@hilolnashr" not in greet[1][1]
    # filialning o'z imzosi berilgan bo'lsa, o'sha qo'yiladi; tahrirda ham saqlanadi
    async with Session() as s:
        t = (await s.execute(select(Target).where(Target.username == "p_Hilol_qoqon_test"))).scalars().first()
        t.signature_html = "@Hilolnashr_qoqon"
        await s.commit()
    store[4] = mk(4, "Juma ayyomi muborak!\n\n@hilolnashr", photo_id=40)
    k = len(only(c.edits))
    await p.on_edit(store[4])
    assert only(c.edits)[k][2] == "Juma ayyomi muborak!\n\n@Hilolnashr_qoqon"
    # imzosiz post: oxiriga filial imzosi qo'shiladi
    store[5] = mk(5, "Ilm olish har bir musulmonga farzdir.")
    n = len(only(c.sent))
    await p.on_new([store[5]])
    assert only(c.sent)[n][1] == "Ilm olish har bir musulmonga farzdir.\n\n@Hilolnashr_qoqon"

    # o'chirish
    await p.on_delete(CHAT, [1, 2])
    deleted = only(c.deleted)
    assert len(deleted) == 2 and len(deleted[0][1]) == 2
    async with Session() as s:
        post = (await s.execute(select(SourcePost).where(SourcePost.chat_id == CHAT))).scalars().first()
        assert post.deleted


def test_full_flow(monkeypatch):
    asyncio.run(scenario(monkeypatch))


def test_migration_adds_missing_column():
    from sqlalchemy import create_engine, inspect, text

    from app.db import _migrate

    eng = create_engine("sqlite://")
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE source_posts (id INTEGER PRIMARY KEY)"))
        conn.execute(text("CREATE TABLE targets (id INTEGER PRIMARY KEY)"))
        conn.execute(text("INSERT INTO targets (id) VALUES (1)"))
        _migrate(conn)
        _migrate(conn)  # ikkinchi marta xatosiz o'tadi
        assert conn.execute(text("SELECT signature_html FROM targets")).scalar() == ""
        assert "block_type" in {c["name"] for c in inspect(conn).get_columns("source_posts")}
        assert "media_ids" in {c["name"] for c in inspect(conn).get_columns("source_posts")}
