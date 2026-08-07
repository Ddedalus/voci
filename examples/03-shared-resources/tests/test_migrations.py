"""Migration tests: `exclusive=True`.

These are serialised against each other and against nothing else. While they take turns on the
migration database, the twenty tests in `test_ledger.py` are running at full width alongside them.
"""

from __future__ import annotations

import asyncio
import sqlite3

import velox
from velox import Depends

from ledger.migrations import LATEST, MIGRATIONS, current_version, migrate
from tests.fixtures import migration_db


async def test_migrates_to_latest(conn: sqlite3.Connection = Depends(migration_db)) -> None:
    version = await asyncio.to_thread(migrate, conn, LATEST)

    assert version == LATEST
    assert await asyncio.to_thread(current_version, conn) == LATEST


@velox.parametrize("target", [m.version for m in MIGRATIONS])
async def test_migrates_to_each_version(
    target: int,
    conn: sqlite3.Connection = Depends(migration_db),
) -> None:
    """Three parametrizations, three tests, all carrying the `migration_db` token.

    They never overlap. Logical order still governs the report, so the failure block for
    `[target=2]` always appears between `[target=1]` and `[target=3]` no matter which finished
    first.
    """
    assert await asyncio.to_thread(migrate, conn, target) == target


async def test_round_trips_down_and_up(conn: sqlite3.Connection = Depends(migration_db)) -> None:
    await asyncio.to_thread(migrate, conn, LATEST)
    await asyncio.to_thread(migrate, conn, 1)
    await asyncio.to_thread(migrate, conn, LATEST)

    tables = await asyncio.to_thread(
        lambda: [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    )

    assert "entries" in tables


async def test_downgrade_below_one_drops_the_table(
    conn: sqlite3.Connection = Depends(migration_db),
) -> None:
    await asyncio.to_thread(migrate, conn, LATEST)
    await asyncio.to_thread(migrate, conn, 0)

    with velox.raises(sqlite3.OperationalError, match="no such table: entries"):
        await asyncio.to_thread(conn.execute, "SELECT * FROM entries")


@velox.tag("slow")
async def test_full_chain_is_idempotent(conn: sqlite3.Connection = Depends(migration_db)) -> None:
    for _ in range(3):
        assert await asyncio.to_thread(migrate, conn, LATEST) == LATEST
