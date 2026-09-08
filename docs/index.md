# voci

**A concurrent test runner for async Python.** voci runs your whole suite in one process on one
event loop, with every test as a concurrent `asyncio` task. For a suite that spends its time
waiting — on a database, on an ASGI app, on a network stub — that turns wall-clock time from the
sum of your tests into roughly the slowest one, with a single connection pool and a single set of
imports behind it.

Tests declare what they need as parameter defaults, the way FastAPI routes do. There is no
`conftest.py` and no name-based lookup: a fixture is a function you import, so "go to definition"
works, renames are safe, and a typo is an `ImportError` at collection rather than a mystery at run
time. Assertions keep the introspection you already know — voci vendors pytest's assertion
rewriter, so `assert a == b` still prints a real diff.

```python
# tests/fixtures.py
from typing import Annotated

import voci
from voci import Depends


@voci.fixture(scope="session")
async def engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine("sqlite+aiosqlite:///./test.db")
    yield engine
    await engine.dispose()


@voci.fixture()
async def session(engine: Annotated[AsyncEngine, Depends(engine)]) -> AsyncIterator[AsyncSession]:
    """A transaction per test, always rolled back — so tests share one engine safely."""
    async with engine.connect() as conn:
        transaction = await conn.begin()
        yield AsyncSession(bind=conn)
        await transaction.rollback()


# tests/test_users.py
from typing import Annotated

from voci import Depends

from tests.fixtures import session


async def test_create_user(db: Annotated[AsyncSession, Depends(session)]) -> None:
    db.add(User(email="alice@example.com"))
    await db.flush()

    assert await db.scalar(select(func.count()).select_from(User)) == 1
```

```console
$ voci
PASS  tests/test_users.py                        10 tests  Σ 3.84s
PASS  tests/test_orders.py                       12 tests  Σ 7.29s

22 tests · 22 passed · 1.14s wall (9.8x concurrency)
```

The last line is the one to watch: the multiplier is what running concurrently bought you over the
sum of every test's own duration (each file's `Σ`, above).

## Install

Python 3.13+. The core package has no dependencies.

```bash
uv pip install voci          # or: pip install voci
```

## Where to go next

- **[Guide](guide/index.md)** — install voci, write a first test, and work through fixtures,
  scopes, concurrency, marks and selection one concept at a time.
- **[How-to](how-to/index.md)** — short recipes for specific tasks, once the guide is behind you.
- **[Migrating](migrate/index.md)** — take an existing pytest suite through `voci-migrate`:
  extract, audit, convert, verify.
- **[Reference](reference/index.md)** — every public symbol, rendered from the source.
