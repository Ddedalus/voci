"""Fixtures shared by the whole suite.

velox has no `conftest.py`. This is an ordinary module and test files import from it by name, so
"go to definition" works, renames are safe, and a typo is an `ImportError` at collection rather
than a fixture-not-found at run time.

`from __future__ import annotations` below costs nothing: velox reads the injection plan from
`__defaults__` and never evaluates an annotation. The annotations are for you and your type
checker.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import velox
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from velox import Depends
from velox import fastapi as velox_fastapi

from app.db import get_session
from app.main import app
from app.models import User
from app.settings import Settings

# Engine construction and the transaction-per-test dance live next door, in `tests/database.py`:
# that part is SQLAlchemy's business rather than velox's.
from tests import database

# --------------------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------------------


@velox.fixture(scope="session")
async def engine(
    tmp: velox.TmpPathFactory = Depends(velox.tmp_path_factory),
) -> AsyncIterator[AsyncEngine]:
    """One engine for the entire run, shared by every concurrent test.

    Session-scoped fixtures are built once, on first use, under a single-flight guard: 200 tests
    starting at the same moment produce exactly one `create_async_engine` call.
    """
    async with database.engine_with_schema(database.url_for(tmp.mktemp("db"))) as e:
        yield e


@velox.fixture()
async def session(engine: AsyncEngine = Depends(engine)) -> AsyncIterator[AsyncSession]:
    """A real session inside a transaction that is always rolled back.

    Every test sees the real schema and none of its neighbours' writes, which is what makes
    hundreds of database tests safe to run at once against a single engine.
    """
    async with database.transaction(engine) as s:
        yield s


# --------------------------------------------------------------------------------------
# Application
# --------------------------------------------------------------------------------------


@velox.fixture()
def settings() -> Settings:
    return Settings(database_url="unused: the session is injected", signup_bonus_cents=0)


@velox.fixture()
async def api_client(
    session: AsyncSession = Depends(session),
    settings: Settings = Depends(settings),
) -> AsyncIterator[AsyncClient]:
    """An HTTP client speaking to `app`, the module-level singleton from `app/main.py`.

    `overrides` is `app.dependency_overrides` and `state` is `app.state`, with their usual
    meanings: an override maps a dependency to another *dependency callable*, hence
    `lambda: session`. Both are layered per test behind a `ContextVar`, so sixteen concurrent
    tests each get their own view of the one app — no locking to arrange, and no teardown
    `.clear()` to remember. The layer goes away when this fixture's `async with` exits, whether
    the test passed, failed, or raised halfway through.

    `ASGITransport` sends no lifespan scope, so `app`'s `lifespan` stays out of the way here. When
    startup builds something your tests need, depend on `velox.fastapi.lifespan(app)`, which runs
    it once for the whole session.
    """
    async with velox_fastapi.client(
        app,
        overrides={get_session: lambda: session},
        state={"settings": settings},
    ) as client:
        yield client


# --------------------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------------------


@velox.fixture()
async def alice(session: AsyncSession = Depends(session)) -> User:
    """A user who exists, for tests that need one to.

    A plain function rather than a generator, because the rollback in `session` is the teardown.
    Sync and async, generator and plain, are all fine as fixtures.
    """
    user = User(email="alice@example.com")
    session.add(user)
    await session.flush()
    return user


# --------------------------------------------------------------------------------------
# An externally-exclusive resource
# --------------------------------------------------------------------------------------


class PaymentSandbox:
    """Stand-in for a third-party sandbox account that only supports one session at a time."""

    def __init__(self) -> None:
        self.charges: list[int] = []

    async def charge(self, cents: int) -> str:
        await asyncio.sleep(0)
        self.charges.append(cents)
        return f"ch_{len(self.charges):04d}"


@velox.fixture(exclusive="payments-sandbox")
async def payment_sandbox() -> AsyncIterator[PaymentSandbox]:
    """The sandbox, held by one test at a time.

    `exclusive` is declared on the resource, so every test whose dependency graph reaches this
    fixture inherits the `payments-sandbox` token — nothing at the call site has to be annotated,
    and nothing can forget to be. See examples/03 for tests that hold two tokens at once.
    """
    sandbox = PaymentSandbox()
    try:
        yield sandbox
    finally:
        sandbox.charges.clear()
