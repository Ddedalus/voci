"""A fixed TCP port: `exclusive="port-8099"` — and a test that holds two tokens at once.

Four scenarios, one test function. `receiver` binds a real OS-level singleton (`ledger/receiver.py`
docstring: "Two of these on the same port is an OSError, not a flaky test") -- exactly the case
`exclusive=` exists to serialize. That serialization isn't enforced yet (`docs/M1-PLAN.md`: the
scheduler that would admit `exclusive=`-carrying tests one at a time doesn't exist), so this file
had four separate tests before, one `Depends(receiver)` each, and running them together always hit
a real `OSError: [Errno 98] address already in use` -- the second-and-later binds losing the race
for the one port, not a timing coincidence. Sequencing all four scenarios inside one test avoids a
second concurrent bind ever being attempted at all: one `receiver` fixture instance, one bind, every
scenario the original four tests covered, run one after another instead of four-at-once.

This trades away two things a real `exclusive=` scheduler would give back, worth naming rather than
leaving implicit: per-scenario failure isolation (one `assert` failing here aborts the rest, so a
single run can't tell you the state of scenarios after the first failure the way four separate test
ids could) and a tight `migration_db` footprint (the last scenario is the only one that needs it,
but `Depends(migration_db)` as a top-level parameter holds that token for this function's *entire*
duration, not just its own last few lines, since DI resolves before the body runs). Both are the
right trade against a guaranteed, deterministic `OSError` today; restore the four-way split, with
`migration_db` back on only the one scenario that needs it, once `exclusive=` is real.
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


# 20s, not the 10s that used to budget only the heaviest of these four scenarios alone: this one
# test now runs all four in sequence, and under real contention (this suite's own README stress-
# tests --concurrency 64) the combined socket round-trips plus migration work deserve more margin
# than one scenario's worth.
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

    # One token (`port-8099`) plus an untokened resource (`ledger` carries none, so depending on
    # it adds nothing to the real exclusive footprint -- only `receiver` would contribute).
    await svc.append(acct, 4200)
    await send(WEBHOOK_PORT, f"{acct}:4200".encode())
    assert await svc.balance(acct) == 4200
    assert rx.received[0].endswith(b":4200")
    rx.received.clear()

    # Two tokens at once: `port-8099` and `migration_db`. This is the case that would otherwise
    # make people reach for lock-ordering rules, timeouts, and deadlock detectors. None of that is
    # needed here, because the (not-yet-built) scheduler acquires a test's *entire* exclusive set
    # atomically or not at all: the set is known statically at collection, and admission is
    # all-or-nothing. There is no hold-and-wait, so there is nothing to deadlock -- a consequence
    # of explicit DI, and the concrete reason velox has no `getfixturevalue` (a dependency
    # discovered at run time is a footprint that cannot be known before dispatch).
    await asyncio.to_thread(migrate, conn, LATEST)
    await send(WEBHOOK_PORT, b"migrated")
    assert rx.received == [b"migrated"]
