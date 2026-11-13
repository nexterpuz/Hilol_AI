"""Butun oqim soxta Telegram va soxta AI bilan: albom, istisno, tahrir, media almashtirish, o'chirish."""
import asyncio
import json
from types import SimpleNamespace as NS

from sqlalchemy import select

from app import ai, pipeline, rules as fallback
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


async def fake_analyze(text, targets, rules, examples=None):
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
        from sqlalchemy import delete

        from app.db import Setting

        await s.execute(delete(Setting))  # boshqa testlar qoldirgan sozlamalar ta'sir qilmasin
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

    # xotira: admin "baribir yuborish" degan postga o'xshashi qayta so'ralmaydi
    async with Session() as s:
        from app.db import Decision

        before = len(list((await s.execute(select(Decision))).scalars()))
    store[7] = mk(7, "Toshkent filiali ertaga ishlamaydi")
    await p.on_new([store[7]])
    async with Session() as s:
        asked = (await s.execute(select(Delivery).where(Delivery.target_id.in_(mine), Delivery.status == "skipped").order_by(Delivery.id.desc()))).scalars().first()
    n, q = len(only(c.sent)), len(b.msgs)
    assert await p.approve(asked.id) and len(only(c.sent)) == n + 1
    store[8] = mk(8, "Toshkent filiali ertaga ishlamaydi!")  # deyarli bir xil matn
    await p.on_new([store[8]])
    async with Session() as s:
        last = list((await s.execute(select(Delivery).where(Delivery.target_id == asked.target_id).order_by(Delivery.id.desc()).limit(1))).scalars())[0]
        assert len(list((await s.execute(select(Decision))).scalars())) == before + 1
    assert last.status == "sent" and last.reason == "Avvalgi qaroringiz bo'yicha"
    # rad etilgan qaror ham eslab qolinadi: o'xshash post jim o'tkazib yuboriladi
    other = next(i for i in mine if i != asked.target_id)
    async with Session() as s:
        d8 = (await s.execute(select(Delivery).where(Delivery.target_id == other).order_by(Delivery.id.desc()))).scalars().first()
    await p.reject(d8.id)
    q = len(b.msgs)
    store[9] = mk(9, "Toshkent filiali ertaga ishlamaydi")
    await p.on_new([store[9]])
    async with Session() as s:
        d9 = (await s.execute(select(Delivery).where(Delivery.target_id == other).order_by(Delivery.id.desc()))).scalars().first()
    assert d9.status == "skipped" and d9.reason == "Avvalgi qaroringiz bo'yicha"
    assert ("ask", d9.id, True) not in b.msgs  # bu kanal uchun so'ralmadi

    # AI ishlamasa: zaxira qoidalar ishlaydi, qolipga mos post avtomatik chiqadi
    async def broken(text, targets, rules, examples=None):
        ai.last_error = "Anthropic hisobida mablag' tugagan."
        return None

    monkeypatch.setattr(ai, "analyze", broken)
    store[6] = mk(6, "Yangi «Sabr» kitobi\nNarxi: 45 000 so'm\n\n📍 Манзилларимиз\nСотиб олиш учун:\n+998712175999\n@hilolnashr", photo_id=60)
    n = len(only(c.sent))
    await p.on_new([store[6]])
    out = only(c.sent)[n:]
    assert len(out) == 2 and out[0][1].startswith("Yangi «Sabr» kitobi\nNarxi: 45 000 so'm\n\nBuyurtma berish uchun")
    assert "Манзилларимиз" not in out[0][1] and "@hilolnashr" not in out[0][1]
    async with Session() as s:
        from app.db import Event

        d = (await s.execute(select(Delivery).where(Delivery.target_id.in_(mine)).order_by(Delivery.id.desc()))).scalars().first()
        assert d.status == "sent" and d.reason.startswith("AI'siz qoidalar")
        ev = (await s.execute(select(Event).where(Event.kind == "ai_failed"))).scalars().first()
        assert ev is not None and "mablag' tugagan" in ev.detail  # tafsilot faqat jurnalda
    # AI'siz, qolipga mos kelmagan uzun post: so'raladi
    store[10] = mk(10, "Bugun nashriyotimizda katta yangilik. " * 12)
    n = len(only(c.sent))
    await p.on_new([store[10]])
    assert len(only(c.sent)) == n
    monkeypatch.setattr(ai, "analyze", fake_analyze)

    # tasdiq talab qiladigan so'z: qolipga mos bo'lsa ham so'raladi
    store[11] = mk(11, "Jamoamizga taklif qilamiz\nАриза юбориш учун профилга ёзинг\n\n@hilolnashr")
    n = len(only(c.sent))
    await p.on_new([store[11]])
    assert len(only(c.sent)) == n
    async with Session() as s:
        d11 = (await s.execute(select(Delivery).where(Delivery.target_id.in_(mine)).order_by(Delivery.id.desc()))).scalars().first()
    assert d11.status == "pending" and "ариза юбориш" in d11.reason

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


