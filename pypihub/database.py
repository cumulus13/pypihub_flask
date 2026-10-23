#!/usr/bin/env python3

# File: pypihub/database.py
# -*- coding: utf-8 -*-
# Author: Hadi Cahyadi <cumulus13@gmail.com>
# Description: Database models and engine setup for PyPihub (SQLite, PostgreSQL, MySQL/MariaDB)
# License: LGPL-3.0-or-later

"""
Models and engine factory.

Column types are chosen to behave the same on SQLite, PostgreSQL and MySQL: every ``String`` has a
length (MySQL requires it), timestamps are naive UTC, and index keys stay below InnoDB's 3072 byte
limit even with utf8mb4.

Connection settings
    DATABASE_URL       full SQLAlchemy URL, e.g. postgresql://user:pass@host/db or mysql://user:pass@host/db
                       (bare ``postgresql``/``mysql`` schemes use the bundled psycopg and PyMySQL drivers)
    DB_POOL_SIZE       connections kept per process (default 10, not used by SQLite)
    DB_MAX_OVERFLOW    extra connections allowed under load (default 20)
    DB_POOL_RECYCLE    seconds before a connection is replaced (default 1800, below MySQL's wait_timeout)
    DB_POOL_TIMEOUT    seconds to wait for a free connection (default 30)
    DB_CONNECT_RETRIES attempts at startup while the database is still coming up (default 10)

Keep ``workers * (DB_POOL_SIZE + DB_MAX_OVERFLOW)`` below the server's connection limit.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional, Union

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import declarative_base, sessionmaker

log = logging.getLogger(__name__)

Base = declarative_base()


def utcnow() -> datetime:
    """Naive UTC timestamp. Naive on purpose: MySQL has no timezone-aware column type."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    username = Column(String(150), unique=True, nullable=False)
    password = Column(String(255), nullable=False)  # bcrypt hash


class Package(Base):
    """Audit log of uploads and cache fills. The browsable index is ``DistFile``."""

    __tablename__ = "packages"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    source = Column(String(16), nullable=False)  # 'pypi' or 'upload'
    added = Column(DateTime, default=utcnow)
    user_id = Column(String(64), nullable=True)


class DistFile(Base):
    """One file stored on disk, either uploaded (``local``) or fetched from upstream (``cached``)."""

    __tablename__ = "dist_files"

    # SQLite only auto-increments an INTEGER primary key.
    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    project_key = Column(String(128), nullable=False)  # PEP 503 normalised name
    project_name = Column(String(128), nullable=False)  # directory name on disk
    filename = Column(String(255), nullable=False)
    source = Column(String(8), nullable=False)  # 'local' or 'cached'
    kind = Column(String(8), nullable=False, default="other")  # 'wheel', 'sdist' or 'other'
    version = Column(String(64), nullable=True)
    is_prerelease = Column(Boolean, nullable=False, default=False)
    size = Column(BigInteger, nullable=False, default=0)
    sha256 = Column(String(64), nullable=True)
    uploaded_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint("source", "project_name", "filename", name="uq_dist_files_location"),
        Index("ix_dist_files_project_key", "project_key"),
        Index("ix_dist_files_source_created", "source", "created_at"),
    )


class PypiMeta(Base):
    """Project details copied from the upstream's JSON API (README, links, classifiers, release history).

    ``version`` is empty for the project's latest release, which also carries the release history,
    and the version number for a single release. ``data`` is JSON; MySQL's plain TEXT holds only 64 kB,
    which a README can exceed, hence LONGTEXT there.
    """

    __tablename__ = "pypi_meta"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    project_key = Column(String(128), nullable=False)
    version = Column(String(128), nullable=False, default="")
    status = Column(String(8), nullable=False, default="ok")  # 'ok' or 'missing' (the upstream does not know it)
    data = Column(Text().with_variant(LONGTEXT(), "mysql"), nullable=True)
    fetched_at = Column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (UniqueConstraint("project_key", "version", name="uq_pypi_meta_release"),)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

