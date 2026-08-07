"""Order endpoints — overriding one node of the dependency graph, and built-in fixtures."""

from __future__ import annotations

import logging
from pathlib import Path

import velox
from httpx import AsyncClient
from velox import Depends

from app.models import User
from app.settings import Settings
from tests.fixtures import PaymentSandbox, alice, api_client, payment_sandbox


async def test_create_order(
    user: User = Depends(alice),
    client: AsyncClient = Depends(api_client),
) -> None:
    response = await client.post(f"/users/{user.id}/orders", json={"total_cents": 1250})

    assert response.status_code == 201
    assert response.json()["total_cents"] == 1250


@velox.parametrize("total", [0, -1, -9999])
async def test_create_order_rejects_non_positive_total(
    total: int,
    user: User = Depends(alice),
    client: AsyncClient = Depends(api_client),
) -> None:
    response = await client.post(f"/users/{user.id}/orders", json={"total_cents": total})
    assert response.status_code == 422


async def test_list_orders_filters_by_minimum(
    user: User = Depends(alice),
    client: AsyncClient = Depends(api_client),
) -> None:
    for total in (500, 1500, 2500):
        await client.post(f"/users/{user.id}/orders", json={"total_cents": total})

    response = await client.get(f"/users/{user.id}/orders", params={"min_total": 1000})

    assert [o["total_cents"] for o in response.json()] == [1500, 2500]


# --------------------------------------------------------------------------------------
# Overriding one dependency for one test
# --------------------------------------------------------------------------------------


@velox.fixture
def premium_settings() -> Settings:
    return Settings(database_url="unused", signup_bonus_cents=5_000, max_orders_per_user=2)


premium_client = api_client.with_(settings=premium_settings)
"""`Fixture.with_()` returns a *derived* fixture with one dependency replaced by name.

`api_client` takes a `settings` parameter; this swaps it. Everything else in the graph — the
session, the engine — is untouched and still shared. There is no patching, no registry override,
and no ordering hazard, because the substitution is part of the static graph: the scheduler and the
validator both see the truth.

Bound to a name rather than written inline in the `Depends(...)`. Both work, but the inline form
trips ruff's B008 with no qualified name to whitelist — and a named derived fixture reads better
and can be shared by a group of tests, as it is below.
"""


async def test_premium_signup_grants_credit(
    client: AsyncClient = Depends(premium_client),
) -> None:
    response = await client.post("/users", json={"email": "frank@example.com"})

    assert response.status_code == 201
    assert response.json()["credit_cents"] == 5_000


async def test_order_limit_is_enforced(
    user: User = Depends(alice),
    client: AsyncClient = Depends(premium_client),
) -> None:
    """The same derived fixture, reused. `with_()` results are values."""
    url = f"/users/{user.id}/orders"
    for _ in range(2):
        assert (await client.post(url, json={"total_cents": 100})).status_code == 201

    limited = await client.post(url, json={"total_cents": 100})

    assert limited.status_code == 429


# --------------------------------------------------------------------------------------
# Exceptions, floats, logs, temp files, and knowing your own name
# --------------------------------------------------------------------------------------


def test_settings_reject_a_non_numeric_bonus() -> None:
    """`velox.raises` is pytest's, verbatim, including `match=`.

    Also a `def`, not an `async def`. Sync tests are supported: velox runs them on a
    context-propagating executor thread so they cannot block the loop, at the cost of holding a
    concurrency slot while they do. Pure-function tests like this one are exactly the case for it.
    """
    with velox.raises(ValueError, match="invalid literal for int"):
        Settings.from_env({"SIGNUP_BONUS_CENTS": "five hundred"})


async def test_order_totals_convert_to_currency(
    user: User = Depends(alice),
    client: AsyncClient = Depends(api_client),
) -> None:
    await client.post(f"/users/{user.id}/orders", json={"total_cents": 1999})
    orders = (await client.get(f"/users/{user.id}/orders")).json()

    assert orders[0]["total_cents"] / 100 == velox.approx(19.99)


async def test_duplicate_email_is_logged(
    user: User = Depends(alice),
    client: AsyncClient = Depends(api_client),
    logs: velox.LogRecords = Depends(velox.log_records),
) -> None:
    """`velox.log_records` is `caplog`, with the records captured per test.

    Attribution is by ContextVar rather than by a global handler swap, so sixteen concurrent tests
    each see only their own records — including records emitted from `asyncio.to_thread` calls,
    which inherit the context.
    """
    with logs.set_level(logging.WARNING, logger="app"):
        await client.post("/users", json={"email": user.email})

    assert any("already registered" in r.message for r in logs.records)


async def test_export_orders_to_disk(
    user: User = Depends(alice),
    client: AsyncClient = Depends(api_client),
    tmp: Path = Depends(velox.tmp_path),
    info: velox.TestInfo = Depends(velox.test_info),
) -> None:
    """`tmp_path` is unique by construction: `basetemp/<sanitized-test-id>`.

    No scan-and-retry for a free numbered directory — that is a serial-era artifact, and it is a
    race under concurrency. `velox.test_info` is the read-only `request` replacement.
    """
    await client.post(f"/users/{user.id}/orders", json={"total_cents": 4200})
    orders = (await client.get(f"/users/{user.id}/orders")).json()

    target = tmp / "orders.csv"
    target.write_text("\n".join(f"{o['id']},{o['total_cents']}" for o in orders))

    assert target.read_text().endswith(",4200")
    assert info.id.endswith("::test_export_orders_to_disk")


# --------------------------------------------------------------------------------------
# An exclusive resource
# --------------------------------------------------------------------------------------


async def test_order_charges_the_sandbox(
    user: User = Depends(alice),
    client: AsyncClient = Depends(api_client),
    sandbox: PaymentSandbox = Depends(payment_sandbox),
) -> None:
    """This test inherits the `payments-sandbox` token from the fixture.

    So does `test_refund_releases_the_sandbox` below. The two never overlap; both still run
    concurrently with every other test in the suite, which is the difference between an exclusive
    token and `@velox.solo`.
    """
    await client.post(f"/users/{user.id}/orders", json={"total_cents": 700})
    charge_id = await sandbox.charge(700)

    assert charge_id == "ch_0001"
    assert sandbox.charges == [700]


async def test_refund_releases_the_sandbox(
    sandbox: PaymentSandbox = Depends(payment_sandbox),
) -> None:
    await sandbox.charge(100)
    await sandbox.charge(200)

    assert sum(sandbox.charges) == 300
