"""Ma'lumotlar bazasi modellari."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .config import settings


def now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Account(Base):
    """Agent ishlaydigan Telegram akkaunt. Sessiya shifrlangan holda saqlanadi."""

    __tablename__ = "accounts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    phone: Mapped[str] = mapped_column(String(32), default="")
    session_enc: Mapped[str] = mapped_column(Text)
    premium: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class Setting(Base):
    """Paneldan o'zgartiriladigan umumiy sozlamalar (kalit -> qiymat)."""

    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class Target(Base):
    """Qabul qiluvchi (filial) kanal."""

    __tablename__ = "targets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    username: Mapped[str] = mapped_column(String(100), unique=True)
    mode: Mapped[str] = mapped_column(String(10), default="smart")  # auto | smart | manual
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    template_html: Mapped[str] = mapped_column(Text, default="")
    # Sotuvga oid bo'lmagan postlar (tabrik, iqtibos) ostiga qo'yiladigan qisqa imzo, masalan @Hilolnashr_qoqon
    signature_html: Mapped[str] = mapped_column(Text, default="", server_default="")
    keywords: Mapped[str] = mapped_column(Text, default="")
    # Filial menejerlari: sotuv postlarini o'z filiali uchun bot orqali tasdiqlaydi
    manager_phones: Mapped[str] = mapped_column(Text, default="", server_default="")
    manager_ids: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # {telegram id: telefon kaliti}
    branch_approval: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    sync_edits: Mapped[bool] = mapped_column(Boolean, default=True)
    sync_deletes: Mapped[bool] = mapped_column(Boolean, default=True)
    silent: Mapped[bool] = mapped_column(Boolean, default=False)


class Rule(Base):
    """Admin oddiy til bilan yozgan qoida yoki istisno."""

    __tablename__ = "rules"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class SourcePost(Base):
    """Manba kanaldagi bitta post (albom bo'lsa, bir nechta xabar)."""

    __tablename__ = "source_posts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    msg_ids: Mapped[list] = mapped_column(JSON)  # albomdagi tartib bo'yicha
    grouped_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    text: Mapped[str] = mapped_column(Text, default="")
    text_hash: Mapped[str] = mapped_column(String(64), default="")
    cta_start_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    block_type: Mapped[str | None] = mapped_column(String(10), nullable=True)  # contact | signature | none
    media_ids: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # {xabar id: media id}
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Delivery(Base):
    """Xarita: manba post -> filial kanaldagi nusxa."""

    __tablename__ = "deliveries"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("source_posts.id"), index=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id"), index=True)
    # pending (tasdiq kutmoqda) | sent | skipped | rejected | failed | deleted
    status: Mapped[str] = mapped_column(String(10), default="pending")
    reason: Mapped[str] = mapped_column(Text, default="")
    target_msg_ids: Mapped[list] = mapped_column(JSON, default=list)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Decision(Base):
    """Adminning qo'lda qabul qilgan qarori: o'xshash post kelganda qayta so'ralmaydi."""

    __tablename__ = "decisions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id"), index=True)
    text: Mapped[str] = mapped_column(Text)
    send: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Event(Base):
    """Jurnal: agent nima qildi va nega."""

    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(30))
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class StatChannel(Base):
    __tablename__ = "stats_channel"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id"), index=True)
    subscribers: Mapped[int] = mapped_column(Integer)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Subscriber(Base):
    """Kanal obunachisi (unikal auditoriyani hisoblash uchun).

    Maxfiylik uchun Telegram ID ning o'zi emas, qaytarib bo'lmaydigan xeshi saqlanadi:
    bir odam ikki kanalda bo'lsa xeshlar mos keladi, lekin kimligini tiklab bo'lmaydi.
    """

    __tablename__ = "subscribers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id"), index=True)
    user_hash: Mapped[str] = mapped_column(String(64), index=True)


class StatPost(Base):
    __tablename__ = "stats_post"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    delivery_id: Mapped[int] = mapped_column(ForeignKey("deliveries.id"), index=True)
    views: Mapped[int] = mapped_column(Integer)
    reach: Mapped[float | None] = mapped_column(Float, nullable=True)  # views / subscribers
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


engine = create_async_engine(settings.database_url)
Session = async_sessionmaker(engine, expire_on_commit=False)


def _migrate(conn) -> None:
    """Mavjud jadvallarga keyin qo'shilgan ustunlarni qo'shadi (create_all buni qilmaydi)."""
    from sqlalchemy import inspect, text

    wanted = {
        "source_posts": {"media_ids": "JSON", "block_type": "VARCHAR(10)"},
        "targets": {
            "signature_html": "TEXT DEFAULT '' NOT NULL", "manager_phones": "TEXT DEFAULT '' NOT NULL",
            "manager_ids": "JSON", "branch_approval": "BOOLEAN DEFAULT FALSE NOT NULL",
        },
    }
    inspector = inspect(conn)
    for table, columns in wanted.items():
        have = {c["name"] for c in inspector.get_columns(table)}
        for name, sql_type in columns.items():
            if name not in have:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"))


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_migrate)


async def log(kind: str, detail: str = "") -> None:
    async with Session() as s:
        s.add(Event(kind=kind, detail=detail))
        await s.commit()
