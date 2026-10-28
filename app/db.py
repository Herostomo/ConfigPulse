from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


def make_engine(database_url: str | None = None):
    return create_engine(
        database_url or settings.database_url,
        pool_pre_ping=True,
        future=True,
    )


# Defer failure until initialization if the PostgreSQL driver has not yet been
# installed. This allows parser/diff unit tests to run without a database driver.
# Real runtime commands call init_db(), which creates and binds the engine.
try:
    engine = make_engine()
except ModuleNotFoundError as exc:
    if "psycopg" not in str(exc):
        raise
    engine = None

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_engine():
    global engine
    if engine is None:
        engine = make_engine()
        SessionLocal.configure(bind=engine)
    return engine


def init_db(target_engine=None) -> None:
    # Import models so all model metadata is registered before create_all.
    from app import models as _models  # noqa: F401

    selected_engine = target_engine or get_engine()
    Base.metadata.create_all(bind=selected_engine)
