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

    async def ask_branch(self, delivery_id, target, text, local=False):
        self.msgs.append(("branch", delivery_id, target.username))
        if local:
            self.msgs.append(("local", delivery_id, target.username))


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
        qoqon_id_early = t.id
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
    # filialga xos e'lon: o'sha filial kanaliga o'zi chiqadi, boshqalarga chiqmaydi va so'ralmaydi
    store[20] = mk(20, "Марғилондаги дўконимиз бугун ва эртага ишламайди\n\n@hilolnashr")
    n, q = len(only(c.sent)), len(b.msgs)
    await p.on_new([store[20]])
    async with Session() as s:
        ds = {d.target_id: d for d in (await s.execute(select(Delivery).where(Delivery.target_id.in_(mine)).order_by(Delivery.id.desc()).limit(2))).scalars()}
        names = {t.id: t.username for t in (await s.execute(select(Target).where(Target.id.in_(mine)))).scalars()}
    marg = next(d for tid, d in ds.items() if "margilon" in names[tid])
    qoq = next(d for tid, d in ds.items() if "qoqon" in names[tid])
    out = only(c.sent)[n:]
    assert len(out) == 1 and out[0][0] == "p_Hilol_margilon_test" and marg.status == "sent"
    assert qoq.status == "skipped" and qoq.reason == "Post boshqa filialga tegishli"
    assert not any(isinstance(m, tuple) and m[1] in (qoq.id, marg.id) for m in b.msgs[q:])  # hech kimdan so'ralmadi

    # AI'siz, qolipga mos kelmagan uzun post: so'raladi
    store[10] = mk(10, "Bugun nashriyotimizda katta yangilik. " * 12)
    n = len(only(c.sent))
    await p.on_new([store[10]])
    assert len(only(c.sent)) == n
    monkeypatch.setattr(ai, "analyze", fake_analyze)

    # navbatda qo'lda tuzatish: admin kesish joyi va yakun turini o'zgartiradi
    store[17] = mk(17, "Diqqat, e'lon\nTafsilot birinchi\nTafsilot ikkinchi\nАриза юбориш\n\n@hilolnashr")
    await p.on_new([store[17]])
    async with Session() as s:
        d17 = (await s.execute(select(Delivery).where(Delivery.target_id == qoqon_id_early).order_by(Delivery.id.desc()))).scalars().first()
        post17, t17 = await s.get(SourcePost, d17.post_id), await s.get(Target, d17.target_id)
    assert d17.status == "pending"
    assert pipeline.preview_text(post17, t17, d17) == "Diqqat, e'lon\nTafsilot birinchi\nTafsilot ikkinchi\nАриза юбориш\n\n@Hilolnashr_qoqon"
    await p.adjust(d17.id, 2, "none")
    async with Session() as s:
        d17 = await s.get(Delivery, d17.id)
    assert pipeline.preview_text(post17, t17, d17) == "Diqqat, e'lon\nTafsilot birinchi"
    n = len(only(c.sent))
    assert await p.publish(d17.id)
    assert only(c.sent)[n][1] == "Diqqat, e'lon\nTafsilot birinchi"
    await p.adjust(d17.id, -1, "contact")  # yuborilgan postga ta'sir qilmaydi
    async with Session() as s:
        assert (await s.get(Delivery, d17.id)).cut_line == 2

    # tasdiq talab qiladigan so'z: qolipga mos bo'lsa ham so'raladi
    store[11] = mk(11, "Jamoamizga taklif qilamiz\nАриза юбориш учун профилга ёзинг\n\n@hilolnashr")
    n = len(only(c.sent))
    await p.on_new([store[11]])
    assert len(only(c.sent)) == n
    async with Session() as s:
        d11 = (await s.execute(select(Delivery).where(Delivery.target_id.in_(mine)).order_by(Delivery.id.desc()))).scalars().first()
    assert d11.status == "pending" and "ариза юбориш" in d11.reason

    # filial tasdig'i: faqat yangi nashr / "tez kunda" posti menejerga boradi
    async with Session() as s:
        t = (await s.execute(select(Target).where(Target.username == "p_Hilol_qoqon_test"))).scalars().first()
        t.branch_approval = True
        await s.commit()
        qoqon_id = t.id
    # oddiy sotuv posti filial tasdig'i yoqilgan kanalga ham kutmasdan chiqadi
    store[16] = mk(16, "«Odob» kitobi sotuvda\nNarxi 30 000\nManzillarimiz\n@hilol", photo_id=71)
    n = len(only(c.sent))
    await p.on_new([store[16]])
    assert len(only(c.sent)) == n + 2
    store[12] = mk(12, "Yangi «Ilm» kitobi nashrdan chiqdi\nNarxi 50 000\nManzillarimiz\n@hilol", photo_id=70)
    n = len(only(c.sent))
    await p.on_new([store[12]])
    assert len(only(c.sent)) == n + 1  # faqat Marg'ilon; Qo'qon menejer tasdig'ini kutadi
    async with Session() as s:
        d12 = (await s.execute(select(Delivery).where(Delivery.target_id == qoqon_id).order_by(Delivery.id.desc()))).scalars().first()
    assert d12.status == "pending" and d12.reason == "Filial tasdig'i kutilmoqda"
    assert ("branch", d12.id, "p_Hilol_qoqon_test") in b.msgs
    assert await p.publish(d12.id) and len(only(c.sent)) == n + 2  # menejer "Kanalga joylash" ni bosdi
    store[13] = mk(13, "Ramazon muborak!\n\n@hilolnashr")
    n = len(only(c.sent))
    await p.on_new([store[13]])
    assert len(only(c.sent)) == n + 2  # tabrik posti tasdiqsiz ikkala filialga

    # postni qaytarib olish: filiallardan o'chadi
    async with Session() as s:
        post12 = d12.post_id
    k = len(only(c.deleted))
    assert await p.recall(post12) >= 2 and len(only(c.deleted)) == k + 2
    async with Session() as s:
        left = list((await s.execute(select(Delivery).where(Delivery.post_id == post12, Delivery.status == "sent"))).scalars())
    assert not left and await p.recall(post12) == 0

    # favqulodda to'xtatish: yangi post chiqmaydi, navbatga tushadi, so'ralmaydi
    await pipeline.set_paused(True)
    store[14] = mk(14, "Shanba muborak!\n\n@hilolnashr")
    n, q = len(only(c.sent)), len(b.msgs)
    await p.on_new([store[14]])
    async with Session() as s:
        d14 = (await s.execute(select(Delivery).where(Delivery.target_id == qoqon_id).order_by(Delivery.id.desc()))).scalars().first()
    assert len(only(c.sent)) == n and d14.status == "pending" and d14.reason == "Favqulodda to'xtatish yoqilgan"
    assert not any(isinstance(m, tuple) and m[1] == d14.id for m in b.msgs[q:])
    await pipeline.set_paused(False)
    assert not await pipeline.is_paused()
    store[15] = mk(15, "Yakshanba muborak!\n\n@hilolnashr")
    await p.on_new([store[15]])
    assert len(only(c.sent)) == n + 2

    # o'chirish
    k = len(only(c.deleted))
    await p.on_delete(CHAT, [1, 2])
    deleted = only(c.deleted)[k:]
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
    assert local.for_target(1).send and local.for_target(1).scope == "own"
    assert not local.for_target(2).send and local.for_target(2).scope == "other"


