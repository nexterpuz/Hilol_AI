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
