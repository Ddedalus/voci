"""User endpoints.

The baseline shape of a velox test: an `async def`, dependencies as parameter defaults, a plain
`assert`.
"""

from __future__ import annotations

from typing import Annotated

from app.models import User
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.fixtures import alice, api_client, session

import velox
from velox import Depends


async def test_health(client: Annotated[AsyncClient, Depends(api_client)]) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@velox.parametrize(
    "email",
    ["bob@example.com", "carol+tag@example.com", "dave@sub.example.com"],
    ids=["bob", "tag-in-local-part", "subdomain"],
)
async def test_create_user(email: str, client: Annotated[AsyncClient, Depends(api_client)]) -> None:
    response = await client.post("/users", json={"email": email})

    assert response.status_code == 201
    assert response.json()["email"] == email
    assert response.json()["is_active"] is True


async def test_create_user_rejects_duplicate_email(
    user: Annotated[User, Depends(alice)],
    client: Annotated[AsyncClient, Depends(api_client)],
) -> None:
    """Two fixtures, one shared session.

    `alice` and `api_client` both depend on `session`, and within a test a fixture is built
    exactly once however many paths reach it — so the user `alice` wrote through the ORM is
    there for the request this test makes over HTTP.
    """
    response = await client.post("/users", json={"email": user.email})

    assert response.status_code == 409
    assert response.json()["detail"] == "email already registered"


async def test_get_missing_user_is_404(client: Annotated[AsyncClient, Depends(api_client)]) -> None:
    response = await client.get("/users/999999")
    assert response.status_code == 404


async def test_create_user_writes_a_row(
    client: Annotated[AsyncClient, Depends(api_client)],
    db: Annotated[AsyncSession, Depends(session)],
) -> None:
    """Drop below HTTP when the assertion is about persistence, not about the API."""
    await client.post("/users", json={"email": "erin@example.com"})

    row = await db.scalar(select(User).where(User.email == "erin@example.com"))

    assert row is not None
    assert row.credit_cents == 0


@velox.tag("slow")
@velox.timeout(30)
async def test_bulk_signup(client: Annotated[AsyncClient, Depends(api_client)]) -> None:
    for i in range(200):
        response = await client.post("/users", json={"email": f"user{i}@example.com"})
        assert response.status_code == 201

    listing = await client.get("/users/1")
    assert listing.status_code == 200


@velox.skip("pagination is not implemented yet (GET /users has no route -- 405, not 200)")
async def test_list_users_is_paginated(client: Annotated[AsyncClient, Depends(api_client)]) -> None:
    response = await client.get("/users?limit=10")
    assert response.status_code == 200


REQUEST_ID_MIDDLEWARE_ENABLED = False


@velox.skipif(not REQUEST_ID_MIDDLEWARE_ENABLED, reason="middleware is behind a feature flag")
async def test_response_carries_request_id(
    client: Annotated[AsyncClient, Depends(api_client)],
) -> None:
    """`skipif` conditions are evaluated once per test at collection, before any test runs."""
    response = await client.get("/health")
    assert "x-request-id" in response.headers


async def test_deactivate(
    user: Annotated[User, Depends(alice)],
    client: Annotated[AsyncClient, Depends(api_client)],
) -> None:
    response = await client.delete(f"/users/{user.id}")
    assert response.status_code == 204

    after = await client.get(f"/users/{user.id}")
    assert after.json()["is_active"] is False


async def test_deactivate_is_idempotent(
    user: Annotated[User, Depends(alice)],
    client: Annotated[AsyncClient, Depends(api_client)],
) -> None:
    assert (await client.delete(f"/users/{user.id}")).status_code == 204
    assert (await client.delete(f"/users/{user.id}")).status_code == 204
