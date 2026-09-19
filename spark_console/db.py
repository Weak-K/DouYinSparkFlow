from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event, inspect
from sqlalchemy.orm import Session

from spark_console.config import Settings
from spark_console.models import Base, WorkerLock


# create_all 只会新建缺失的表，不会给既有表补列，因此线上老库要在这里显式补齐新增字段。
_ADDITIVE_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "users": (("email", "VARCHAR(254)"),),
}

_ADDITIVE_INDEXES: tuple[tuple[str, str, str], ...] = (
    ("ix_users_email", "users", "email"),
)


def create_engine_for(settings: Settings) -> Engine:
    engine = create_engine(
        settings.database_url,
        connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {},
    )
    if settings.database_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def set_sqlite_pragmas(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()
    return engine


def _existing_index_names(inspector, table: str) -> set[str]:
    names = {index["name"] for index in inspector.get_indexes(table)}
    if inspector.has_table(table):
        names |= {constraint["name"] for constraint in inspector.get_unique_constraints(table)}
    return {name for name in names if name}


def upgrade_existing_tables(engine: Engine) -> None:
    """为早于当前模型创建的数据库补齐新增列与索引，对已是最新的库是空操作。"""

    inspector = inspect(engine)
    for table, columns in _ADDITIVE_COLUMNS.items():
        if not inspector.has_table(table):
            continue
        present = {column["name"] for column in inspector.get_columns(table)}
        for name, ddl in columns:
            if name in present:
                continue
            with engine.begin() as connection:
                connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    inspector = inspect(engine)
    for index_name, table, column in _ADDITIVE_INDEXES:
        if not inspector.has_table(table):
            continue
        if index_name in _existing_index_names(inspector, table):
            continue
        with engine.begin() as connection:
            connection.exec_driver_sql(
                f"CREATE INDEX IF NOT EXISTS {index_name} ON {table} ({column})"
            )


def create_schema(engine: Engine) -> None:
    """Create all declared tables additively for fresh and existing databases."""
    Base.metadata.create_all(engine)
    upgrade_existing_tables(engine)
    with session_scope(engine) as session:
        if session.get(WorkerLock, 1) is None:
            session.add(WorkerLock(id=1))


@contextmanager
def session_scope(engine: Engine):
    session = Session(engine, expire_on_commit=False)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
