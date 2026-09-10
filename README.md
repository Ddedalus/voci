<p align="center">
  <img src="docs/static/voci-logo-2.svg" alt="" width="160">
</p>

<p align="center">
  <b>voci: a concurrent test runner for async Python.</b>
</p>

<p align="center">
  <a href="https://pypi.org/project/voci/"><img src="https://img.shields.io/pypi/v/voci" alt="PyPI"></a>
  <a href="https://github.com/Ddedalus/voci/actions/workflows/ci.yml"><img src="https://github.com/Ddedalus/voci/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/Ddedalus/voci/actions/workflows/ci.yml"><img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/Ddedalus/voci/badges/coverage.json" alt="Coverage"></a>
  <img src="https://img.shields.io/badge/python-3.13%20%7C%203.14-blue" alt="Python 3.13 | 3.14">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="MIT License"></a>
  <a href="https://github.com/astral-sh/uv"><img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json" alt="uv"></a>
  <a href="https://github.com/astral-sh/ruff"><img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json" alt="Ruff"></a>
  <a href="https://github.com/facebook/pyrefly"><img src="https://img.shields.io/endpoint?url=https://pyrefly.org/badge.json" alt="Pyrefly"></a>
</p>

<p align="center">
  📖 <a href="https://ddedalus.github.io/voci/"><b>Documentation</b></a>
</p>

voci runs your whole suite in one process on one event loop, with every test as a concurrent
`asyncio` task. For a suite that spends its time waiting — on a database, on an ASGI app, on a
network stub — that turns wall-clock time from the sum of your tests into roughly the slowest one,
with a single connection pool and a single set of imports behind it.

Tests declare what they need as parameter defaults, the way FastAPI routes do. There is no
`conftest.py` and no name-based lookup: a fixture is a function you import, so "go to definition"
works, renames are safe, and a typo is an `ImportError` at collection rather than a mystery at run
time. When a fixture is a side effect no test needs the value of, a module — or a package, for
everything under it — declares it once: `voci.use(reset_cache)`, the way a FastAPI router
declares dependencies for every route on it. Assertions keep the introspection you already know —
voci vendors pytest's assertion rewriter, so `assert a == b` still prints a real diff.

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

The last line is the one to watch: the multiplier shows what running concurrently bought you over
the sum of every test's own duration (each file's `Σ`, above).

## Install

You only need Python 3.13+ - voci has no dependencies.

```bash
uv add voci --dev          # or: pip install voci
```

## Configure

Everything is optional. voci reads `[tool.voci]` from the nearest `pyproject.toml`:

```toml
[tool.voci]
testpaths = ["tests"]
concurrency = 16                   # tests in flight at once
timeout = 60                       # per-test budget, in seconds
env = { ENVIRONMENT = "test" }
```

CLI flags win over config, which wins over the defaults:

```bash
voci                              # run everything
voci tests/test_users.py          # run one file
voci tests/test_users.py::test_create[admin]   # run one test, or one of its cases
voci -k "users and not slow"      # select by substring of the test id
voci -m "smoke"                   # select by @voci.tag
voci --serial                     # exactly serial — the first debugging step
voci -x                           # stop at the first failure, cancelling what is in flight
voci --durations 10               # the slowest tests, to tune --concurrency by
voci --loop-watchdog 10           # how long the loop may block before voci names the call
voci --collect-only               # print the ids that would run, and stop
voci -s                           # live, id-prefixed output instead of captured
```

## Where to go next

- **[examples/](examples/)** — three worked suites: a FastAPI + async SQLAlchemy CRUD API, a
  pure-async library, and shared resources under concurrency. Each is runnable.
- **[ROADMAP.md](ROADMAP.md)** — what isn't built yet, and what's next.
- **[plans/rationale.md](plans/rationale.md)** — why voci is shaped the way it is. Read this before
  changing anything central to it.

## Status

Pre-release, ahead of v0.1. The core is real and exercised by voci's own suite and the three
examples: discovery, dependency injection with four scopes and inverted teardown, concurrent
execution with per-test timeouts, capture, assertion introspection, selection, and the reporter.
Fair scheduling around shared resources, the migration tool, and machine-readable reports are
still ahead — see [ROADMAP.md](ROADMAP.md). The public API is not frozen yet.

## Trade-offs

voci is not pytest and does not aim to be compatible with it. There is no plugin ecosystem and no
hook system — dependency injection is the extension point. Adopting it means rewriting your fixture
wiring, and it requires Python 3.13+. Linux and macOS are supported; Windows is best-effort.

`unittest.mock` keeps working, at a price: `mock.patch` writes to a module or class, which every
concurrently running test would see, so voci runs a patch-decorated test alone and reports what
that cost. A patch opened inside a test body fails that test unless it is marked `@voci.solo`.
[examples/02-async-library](examples/02-async-library/) walks the alternatives.
