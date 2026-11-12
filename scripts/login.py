"""Bir martalik: Telegram akkauntga kirib, sessiya satrini chiqaradi.

Ishlatish:  TG_API_ID=... TG_API_HASH=... python scripts/login.py
Chiqqan satrni .env faylidagi TG_SESSION_STRING ga yozing. Bu satr akkauntga
to'liq kirish beradi — hech kimga bermang va repozitoriyga qo'shmang.
"""
import os

from telethon.sessions import StringSession
from telethon.sync import TelegramClient

with TelegramClient(StringSession(), int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"]) as client:
    print("\nTG_SESSION_STRING=" + client.session.save())
