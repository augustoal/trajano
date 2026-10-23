import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import TypeDecorator
from sqlmodel import Session

from trajano.config import Settings, get_settings

logger = logging.getLogger(__name__)

# Tests replace this. Domain code calls utc_now() and never datetime.now().
_frozen: datetime | None = None
_engine: Engine | None = None
_engine_url: str | None = None


class UTCDateTime(TypeDecorator):
    """Timezone-aware UTC datetimes. SQLite gives back naive values; treat those as UTC."""

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect):  # noqa: ANN001
        return dialect.type_descriptor(DateTime(timezone=True))

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:  # noqa: ANN001
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("datetimes must be timezone-aware")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:  # noqa: ANN001
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


def utc_now() -> datetime:
    if _frozen is not None:
        if _frozen.tzinfo is None:
            raise ValueError("frozen clock must be timezone-aware")
        return _frozen.astimezone(UTC)
    return datetime.now(UTC)


def freeze_clock(moment: datetime) -> None:
    global _frozen
    if moment.tzinfo is None:
        raise ValueError("frozen clock must be timezone-aware")
    _frozen = moment.astimezone(UTC)


def reset_clock() -> None:
    global _frozen
    _frozen = None


def make_engine(url: str) -> Engine:
    kwargs: dict[str, Any] = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 5}
        # A plain :memory: database is per-connection. StaticPool keeps one connection.
        if url in {"sqlite://", "sqlite:///:memory:"} or ":memory:" in url:
            kwargs["poolclass"] = StaticPool
    engine = create_engine(url, **kwargs)
    if engine.dialect.name == "sqlite":
        _install_sqlite_hooks(engine)
    return engine


def _install_sqlite_hooks(engine: Engine) -> None:
    # pysqlite emits its own BEGIN, which is deferred and too late for two buyers
    # racing on the last unit of stock. Owning the transaction lets us take the
    # write lock immediately. Postgres does not use these hooks.
    @event.listens_for(engine, "connect")
    def _connect(dbapi_connection, _record) -> None:  # noqa: ANN001
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    @event.listens_for(engine, "begin")
    def _begin(conn) -> None:  # noqa: ANN001
        conn.exec_driver_sql("BEGIN IMMEDIATE")


def configure_engine(settings: Settings | None = None) -> Engine:
    global _engine, _engine_url
    settings = settings or get_settings()
    if _engine is not None and _engine_url == settings.database_url:
        return _engine
    if _engine is not None:
        _engine.dispose()
    _engine = make_engine(settings.database_url)
    _engine_url = settings.database_url
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        return configure_engine()
    return _engine


def reset_engine() -> None:
    global _engine, _engine_url
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _engine_url = None


def init_db() -> None:
    from sqlmodel import SQLModel

    import trajano.models  # noqa: F401  (register tables)

    SQLModel.metadata.create_all(get_engine())


@contextmanager
def session_scope() -> Iterator[Session]:
    """Commit on success. Domain functions flush and do not commit themselves."""
    session = Session(get_engine(), expire_on_commit=False)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def warn_if_dev_secrets(settings: Settings) -> None:
    if settings.api_key_pepper == "dev-pepper-change-me":
        logger.warning("TRAJANO_API_KEY_PEPPER is the dev default")
    if settings.mandate_signing_key == "dev-mandate-key-change-me":
        logger.warning("TRAJANO_MANDATE_SIGNING_KEY is the dev default")
