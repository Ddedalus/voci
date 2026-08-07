"""Project-wide fixtures.

velox has no `conftest.py`. This is an ordinary module; test files import from it by name, so
"go to definition" works, renames are safe, and a typo is an `ImportError` at collection rather
than a fixture-not-found at run time.

Note the `from __future__ import annotations` below: velox reads the injection plan from
`__defaults__` only and never evaluates an annotation, so PEP 563 costs nothing here. The
annotations are for you and your type checker.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import velox
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from velox import Depends

from app.db import get_session
from app.main import create_app
from app.models import Base, User
from app.settings import Settings

# --------------------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------------------


@velox.fixture(scope="session")
def database_url(tmp: velox.TmpPathFactory = Depends(velox.tmp_path_factory)) -> str:
    # For a suite you actually care about the wall clock of, point this at Postgres:
    #     return "postgresql+asyncpg://velox:velox@localhost/velox_test"
    # SQLite serialises writers, so it caps the concurrency win at the storage layer.
    return f"sqlite+aiosqlite:///{tmp.mktemp('db') / 'app.sqlite'}"


@velox.fixture(scope="session")
async def engine(url: str = Depends(database_url)) -> AsyncIterator[AsyncEngine]:
    """One engine for the entire run.

    This fixture is the head-to-head argument. Under `pytest-xdist -n 4` this is four engines in
    four processes, each with its own pool, each paying the schema setup. Here it is constructed
    once, on first use, and every concurrent test shares it — the single-flight guarantee in
    spec/04 means 200 tests starting at once produce exactly one `create_async_engine` call.
    """
    e = create_async_engine(url)
    async with e.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield e
    finally:
        await e.dispose()


@velox.fixture
async def session(engine: AsyncEngine = Depends(engine)) -> AsyncIterator[AsyncSession]:
    """A real session inside a transaction that is always rolled back.

    Every test sees the real schema and none of its neighbours' writes, which is what makes
    hundreds of database tests safe to run concurrently against one engine. Note that the test's
    own `session.commit()` calls commit the *nested* transaction, not this one.
    """
    async with engine.connect() as conn:
        trans = await conn.begin()
        async with AsyncSession(
            bind=conn,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        ) as s:
            yield s
        await trans.rollback()


# --------------------------------------------------------------------------------------
# Application
# --------------------------------------------------------------------------------------


@velox.fixture
def settings() -> Settings:
    return Settings(database_url="unused: the session is injected", signup_bonus_cents=0)


@velox.fixture
async def api_client(
    session: AsyncSession = Depends(session),
    settings: Settings = Depends(settings),
) -> AsyncIterator[AsyncClient]:
    """An HTTP client bound to a fresh app instance.

    Fresh *per test*, deliberately. `app.dependency_overrides` is per-app-instance state; a
    module-level app shared by sixteen concurrent tests would have them overwriting each other's
    overrides and never notice. Building the app costs microseconds — see spec/08 §3.

    Because the app is per-test, there is nothing to clean up: no `dependency_overrides.clear()`,
    no ordering hazard if a test fails partway.
    """
    app = create_app(settings)
    app.dependency_overrides[get_session] = lambda: session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


# --------------------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------------------


@velox.fixture
async def alice(session: AsyncSession = Depends(session)) -> User:
    """A committed-enough user for tests that need one to exist.

    Not a generator: there is no teardown, because the enclosing transaction rollback is the
    teardown. Sync and async plain functions are both fine as fixtures.
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
    """`exclusive` is declared on the *resource*, not on the tests that use it.

    Any test whose dependency graph transitively reaches this fixture inherits the
    `payments-sandbox` token, and the scheduler never runs two of them at the same time. Nothing
    has to be annotated at the call site, and nothing can forget to be.

    See examples/03 for the general case, including tests that hold two tokens at once.
    """
    sandbox = PaymentSandbox()
    try:
        yield sandbox
    finally:
        sandbox.charges.clear()
