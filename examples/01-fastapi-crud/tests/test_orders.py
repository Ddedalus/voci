"""Order endpoints — overriding one node of the dependency graph, and built-in fixtures."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path

import velox
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from velox import Depends
from velox import fastapi as velox_fastapi

from app.db import get_session
from app.main import app
from app.models import User
from app.settings import Settings
from tests.fixtures import PaymentSandbox, alice, api_client, payment_sandbox, session


async def test_create_order(
    user: User = Depends(alice),
    client: AsyncClient = Depends(api_client),
) -> None:
    response = await client.post(f"/users/{user.id}/orders", json={"total_cents": 1250})

    assert response.status_code == 201
    assert response.json()["total_cents"] == 1250


async def _assert_order_rejected(client: AsyncClient, user: User, total_cents: int) -> None:
    response = await client.post(f"/users/{user.id}/orders", json={"total_cents": total_cents})
    assert response.status_code == 422


# `@velox.parametrize` would collapse these into one test -- declared public API, not yet expanded
# by the collector into records (see `tests/test_users.py`'s `test_create_user_bob` and neighbours
# for the same gap, and M1-PLAN.md for the tracked item).
async def test_create_order_rejects_zero_total(
    user: User = Depends(alice),
    client: AsyncClient = Depends(api_client),
) -> None:
    await _assert_order_rejected(client, user, 0)


async def test_create_order_rejects_negative_total(
    user: User = Depends(alice),
    client: AsyncClient = Depends(api_client),
) -> None:
    await _assert_order_rejected(client, user, -1)


async def test_create_order_rejects_very_negative_total(
    user: User = Depends(alice),
    client: AsyncClient = Depends(api_client),
) -> None:
    await _assert_order_rejected(client, user, -9999)


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


@velox.fixture()
def premium_settings() -> Settings:
    return Settings(database_url="unused", signup_bonus_cents=5_000, max_orders_per_user=2)


@velox.fixture()
async def premium_client(
    session: AsyncSession = Depends(session),
    settings: Settings = Depends(premium_settings),
) -> AsyncIterator[AsyncClient]:
    """`api_client`, rebuilt with `premium_settings` in place of the default.

    Per-node override of an existing fixture — deriving this from `api_client` by writing
    `api_client.with_(settings=premium_settings)` — is roadmap (spec/01 §10). `Fixture.with_()`
    was prototyped and pulled before the runtime landed: it returns a fresh, identity-keyed
    `Fixture` on every call, which is fine at function scope but breaks module/session-scope
    caching (two callers of `.with_()` with equal overrides get two distinct instances of what
    should be one shared resource). Until that is resolved, the replacement idiom is this: a
    sibling fixture, built exactly like `api_client`, with one dependency swapped by hand.

    Everything else in the graph — the session, the engine, the app itself — is still shared with
    every other test. There is no patching and no override registry; the substitution is an
    ordinary fixture in the static graph, so the scheduler and the validator both see the truth.

    The tests below run against the same singleton `app` as every other test in this suite, and
    read `settings.max_orders_per_user == 2` while their neighbours read the default. That is the
    layering in `velox.fastapi.client` doing its job: the substituted value reaches `app.state`
    for this test's context and no other.
    """
    async with velox_fastapi.client(
        app,
        overrides={get_session: lambda: session},
        state={"settings": settings},
    ) as client:
        yield client


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
    """The same `premium_client` fixture, reused. Fixtures are values."""
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

    `logs.messages` is `record.getMessage()` already applied — velox's handler never formats a
    record onto a stream the way pytest's does, so the raw `logging.LogRecord`s in `logs.records`
    never get a `.message` attribute set on them. Read `.records` for level/name/exc_info; read
    `.messages` for text.
    """
    with logs.set_level(logging.WARNING, logger="app"):
        await client.post("/users", json={"email": user.email})

    assert any("already registered" in message for message in logs.messages)


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