def test_rules_real_post_shapes():
    """Haqiqiy kanaldagi post qoliplari: filialga xos e'lon va bosh do'kon tadbiri."""
    from types import SimpleNamespace as T

    targets = [T(id=1, name="Qo'qon filiali", keywords="Qo'qon"), T(id=2, name="Marg'ilon", keywords=""), T(id=3, name="Namangan do'koni", keywords="")]
    closed = "✨ Бугун ва эртага Намангандаги дўконимиз ишламайди\n\n⏺ Бошқа дўконларимиз одатдаги тартибда ишлайди.\n\n📍 Манзилларимиз\n\n@hilolnashr"
    a = fallback.analyze(closed, targets)
    assert a.for_target(3).send and a.for_target(3).scope == "own"  # Namangan menejeridan so'raladi
    assert not a.for_target(1).send and a.for_target(1).scope == "other"  # qolganlariga chiqmaydi, so'ralmaydi
    assert not a.for_target(2).send and a.for_target(2).scope == "other"
    # do'kon so'zisiz, ish tartibi so'zi bilan ham taniydi
    w = fallback.analyze("Марғилонда бугун ва эртага ишламаймиз\n\n@hilolnashr", targets)
    assert w.for_target(2).scope == "own" and w.for_target(1).scope == "other"
    # kirillcha yozilgan shahar lotincha nomlangan kanalga mos keladi
    b = fallback.analyze("Қўқондаги дўконимиз янги манзилга кўчди\n\n@hilolnashr", targets)
    assert b.for_target(1).scope == "own" and b.for_target(2).scope == "other"
    # bosh do'kondagi tadbir: hech bir filialga avtomatik chiqmaydi
    event = "Марҳамат, навбатдаги илм мажлисига ташриф буюринг! 😊\n\n📆 17 ноябрь 14:00\n\n📍 «Hilol-Nashr» бош дўкони\n\n@hilolnashr"
    c = fallback.analyze(event, targets)
    assert all(not d.send and d.confidence < 0.8 and d.scope == "general" for d in c.decisions)
    # kitob tavsifidagi shahar nomi va umumiy "do'konlarimizdan" so'zi — oddiy sotuv posti
    sale = "🔥 Имом Бухорий ҳазратларининг ҳаёти ҳақидаги асар. Муаллиф Тошкентда яшаган.\n📚 Китобни дўконларимиздан сотиб олинг.\n👉 Нархи: 43 000 сўм\n\n📍 Манзилларимиз\n📞 +998712175999\n@hilolnashr"
    d = fallback.analyze(sale, targets)
    assert d.block_type == "contact" and all(x.send and x.confidence >= 0.8 and x.scope == "general" for x in d.decisions)


