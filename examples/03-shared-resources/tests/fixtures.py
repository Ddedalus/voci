"""Fixtures for the ledger suite.

Three resources, three different answers:

  `ledger`          — no conflict. Each test owns an account namespace, so 32 run at once.
  `migration_db`    — `exclusive=True`. One migration database, one test at a time.
  `receiver`        — `exclusive="port-8099"`. One TCP port, one test at a time.

The exclusivity is declared on the *resource*, never on the test. A test inherits the union of
tokens over the transitive closure of its dependency graph, computed once at collection and never
changed. Nothing at a call site has to remember to annotate itself, so nothing can forget.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import ModuleType

import velox
from velox import Depends

from ledger import flags
from ledger.migrations import LATEST, migrate
from ledger.receiver import WEBHOOK_PORT, Receiver
from ledger.service import LedgerService
from ledger.store import Store

# --------------------------------------------------------------------------------------
# The concurrent path: no tokens at all
# --------------------------------------------------------------------------------------


@velox.fixture(scope="session")
def app_db(tmp: velox.TmpPathFactory = Depends(velox.tmp_path_factory)) -> Path:
    return tmp.mktemp("app-db") / "ledger.sqlite"


@velox.fixture(scope="session")
async def migrated_store(path: Path = Depends(app_db)) -> AsyncIterator[Store]:
    """Built once for the whole run, under a single-flight guard.

    Thirty-two tests demanding this at the same instant produce exactly one migration: the first
    caller creates the `Future`, the rest await it. Teardown happens when the last test that
    depends on it finishes — by refcount, not by unwinding a setup stack.
    """
    store = Store(path)
    # `check_same_thread=False`: `asyncio.to_thread` runs each call on whichever worker the
    # default executor's pool happens to hand it, not the same one call to call, and a plain
    # `sqlite3.connect` refuses to touch its connection from any thread but the one that created
    # it. The awaits below still fully serialize access (one `to_thread` call finishes before the
    # next starts), so nothing actually touches this connection from two threads *at once* --
    # `check_same_thread` is guarding against a case that can't happen here, just not on the
    # thread it originally expected.
    conn = await asyncio.to_thread(sqlite3.connect, str(path), check_same_thread=False)
    try:
        await asyncio.to_thread(migrate, conn, LATEST)
        # Switching journal mode requires an exclusive lock (a real, if brief, write lock -- not
        # the read/write concurrency WAL itself provides once active), and `Store.connect` below
        # re-issues `PRAGMA journal_mode=WAL` on every single call from every concurrent test as
        # its first statement on a brand-new connection. Left at the default rollback journal
        # until the first real query, dozens of those `PRAGMA` calls would race each other for
        # that same exclusive lock the moment the suite's tests start hitting the database at
        # once -- `sqlite3.OperationalError: database is locked`, reproduced directly under
        # `--concurrency 64`. Switching once here, before any test fixture depending on this one
        # resolves, means every later `PRAGMA journal_mode=WAL` is already-WAL and a no-op.
        await asyncio.to_thread(conn.execute, "PRAGMA journal_mode=WAL")
    finally:
        await asyncio.to_thread(conn.close)
    try:
        yield store
    finally:
        await asyncio.to_thread(store.truncate)


@velox.fixture()
def account(info: velox.TestInfo = Depends(velox.test_info)) -> str:
    """A per-test account namespace derived from the test id.

    This is the fixture that does the real work in this example. Isolating tests by *data* rather
    than by *exclusion* is almost always available, almost always cheaper, and is the reason the
    exclusive-token machinery below is needed for three fixtures instead of thirty.
    """
    return f"acct::{info.id}"


@velox.fixture()
async def ledger(store: Store = Depends(migrated_store)) -> LedgerService:
    return LedgerService(store)


# --------------------------------------------------------------------------------------
# `exclusive=True`: the token is the fixture's own name
# --------------------------------------------------------------------------------------


@velox.fixture(exclusive=True)
async def migration_db(
    tmp: Path = Depends(velox.tmp_path),
) -> AsyncIterator[sqlite3.Connection]:
    """A database the migration tests are allowed to destroy.

    `exclusive=True` means "token = this fixture's name", i.e. `migration_db`. Every test whose
    graph reaches this fixture is serialised against every other such test — and runs concurrently
    with everything else in the suite, which is the difference between an exclusive token and
    `@velox.solo`.

    Strictly, a fresh `tmp_path`-scoped file per test would need no token at all. It has one here
    because schema migration against a *shared* database is the case people actually have.
    """
    path = tmp / "migrations.sqlite"
    # See `migrated_store` above for why `check_same_thread=False`: this connection crosses
    # `asyncio.to_thread` calls too (here, and in every test that uses it).
    conn = await asyncio.to_thread(sqlite3.connect, str(path), check_same_thread=False)
    try:
        yield conn
    finally:
        await asyncio.to_thread(conn.close)


# --------------------------------------------------------------------------------------
# `exclusive="token"`: a named resource, possibly shared by several fixtures
# --------------------------------------------------------------------------------------


@velox.fixture(exclusive=f"port-{WEBHOOK_PORT}")
async def receiver() -> AsyncIterator[Receiver]:
    """A real listening socket on a fixed port.

    A string token rather than `True`, because the resource is the *port*, not this fixture. If a
    second fixture also bound 8099 — a TLS variant, say — it would carry the same token and the
    scheduler would keep them apart too. That is the case `exclusive=True` cannot express.
    """
    rx = Receiver(port=WEBHOOK_PORT)
    await rx.start()
    try:
        yield rx
    finally:
        await rx.stop()


# --------------------------------------------------------------------------------------
# Process-global state: not a token, a solo test
# --------------------------------------------------------------------------------------


@velox.fixture()
def feature_flags() -> Iterator[ModuleType]:
    """Save/restore around a test that mutates the global flag registry.

    The save/restore makes the mutation *reversible*; it does not make it *invisible*. Any test
    using this must also be `@velox.solo`, because while it runs, every other test in flight would
    see the flipped flag. A sync generator fixture — velox accepts all four shapes.
    """
    saved = flags.snapshot()
    try:
        yield flags
    finally:
        flags.restore(saved)
