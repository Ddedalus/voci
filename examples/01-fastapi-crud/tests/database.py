"""The SQLAlchemy plumbing behind the `engine` and `session` fixtures.

The suite runs on SQLite so that `uv sync && voci` is the whole setup — no server, no container.
SQLite serialises writers, so it caps how much of the concurrency win reaches the storage layer;
`url_for` is where a suite that cares about its wall clock would point somewhere else.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import Connection, event
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import ConnectionPoolEntry

from app.models import Base


def url_for(directory: Path) -> str:
    """A SQLite URL for a database file under `directory`.

    Postgres is the swap for a real suite — `"postgresql+asyncpg://voci:voci@localhost/voci_test"`,
    once `asyncpg` is installed — and everything below works against either.
    """
    return f"sqlite+aiosqlite:///{directory / 'app.sqlite'}"


@asynccontextmanager
async def engine_with_schema(url: str) -> AsyncIterator[AsyncEngine]:
    """An engine for `url` with every table created, disposed on exit."""
    engine = create_async_engine(url)
    try:
        if engine.dialect.name == "sqlite":
            _drive_sqlite_transactions_explicitly(engine)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield engine
    finally:
        await engine.dispose()


@asynccontextmanager
async def transaction(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session in a transaction that is rolled back on exit.

    The session joins the transaction through a SAVEPOINT, so a test may `commit()` as often as it
    likes and still leave the database exactly as it found it.
    """
    async with engine.connect() as conn:
        trans = await conn.begin()
        async with AsyncSession(
            bind=conn,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        ) as session:
            yield session
        await trans.rollback()


def _drive_sqlite_transactions_explicitly(engine: AsyncEngine) -> None:
    """Issue `BEGIN` from SQLAlchemy rather than from the SQLite driver.

    pysqlite and aiosqlite open and commit transactions on a heuristic of their own, which the
    rollback in `transaction` cannot undo — rows from one test would survive into the next. These
    two listeners are SQLAlchemy's documented remedy (`AsyncEngine.begin()`, "DBAPI AUTOCOMMIT").
    """

    @event.listens_for(engine.sync_engine, "connect")
    def _hand_over(dbapi_connection: DBAPIConnection, _record: ConnectionPoolEntry) -> None:
        # `isolation_level` is a pysqlite/aiosqlite extension, absent from the DBAPI-2.0 types.
        dbapi_connection.isolation_level = None  # type: ignore[attr-defined]

    @event.listens_for(engine.sync_engine, "begin")
    def _begin(conn: Connection) -> None:
        conn.exec_driver_sql("BEGIN")
