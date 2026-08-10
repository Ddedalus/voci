"""Migration tests: `exclusive=True`.

`migration_db` carries the token that would, once exclusive-resource admission is real (it isn't
yet -- M1-PLAN.md), serialise these against each other and against nothing else, while the rest of
`test_ledger.py` runs at full width alongside them. Nothing here actually depends on that today:
each test gets its own on-disk database via `tmp_path` (the fixture's own docstring says so), so
running them concurrently is safe regardless -- the token declares an intent this suite doesn't
yet need enforced, the same way `test_webhooks.py`'s fixed port genuinely does.
"""

from __future__ import annotations

import asyncio
import sqlite3

import velox
from velox import Depends

from ledger.migrations import LATEST, current_version, migrate
from tests.fixtures import migration_db


async def test_migrates_to_latest(conn: sqlite3.Connection = Depends(migration_db)) -> None:
    version = await asyncio.to_thread(migrate, conn, LATEST)

    assert version == LATEST
    assert await asyncio.to_thread(current_version, conn) == LATEST


async def _assert_migrates_to(target: int, conn: sqlite3.Connection) -> None:
    assert await asyncio.to_thread(migrate, conn, target) == target


# `@velox.parametrize("target", [m.version for m in MIGRATIONS])` would collapse the three cases
# below into one test -- declared public API, not yet expanded by the collector into records
# (M1-PLAN.md), so they're separate tests instead, each carrying the `migration_db` token (would
# be serialized against each other and nothing else, once exclusive= admission is real -- also
# M1-PLAN.md; each already gets its own on-disk database via `tmp_path`, so nothing is actually at
# stake if they overlap today, only the demonstration of the token itself). Logical order still
# governs the report regardless: the failure block for the middle version always appears between
# the other two, no matter which finished first.
async def test_migrates_to_version_1(conn: sqlite3.Connection = Depends(migration_db)) -> None:
    await _assert_migrates_to(1, conn)


async def test_migrates_to_version_2(conn: sqlite3.Connection = Depends(migration_db)) -> None:
    await _assert_migrates_to(2, conn)


async def test_migrates_to_version_3(conn: sqlite3.Connection = Depends(migration_db)) -> None:
    await _assert_migrates_to(3, conn)


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
