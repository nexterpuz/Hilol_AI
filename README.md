# Hilol AI — repost agent

Bosh Telegram kanaldagi postlarni filial kanallariga ko'chiradi: mediani qayta
yuklamasdan oladi, bosh kanalning aloqa blokini kesib, o'rniga filialning o'z
shablonini qo'yadi. Manbada post tahrirlansa yoki o'chirilsa, nusxalar ham
yangilanadi yoki o'chiriladi.

## Tuzilishi

| Fayl | Vazifasi |
|---|---|
| `app/main.py` | Ishga tushirish, Telegram hodisalarini tinglash |
| `app/pipeline.py` | Yangi post, tahrir, o'chirish oqimi; rejimlar (auto / smart / manual) |
| `app/ai.py` | Claude: aloqa bloki qayerda, post qaysi filialga mos |
| `app/textops.py` | Matnni kesish va shablon qo'shish (formatlash saqlanadi) |
| `app/adminbot.py` | Adminga hisobot va tasdiq tugmalari |
| `app/stats.py` | Obunachilar va ko'rishlar sonini soatiga bir marta yozib borish |
| `app/db.py` | Baza jadvallari |
| `seed.json` | Manba kanal, filial kanallari, shablonlar, qoidalar (birinchi ishga tushirish uchun) |

## Talablar

- Agent akkaunti manba kanalga obuna bo'lgan va barcha filial kanallarida admin
  (post joylash, tahrirlash, o'chirish huquqi bilan).
- Admin tasdiq botiga bir marta `/start` bosgan.

## Ishga tushirish

1. `.env.example` ni `.env` ga nusxalab to'ldiring.
2. Sessiya satrini oling: `docker compose run --rm agent python scripts/login.py` — chiqqan satrni `.env` dagi `TG_SESSION_STRING` ga yozing.
3. `docker compose up -d --build`
4. Jurnal: `docker compose logs -f agent`

Lokal sinov (Docker'siz, SQLite bilan): `pip install -r requirements.txt && python -m app.main`

Testlar: `pip install pytest && python -m pytest tests`

## Hozircha yo'q

- Veb admin panel (kanallar hozircha `seed.json` va baza orqali boshqariladi).
- Manbada media almashtirilgan tahrirni sinxronlash (faqat matn tahriri sinxronlanadi).
- Instagram va boshqa platformalar.
