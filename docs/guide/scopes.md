# Scopes

`@voci.fixture()`'s `scope` decides how widely one built instance is shared, from a fresh instance
per `Depends()` site up to one for the whole run:

```python
@voci.fixture()  # scope="function", the default
def settings() -> Settings: ...


@voci.fixture(scope="module")
async def app() -> AsyncIterator[FastAPI]: ...


@voci.fixture(scope="session")
async def engine() -> AsyncIterator[AsyncEngine]: ...


@voci.fixture(scope="call")
def request_id() -> str: ...
```

`"function"` is one instance per test — the fixture builds once even if several parameters or
transitive dependencies ask for it, and tears down when that test finishes. `"module"` widens the
same sharing to every test in one file; `"session"` widens it to the whole run. `"call"` narrows
it instead: a fresh instance for every `Depends(...)` site, so a test naming the same fixture
twice through two parameters gets two distinct values, each torn down at end of test regardless.

Declaring a dependency doesn't declare its scope — a `"function"`-scope fixture depending on a
`"session"`-scope one is enough to reach the shared instance; nothing about the dependent needs to
say so.

## Build order and teardown order

A test's fixtures build in dependency order — a fixture with `Depends(x)` builds after `x` — and
tear down in the exact reverse, so nothing releases a resource a still-live sibling depends on:

```python
@voci.fixture(scope="session")
async def engine() -> AsyncIterator[AsyncEngine]:
    e = create_async_engine(...)
    yield e
    await e.dispose()  # runs last: nothing else can still be using e


@voci.fixture()
async def session(engine: AsyncEngine = Depends(engine)) -> AsyncIterator[AsyncSession]:
    async with AsyncSession(engine) as s:
        yield s  # this teardown, and every test using `session`, runs before dispose()
```

A plain-return fixture has nothing to release, and a generator fixture releases whatever runs
after its `yield` — same as a `@contextlib.contextmanager`, but without needing the decorator.

## Sharing under concurrency

A fixture wider than `"function"` scope is shared by tests running at the same moment, so voci
builds it exactly once regardless of how many tests ask for it concurrently: whichever test
reaches it first constructs it, and every other test in the meantime waits on that same
construction rather than starting one of its own. 200 tests starting together against a
`scope="session"` engine produce exactly one `create_async_engine` call.

A `"module"` fixture's teardown waits for that module's own last test to finish, not for the
fixture's own use to end mid-run — two test files sharing a fixture at `"module"` scope get two
separate instances, one per file, each torn down once. A `"session"` fixture's teardown instead
waits for the whole run, and runs even for a fixture nothing built until the very last test asked
for it.

## Choosing a scope

`"function"` is the right default: a fresh instance means one test's mutation is never a
neighbour's problem, at the cost of building it every time. Widen to `"module"` or `"session"`
for something expensive to build and safe to share reads of the same instance
across tests — a database engine, an HTTP client's connection pool — and pair it with a
`"function"`-scope fixture underneath for whatever each test needs to see reset: see [Sharing a
database engine](../how-to/sharing-a-database-engine.md) for the session-engine/function-session
pair worked through in full, transaction rollback included.

`"call"` is the narrow exception, for a fixture whose whole point is to hand out a fresh value
per site rather than per test — a request ID generator, a counter — where even `"function"` scope
would be too much sharing.
