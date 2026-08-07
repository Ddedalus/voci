"""A fixed TCP port: `exclusive="port-8099"` — and a test that holds two tokens at once."""

from __future__ import annotations

import asyncio
import sqlite3

import velox
from velox import Depends

from ledger.migrations import LATEST, migrate
from ledger.receiver import WEBHOOK_PORT, Receiver, send
from ledger.service import LedgerService
from tests.fixtures import account, ledger, migration_db, receiver


async def test_receiver_accepts_a_payload(rx: Receiver = Depends(receiver)) -> None:
    await send(WEBHOOK_PORT, b"hello")

    assert rx.received == [b"hello"]


async def test_receiver_accepts_several_payloads(rx: Receiver = Depends(receiver)) -> None:
    for payload in (b"a", b"b", b"c"):
        await send(WEBHOOK_PORT, payload)

    assert rx.received == [b"a", b"b", b"c"]


@velox.timeout(10)
async def test_ledger_writes_notify_the_receiver(
    rx: Receiver = Depends(receiver),
    svc: LedgerService = Depends(ledger),
    acct: str = Depends(account),
) -> None:
    """One token (`port-8099`), two resources.

    The `ledger` fixture carries no token, so depending on it adds nothing to this test's
    footprint. Only `receiver` contributes.
    """
    await svc.append(acct, 4200)
    await send(WEBHOOK_PORT, f"{acct}:4200".encode())

    assert await svc.balance(acct) == 4200
    assert rx.received[0].endswith(b":4200")


async def test_migration_notifies_the_receiver(
    rx: Receiver = Depends(receiver),
    conn: sqlite3.Connection = Depends(migration_db),
) -> None:
    """**Two tokens: `port-8099` and `migration_db`.**

    This is the case that makes people reach for lock-ordering rules, timeouts, and deadlock
    detectors. None of them are here, because the scheduler acquires a test's *entire* exclusive
    set atomically or not at all: the set is known statically at collection, and admission is
    all-or-nothing. There is no hold-and-wait, so there is nothing to deadlock.

    That property is a consequence of explicit DI. It is also the concrete reason velox has no
    `getfixturevalue` — a dependency discovered at run time is a footprint that cannot be known
    before dispatch, and the guarantee evaporates.
    """
    await asyncio.to_thread(migrate, conn, LATEST)
    await send(WEBHOOK_PORT, b"migrated")

    assert rx.received == [b"migrated"]
