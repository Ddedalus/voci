"""HTTP layer and the app factory."""

from __future__ import annotations

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
    settings: Settings = app.state.settings
    engine = create_async_engine(settings.database_url)
    app.state.sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield
    finally:
        await engine.dispose()


def create_app(settings: Settings) -> FastAPI:
    """Build an app instance.

    A *factory*, not a module-level `app`. Tests build one app per test and override
    `get_session` on it; `dependency_overrides` is per-instance state, so a shared module-level app
    would have concurrent tests clobbering each other's overrides — see spec/08 §3.
    """
    app = FastAPI(lifespan=lifespan)
    app.state.settings = settings
    app.include_router(router)
    return app
