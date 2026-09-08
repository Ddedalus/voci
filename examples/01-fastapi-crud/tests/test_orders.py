"""Order endpoints — overriding one node of the dependency graph, and built-in fixtures."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated

import voci
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from voci import Depends
from voci import fastapi as voci_fastapi

from app.db import get_session
from app.main import app
from app.models import User
from app.settings import Settings
from tests.fixtures import PaymentSandbox, alice, api_client, payment_sandbox, session


async def test_create_order(
    user: Annotated[User, Depends(alice)],
    client: Annotated[AsyncClient, Depends(api_client)],
) -> None:
    response = await client.post(f"/users/{user.id}/orders", json={"total_cents": 1250})

    assert response.status_code == 201
    assert response.json()["total_cents"] == 1250


async def _assert_order_rejected(client: AsyncClient, user: User, total_cents: int) -> None:
    response = await client.post(f"/users/{user.id}/orders", json={"total_cents": total_cents})
    assert response.status_code == 422


async def test_create_order_rejects_zero_total(
    user: Annotated[User, Depends(alice)],
    client: Annotated[AsyncClient, Depends(api_client)],
) -> None:
    await _assert_order_rejected(client, user, 0)


async def test_create_order_rejects_negative_total(
    user: Annotated[User, Depends(alice)],
    client: Annotated[AsyncClient, Depends(api_client)],
) -> None:
    await _assert_order_rejected(client, user, -1)


async def test_create_order_rejects_very_negative_total(
    user: Annotated[User, Depends(alice)],
    client: Annotated[AsyncClient, Depends(api_client)],
) -> None:
    await _assert_order_rejected(client, user, -9999)


async def test_list_orders_filters_by_minimum(
    user: Annotated[User, Depends(alice)],
    client: Annotated[AsyncClient, Depends(api_client)],
) -> None:
    for total in (500, 1500, 2500):
        await client.post(f"/users/{user.id}/orders", json={"total_cents": total})

    response = await client.get(f"/users/{user.id}/orders", params={"min_total": 1000})

    assert [o["total_cents"] for o in response.json()] == [1500, 2500]


# --------------------------------------------------------------------------------------
# Overriding one dependency for one test
# --------------------------------------------------------------------------------------


@voci.fixture()
def premium_settings() -> Settings:
    return Settings(database_url="unused", signup_bonus_cents=5_000, max_orders_per_user=2)


@voci.fixture()
async def premium_client(
    session: Annotated[AsyncSession, Depends(session)],
    settings: Annotated[Settings, Depends(premium_settings)],
) -> AsyncIterator[AsyncClient]:
    """`api_client`, rebuilt with `premium_settings` in place of the default.

    A sibling fixture with one dependency swapped by hand; the session, the engine and the app
    are still the ones everybody else uses. The tests below read `max_orders_per_user == 2` while
    their neighbours, running at the same moment against the same `app`, read the default.
    """
    async with voci_fastapi.client(
        app,
        overrides={get_session: lambda: session},
        state={"settings": settings},
    ) as client:
        yield client


async def test_premium_signup_grants_credit(
    client: Annotated[AsyncClient, Depends(premium_client)],
) -> None:
    response = await client.post("/users", json={"email": "frank@example.com"})

    assert response.status_code == 201
    assert response.json()["credit_cents"] == 5_000


async def test_order_limit_is_enforced(
    user: Annotated[User, Depends(alice)],
    client: Annotated[AsyncClient, Depends(premium_client)],
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
    """`voci.raises`, with `match=` as a regex against the message.

    A `def`, not an `async def`: voci runs sync tests on a context-propagating executor thread,
    where they hold a concurrency slot but cannot block the loop.
    """
    with voci.raises(ValueError, match="invalid literal for int"):
        Settings.from_env({"SIGNUP_BONUS_CENTS": "five hundred"})


async def test_order_totals_convert_to_currency(
    user: Annotated[User, Depends(alice)],
    client: Annotated[AsyncClient, Depends(api_client)],
) -> None:
    await client.post(f"/users/{user.id}/orders", json={"total_cents": 1999})
    orders = (await client.get(f"/users/{user.id}/orders")).json()

    assert orders[0]["total_cents"] / 100 == voci.approx(19.99)


async def test_duplicate_email_is_logged(
    user: Annotated[User, Depends(alice)],
    client: Annotated[AsyncClient, Depends(api_client)],
    logs: Annotated[voci.LogRecords, Depends(voci.log_records)],
) -> None:
    """Log records captured for this test alone.

    Attribution is by `ContextVar`, so sixteen concurrent tests each see only their own records,
    including those emitted from `asyncio.to_thread` calls. Read `.messages` for formatted text
    and `.records` for level, logger name and `exc_info`.
    """
    with logs.set_level(logging.WARNING, logger="app"):
        await client.post("/users", json={"email": user.email})

    assert any("already registered" in message for message in logs.messages)


async def test_export_orders_to_disk(
    user: Annotated[User, Depends(alice)],
    client: Annotated[AsyncClient, Depends(api_client)],
    tmp: Annotated[Path, Depends(voci.tmp_path)],
    info: Annotated[voci.TestInfo, Depends(voci.test_info)],
) -> None:
    """`tmp_path` is unique by construction: `basetemp/<sanitized-test-id>`.

    `voci.test_info` carries this test's id, tags, timeout budget and concurrency slot, and is
    read-only.
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
    user: Annotated[User, Depends(alice)],
    client: Annotated[AsyncClient, Depends(api_client)],
    sandbox: Annotated[PaymentSandbox, Depends(payment_sandbox)],
) -> None:
    """A test that inherits the `payments-sandbox` token from the fixture.

    So does `test_refund_releases_the_sandbox` below, and the two never overlap — while both still
    run alongside every other test in the suite, which is the difference between an exclusive
    token and `@voci.solo`.
    """
    await client.post(f"/users/{user.id}/orders", json={"total_cents": 700})
    charge_id = await sandbox.charge(700)

    assert charge_id == "ch_0001"
    assert sandbox.charges == [700]


async def test_refund_releases_the_sandbox(
    sandbox: Annotated[PaymentSandbox, Depends(payment_sandbox)],
) -> None:
    await sandbox.charge(100)
    await sandbox.charge(200)

    assert sum(sandbox.charges) == 300
