from types import SimpleNamespace

from telethon.tl.types import MessageEntityBold, MessageEntityCustomEmoji, MessageEntityTextUrl

from app.pipeline import decide
from app.textops import build_post, cut_from_line, fits, numbered_lines, utf16_len

POST = (
    "🎉 Yangi «Xayola» kitobi nashrdan chiqdi\n"
    "\n"
    "👉 Narxi: 39 000 so'm\n"
    "\n"
    "📍 Manzillarimiz\n"
    "Sotib olish uchun:\n"
    "@hilol_reception"
)
TEMPLATE = 'Buyurtma berish uchun\n🤙 +998-90-939-29-27\n📍 Manzil: <a href="https://maps.example/x">Qoʻqon shahri</a>'


def test_numbered_lines():
    assert numbered_lines("a\nb").split("\n") == ["0: a", "1: b"]


def test_cut_removes_contact_block_and_trims_entities():
    cta_offset = utf16_len(POST.split("📍")[0])
    ents = [
        MessageEntityBold(offset=3, length=10),  # sarlavhada — qoladi
        MessageEntityCustomEmoji(offset=0, length=2, document_id=1),  # premium emoji — qoladi
        MessageEntityBold(offset=cta_offset, length=5),  # aloqa blokida — o'chadi
    ]
    text, out = cut_from_line(POST, ents, 4)
    assert text.endswith("39 000 so'm")
    assert "Manzillarimiz" not in text
    assert len(out) == 2
    assert ents[2].offset == cta_offset  # asl entity o'zgarmagan


def test_cut_truncates_overlapping_entity():
    text, out = cut_from_line("abc\ndef", [MessageEntityBold(offset=1, length=6)], 1)
    assert text == "abc" and out[0].length == 2


def test_no_cta_keeps_text():
    text, _ = cut_from_line(POST, [], None)
    assert text == POST


def test_build_post_appends_template_with_correct_offsets():
    text, ents = build_post(POST, [], 4, TEMPLATE)
    assert text.endswith("Qoʻqon shahri")
    assert "hilol_reception" not in text
    assert "\n\nBuyurtma berish uchun\n🤙 +998" in text
    link = next(e for e in ents if isinstance(e, MessageEntityTextUrl))
    # offset UTF-16 birliklarida: emoji 2 birlik egallaydi
    from telethon.helpers import add_surrogate, del_surrogate

    s = add_surrogate(text)
    assert del_surrogate(s[link.offset : link.offset + link.length]) == "Qoʻqon shahri"
    assert link.url == "https://maps.example/x"


def test_fits_limits():
    assert fits("a" * 1024, has_media=True, premium=False)
    assert not fits("a" * 1025, has_media=True, premium=False)
    assert fits("a" * 2048, has_media=True, premium=True)
    assert fits("a" * 4096, has_media=False, premium=False)


def test_decide():
    yes = SimpleNamespace(send=True, confidence=0.95, reason="umumiy yangilik")
    no = SimpleNamespace(send=False, confidence=0.95, reason="faqat Toshkent")
    unsure = SimpleNamespace(send=True, confidence=0.5, reason="aniq emas")
    assert decide("auto", no, 0.8)[0] == "send"
    assert decide("manual", yes, 0.8)[0] == "ask"
    assert decide("smart", yes, 0.8)[0] == "send"
    assert decide("smart", no, 0.8)[0] == "skip"
    assert decide("smart", unsure, 0.8)[0] == "ask"
    assert decide("smart", None, 0.8)[0] == "ask"


def test_message_link_and_premium_emoji_template():
    from telethon.helpers import add_surrogate

    from app.textops import message_to_template, parse_message_link

    assert parse_message_link("https://t.me/Hilol_qoqon_test/15") == ("Hilol_qoqon_test", 15)
    assert parse_message_link("https://t.me/c/1234567890/42") == (-1001234567890, 42)
    assert parse_message_link("t.me/Hilol_qoqon_test/15?single") == ("Hilol_qoqon_test", 15)
    assert parse_message_link("https://t.me/Hilol_qoqon_test") is None

    text = "🤙 +998-90\n📍 Manzil"
    ents = [
        MessageEntityCustomEmoji(offset=0, length=2, document_id=111),
        MessageEntityCustomEmoji(offset=len(add_surrogate("🤙 +998-90\n")), length=2, document_id=222),
        MessageEntityTextUrl(offset=len(add_surrogate("🤙 +998-90\n📍 ")), length=6, url="https://maps.example/x"),
    ]
    tpl = message_to_template(text, ents)
    assert 'emoji-id="111"' in tpl and 'emoji-id="222"' in tpl and "maps.example" in tpl
    # shablon postga qo'shilganda premium emoji entity sifatida qaytadi, offseti to'g'ri
    out, out_ents = build_post("Kitob 📚", [], None, tpl)
    custom = [e for e in out_ents if isinstance(e, MessageEntityCustomEmoji)]
    assert [e.document_id for e in custom] == [111, 222]
    s = add_surrogate(out)
    assert s[custom[0].offset : custom[0].offset + custom[0].length] == add_surrogate("🤙")
    assert s[custom[1].offset : custom[1].offset + custom[1].length] == add_surrogate("📍")
    assert out.startswith("Kitob 📚\n\n🤙 +998-90\n📍 Manzil")


