"""A toy migration chain.

Migrations are the archetypal exclusive resource: they mutate schema, they are ordered, and two of
them running at once against the same database is not a race you can retry your way out of.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    up: Callable[[sqlite3.Connection], None]
    down: Callable[[sqlite3.Connection], None]


def _create_entries(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE entries ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " account TEXT NOT NULL,"
        " cents INTEGER NOT NULL)"
    )


def _drop_entries(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE entries")


def _index_account(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE INDEX idx_entries_account ON entries (account)")


def _drop_index_account(conn: sqlite3.Connection) -> None:
    conn.execute("DROP INDEX idx_entries_account")


def _add_memo(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE entries ADD COLUMN memo TEXT")


def _drop_memo(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE entries DROP COLUMN memo")


MIGRATIONS: list[Migration] = [
    Migration(1, "create_entries", _create_entries, _drop_entries),
    Migration(2, "index_account", _index_account, _drop_index_account),
    Migration(3, "add_memo", _add_memo, _drop_memo),
]

LATEST = MIGRATIONS[-1].version


def current_version(conn: sqlite3.Connection) -> int:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()
    return int(row[0])


def _record(conn: sqlite3.Connection, version: int) -> None:
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))


def migrate(conn: sqlite3.Connection, to_version: int = LATEST) -> int:
    """Move the schema to `to_version`, up or down. Blocking."""
    version = current_version(conn)
    while version < to_version:
        step = MIGRATIONS[version]
        step.up(conn)
        version = step.version
        _record(conn, version)
    while version > to_version:
        step = MIGRATIONS[version - 1]
        step.down(conn)
        version = step.version - 1
        _record(conn, version)
    conn.commit()
    return version