def test_ask_words():
    from app.pipeline import DEFAULT_ASK_WORDS, ask_word_in

    assert ask_word_in("📩Ариза юбориш учун:\n@hilolnashrhr", DEFAULT_ASK_WORDS) == "ариза юбориш"
    assert ask_word_in("Жума муборак!", DEFAULT_ASK_WORDS) == ""
    assert ask_word_in("Вакансия", "") == ""


def test_phone_keys():
    from app.pipeline import phone_key, phone_keys

    assert phone_key("+998 90 939-29-27") == phone_key("998909392927") == phone_key("909392927") == "909392927"
    assert phone_key("12345") == ""
    assert phone_keys("+998 90 939 29 27, 91-765-43-21\n") == {"909392927"} | ({"917654321"} if phone_key("91-765-43-21") else set())


def test_bot_manager_binding_and_permissions():
    """Menejer faqat o'z raqamini ulashib bog'lanadi va faqat o'z filiali postini tasdiqlay oladi."""
    from types import SimpleNamespace as NS2

    from app.adminbot import AdminBot

    class Ev:
        def __init__(self, sender, contact=None, text="", data=b""):
            self.sender_id, self.raw_text, self.data = sender, text, data
            self.message = NS2(contact=contact, message="Yangi post")
            self.out, self.answers = [], []

        async def respond(self, text, buttons=None):
            self.out.append(text)

        async def answer(self, text, alert=False):
            self.answers.append(text)

        async def get_message(self):
            return self.message

        async def edit(self, text, **k):
            self.out.append(text)

    class Pipe:
        def __init__(self):
            self.calls = []

        async def publish(self, i):
            self.calls.append(("publish", i))
            return True

        async def approve(self, i):
            self.calls.append(("approve", i))
            return True

        async def reject(self, i, remember=True):
            self.calls.append(("reject", i, remember))

    async def run():
        await init_db()
        async with Session() as s:
            a = Target(name="Bog' A", username="bind_a", manager_phones="+998 90 111 22 33")
            b = Target(name="Bog' B", username="bind_b", manager_phones="+998 91 444 55 66")
            s.add_all([a, b])
            post = SourcePost(chat_id=-9, msg_ids=[1], text="Yangi kitob")
            s.add(post)
            await s.flush()
            da = Delivery(post_id=post.id, target_id=a.id, status="pending")
            dbb = Delivery(post_id=post.id, target_id=b.id, status="pending")
            s.add_all([da, dbb])
            await s.commit()
            ids = (a.id, da.id, dbb.id)
        bot = AdminBot()
        sent = []

        async def fake_send(user_ids, text, buttons=None):
            sent.append((list(user_ids), text))
            return len(list(user_ids))

        bot.send = fake_send
        bot.pipeline = Pipe()
        # begona odamning raqamini ulashish rad etiladi
        e1 = Ev(500, contact=NS2(user_id=999, phone_number="+998901112233"))
        await bot._bind(e1)
        # ro'yxatda yo'q raqam
        e2 = Ev(501, contact=NS2(user_id=501, phone_number="+998930000000"))
        await bot._bind(e2)
        # to'g'ri raqam: A filialiga bog'lanadi va kutayotgan posti keladi
        e3 = Ev(502, contact=NS2(user_id=502, phone_number="998901112233"))
        await bot._bind(e3)
        async with Session() as s:
            bound = (await s.get(Target, ids[0])).manager_ids
        # o'z filiali postini joylaydi (xotirasiz), begona filialnikini — yo'q
        ok = Ev(502, data=f"ok:{ids[1]}".encode())
        await bot._on_click(ok)
        foreign = Ev(502, data=f"ok:{ids[2]}".encode())
        await bot._on_click(foreign)
        admin = Ev(777, data=f"no:{ids[2]}".encode())
        await bot._on_click(admin)
        return e1.out, e2.out, e3.out, bound, bot.pipeline.calls, foreign.answers, sent, ids

    e1, e2, e3, bound, calls, foreign, sent, ids = asyncio.run(run())
    assert "o'z raqamingizni" in e1[0] and "biriktirilmagan" in e2[0] and "Bog' A" in e3[0]
    assert bound == {"502": "901112233"}
    assert calls == [("publish", ids[1]), ("reject", ids[2], True)]
    assert foreign == ["Ruxsat yo'q"]
    assert any(u == [502] and "Kutmoqda" in t for u, t in sent)  # bog'langach kutayotgan post yuborildi


def test_branch_words_default():
    from app.pipeline import DEFAULT_BRANCH_WORDS, ask_word_in

    assert ask_word_in("🎉 «Таъвилот ал-Қуръон» китобининг 17–18-жузи нашр қилинди", DEFAULT_BRANCH_WORDS)
    assert ask_word_in("Тез кунда барча филиалларимизда сотувда бўлади", DEFAULT_BRANCH_WORDS)
    assert not ask_word_in("✨ «Кифоя» – фиқҳ илмини ўрганмоқчи бўлганлар учун\n👉 Нархи: 345 000 сўм", DEFAULT_BRANCH_WORDS)
