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
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from velox import Depends
from velox import fastapi as velox_fastapi

from app.db import get_session
from app.main import app
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


@velox.fixture()
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


@velox.fixture()
def settings() -> Settings:
    return Settings(database_url="unused: the session is injected", signup_bonus_cents=0)


@velox.fixture()
async def api_client(
    session: AsyncSession = Depends(session),
    settings: Settings = Depends(settings),
) -> AsyncIterator[AsyncClient]:
    """An HTTP client speaking to `app` — the real one, the module-level singleton.

    Not a copy, not a factory call, not a per-test rebuild. `app/main.py` is written the way every
    FastAPI deployment guide writes it, and the tests take it as it is.

    Read the two mappings below and compare them to what you already hand-roll from the FastAPI
    testing docs: `app.dependency_overrides[get_session] = lambda: session`. Character for
    character the same substitution, with the same semantics — the value is a *dependency
    callable*, which is why the session is passed as `lambda: session`.

    What is gone is the part the docs cannot help with. `dependency_overrides` and `state` are
    per-app-instance dicts, so sixteen concurrent tests writing them are sixteen tests writing one
    dict, and the `.clear()` those docs put in teardown wipes the fifteen that are still running.
    velox routes both through a `ContextVar` keyed to this test's context (`velox/fastapi.py`), so
    the writes cannot collide and the reset is unnecessary — the layer goes away when this fixture's
    `async with` exits, whether the test passed, failed, or raised halfway through.

    Note what did *not* have to happen for that: no change to `app/main.py`, no factory, no
    `create_app(settings)` that exists only because the tests asked for it.

    The app's `lifespan` does not run here — `ASGITransport` sends no lifespan scope — so no real
    engine is created and `app.state.sessionmaker` stays unset. Nothing reads it, because
    `get_session` is overridden above. For an app whose startup builds something the tests need,
    depend on `velox.fastapi.lifespan(app)`, a session-scoped fixture that runs it once per run.
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
