"""Process-global state, and the safety net under all of it."""

from __future__ import annotations

import asyncio
from types import ModuleType

import velox
from velox import Depends

from ledger.service import LedgerService
from tests.fixtures import account, feature_flags, ledger

# --------------------------------------------------------------------------------------
# @velox.solo — for state with no per-task view
# --------------------------------------------------------------------------------------


@velox.solo
async def test_strict_transfers_rejects_overdraft(
    flags: ModuleType = Depends(feature_flags),
    svc: LedgerService = Depends(ledger),
    acct: str = Depends(account),
) -> None:
    """The suite drains, this runs alone, the suite resumes.

    Nothing is being mocked here — the reason for `@velox.solo` is a module-level dict read at call
    time, and no mocking library would change that. `test_ledger.py` has a test asserting that
    overdrafts *are* allowed; while this one runs, that test must not be in flight.

    Model it as a reader-writer lock over the whole suite: ordinary tests hold a read lock
    implicitly, a solo test takes the write lock. Aging in the scheduler keeps it from starving,
    and the summary reports what it cost.
    """
    flags.set_enabled("strict_transfers", True)
    source, target = f"{acct}::a", f"{acct}::b"
    await svc.append(source, 100)

    with velox.raises(ValueError, match="insufficient balance"):
        await svc.transfer(source, target, 500)


@velox.solo
async def test_audit_flag_is_restored_afterwards(
    flags: ModuleType = Depends(feature_flags),
) -> None:
    """`feature_flags` snapshots and restores, so the mutation is reversible.

    Reversible is not the same as invisible, which is why the decorator is still required. The
    fixture handles cleanup; `@velox.solo` handles the window during which the flag is wrong.
    """
    flags.set_enabled("audit_every_write", True)
    assert flags.is_enabled("audit_every_write") is True


# --------------------------------------------------------------------------------------
# The watchdog
# --------------------------------------------------------------------------------------


@velox.tag("watchdog-demo")
async def test_blocking_call_stalls_the_loop(
    svc: LedgerService = Depends(ledger),
    acct: str = Depends(account),
) -> None:
    """Deliberately wrong, so there is something to catch.

    `balance_blocking` is a plain synchronous method. Called from a coroutine it runs *on the event
    loop thread*, and while sqlite is busy every other in-flight test is frozen — no timeout fires,
    no exception is raised, and the only symptom is that the suite got slower.

    velox notices from outside: a daemon thread watches a 100 ms loop heartbeat, and when the
    timestamp goes stale past `--watchdog-threshold` it captures every task stack plus a
    `faulthandler` dump of every *thread* stack, because the blocking frame is in a thread stack,
    not a task stack.

    Run `velox -k watchdog-demo --watchdog-threshold 0.05` to see it:

        ⚠ Event loop blocked for 0.31s during
          tests/test_safety.py::test_blocking_call_stalls_the_loop
            blocking frame:  ledger/store.py:34 in balance
            5 other tests were stalled: test_balance_sums_entries, test_entries_are_ordered, ...
            → wrap blocking calls in `await asyncio.to_thread(...)`

    Diagnosing that is worth more than the test: the same call in a request handler blocks the
    production server the same way, and nothing in pytest could have told you.

    The correct version is one line different — and it is what `LedgerService.balance` already does.
    """
    await svc.append(acct, 999)

    blocking = svc.balance_blocking(acct)  # ← the bug the watchdog reports
    correct = await asyncio.to_thread(svc.balance_blocking, acct)  # ← what it should have been

    assert blocking == correct == 999


@velox.timeout(2)
async def test_a_hang_is_reported_as_a_timeout_not_a_failure() -> None:
    """`asyncio.timeout` wraps the whole envelope — setup, call, and teardown.

    A test that exceeds it is cancelled and reported with the `timeout` outcome, distinct from
    `failed`, so "this test is slow" and "this test is wrong" never look the same in CI. The live
    footer will already have been naming this test with a growing elapsed time before the timeout
    fires.
    """
    await asyncio.sleep(0.1)


async def test_a_test_must_not_return_a_value() -> None:
    """Returning a non-`None` value from a test is an error, not a pass.

    That is the "forgot to await" bug: a coroutine returned instead of awaited is truthy, silently
    never runs, and pytest reports green. Same for an un-awaited coroutine warning raised during a
    test — it fails the test. Silent passes are bugs (invariant I8).
    """
    result = await asyncio.sleep(0, result=42)
    assert result == 42
