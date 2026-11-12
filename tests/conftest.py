import os
import tempfile

# app modullari import qilinishidan oldin sinov sozlamalari
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///" + os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["ADMIN_USER"] = "admin"
os.environ["ADMIN_PASSWORD"] = "sinov-parol"
