import os
import tempfile

# app modullari import qilinishidan oldin sinov sozlamalari
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///" + os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["ADMIN_USER"] = "admin"
os.environ["ADMIN_PASSWORD"] = "sinov-parol"
os.environ["TG_API_ID"] = "1"
os.environ["TG_API_HASH"] = "a"
os.environ["ADMIN_IDS"] = "777"
