# Hilol AI — repost agent

Bosh Telegram kanaldagi postlarni filial kanallariga ko'chiradi: mediani qayta
yuklamasdan oladi, bosh kanalning aloqa blokini kesib, o'rniga filialning o'z
shablonini qo'yadi. Manbada post tahrirlansa yoki o'chirilsa, nusxalar ham
yangilanadi yoki o'chiriladi.

## Tuzilishi

| Fayl | Vazifasi |
|---|---|
| `app/main.py` | Ishga tushirish |
| `app/agent.py` | Telegram akkaunt holati, hodisalarni tinglash, panel orqali kirish va almashtirish |
| `app/pipeline.py` | Yangi post, tahrir, o'chirish oqimi; rejimlar (auto / smart / manual) |
| `app/ai.py` | Claude: aloqa bloki qayerda, post qaysi filialga mos |
| `app/textops.py` | Matnni kesish va shablon qo'shish (formatlash saqlanadi) |
| `app/adminbot.py` | Adminga hisobot va tasdiq tugmalari |
| `app/stats.py` | Obunachilar va ko'rishlar sonini soatiga bir marta yozib borish |
| `app/web.py`, `app/templates/` | Veb admin panel: dashboard, kanallar, tasdiq navbati, AI qoidalari |
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

## Railway'da ishga tushirish

1. Railway loyihasiga shu repozitoriyni ulang (Dockerfile avtomatik ishlatiladi).
2. Loyihaga **PostgreSQL** qo'shing va xizmatning Variables bo'limida
   `DATABASE_URL = ${{Postgres.DATABASE_URL}}` deb yozing.
3. Variables: `TG_API_ID`, `TG_API_HASH`, `TG_BOT_TOKEN`, `ADMIN_IDS`,
   `ANTHROPIC_API_KEY`, `ADMIN_USER`, `ADMIN_PASSWORD`, `SECRET_KEY`.
4. Settings → Networking → **Generate Domain**. Panel shu HTTPS manzilda ochiladi.
5. Panelda **Telegram akkaunt** sahifasida telefon raqam, kod va (bo'lsa) ikki
   bosqichli parolni kiriting. `TG_SESSION_STRING` va `scripts/login.py` kerak emas.

`SECRET_KEY` ni keyin o'zgartirmang: saqlangan Telegram sessiyasi shu kalit bilan shifrlangan.

## Admin panel

Panel xavfsizlik uchun faqat serverning ichki manzilida (`127.0.0.1:8080`) ochiladi.
O'z kompyuteringizdan kirish uchun SSH tunnel oching va brauzerda
`http://localhost:8080` ga kiring:

    ssh -L 8080:127.0.0.1:8080 root@SERVER_IP

Login va parol `.env` dagi `ADMIN_USER` va `ADMIN_PASSWORD`. Domen ulanganda panelni
HTTPS orqali ochiq manzilga chiqarish mumkin (`COOKIE_SECURE=1` bilan).

## Hozircha yo'q

- Manbada media almashtirilgan tahrirni sinxronlash (faqat matn tahriri sinxronlanadi).
- Instagram va boshqa platformalar.
