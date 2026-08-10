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
@velox.skip(
    "would race test_ledger.py::test_transfer_is_permitted_to_overdraw_by_default, which "
    "expects strict_transfers to stay off, without real solo scheduling (M1-PLAN.md); verified "
    "directly (a standalone asyncio.TaskGroup running both bodies concurrently, 3000/3000 "
    "trials): the overdraw test raised 'insufficient balance' or came back with the wrong "
    "balance essentially every time they overlapped. Safe to re-enable once @velox.solo is "
    "enforced."
)
async def test_strict_transfers_rejects_overdraft(
    flags: ModuleType = Depends(feature_flags),
    svc: LedgerService = Depends(ledger),
    acct: str = Depends(account),
) -> None:
    """In a fuller suite this would drain the suite, run alone, and let it resume.

    Nothing is being mocked here — the reason for `@velox.solo` is a module-level dict read at call
    time, and no mocking library would change that. `test_ledger.py` has a test asserting that
    overdrafts *are* allowed; while this one runs, that test must not be in flight — see the
    `@velox.skip` reason above for what happens today when it is.

    Model it as a reader-writer lock over the whole suite: ordinary tests hold a read lock
    implicitly, a solo test takes the write lock. Aging in the scheduler keeps it from starving,
    and the summary reports what it cost — none of which is built yet (`_run.py`'s own module
    docstring lists exclusive-resource admission and the solo write-lock tier as still deferred).
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

    Reversible is not the same as invisible, which is why the decorator is still required — in a
    fuller suite. `@velox.solo` isn't enforced yet (see `test_strict_transfers_rejects_overdraft`
    above for what that gap is), but this one is live rather than skipped: nothing else in this
    suite reads `audit_every_write`, so there's no live neighbour for the unguarded window to
    actually corrupt, unlike the flag `test_strict_transfers_rejects_overdraft` flips.
    """
    flags.set_enabled("audit_every_write", True)
    assert flags.is_enabled("audit_every_write") is True


# --------------------------------------------------------------------------------------
# The watchdog
# --------------------------------------------------------------------------------------


async def test_blocking_call_stalls_the_loop(
    svc: LedgerService = Depends(ledger),
    acct: str = Depends(account),
) -> None:
    """`balance_blocking` is deliberately wrong: a plain synchronous method that, called from a
    coroutine, runs *on the event loop thread*. While sqlite is busy every other in-flight test is
    frozen — no timeout fires, no exception is raised, and the only symptom is that the suite got
    slower.

    The loop-starvation watchdog that would notice this from outside — a daemon thread watching a
    heartbeat, capturing every task stack plus a `faulthandler` dump of every *thread* stack once
    the heartbeat goes stale, since the blocking frame lives in a thread stack, not a task stack —
    doesn't exist yet (`docs/M1-PLAN.md`; the `watchdog_threshold` config key this example's
    `pyproject.toml` would otherwise set is rejected outright, "no consumer before M2"). So this
    test doesn't demonstrate a diagnostic firing, only the underlying bug's own symptom: it still
    passes (`balance_blocking` returns the right number either way, it just does it rudely), and
    the "stall" is real but brief enough here — one small indexed lookup — that nothing else in
    this small suite times out waiting for the loop to come back. Diagnosing *this class* of bug
    is worth more than the test: the same call in a request handler blocks the production server
    the same way, and nothing in pytest could have told you either.

    The correct version is one line different — and it is what `LedgerService.balance` already
    does.
    """
    await svc.append(acct, 999)

    blocking = svc.balance_blocking(acct)  # the bug a watchdog would report, once one exists
    correct = await asyncio.to_thread(svc.balance_blocking, acct)  # what it should have been

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


async def test_awaiting_actually_runs_the_coroutine() -> None:
    """A basic sanity check, not a demonstration of an enforced velox rule.

    The "forgot to await" bug is real — a coroutine returned instead of awaited is truthy, silently
    never runs, and would report green if nothing caught it — and I8 ("silent passes are bugs")
    names exactly this shape of failure as unacceptable. But *catching* it (failing a test that
    returns non-`None`, or one that logs an un-awaited-coroutine `RuntimeWarning`) isn't wired up
    yet: nothing in `_run.py` inspects a test's return value, and no warnings filter escalates
    `RuntimeWarning: coroutine ... was never awaited` to a failure (M1-PLAN.md). What's below is
    just confirming `asyncio.sleep(..., result=...)` behaves the ordinary way once you do await it.
    """
    result = await asyncio.sleep(0, result=42)
    assert result == 42
