# Sharing a database engine

A suite that hits a real database needs the connection pool built once, not once per test, while
still keeping every test's writes invisible to its neighbours. Two fixtures do this: a
session-scoped engine, and a function-scoped session wrapped in a transaction that always rolls
back.

```python
@voci.fixture(scope="session")
async def engine(
    tmp: voci.TmpPathFactory = Depends(voci.tmp_path_factory),
) -> AsyncIterator[AsyncEngine]:
    """One engine for the entire run, shared by every concurrent test.

    Session-scoped fixtures are built once, on first use, under a single-flight guard: 200 tests
    starting at the same moment produce exactly one `create_async_engine` call.
    """
    async with database.engine_with_schema(database.url_for(tmp.mktemp("db"))) as e:
        yield e


@voci.fixture()
async def session(engine: AsyncEngine = Depends(engine)) -> AsyncIterator[AsyncSession]:
    """A real session inside a transaction that is always rolled back.

    Every test sees the real schema and none of its neighbours' writes, so hundreds of database
    tests run safely at once against a single engine.
    """
    async with database.transaction(engine) as s:
        yield s
```

`scope="session"` makes `engine` a run-wide singleton rather than a per-test one — see
[Scopes](../guide/scopes.md) for the full set. Depending on it from `session` is enough to reach
it — nothing about the dependent fixture needs to know its dependency is shared.

The rollback itself is ordinary SQLAlchemy, not a voci mechanism: `session` opens a connection,
begins a transaction, and hands the test a session bound to it. Whatever the test does — insert,
update, even `commit()`, since the session joins through a SAVEPOINT — is undone by
`trans.rollback()` when the fixture's `async with` exits, pass or fail.

```python
@asynccontextmanager
async def transaction(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with engine.connect() as conn:
        trans = await conn.begin()
        async with AsyncSession(
            bind=conn,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        ) as session:
            yield session
        await trans.rollback()
```

On SQLite specifically, the driver's own autocommit heuristics fight the rollback unless you take
over `BEGIN` yourself — two `sqlalchemy.event` listeners on connect and on begin, applied once when
the engine is built. A suite on Postgres or another server-backed database doesn't need them; they
exist here because SQLite needs no server of its own — nothing beyond `uv sync && voci`.

The full fixtures, plus the SQLAlchemy plumbing behind them, are in
`examples/01-fastapi-crud/tests/fixtures.py` and `examples/01-fastapi-crud/tests/database.py`.
