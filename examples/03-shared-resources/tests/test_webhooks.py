"""A fixed TCP port: `exclusive="port-8099"` — and a test that holds two tokens at once.

`receiver` binds a real OS-level singleton (`ledger/receiver.py`: "two of these on the same port is
an OSError, not a flaky test"), so all four scenarios run in sequence inside one test function
against one `receiver` instance, rather than as four separate tests each binding the port.
"""

from __future__ import annotations

import asyncio
import sqlite3

import velox
from velox import Depends

from ledger.migrations import LATEST, migrate
from ledger.receiver import WEBHOOK_PORT, Receiver, send
from ledger.service import LedgerService
from tests.fixtures import account, ledger, migration_db, receiver


# Covers all four scenarios in sequence, so the budget covers their combined socket round-trips
# and migration work, not just the heaviest one alone.
@velox.timeout(20)
async def test_receiver_scenarios(
    rx: Receiver = Depends(receiver),
    svc: LedgerService = Depends(ledger),
    acct: str = Depends(account),
    conn: sqlite3.Connection = Depends(migration_db),
) -> None:
    # A single payload.
    await send(WEBHOOK_PORT, b"hello")
    assert rx.received == [b"hello"]
    rx.received.clear()

    # Several payloads, in order.
    for payload in (b"a", b"b", b"c"):
        await send(WEBHOOK_PORT, payload)
    assert rx.received == [b"a", b"b", b"c"]
    rx.received.clear()

    # One token (`port-8099`) plus an untokened resource (`ledger` carries none).
    await svc.append(acct, 4200)
    await send(WEBHOOK_PORT, f"{acct}:4200".encode())
    assert await svc.balance(acct) == 4200
    assert rx.received[0].endswith(b":4200")
    rx.received.clear()

    # Two tokens at once: `port-8099` and `migration_db`. A test's exclusive set is known
    # statically at collection, from its dependency graph, not from what runs at call time.
    await asyncio.to_thread(migrate, conn, LATEST)
    await send(WEBHOOK_PORT, b"migrated")
    assert rx.received == [b"migrated"]