EDITED = (
    "✨ Шайх ҳазратлари вақтни қандай тақсимлаганлар?\n\n"
    "📚 Тўпламни «Hilol-Nashr» нашриётининг дўконларидан сотиб олишингиз ёки hilol.uz сайтимиздан буюртма беришингиз мумкин. 😊 \n\n"
    "👉 Нархи: 798 000 сўм\n\n📍 Манзилларимиз\n\n✅ Сайт орқали буюртма бериш\n\nСотиб олиш учун:\n✍️ @hilol_reception\n📞 +998712175999\n\n"
    "🎉 17 ноябрь куни фақат бир кун «Тафсири Ҳилол» тўпламини 10% чегирма билан сотиб олишингиз мумкин.\n\n@hilolnashr"
)


def test_note_after_contact_block_is_kept():
    """Haqiqiy tahrirlangan post: aloqa blokidan keyin qo'shilgan chegirma qatori saqlanadi, bosh kanal bloki kesiladi."""
    from telethon.helpers import add_surrogate

    from app.rules import find_block

    kind, start = find_block(EDITED)
    assert (kind, EDITED.split("\n")[start]) == ("contact", "📍 Манзилларимиз")
    note = "🎉 17 ноябрь куни фақат бир кун «Тафсири Ҳилол» тўпламини 10% чегирма билан сотиб олишингиз мумкин."
    off = len(add_surrogate(EDITED[: EDITED.index(note)]))
    ents = [MessageEntityBold(offset=off + 2, length=10), MessageEntityBold(offset=0, length=4)]
    out, oe = build_post(EDITED, ents, start, "Buyurtma berish uchun")
    assert "Манзилларимиз" not in out and "@hilol_reception" not in out and "+998712175999" not in out and "@hilolnashr" not in out
    assert out.endswith("👉 Нархи: 798 000 сўм\n\n" + note + "\n\nBuyurtma berish uchun")
    s = add_surrogate(out)
    bold = sorted(oe, key=lambda e: e.offset)
    assert s[bold[1].offset: bold[1].offset + bold[1].length] == add_surrogate(note)[2:12]  # formatlash siljimadi
    assert bold[0].offset == 0


def test_remove_lines_entities():
    from app.textops import remove_lines

    text, ents = remove_lines("aa\nbb\ncc\ndd", [MessageEntityBold(offset=0, length=2), MessageEntityBold(offset=3, length=2),
                                                   MessageEntityBold(offset=6, length=5), MessageEntityBold(offset=9, length=2)], 1, 3)
    assert text == "aa\ndd"
    spans = sorted((e.offset, e.length) for e in ents)
    assert spans == [(0, 2), (3, 2), (3, 2)]  # bb o'chdi; "cc\ndd" ning faqat "dd" qismi qoldi


def test_to_latin_uzbek():
    from app.textops import to_latin

    cases = {
        "Шайх ҳазратлари вақтни қандай тақсимлаганлар?": "Shayx hazratlari vaqtni qanday taqsimlaganlar?",
        "Нархи: 798 000 сўм": "Narxi: 798 000 soʻm",
        "Ёзилган китоб": "Yozilgan kitob",
        "Ер юзи, мевалар, ҳаёт": "Yer yuzi, mevalar, hayot",
        "Қўқондаги дўконимиз": "Qoʻqondagi doʻkonimiz",
        "Ўзбекистон бўйлаб етказиб берилади": "Oʻzbekiston boʻylab yetkazib beriladi",
        "маълумот, Ғайрат": "maʼlumot, Gʻayrat",
        "ДЎКОН ВА ШАЙХ": "DOʻKON VA SHAYX",
        "Цирк ва федерация": "Sirk va federatsiya",
        "Already latin: Qoʻqon @hilol_reception": "Already latin: Qoʻqon @hilol_reception",
    }
    for cyr, lat in cases.items():
        assert to_latin(cyr, [], "old")[0] == lat, (cyr, to_latin(cyr, [], "old")[0])


def test_to_latin_new_alphabet():
    """Takomillashtirilgan alifbo: oʻ→ö, gʻ→ğ, sh→ş, ch→ç."""
    from app.textops import to_latin

    cases = {
        "Шайх ҳазратлари": "Şayx hazratlari",
        "Қўқондаги дўконимиз": "Qöqondagi dökonimiz",
        "Ўзбекистон бўйлаб": "Özbekiston böylab",
        "жамғармаси, Ғайрат": "jamğarmasi, Ğayrat",
        "кўрсатиш, чегирма, Чиқди": "körsatiş, çegirma, Çiqdi",
        "ДЎКОН ВА ШАЙХ": "DÖKON VA ŞAYX",
        "Ер юзи, маълумот": "Yer yuzi, maʼlumot",
    }
    for cyr, lat in cases.items():
        assert to_latin(cyr, [])[0] == lat, (cyr, to_latin(cyr, [])[0])


def test_to_latin_keeps_entities():
    from telethon.helpers import add_surrogate

    from app.textops import to_latin

    text = "👉 Нархи: 78 000 сўм\nСайт: https://hilol.uz/шоп"
    s = add_surrogate(text)
    bold = MessageEntityBold(offset=s.index("Нархи"), length=5)
    from telethon.tl.types import MessageEntityUrl

    url = MessageEntityUrl(offset=s.index("https"), length=len(s) - s.index("https"))
    out, ents = to_latin(text, [bold, url], "old")
    o = add_surrogate(out)
    b, u = sorted(ents, key=lambda e: e.offset)
    assert out.startswith("👉 Narxi: 78 000 soʻm\nSayt: ")
    assert to_latin(text, [bold, url])[0].startswith("👉 Narxi: 78 000 söm\nSayt: ")
    assert o[b.offset:b.offset + b.length] == "Narxi"
    assert o[u.offset:u.offset + u.length] == "https://hilol.uz/шоп"  # havolaga tegilmaydi
