# Testing a FastAPI app

FastAPI apps carry two pieces of mutable state on the app object itself —
`dependency_overrides` and `state` — which is fine under pytest's one-test-at-a-time model and
wrong under velox's, where sixteen tests may be reading and writing the same app at once.
[`velox.fastapi.client`](../reference/fastapi.md) swaps both for `ContextVar`-backed proxies so
each concurrent test gets its own view, and layers it away when the `async with` exits.

```python
@velox.fixture()
async def api_client(
    session: AsyncSession = Depends(session),
    settings: Settings = Depends(settings),
) -> AsyncIterator[AsyncClient]:
    async with velox_fastapi.client(
        app,
        overrides={get_session: lambda: session},
        state={"settings": settings},
    ) as client:
        yield client
```

`app` is the module-level `FastAPI()` instance from application code, imported unchanged — nothing
about it has to be written for tests. `overrides` maps a dependency callable to another dependency
callable, the same shape as `app.dependency_overrides` itself, so `get_session` resolves to
`lambda: session` for the lifetime of this fixture and to nothing in particular for a test that
never depended on `api_client`. `ASGITransport` sends no lifespan scope, so `app`'s own `lifespan`
stays out of the way; a test that needs whatever startup builds should depend on
`velox.fastapi.lifespan(app)` instead, which runs it once for the whole session.

## Overriding one dependency for one test

`api_client` is one fixture among the graph everyone shares. A test that needs a different value
for a single dependency writes a sibling fixture rather than mutating the shared one:

```python
@velox.fixture()
def premium_settings() -> Settings:
    return Settings(database_url="unused", signup_bonus_cents=5_000, max_orders_per_user=2)


@velox.fixture()
async def premium_client(
    session: AsyncSession = Depends(session),
    settings: Settings = Depends(premium_settings),
) -> AsyncIterator[AsyncClient]:
    async with velox_fastapi.client(
        app,
        overrides={get_session: lambda: session},
        state={"settings": settings},
    ) as client:
        yield client


async def test_premium_signup_grants_credit(
    client: AsyncClient = Depends(premium_client),
) -> None:
    response = await client.post("/users", json={"email": "frank@example.com"})

    assert response.status_code == 201
    assert response.json()["credit_cents"] == 5_000
```

`premium_client` still depends on the shared `session`, so it sees the same transaction-per-test
isolation as everything else — only `settings` changed. Tests using `api_client` and tests using
`premium_client` run against the same `app` at the same time, each reading its own `state` and its
own overrides.

The full walkthrough is in `examples/01-fastapi-crud/tests/fixtures.py` (`api_client`) and
`examples/01-fastapi-crud/tests/test_orders.py` (`premium_client` and the tests above it).