_DRIVERS = {
    "postgres": "postgresql+psycopg",
    "postgresql": "postgresql+psycopg",
    "mysql": "mysql+pymysql",
    "mariadb": "mysql+pymysql",
}


def normalize_url(url: Union[str, URL]) -> URL:
    """Pick a bundled driver for bare ``postgresql://`` and ``mysql://`` URLs; leave explicit drivers alone."""
    parsed = make_url(url)
    driver = _DRIVERS.get(parsed.drivername)
    if driver:
        parsed = parsed.set(drivername=driver)
    if parsed.get_backend_name() == "mysql" and "charset" not in parsed.query:
        parsed = parsed.update_query_dict({"charset": "utf8mb4"})
    return parsed


def build_url(
    db_type: str,
    *,
    path: Optional[str] = None,
    name: Optional[str] = None,
    user: Optional[str] = None,
    password: Optional[str] = None,
    host: Optional[str] = None,
    port: Optional[Union[int, str]] = None,
) -> URL:
    """URL from separate settings. Unlike string formatting this escapes ``@``, ``:`` and ``/`` in passwords."""
    db_type = (db_type or "sqlite").lower()
    if db_type == "sqlite":
        return URL.create("sqlite", database=str(path or "pypihub.db"))
    if db_type not in _DRIVERS:
        raise ValueError(f"Unsupported DB_TYPE: {db_type!r} (use sqlite, postgres or mysql, or set DATABASE_URL)")
    default_port = 5432 if _DRIVERS[db_type].startswith("postgresql") else 3306
    return normalize_url(
        URL.create(
            db_type,
            username=user or None,
            password=password or None,
            host=host or "localhost",
            port=int(port) if port else default_port,
            database=name or "pypihub",
        )
    )


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def make_engine(url: Union[str, URL], echo: bool = False) -> Engine:
    """Engine tuned for a threaded gunicorn worker."""
    url = normalize_url(url)
    kwargs: dict = {"echo": echo, "future": True, "pool_pre_ping": True}
    backend = url.get_backend_name()

    if backend == "sqlite":
        in_memory = url.database in (None, "", ":memory:")
        if not in_memory:
            os.makedirs(os.path.dirname(os.path.abspath(url.database)) or ".", exist_ok=True)
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        if in_memory:
            from sqlalchemy.pool import StaticPool

            kwargs["poolclass"] = StaticPool  # one shared connection, or every connection would see its own empty database
    else:
        kwargs.update(
            pool_size=_int_env("DB_POOL_SIZE", 10),
            max_overflow=_int_env("DB_MAX_OVERFLOW", 20),
            pool_recycle=_int_env("DB_POOL_RECYCLE", 1800),
            pool_timeout=_int_env("DB_POOL_TIMEOUT", 30),
        )

    engine = create_engine(url, **kwargs)

    if backend == "sqlite":

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover - exercised on every SQLite connect
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
            if url.database not in (None, "", ":memory:"):
                cursor.execute("PRAGMA journal_mode=WAL")  # readers do not block the writer
                cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

    return engine


def init_db(engine: Engine, attempts: Optional[int] = None, delay: float = 1.0) -> None:
    """Create missing tables.

    Retries because two things are normal at startup: the database container may not accept
    connections yet, and several gunicorn workers may try to create the same tables at once
    (one of them loses the race and fails with "already exists").
    """
    attempts = attempts if attempts is not None else max(1, _int_env("DB_CONNECT_RETRIES", 10))
    for attempt in range(1, attempts + 1):
        try:
            Base.metadata.create_all(engine)
            return
        except DBAPIError as exc:
            if attempt == attempts:
                raise
            wait = min(delay * attempt, 5.0)
            log.warning("Database not ready (attempt %s/%s): %s. Retrying in %.1fs", attempt, attempts,
                        str(exc).splitlines()[0][:160], wait)
            time.sleep(wait)


def session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, expire_on_commit=False)
