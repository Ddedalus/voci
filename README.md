# velox

**A concurrent test runner for async Python.** velox runs your whole suite in one process on one
event loop, with every test as a concurrent `asyncio` task. For a suite that spends its time
waiting — on a database, on an ASGI app, on a network stub — that turns wall-clock time from the
sum of your tests into roughly the slowest one, with a single connection pool and a single set of
imports behind it.

Tests declare what they need as parameter defaults, the way FastAPI routes do. There is no
`conftest.py` and no name-based lookup: a fixture is a function you import, so "go to definition"
works, renames are safe, and a typo is an `ImportError` at collection rather than a mystery at run
time. Assertions keep the introspection you already know — velox vendors pytest's assertion
rewriter, so `assert a == b` still prints a real diff.

```python
# tests/fixtures.py
import velox
from velox import Depends


@velox.fixture(scope="session")
async def engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine("sqlite+aiosqlite:///./test.db")
    yield engine
    await engine.dispose()


@velox.fixture()
async def session(engine: AsyncEngine = Depends(engine)) -> AsyncIterator[AsyncSession]:
    """A transaction per test, always rolled back — so tests share one engine safely."""
    async with engine.connect() as conn:
        transaction = await conn.begin()
        yield AsyncSession(bind=conn)
        await transaction.rollback()


# tests/test_users.py
from tests.fixtures import session


async def test_create_user(db: AsyncSession = Depends(session)) -> None:
    db.add(User(email="alice@example.com"))
    await db.flush()

    assert await db.scalar(select(func.count()).select_from(User)) == 1
```

```console
$ velox
PASS  tests/test_users.py                10 tests   Σ 3.84s
PASS  tests/test_orders.py               12 tests   Σ 7.29s
22 tests: 22 passed, 0 failed, 0 errored, 0 skipped, 0 collection error(s)

22 tests · 0 failed · 1.14s wall (Σ 11.13s, 9.8x concurrency)
```

The last line is the one to watch: `Σ` is what the suite would have cost serially, and the
multiplier is what the concurrency bought you.

## Install

Python 3.13+. The core package has no dependencies.

```bash
uv pip install velox-test          # or: pip install velox-test
```

## Configure

Everything is optional. velox reads `[tool.velox]` from the nearest `pyproject.toml`:

```toml
[tool.velox]
testpaths = ["tests"]
concurrency = 16                   # tests in flight at once
timeout = 60                       # per-test budget, in seconds
env = { ENVIRONMENT = "test" }

[tool.ruff.lint.flake8-bugbear]
# `Depends(...)` in a parameter default is the injection syntax, as it is for FastAPI.
# Without this line, B008 fires on every test you write.
extend-immutable-calls = ["velox.Depends"]
```

CLI flags win over config, which wins over the defaults:

```bash
velox                              # run everything
velox tests/test_users.py          # run one file
velox --concurrency 1              # exactly serial — the first debugging step
velox --timeout 5                  # per-test setup+call budget
velox -s                           # live, id-prefixed output instead of captured
```

## Where to go next

- **[examples/](examples/)** — three worked suites: a FastAPI + async SQLAlchemy CRUD API, a
  pure-async library, and shared resources under concurrency. Each is runnable.
- **[ROADMAP.md](ROADMAP.md)** — what isn't built yet, and what's next.
- **[docs/rationale.md](docs/rationale.md)** — why velox is shaped the way it is. Read this before
  changing anything load-bearing.

## Status

Pre-release, ahead of v0.1. The core is real and exercised by velox's own suite and the three
examples: discovery, dependency injection with four scopes and inverted teardown, concurrent
execution with per-test timeouts, capture, assertion introspection, and the reporter. Scheduling
around shared resources, the migration tool, and most of the CLI surface are still ahead — see
[ROADMAP.md](ROADMAP.md). The public API is not frozen yet.

## Trade-offs

velox is not pytest and does not aim to be compatible with it. There is no plugin ecosystem and no
hook system — dependency injection is the extension point. Adopting it means rewriting your fixture
wiring, and it requires Python 3.13+. Linux and macOS are supported; Windows is best-effort.