def test_rules_find_block():
    assert fallback.find_block("✨ Жума муборак!\n\n@hilolnashr") == ("signature", 2)
    sale = "📕 Yangi kitob\n\n👉 Нархи: 39 000 сўм\n\n📍 Манзилларимиз\n\n✅ Сайт орқали буюртма бериш\n\nСотиб олиш учун:\n@hilol_reception\n+998712175999\n\n✅ «Hilol» иловаси орқали сотиб олсангиз, ҳар сафар 1% бонусга эга бўласиз.\n\n@hilolnashr"
    assert fallback.find_block(sale) == ("contact", 4)
    assert fallback.find_block("Ilm olish farzdir.") == ("none", None)
    assert fallback.find_block("@hilolnashr") == ("none", None)  # birinchi qator har doim mazmun
    # mazmun ichidagi "manzil" so'zi blok boshlanishi emas
    assert fallback.find_block("Kitobning manzili — qalb.\nDavomi bor.\n\n@hilolnashr") == ("signature", 3)


def test_rules_relevance():
    from types import SimpleNamespace as T

    targets = [T(id=1, keywords="Qo'qon"), T(id=2, keywords="Marg'ilon, Margilon")]
    general = fallback.analyze("Yangi kitob chiqdi\n\n@hilolnashr", targets)
    assert all(d.send and d.confidence >= 0.8 for d in general.decisions)
    local = fallback.analyze("Qo'qon filialimiz bugun ishlamaydi\n\n@hilolnashr", targets)
    assert local.for_target(1).send and local.for_target(1).confidence >= 0.8
    assert not local.for_target(2).send and local.for_target(2).confidence < 0.8  # admindan so'raladi


def test_rules_real_post_shapes():
    """Haqiqiy kanaldagi post qoliplari: filialga xos e'lon va bosh do'kon tadbiri."""
    from types import SimpleNamespace as T

    targets = [T(id=1, name="Qo'qon filiali", keywords="Qo'qon"), T(id=2, name="Marg'ilon", keywords=""), T(id=3, name="Namangan do'koni", keywords="")]
    closed = "✨ Бугун ва эртага Намангандаги дўконимиз ишламайди\n\n⏺ Бошқа дўконларимиз одатдаги тартибда ишлайди.\n\n📍 Манзилларимиз\n\n@hilolnashr"
    a = fallback.analyze(closed, targets)
    assert a.for_target(3).send and a.for_target(3).confidence >= 0.8  # Namangan kanaliga chiqadi
    assert not a.for_target(1).send and a.for_target(1).confidence < 0.8  # qolganlarida so'raladi
    assert not a.for_target(2).send
    # kirillcha yozilgan shahar lotincha nomlangan kanalga mos keladi
    b = fallback.analyze("Қўқондаги дўконимиз янги манзилга кўчди\n\n@hilolnashr", targets)
    assert b.for_target(1).send and not b.for_target(2).send
    # bosh do'kondagi tadbir: hech bir filialga avtomatik chiqmaydi
    event = "Марҳамат, навбатдаги илм мажлисига ташриф буюринг! 😊\n\n📆 17 ноябрь 14:00\n\n📍 «Hilol-Nashr» бош дўкони\n\n@hilolnashr"
    c = fallback.analyze(event, targets)
    assert all(not d.send and d.confidence < 0.8 for d in c.decisions)
    # kitob tavsifidagi shahar nomi va umumiy "do'konlarimizdan" so'zi — oddiy sotuv posti
    sale = "🔥 Имом Бухорий ҳазратларининг ҳаёти ҳақидаги асар. Муаллиф Тошкентда яшаган.\n📚 Китобни дўконларимиздан сотиб олинг.\n👉 Нархи: 43 000 сўм\n\n📍 Манзилларимиз\n📞 +998712175999\n@hilolnashr"
    d = fallback.analyze(sale, targets)
    assert d.block_type == "contact" and all(x.send and x.confidence >= 0.8 for x in d.decisions)


def test_ask_words():
    from app.pipeline import DEFAULT_ASK_WORDS, ask_word_in

    assert ask_word_in("📩Ариза юбориш учун:\n@hilolnashrhr", DEFAULT_ASK_WORDS) == "ариза юбориш"
    assert ask_word_in("Жума муборак!", DEFAULT_ASK_WORDS) == ""
    assert ask_word_in("Вакансия", "") == ""
