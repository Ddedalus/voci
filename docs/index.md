---
# Zensical treats the homepage's H1 as its tab title too, so without this it renders "voci - voci"
# (`page.title - config.site_name`). This front-matter title takes precedence and reads as "Home -
# voci" instead.
title: Home
---

# voci

<p align="center">
  <img src="static/voci-logo-2.svg" alt="" width="160">
</p>

**A concurrent test runner for async Python.** voci runs your whole suite on one event loop, with every test as a concurrent `asyncio` task. For a suite that spends its time waiting — on a database, an ASGI app or a network stub, you can get substantial speedup, within a single process.

Tests declare what they need via dependency injection, just like FastAPI routes. There is no `conftest.py` or name-based lookup. A fixture is a function you import, so "go to definition" works, renames are safe, and type-checking your fixtures comes naturally.

Assertions work exactly like in pytest — `assert a == b` still prints a useful diff in case of failure.

Here's how some fixtures and tests could look like:
```python
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
    """A transaction per test, rolled back so tests stay isolated."""
    async with engine.connect() as conn:
        transaction = await conn.begin()
        yield AsyncSession(bind=conn)
        await transaction.rollback()


async def test_create_user(db: Annotated[AsyncSession, Depends(session)]) -> None:
    await db.add(User(email="alice@example.com"))
    assert await db.scalar(select(func.count()).select_from(User)) == 1
```

And here's what a passing run looks like:
```console
$ voci
PASS  tests/test_users.py                        10 tests  Σ 3.84s
PASS  tests/test_orders.py                       12 tests  Σ 7.29s

22 tests · 22 passed · 1.14s wall (9.8x concurrency)
```

Run one after another, these tests would take 11.13 seconds. voci gets the same work done in 1.14 seconds by running them concurrently!

## Install

You only need Python 3.13+ - voci has no dependencies.

```bash
uv add voci --dev          # or: pip install voci
```

## Where to go next

- **[Guide](guide/index.md)** — install voci, write a first test, and work through fixtures,
  scopes, concurrency, marks and selection one concept at a time.
- **[How-to](how-to/index.md)** — short recipes for specific tasks, once the guide is behind you.
- **[Migrating](migrate/index.md)** — take an existing pytest suite through `voci-migrate`:
  extract, audit, convert, verify.
- **[Reference](reference/index.md)** — every public symbol, rendered from the source.
