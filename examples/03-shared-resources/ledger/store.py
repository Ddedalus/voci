"""A blocking sqlite3 store.

Every method here is synchronous and will block whatever thread calls it. That is deliberate: it is
what most real database drivers, file APIs, and crypto libraries look like, and the interesting
question is what happens when one of them is called from the event loop by accident. See
`ledger/service.py` and `tests/test_safety.py`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Entry:
    id: int
    account: str
    cents: int


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def append(self, account: str, cents: int) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO entries (account, cents) VALUES (?, ?)", (account, cents)
            )
            return int(cursor.lastrowid or 0)

    def balance(self, account: str) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(cents), 0) FROM entries WHERE account = ?", (account,)
            ).fetchone()
            return int(row[0])

    def entries(self, account: str) -> list[Entry]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, account, cents FROM entries WHERE account = ? ORDER BY id", (account,)
            ).fetchall()
            return [Entry(*row) for row in rows]

    def total(self) -> int:
        """Aggregate over *every* account — the query that cannot tolerate concurrent writers."""
        with self.connect() as conn:
            row = conn.execute("SELECT COALESCE(SUM(cents), 0) FROM entries").fetchone()
            return int(row[0])

    def truncate(self) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM entries")
