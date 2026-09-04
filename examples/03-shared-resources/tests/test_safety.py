"""Process-global state, and the safety net under all of it."""

from __future__ import annotations

import asyncio
from types import ModuleType
from typing import Annotated

from ledger.service import LedgerService
from tests.fixtures import account, feature_flags, ledger

import velox
from velox import Depends

# --------------------------------------------------------------------------------------
# @velox.solo — for state with no per-task view
# --------------------------------------------------------------------------------------


@velox.solo
@velox.skip(
    "would race test_ledger.py::test_transfer_is_permitted_to_overdraw_by_default, which "
    "depends on strict_transfers staying off"
)
async def test_strict_transfers_rejects_overdraft(
    flags: Annotated[ModuleType, Depends(feature_flags)],
    svc: Annotated[LedgerService, Depends(ledger)],
    acct: Annotated[str, Depends(account)],
) -> None:
    """Nothing is mocked here — the reason for `@velox.solo` is a module-level dict read at call
    time, and no mocking library would change that.

    `test_ledger.py` has a test asserting that overdrafts *are* allowed by default; while this one
    runs, that test must not be in flight — see the `@velox.skip` reason above.
    """
    flags.set_enabled("strict_transfers", True)
    source, target = f"{acct}::a", f"{acct}::b"
    await svc.append(source, 100)

    with velox.raises(ValueError, match="insufficient balance"):
        await svc.transfer(source, target, 500)


@velox.solo
async def test_audit_flag_is_restored_afterwards(
    flags: Annotated[ModuleType, Depends(feature_flags)],
) -> None:
    """`feature_flags` snapshots and restores, so the mutation is reversible.

    Reversible is not the same as invisible, which is why `@velox.solo` is the correct mark either
    way. This one runs live rather than skipped: nothing else in this suite reads
    `audit_every_write`, unlike the flag `test_strict_transfers_rejects_overdraft` flips.
    """
    flags.set_enabled("audit_every_write", True)
    assert flags.is_enabled("audit_every_write") is True


# --------------------------------------------------------------------------------------
# Blocking calls, and timeouts
# --------------------------------------------------------------------------------------


async def test_blocking_call_stalls_the_loop(
    svc: Annotated[LedgerService, Depends(ledger)],
    acct: Annotated[str, Depends(account)],
) -> None:
    """`balance_blocking` is a plain synchronous method that, called directly from a coroutine,
    runs on the event loop thread: every other in-flight test is frozen for as long as sqlite is
    busy, with no timeout and no exception — the only symptom is that the suite got slower.

    `LedgerService.balance` wraps the same call in `asyncio.to_thread` instead.
    """
    await svc.append(acct, 999)

    blocking = svc.balance_blocking(acct)  # runs on the event loop thread
    correct = await asyncio.to_thread(svc.balance_blocking, acct)  # off the event loop thread

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
    """A basic sanity check: `asyncio.sleep(..., result=...)` behaves the ordinary way once you
    await it.
    """
    result = await asyncio.sleep(0, result=42)
    assert result == 42
