# Exclusive fixtures

`@voci.fixture(exclusive=...)` marks a fixture as bound to a resource many tests can use, just not
two of them at the same moment — a fixed local port, a temp directory reused across tests,
anything that would collide under real concurrency without needing to hold the whole suite back.
The admission gate that bounds `--concurrency` enforces this too: a test whose dependency graph
reaches an exclusive fixture, directly or through another fixture that depends on it, never runs
at the same time as another test whose graph reaches a fixture carrying the same token.

```python
@voci.fixture(exclusive=True)
async def local_server() -> AsyncIterator[str]:
    server = await start_test_server(port=8765)
    try:
        yield "http://127.0.0.1:8765"
    finally:
        await server.aclose()


@voci.fixture()
async def client(base_url: Annotated[str, Depends(local_server)]) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(base_url=base_url) as c:
        yield c


async def test_health(base_url: Annotated[str, Depends(local_server)]) -> None: ...


async def test_metrics(client: Annotated[AsyncClient, Depends(client)]) -> None: ...
```

`local_server` binds a fixed port, so two live instances would fail to bind at once.
`exclusive=True` is enough to say so: `test_health` depends on `local_server` directly, and
`test_metrics` reaches the same fixture through `client`, so both carry its token and neither runs
while the other holds it. `local_server` is `"function"` scope, the default: each test builds and
tears down its own server on port 8765, and `exclusive=True` is what keeps those builds from
overlapping.

`exclusive=True` gives the fixture a private token — the fixture itself, not its name or a string
— so it's exclusive only with itself, and never collides with a different fixture elsewhere in the
suite that also passes `exclusive=True`.

## Sharing a token by name

`exclusive="some-name"` takes a string instead, shared by every fixture in the suite declared with
that same string — for a resource, like a fixed port or database, that more than one fixture might
bind:

```python
@voci.fixture(scope="module", exclusive="redis-test-db")
async def redis_migrated() -> AsyncIterator[Redis]:
    r = Redis.from_url("redis://localhost:6379/0")
    await run_migrations(r)
    try:
        yield r
    finally:
        await r.flushdb()


@voci.fixture(exclusive="redis-test-db")
def redis_client() -> Redis:
    return Redis.from_url("redis://localhost:6379/0")
```

`redis_migrated` and `redis_client` are otherwise unrelated fixtures — different scope, different
bodies — but both point at database 0 on the same Redis instance. Naming them with the same string
ties them to one token: a test reaching `redis_client` waits out any other test currently holding
`"redis-test-db"`, including one that got there through `redis_migrated` instead.

## How admission works

A test with more than one exclusive token acquires its whole set in a single step, never one token
at a time — so two tests holding different tokens can never end up each waiting on the other's. A
test queued behind a contended token holds no concurrency slot while it waits: it doesn't count
against `--concurrency`, and can't leave a slot idle that an unrelated test could otherwise use.
Admission gives no fairness guarantee among waiters for the same token.

[Isolating tests](isolated.md) covers `@voci.solo` and `@voci.isolated`, for a resource that can't
tolerate any other test running at all, not just its own other users.
