"""Migration tests: `exclusive=True`.

`migration_db` carries the token, inherited by every test whose dependency graph reaches it. Each
test also gets its own on-disk database via `tmp_path`.
"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Annotated

from ledger.migrations import LATEST, current_version, migrate
from tests.fixtures import migration_db

import velox
from velox import Depends


async def test_migrates_to_latest(
    conn: Annotated[sqlite3.Connection, Depends(migration_db)],
) -> None:
    version = await asyncio.to_thread(migrate, conn, LATEST)

    assert version == LATEST
    assert await asyncio.to_thread(current_version, conn) == LATEST


async def _assert_migrates_to(target: int, conn: sqlite3.Connection) -> None:
    assert await asyncio.to_thread(migrate, conn, target) == target


# Each target version is its own test, each carrying the `migration_db` token. Failure detail
# prints in logical order regardless of completion order: the middle version's failure block
# always appears between the other two.
async def test_migrates_to_version_1(
    conn: Annotated[sqlite3.Connection, Depends(migration_db)],
) -> None:
    await _assert_migrates_to(1, conn)


async def test_migrates_to_version_2(
    conn: Annotated[sqlite3.Connection, Depends(migration_db)],
) -> None:
    await _assert_migrates_to(2, conn)


async def test_migrates_to_version_3(
    conn: Annotated[sqlite3.Connection, Depends(migration_db)],
) -> None:
    await _assert_migrates_to(3, conn)


async def test_round_trips_down_and_up(
    conn: Annotated[sqlite3.Connection, Depends(migration_db)],
) -> None:
    await asyncio.to_thread(migrate, conn, LATEST)
    await asyncio.to_thread(migrate, conn, 1)
    await asyncio.to_thread(migrate, conn, LATEST)

    tables = await asyncio.to_thread(
        lambda: [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    )

    assert "entries" in tables


async def test_downgrade_below_one_drops_the_table(
    conn: Annotated[sqlite3.Connection, Depends(migration_db)],
) -> None:
    await asyncio.to_thread(migrate, conn, LATEST)
    await asyncio.to_thread(migrate, conn, 0)

    with velox.raises(sqlite3.OperationalError, match="no such table: entries"):
        await asyncio.to_thread(conn.execute, "SELECT * FROM entries")


@velox.tag("slow")
async def test_full_chain_is_idempotent(
    conn: Annotated[sqlite3.Connection, Depends(migration_db)],
) -> None:
    for _ in range(3):
        assert await asyncio.to_thread(migrate, conn, LATEST) == LATEST
