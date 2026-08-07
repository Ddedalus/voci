"""User endpoints.

The baseline shape of a velox test: an `async def`, dependencies as parameter defaults, a plain
`assert`.
"""

from __future__ import annotations

import velox
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from velox import Depends

from app.models import User
from tests.fixtures import alice, api_client, session


async def test_health(client: AsyncClient = Depends(api_client)) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@velox.parametrize("email", ["bob@example.com", "carol+tag@example.com", "dave@sub.example.com"])
async def test_create_user(email: str, client: AsyncClient = Depends(api_client)) -> None:
    """Parametrized values are ordinary parameters; injected ones have a `Depends` default.

    velox tells them apart by looking at the defaults, so the two mix in any order and there is
    no `indirect=` to reason about.

    Ids: `tests/test_users.py::test_create_user[bob@example.com]`, and so on — generated from the
    value, stable across runs, and copy-pasteable back onto the command line.
    """
    response = await client.post("/users", json={"email": email})

    assert response.status_code == 201
    assert response.json()["email"] == email
    assert response.json()["is_active"] is True


async def test_create_user_rejects_duplicate_email(
    user: User = Depends(alice),
    client: AsyncClient = Depends(api_client),
) -> None:
    """Two fixtures, one shared instance.

    `alice` depends on `session`, and so does `api_client`. Both get the *same* `AsyncSession`:
    within one test, a function-scoped fixture is constructed exactly once no matter how many
    paths reach it (spec/04 §4). So the user this test created through the ORM is visible to the
    request it makes over HTTP.
    """
    response = await client.post("/users", json={"email": user.email})

    assert response.status_code == 409
    assert response.json()["detail"] == "email already registered"


async def test_get_missing_user_is_404(client: AsyncClient = Depends(api_client)) -> None:
    response = await client.get("/users/999999")
    assert response.status_code == 404


async def test_create_user_writes_a_row(
    client: AsyncClient = Depends(api_client),
    db: AsyncSession = Depends(session),
) -> None:
    """Drop below HTTP when the assertion is about persistence, not about the API."""
    await client.post("/users", json={"email": "erin@example.com"})

    row = await db.scalar(select(User).where(User.email == "erin@example.com"))

    assert row is not None
    assert row.credit_cents == 0


@velox.tag("slow")
@velox.timeout(30)
async def test_bulk_signup(client: AsyncClient = Depends(api_client)) -> None:
    """`@velox.tag` is the `@pytest.mark.<name>` replacement for the selection use case:
    `velox -m "not slow"`. `@velox.timeout` overrides the per-test default (300s) for this test
    only.
    """
    for i in range(200):
        response = await client.post("/users", json={"email": f"user{i}@example.com"})
        assert response.status_code == 201

    listing = await client.get("/users/1")
    assert listing.status_code == 200


@velox.xfail("pagination is not implemented yet", strict=True)
async def test_list_users_is_paginated(client: AsyncClient = Depends(api_client)) -> None:
    """`strict=True` means this failing is expected but *passing* is a failure.

    That is the property that makes xfail a to-do list rather than a graveyard: the day someone
    implements pagination, this test turns red and tells them to delete the marker.
    """
    response = await client.get("/users?limit=10")
    assert response.status_code == 200


REQUEST_ID_MIDDLEWARE_ENABLED = False


@velox.skipif(not REQUEST_ID_MIDDLEWARE_ENABLED, reason="middleware is behind a feature flag")
async def test_response_carries_request_id(client: AsyncClient = Depends(api_client)) -> None:
    """`skipif` conditions are evaluated at run time, not at collection.

    Same as pytest, and deliberately so: a collection-time evaluation would put arbitrary user
    code in the startup budget.
    """
    response = await client.get("/health")
    assert "x-request-id" in response.headers


class TestDeactivation:
    """Classes are pure namespacing.

    No `__init__`, no `setup_method`, no shared instance state — velox instantiates the class per
    test and ignores `self`. If you want setup, that is what a function-scoped fixture is. The ids
    read `tests/test_users.py::TestDeactivation::test_deactivate`.
    """

    async def test_deactivate(
        self,
        user: User = Depends(alice),
        client: AsyncClient = Depends(api_client),
    ) -> None:
        response = await client.delete(f"/users/{user.id}")
        assert response.status_code == 204

        after = await client.get(f"/users/{user.id}")
        assert after.json()["is_active"] is False

    async def test_deactivate_is_idempotent(
        self,
        user: User = Depends(alice),
        client: AsyncClient = Depends(api_client),
    ) -> None:
        assert (await client.delete(f"/users/{user.id}")).status_code == 204
        assert (await client.delete(f"/users/{user.id}")).status_code == 204
