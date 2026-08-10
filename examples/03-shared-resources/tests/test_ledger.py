"""The bulk of the suite: no tokens, no solo, 32 tests in flight.

Every test here shares one database and one connection path, and none of them conflict, because
each owns an account namespace derived from its own test id. Isolating by data instead of by
exclusion is the cheapest concurrency you will ever buy.
"""

from __future__ import annotations

import asyncio

import velox
from velox import Depends

from ledger.service import LedgerService
from tests.fixtures import account, ledger


async def test_append_returns_an_id(
    svc: LedgerService = Depends(ledger),
    acct: str = Depends(account),
) -> None:
    entry_id = await svc.append(acct, 1000)
    assert entry_id > 0


async def test_balance_sums_entries(
    svc: LedgerService = Depends(ledger),
    acct: str = Depends(account),
) -> None:
    await svc.append(acct, 1000)
    await svc.append(acct, -250)
    await svc.append(acct, 75)

    assert await svc.balance(acct) == 825


async def test_balance_of_unknown_account_is_zero(
    svc: LedgerService = Depends(ledger),
) -> None:
    assert await svc.balance("acct::nobody") == 0


async def _assert_balance_arithmetic(
    amounts: list[int], expected: int, svc: LedgerService, acct: str
) -> None:
    for amount in amounts:
        await svc.append(acct, amount)

    assert await svc.balance(acct) == expected


# Each case is its own test, and each still gets its own `account`: `velox.test_info.id` includes
# the function's own qualname, so distinct functions can't collide even though all four run at once.
async def test_balance_arithmetic_single_deposit(
    svc: LedgerService = Depends(ledger),
    acct: str = Depends(account),
) -> None:
    await _assert_balance_arithmetic([1], 1, svc, acct)


async def test_balance_arithmetic_multiple_deposits(
    svc: LedgerService = Depends(ledger),
    acct: str = Depends(account),
) -> None:
    await _assert_balance_arithmetic([1, 2, 3], 6, svc, acct)


async def test_balance_arithmetic_deposit_and_withdrawal_cancel_out(
    svc: LedgerService = Depends(ledger),
    acct: str = Depends(account),
) -> None:
    await _assert_balance_arithmetic([-5, 5], 0, svc, acct)


async def test_balance_arithmetic_no_entries(
    svc: LedgerService = Depends(ledger),
    acct: str = Depends(account),
) -> None:
    await _assert_balance_arithmetic([], 0, svc, acct)


async def test_entries_are_ordered(
    svc: LedgerService = Depends(ledger),
    acct: str = Depends(account),
) -> None:
    for amount in (10, 20, 30):
        await svc.append(acct, amount)

    assert [e.cents for e in await svc.entries(acct)] == [10, 20, 30]


async def test_transfer_moves_money(
    svc: LedgerService = Depends(ledger),
    acct: str = Depends(account),
) -> None:
    source, target = f"{acct}::a", f"{acct}::b"
    await svc.append(source, 500)

    await svc.transfer(source, target, 200)

    assert await svc.balance(source) == 300
    assert await svc.balance(target) == 200


async def test_transfer_is_permitted_to_overdraw_by_default(
    svc: LedgerService = Depends(ledger),
    acct: str = Depends(account),
) -> None:
    """`strict_transfers` is off. The test that turns it on is in `test_safety.py`, and it is solo
    — because it changes the answer this test depends on.
    """
    source, target = f"{acct}::a", f"{acct}::b"

    await svc.transfer(source, target, 1_000_000)

    assert await svc.balance(source) == -1_000_000


@velox.timeout(20)
async def test_many_concurrent_appends(
    svc: LedgerService = Depends(ledger),
    acct: str = Depends(account),
) -> None:
    """The test itself is concurrent, inside a test that is one of 32 running concurrently.

    Every `append` hops to a thread from the context-propagating default executor, so captured
    output, log records, and (later) patch overrides stay attributed to this test even though the
    work happens off the loop.
    """
    async with asyncio.TaskGroup() as tg:
        for i in range(50):
            tg.create_task(svc.append(acct, i))

    assert await svc.balance(acct) == sum(range(50))
