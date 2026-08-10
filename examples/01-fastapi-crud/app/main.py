"""HTTP layer and the app.

Nothing in this file is arranged for the tests. `app` is a module-level singleton built at import
— the shape every FastAPI tutorial, deployment guide and `uvicorn app.main:app` command line
assumes — and the test suite consumes it exactly as it is. See `tests/fixtures.py::api_client`.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, EmailStr
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import get_session
from app.models import Order, User
from app.settings import Settings

logger = logging.getLogger("app")
router = APIRouter()


class UserIn(BaseModel):
    email: EmailStr


class UserOut(BaseModel):
    id: int
    email: str
    is_active: bool
    credit_cents: int


class OrderIn(BaseModel):
    total_cents: int


class OrderOut(BaseModel):
    id: int
    user_id: int
    total_cents: int


def _settings(request: Request) -> Settings:
    return request.app.state.settings


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/users", status_code=201)
async def create_user(
    payload: UserIn,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(_settings),
) -> UserOut:
    user = User(email=payload.email, credit_cents=settings.signup_bonus_cents)
    session.add(user)
    try:
        await session.flush()
    except IntegrityError as exc:
        # A failed flush leaves the session's transaction in a "pending rollback" state --
        # SQLAlchemy raises on the *next* operation against it, not this one, until this is
        # issued. In production `get_session` hands each request a fresh session (`db.py`), so
        # that next operation would always be a different session on a different request; in
        # tests, `api_client` overrides `get_session` with the one `session` fixture shared by
        # every request the test makes (`tests/fixtures.py`), so a second request in the same
        # test landing on this same session is exactly the case this rollback is for.
        await session.rollback()
        logger.warning("signup rejected: email already registered: %s", payload.email)
        raise HTTPException(status_code=409, detail="email already registered") from exc
    await session.commit()
    return UserOut.model_validate(user, from_attributes=True)


@router.get("/users/{user_id}")
async def get_user(user_id: int, session: AsyncSession = Depends(get_session)) -> UserOut:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    return UserOut.model_validate(user, from_attributes=True)


@router.delete("/users/{user_id}", status_code=204)
async def deactivate_user(user_id: int, session: AsyncSession = Depends(get_session)) -> None:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    user.is_active = False
    await session.commit()


@router.post("/users/{user_id}/orders", status_code=201)
async def create_order(
    user_id: int,
    payload: OrderIn,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(_settings),
) -> OrderOut:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="user is deactivated")
    if payload.total_cents <= 0:
        raise HTTPException(status_code=422, detail="total must be positive")

    count = await session.scalar(
        select(func.count()).select_from(Order).where(Order.user_id == user_id)
    )
    if (count or 0) >= settings.max_orders_per_user:
        raise HTTPException(status_code=429, detail="order limit reached")

    order = Order(user_id=user_id, total_cents=payload.total_cents)
    session.add(order)
    await session.flush()
    await session.commit()
    return OrderOut.model_validate(order, from_attributes=True)


@router.get("/users/{user_id}/orders")
async def list_orders(
    user_id: int,
    min_total: int = 0,
    session: AsyncSession = Depends(get_session),
) -> list[OrderOut]:
    stmt = (
        select(Order)
        .where(Order.user_id == user_id, Order.total_cents >= min_total)
        .order_by(Order.id)
    )
    rows = (await session.scalars(stmt)).all()
    return [OrderOut.model_validate(o, from_attributes=True) for o in rows]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Real startup: one engine, one sessionmaker, disposed on shutdown.

    The test suite never runs this. `httpx.ASGITransport` sends no lifespan scope, and building a
    real engine per test would be the wrong thing to do anyway — the tests override `get_session`
    outright, so nothing reads `app.state.sessionmaker`. An app whose startup *does* put something
    under test in place would depend on `velox.fastapi.lifespan(app)` instead.
    """
    settings: Settings = app.state.settings
    engine = create_async_engine(settings.database_url)
    app.state.sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield
    finally:
        await engine.dispose()


app = FastAPI(lifespan=lifespan)
app.state.settings = Settings.from_env(os.environ)
app.include_router(router)
